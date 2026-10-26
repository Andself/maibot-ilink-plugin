"""Explicit binding-triggered welcome queue; each user receives at most one attempt."""
import time
import uuid
from .accounts import account_lock, read_json
from .transport import STATE_DIR, api, save_json


def path_for(account, user):
    from .nicknames import user_key
    return STATE_DIR / 'welcome' / (user_key(account,user)+'.local.json')


def queue(account, user):
    with account_lock():
        path = path_for(account,user)
        if not path.exists():
            save_json(path, {'status':'pending','created_at':time.time()})


def state(account, user):
    return read_json(path_for(account,user),{}).get('status','')


def claim(account, user):
    with account_lock():
        path = path_for(account,user)
        record = read_json(path,{})
        if record.get('status') != 'pending':
            return False
        save_json(path,{**record,'status':'attempting','attempted_at':time.time()})
        return True


def finish(account, user, status):
    path = path_for(account,user)
    record = read_json(path,{})
    save_json(path,{**record,'status':status,'updated_at':time.time()})


def greeting(name):
    prefix = '你好呀！' if name in ('微信用户','管理员','') else f'{name}，你好呀！'
    return prefix+'很高兴认识你，有朋友把你介绍给我啦。以后想聊天就来找我～'


def send_without_context(account, user, text):
    raise RuntimeError('欢迎消息不能绕过24小时窗口，请等待用户先发消息。')
