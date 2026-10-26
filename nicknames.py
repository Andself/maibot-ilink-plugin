import hashlib
import unicodedata
from .accounts import is_admin, load_accounts, read_json, settings
from .transport import STATE_DIR, save_json


def user_key(account_id, user_id):
    return hashlib.sha256((account_id + '\0' + user_id).encode()).hexdigest()


def nickname_path(account_id, user_id):
    return STATE_DIR / 'nicknames' / (user_key(account_id, user_id) + '.local.json')


def display_name(account_id, user_id, config=None):
    from .profiles import description_for, introduced_name
    record = read_json(nickname_path(account_id, user_id), {})
    if record.get('nickname'):
        return record['nickname']
    name = introduced_name(description_for(account_id, user_id))
    if name:
        return name
    # Permission roles must never become person names. Distinguish unnamed users
    # without changing the stable identity used by conversations and memory.
    return '微信用户-' + user_key(account_id, user_id)[:8]


def list_users():
    from .profiles import description_for, identity_context
    from .welcome import state
    from .reply_contexts import window
    config = settings()
    rows = []
    for account_id, account in load_accounts().items():
        for user_id in account.get('allow_from', []):
            record = read_json(nickname_path(account_id, user_id), {})
            provenance = identity_context(account_id, user_id).get('introduction', {})
            activity=window({**account,'account_id':account_id},user_id)
            activity.pop('context_token',None)
            rows.append({'reply_window':activity,'key': user_key(account_id, user_id), 'user_id': user_id,
                         'account': account_id, 'nickname': record.get('nickname', ''),
                         'display_name': display_name(account_id, user_id, config),
                         'description': description_for(account_id, user_id),
                         'introduced_by': provenance.get('introduced_by', {}).get('name', ''),
                         'welcome_status': state(account_id,user_id),
                         'role': '管理员' if is_admin(config, account_id, user_id) else '普通用户'})
    return {'users': rows}


def set_nickname(user_key_value, nickname):
    if not isinstance(nickname, str):
        raise ValueError('昵称必须是文字')
    nickname = nickname.strip()
    if len(nickname) > 40 or any(unicodedata.category(char).startswith('C') for char in nickname):
        raise ValueError('昵称最多40字，不能含控制字符')
    row = next((row for row in list_users()['users'] if row['key'] == user_key_value), None)
    if row is None:
        raise ValueError('只能为已接入的白名单用户设置昵称')
    save_json(nickname_path(row['account'], row['user_id']), {'nickname': nickname})
    return {'success': True, 'display_name': display_name(row['account'], row['user_id'])}
