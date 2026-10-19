import argparse
import json
import re
from datetime import datetime,timedelta
from pathlib import Path
from runtime_io import read_json

FIELDS={'query_id','raw_query','city_id','region_id','scene_id','history_start','history_end_exclusive'}
def validate(task):
    if not isinstance(task,dict):return ['input must be an object']
    errors=[]
    for key in FIELDS:
        if not isinstance(task.get(key),str) or not task[key].strip():errors.append(key+' required nonempty string')
    if errors:return errors
    if set(task)-FIELDS:errors.append('Unknown input fields: '+','.join(sorted(set(task)-FIELDS)))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',task['query_id']):errors.append('Invalid query_id')
    if len(task['raw_query'])>200:errors.append('Query too long')
    if task['city_id'] not in {'330100','310100','320500'}:errors.append('Unsupported city')
    if task['region_id'] not in {task['city_id']+'-A',task['city_id']+'-B'}:errors.append('Invalid region')
    if task['scene_id']!='medical_search':errors.append('Unsupported scene')
    try:
        start=datetime.fromisoformat(task['history_start']);end=datetime.fromisoformat(task['history_end_exclusive'])
        if start.utcoffset()!=timedelta(hours=8) or end.utcoffset()!=timedelta(hours=8):errors.append('Expected +08:00 timestamps')
        if any([start.hour,start.minute,start.second,start.microsecond,end.hour,end.minute,end.second,end.microsecond]):errors.append('Window requires complete natural days')
        if not timedelta(0)<end-start<=timedelta(days=31):errors.append('Window must be 1-31 days')
    except (ValueError,TypeError):errors.append('Invalid time window')
    return errors

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);a=p.parse_args()
    errors=validate(read_json(a.input));print(json.dumps({'valid':not errors,'errors':errors},ensure_ascii=False))
    raise SystemExit(1 if errors else 0)
