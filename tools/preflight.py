import argparse
import json
import sqlite3
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'skill-source/medical-query-safety-review/scripts'))
from runtime_io import read_json

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--database',required=True);a=p.parse_args()
    if sys.version_info<(3,10):raise SystemExit('Python 3.10+ required')
    config=read_json(a.config)
    if config.get('mode')!='mock':raise SystemExit('Only mock mode implemented')
    path=Path(a.database).resolve()
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise SystemExit('Database integrity failed')
        count=db.execute('SELECT count(*) FROM queries').fetchone()[0]
    from fetch_evidence import collect_catalog
    evidence=collect_catalog(config)
    if evidence.get('errors'):raise SystemExit('API preflight failed; inspect the service and contract')
    print(json.dumps({'mode':'mock','query_count':count,'term_count':len(evidence.get('terms',[])),'ready':True},ensure_ascii=False))
if __name__=='__main__':main()
