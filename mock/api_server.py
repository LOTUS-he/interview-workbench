"""Local, read-only company-data service. No skill or review logic."""
import argparse
import copy
import json
import os
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
def read(name):
    return json.loads((ROOT / '数据' / name).read_text(encoding='utf-8'))
PRODUCTS = read('商品主数据_96条.json')
TERMS = read('关键词索引_48条.json')
OFFERS = read('当前供给快照.json')
PLANS = read('当前广告计划快照.json')
LINKS = read('词计划关联.json')
STORES = read('门店信息.json')
LEXICON = read('标准词与别名.json')
PARAMS = json.loads((ROOT / '配置' / '连接参数.json').read_text(encoding='utf-8'))
CITY_IDS = {x['city_id'] for x in PARAMS['cities']}
PLAN_MAP = {x['ad_plan_id']: x for x in PLANS}
OFFER_MAP = {x['offer_id']: x for x in OFFERS}
SNAPSHOT = PARAMS['business_time']

class Invalid(ValueError):
    pass

class Handler(BaseHTTPRequestHandler):
    server_version = 'MedicalDataMock/1.0'
    def log_message(self, fmt, *args):
        # Do not log tokens, query text, or response bodies.
        print('%s request_completed' % self.log_date_time_string(), flush=True)

    def send(self, code, data=None, error=None, next_cursor=None, total=None, item_errors=None):
        payload = {
            'request_id': str(uuid.uuid4()),
            'meta': {'source': 'synthetic_company_api', 'dataset_version': 'assignment-1.0',
                     'scenario': self.server.scenario, 'as_of': self.server.business_time,
                     'served_at': datetime.now(timezone.utc).isoformat(),
                     'next_cursor': next_cursor, 'total': total},
            'data': data if data is not None else [],
            'errors': item_errors or [],
            'error': error,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            if code == 429: self.send_header('Retry-After', '1')
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reject(self, status, code, message, retryable=False):
        self.send(status, error={'code': code, 'message': message, 'retryable': retryable})

    def do_POST(self):
        self.reject(405, 'READ_ONLY', 'Only GET is supported')
    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == '/health':
            self.send(200, data={'status': 'ok', 'mode': 'read_only', 'business_time': self.server.business_time})
            return
        if self.headers.get('Authorization') != 'Bearer ' + self.server.token:
            self.reject(401, 'UNAUTHORIZED', 'A valid demo bearer token is required')
            return
        known = {'/v1/terms', '/v1/recall', '/v1/products', '/v1/offers', '/v1/ad-plans', '/v1/stores', '/v1/qualifications'}
        if u.path not in known:
            self.reject(404, 'ENDPOINT_NOT_FOUND', 'Unknown endpoint')
            return
        if self.server.scenario == 'rate-limit':
            self.reject(429, 'RATE_LIMITED', 'Injected rate limit', True)
            return
        if self.server.scenario == 'slow' and u.path in {'/v1/offers', '/v1/qualifications'}:
            time.sleep(1.5)
        try:
            q = parse_qs(u.query, keep_blank_values=True)
            if any(len(v) != 1 for v in q.values()): raise Invalid('Duplicate parameter')
            def one(key, default=None):
                return q.get(key, [default])[0]
            def ids(key, required=True):
                value = one(key)
                if value is None:
                    if required: raise Invalid(key + ' is required')
                    return None
                result = value.split(',')
                if not result or len(result) > 50 or any(not x or len(x) > 100 for x in result):
                    raise Invalid(key + ' requires 1-50 nonempty IDs')
                return list(dict.fromkeys(result))
            def city():
                c = one('city_id')
                if c not in CITY_IDS: raise Invalid('city_id must be a supported city')
                return c
            allowed = {'limit', 'cursor'}
            allowed |= {
                '/v1/terms': {'q'}, '/v1/recall': {'term_id', 'city_id'},
                '/v1/products': {'sku_ids'}, '/v1/offers': {'sku_ids', 'city_id'},
                '/v1/ad-plans': {'offer_ids'}, '/v1/stores': {'store_ids'},
                '/v1/qualifications': {'sku_ids'},
            }[u.path]
            if set(q) - allowed: raise Invalid('Unsupported parameter: ' + ','.join(sorted(set(q)-allowed)))
            try:
                limit, offset = int(one('limit', '50')), int(one('cursor', '0'))
            except ValueError: raise Invalid('limit/cursor must be integers')
            if limit < 1 or limit > 100 or offset < 0: raise Invalid('limit 1-100; cursor >= 0')
            rows, errors = [], []
            if u.path == '/v1/terms':
                needle = one('q', '')
                if len(needle) > 200: raise Invalid('q too long')
                needles = [needle]
                for a in LEXICON['aliases']:
                    if needle and needle == a['alias']: needles.append(a['canonical_name'])
                rows = [t for t in TERMS if any(x in t['term'] for x in needles)]
            elif u.path == '/v1/recall':
                c = city(); t = one('term_id')
                if not t: raise Invalid('term_id is required')
                term = next((x for x in TERMS if x['term_id'] == t), None)
                if term is None:
                    self.reject(404, 'TERM_NOT_FOUND', 'Unknown term_id'); return
                if term['enabled']:
                    for link in LINKS:
                        plan = PLAN_MAP[link['ad_plan_id']]; offer = OFFER_MAP[plan['offer_id']]
                        if link['term_id'] == t and offer['city_id'] == c:
                            rows.append({**link, 'candidate_id': plan['ad_plan_id'], 'offer_id': offer['offer_id'],
                                         'sku_id': offer['sku_id'], 'store_id': offer['store_id'], 'city_id': c})
            else:
                key, id_key, source = {
                    '/v1/products': ('sku_ids','sku_id',PRODUCTS),
                    '/v1/qualifications': ('sku_ids','sku_id',PRODUCTS),
                    '/v1/offers': ('sku_ids','sku_id',OFFERS),
                    '/v1/ad-plans': ('offer_ids','offer_id',PLANS),
                    '/v1/stores': ('store_ids','store_id',STORES),
                }[u.path]
                wanted = ids(key)
                c = city() if u.path == '/v1/offers' else None
                for ident in wanted:
                    subset = [x for x in source if x[id_key] == ident and (c is None or x['city_id'] == c)]
                    if not subset:
                        errors.append({'id': ident, 'code': 'NOT_FOUND', 'retryable': False}); continue
                    if self.server.scenario == 'partial-failure' and ident == 'SKU0008' and u.path in {'/v1/products','/v1/qualifications'}:
                        errors.append({'id': ident, 'code': 'UPSTREAM_TIMEOUT', 'retryable': True}); continue
                    for raw in subset:
                        row = copy.deepcopy(raw)
                        if u.path == '/v1/qualifications':
                            row = {k: raw[k] for k in ['sku_id','category','prescription_type','qualification_status','product_version']}
                            row['as_of'] = SNAPSHOT
                        if u.path == '/v1/offers' and row['offer_id']=='O00001' and self.server.scenario=='stock-change':
                            row.update(stock=0, as_of=self.server.business_time, version='offer-20260920-2')
                        if u.path == '/v1/products' and row['sku_id']=='SKU0007' and self.server.scenario=='schema-drift':
                            row['pack_count'] = str(row['pack_count'])
                        rows.append(row)
            total = len(rows)
            if offset > total: raise Invalid('cursor exceeds result length')
            page = rows[offset:offset+limit]
            cursor = str(offset+limit) if offset+limit < total else None
            self.send(200, data=page, total=total, next_cursor=cursor, item_errors=errors)
        except Invalid as e:
            self.reject(400, 'INVALID_ARGUMENT', str(e))
        except Exception:
            self.reject(500, 'INTERNAL_ERROR', 'Service error', False)

def main():
    parser=argparse.ArgumentParser(description='Local read-only business API service')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--scenario',choices=['normal','stock-change','partial-failure','slow','rate-limit','schema-drift'],default='normal')
    args=parser.parse_args()
    if not 1024 <= args.port <= 65535: parser.error('port must be 1024-65535')
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    server.daemon_threads=True
    server.scenario=args.scenario
    server.token=os.environ.get('MEDICAL_DEMO_TOKEN',PARAMS['demo_token'])
    server.business_time='2026-09-20T12:10:00+08:00' if args.scenario=='stock-change' else SNAPSHOT
    print(f'Mock business data: http://127.0.0.1:{args.port}; scenario={args.scenario}; snapshot={server.business_time}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
