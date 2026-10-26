"""Group-scoped relay ledger, separate from personal and global memory."""
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from . import relay

RETENTION_SECONDS = 30 * 86400


def database():
    relay.STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(relay.STATE_DIR / 'relay-records.local.db', timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, created_at REAL NOT NULL, '
               'sender TEXT NOT NULL, recipient TEXT NOT NULL, sender_name TEXT NOT NULL, '
               'recipient_name TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS shares (record_id TEXT NOT NULL, group_id TEXT NOT NULL, '
               'PRIMARY KEY(record_id,group_id))')
    return db


def record_attempt(sender, recipient, content):
    config, known = relay.load_config(), relay.members()
    if not relay.permitted(config,sender,recipient,known):
        return None
    groups = [g['id'] for g in config['groups'] if g.get('share_records') is True
              and sender in g['members'] and recipient in g['members']]
    if not groups:
        return None
    event_id, now = uuid.uuid4().hex, time.time()
    db = database()
    try:
        with db:
            expired = [row[0] for row in db.execute('SELECT id FROM records WHERE created_at<?',(now-RETENTION_SECONDS,))]
            db.executemany('DELETE FROM shares WHERE record_id=?',[(value,) for value in expired])
            db.execute('DELETE FROM records WHERE created_at<?',(now-RETENTION_SECONDS,))
            db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?)',
                       (event_id,now,sender,recipient,known[sender]['name'],known[recipient]['name'],content,'attempting'))
            db.executemany('INSERT INTO shares VALUES (?,?)',[(event_id,gid) for gid in groups])
    finally:
        db.close()
    return event_id


def finish(event_id, status):
    if not event_id:
        return
    if status not in ('submitted','unconfirmed'):
        raise ValueError('Unknown relay record status')
    db = database()
    try:
        with db:
            db.execute('UPDATE records SET status=? WHERE id=?',(status,event_id))
    finally:
        db.close()


def recent(viewer):
    # Resolve membership on every request; historical membership grants nothing.
    config, known = relay.load_config(), relay.members()
    if not config.get('enabled') or viewer not in known:
        return []
    groups = {g['id']:g for g in config.get('groups',[]) if g.get('share_records') is True and viewer in g['members']}
    if not groups or not (relay.STATE_DIR/'relay-records.local.db').exists():
        return []
    db = database()
    try:
        result = []
        for gid, group in groups.items():
            rows = db.execute('SELECT r.id,r.created_at,r.sender_name,r.recipient_name,r.content,r.status '
                              'FROM records r JOIN shares s ON s.record_id=r.id '
                              'WHERE s.group_id=? AND r.created_at>=? ORDER BY r.created_at DESC LIMIT 6',
                              (gid,time.time()-RETENTION_SECONDS)).fetchall()
            for rid,created,sender,recipient,content,status in rows:
                result.append({'record_id':rid,'group':group['name'],'created_at':created,'sender':sender,
                               'recorded_at':datetime.fromtimestamp(created,timezone.utc).isoformat(),
                               'recipient':recipient,'content':content[:600],'truncated':len(content)>600,'status':status})
        return sorted(result,key=lambda r:r['created_at'],reverse=True)[:6]
    finally:
        db.close()
