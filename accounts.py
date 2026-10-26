"""Independent account files avoid lost updates between login and plugin processes."""
import hashlib
import json
import msvcrt
from contextlib import contextmanager
from .transport import ACCOUNT_FILE, STATE_DIR, official_url, save_json

SETTINGS = STATE_DIR / 'settings.local.json'


def key(account_id):
    return hashlib.sha256(account_id.encode()).hexdigest()[:32]


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def settings():
    value = read_json(SETTINGS, {})
    if not value.get('primary_account_id') and ACCOUNT_FILE.exists():
        value['primary_account_id'] = read_json(ACCOUNT_FILE)['account_id']
    return value


def load_accounts():
    values = {}
    old = read_json(ACCOUNT_FILE)
    if old:
        values[old['account_id']] = old
    for path in (STATE_DIR / 'accounts').glob('*.local.json'):
        item = read_json(path)
        if item.get('enabled', True):
            values[item['account_id']] = item
        else:
            values.pop(item['account_id'], None)
    return values


@contextmanager
def account_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / 'registration.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def register(status, *, admin=False, bootstrap=False):
    with account_lock():
        return _register(status, admin=admin, bootstrap=bootstrap)


def remove_user(user_key_value):
    from .nicknames import user_key
    with account_lock():
        config = settings()
        for account_id, account in load_accounts().items():
            for user in account.get('allow_from', []):
                if user_key(account_id, user) != user_key_value:
                    continue
                if is_admin(config, account_id, user):
                    raise ValueError('不能在此移除管理员账号。')
                remaining = [value for value in account['allow_from'] if value != user]
                # Independent disabled records override legacy login data as well.
                # Preserve recoverable data and the logical primary identity.
                save_json(STATE_DIR / 'accounts' / (key(account_id) + '.local.json'),
                          {**account, 'allow_from': remaining, 'enabled': bool(remaining)})
                return account_id, user
        raise ValueError('账号已移除或不存在，请刷新名单。')


def _register(status, *, admin=False, bootstrap=False):
    owner = status.get('ilink_user_id')
    bot = status.get('ilink_bot_id')
    if not owner or not bot or not status.get('bot_token'):
        raise ValueError('Incomplete authorization')
    config = settings()
    if bootstrap and (config.get('admin_account_id') or config.get('admin_user_id')):
        raise ValueError('管理员已经绑定，不能重复初始化。')
    accounts = load_accounts()
    if not admin and len(accounts) >= 16 and bot not in accounts:
        raise ValueError('Account limit reached')
    # Invitations can refresh an existing owner's token, never change its owner.
    if bot in accounts and owner not in accounts[bot]['allow_from']:
        raise ValueError('Existing account owner mismatch')
    if not admin and bot == config.get('admin_account_id'):
        raise ValueError('Administrator account cannot be rebound by invitation')
    item = {'account_id': bot, 'token': status['bot_token'],
            'base_url': official_url(status.get('baseurl') or 'https://ilinkai.weixin.qq.com'),
            'allow_from': [owner], 'enabled': True}
    path = STATE_DIR / 'accounts' / (key(bot) + '.local.json')
    save_json(path, item)
    if not config.get('primary_account_id'):
        config['primary_account_id'] = bot
    if admin:
        config.update(admin_account_id=bot, admin_user_id=owner)
    save_json(SETTINGS, config)
    return item


def is_admin(config, account_id, user_id):
    return bool(config.get('admin_user_id') and config.get('admin_account_id') and
                config['admin_user_id'] == user_id and config['admin_account_id'] == account_id)


def session_user(account_id, user_id, primary):
    # Preserve existing conversations for the original account.
    if account_id == primary:
        return user_id
    return 'ilink-' + hashlib.sha256((account_id + '\0' + user_id).encode()).hexdigest()
