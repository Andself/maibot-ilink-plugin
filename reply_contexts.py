"""Persist short-lived reply credentials alongside existing private account data."""
import hashlib
import math
import time
from .accounts import read_json, session_user
from .nicknames import user_key
from .transport import STATE_DIR, save_json

MAX_AGE = 24 * 3600


def active(timestamp, now=None):
    now=time.time() if now is None else now
    return type(timestamp) in (int,float) and math.isfinite(timestamp) and 0 <= now-timestamp < MAX_AGE


def message_time(message):
    value=message.get('create_time_ms')
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
        return 0
    stamp=value/1000
    # A delayed poll must not restart the window. Future/invalid times are unknown.
    return stamp if stamp<=time.time() else 0


def window(account,user):
    path=STATE_DIR/'reply-contexts'/(user_key(account['account_id'],user)+'.local.json')
    record=read_json(path,{})
    stamp=record.get('received_at')
    valid=(record.get('schema_version')==2 and record.get('user_id')==user
           and isinstance(record.get('context_token'),str) and bool(record['context_token'])
           and record.get('credential_fingerprint')==fingerprint(account)
           and type(stamp) in (int,float) and math.isfinite(stamp) and 0<stamp<=time.time())
    return {'status':('active' if active(stamp) else 'expired') if valid else 'unknown',
            'last_inbound_at':stamp if valid else None,'expires_at':stamp+MAX_AGE if valid else None,
            'context_available':bool(record.get('context_token')) if valid else False,
            'context_token':record.get('context_token') if valid else None}


def require_window(account,user,token):
    if user not in account.get('allow_from',[]):
        raise ValueError('Unapproved reply recipient')
    state=window(account,user)
    if state['status']!='active' or not token or token!=state['context_token']:
        raise RuntimeError('微信24小时回复窗口已过期或未知，尚未发送；请让接收方先给Bot发一条消息。')


def fingerprint(account):
    return hashlib.sha256(account['token'].encode()).hexdigest()


def remember(account, user, token, received_at):
    if user not in account.get('allow_from', []) or not token:
        raise ValueError('Unapproved reply context')
    if type(received_at) not in (int,float) or not math.isfinite(received_at) or received_at<0:
        raise ValueError('Invalid inbound timestamp')
    path = STATE_DIR / 'reply-contexts' / (user_key(account['account_id'],user)+'.local.json')
    old=read_json(path,{})
    if (old.get('schema_version')==2 and old.get('credential_fingerprint')==fingerprint(account)
            and old.get('user_id')==user
            and type(old.get('received_at')) in (int,float)
            and math.isfinite(old['received_at']) and old['received_at']<=time.time()
            and old['received_at']>received_at):
        return False
    save_json(path, {'schema_version':2,'user_id':user,'context_token':token,'received_at':received_at,
                     'credential_fingerprint':fingerprint(account)})
    return True


def restore(records, primary):
    contexts = {}
    if not primary:
        return contexts
    now = time.time()
    for account_id, account in records.items():
        for user in account.get('allow_from', []):
            path = STATE_DIR / 'reply-contexts' / (user_key(account_id,user)+'.local.json')
            record = read_json(path,{})
            timestamp = record.get('received_at')
            if (record.get('schema_version')!=2 or record.get('user_id') != user or not isinstance(record.get('context_token'),str)
                    or not record['context_token'] or type(timestamp) not in (int,float)
                    or not math.isfinite(timestamp) or not 0 <= now-timestamp < MAX_AGE
                    or record.get('credential_fingerprint') != fingerprint(account)):
                continue
            uid = session_user(account_id,user,primary)
            contexts[uid] = (record['context_token'],timestamp,account_id,user)
    return contexts
