"""Create a fresh, isolated isolated business database; never overwrite a target."""
import argparse
import sqlite3
import tempfile
from pathlib import Path

def initialize(target=None):
    root=Path(__file__).resolve().parent
    target=Path(target).resolve() if target else Path(tempfile.mkdtemp(prefix='medical-review-db-'))/'warehouse.sqlite'
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb'):pass
    try:
        with sqlite3.connect(target) as db:
            db.executescript((root/'schema.sql').read_text(encoding='utf-8'))
            db.executescript((root/'seed.sql').read_text(encoding='utf-8'))
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Database integrity failed')
            if db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Foreign key integrity failed')
    except Exception:
        # Keep the failed artifact for inspection; no automatic deletion.
        raise
    return target

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output')
    print(initialize(p.parse_args().output))
