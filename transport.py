"""Minimal text-only client for Tencent's published iLink HTTP protocol."""
import base64
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = '2.4.10'
BASE_URL = 'https://ilinkai.weixin.qq.com'
STATE_DIR = Path(__file__).resolve().parents[2] / 'depends-data' / 'anse.ilink'
ACCOUNT_FILE = STATE_DIR / 'account.local.json'


def official_url(value):
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('Invalid iLink API URL')
    if not (host == 'weixin.qq.com' or host.endswith('.weixin.qq.com') or
            host == 'wechat.com' or host.endswith('.wechat.com')):
        raise ValueError('API host is outside the permitted Weixin/WeChat domains: ' + host)
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Unexpected API base path')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Unexpected HTTP redirect; request stopped')


def request(base, endpoint, body=None, token='', timeout=40):
    base = official_url(base)
    headers = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': str((2 << 16) | (4 << 8) | 10)}
    if body is not None:
        headers.update({'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
                        'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
    if token:
        headers['Authorization'] = 'Bearer ' + token
    raw = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + '/' + endpoint.lstrip('/'), data=raw, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'iLink HTTP {exc.code}') from None
    except urllib.error.URLError:
        raise RuntimeError('iLink connection failed') from None
    if not isinstance(data, dict):
        raise RuntimeError('Invalid iLink response')
    for key in ('ret', 'errcode'):
        if data.get(key, 0) not in (0, None):
            raise RuntimeError(f'iLink {key}={data[key]}')
    return data


def api(account, endpoint, body):
    if endpoint=='sendmessage':
        from .reply_contexts import require_window
        msg=body.get('msg',{})
        require_window(account,msg.get('to_user_id',''),msg.get('context_token'))
    body = {**body, 'base_info': {'channel_version': VERSION, 'bot_agent': 'MaiBotAnse/0.1.0'}}
    return request(account['base_url'], 'ilink/bot/' + endpoint, body, account['token'])


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def allowed_text(message, account):
    sender = message.get('from_user_id', '')
    if message.get('group_id') or message.get('message_type') != 1:
        return None
    if not sender or sender not in account.get('allow_from', []):
        return None
    items = message.get('item_list', [])
    text = '\n'.join(item.get('text_item' if item.get('type') == 1 else 'voice_item', {}).get('text', '')
                     for item in items if item.get('type') in (1, 3)).strip()
    # Keep untranscribed voice messages so the caller can explain the limitation.
    if (not text and not any(item.get('type') in (2, 3) for item in items)) or not message.get('context_token'):
        return None
    return sender, text
