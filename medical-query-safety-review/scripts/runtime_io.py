"""Portable, bounded file and evidence helpers."""
import hashlib
import json
import os
from pathlib import Path

SKILL_ROOT=Path(__file__).resolve().parents[1]
MAX_JSON_BYTES=32*1024*1024

def read_json(path):
    path=Path(path)
    if path.stat().st_size>MAX_JSON_BYTES:raise ValueError('JSON exceeds size limit')
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('Duplicate JSON key: '+k)
            result[k]=v
        return result
    return json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique,
                      parse_constant=lambda s:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))

def write_json(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.pending')
    with temporary.open('w',encoding='utf-8') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')
    temporary.replace(path)

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def inside(root,relative):
    root=Path(root).resolve();path=(root/relative).resolve()
    if path!=root and root not in path.parents:raise ValueError('Path escapes allowed root')
    return path

def evidence_value(run_dir,ref):
    if not isinstance(ref,str) or not ref.startswith('evidence/'):raise ValueError('Invalid evidence reference')
    name,_,pointer=ref.partition('#')
    path=inside(run_dir,name)
    if not path.is_file():raise ValueError('Missing evidence file: '+name)
    obj=read_json(path)
    if pointer:
        if not pointer.startswith('/'):raise ValueError('Invalid JSON pointer')
        for part in pointer[1:].split('/'):
            part=part.replace('~1','/').replace('~0','~')
            obj=obj[int(part)] if isinstance(obj,list) else obj[part]
    return obj

def source_hashes():
    return {p.relative_to(SKILL_ROOT).as_posix():sha(p) for p in sorted(SKILL_ROOT.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}

def clean_config(config):
    return {k:v for k,v in config.items() if 'token' not in k.lower() and 'password' not in k.lower() and 'secret' not in k.lower()}
