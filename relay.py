"""Explicit, non-transitive relay groups configured only through the WebUI."""
import json
import unicodedata
import uuid
from .accounts import load_accounts, read_json
from .nicknames import display_name, user_key
from .transport import STATE_DIR, save_json


def members():
    return {user_key(account_id, user): {'account_id': account_id, 'user_id': user,
                'name': display_name(account_id, user)}
            for account_id, account in load_accounts().items() for user in account.get('allow_from', [])}


def load_config():
    config = read_json(STATE_DIR / 'relay.local.json', {'enabled': False, 'groups': []})
    for group in config.get('groups', []):
        # Deterministic migration until the next configuration save persists it.
        group.setdefault('id', uuid.uuid5(uuid.NAMESPACE_URL, 'anse.ilink/legacy/'+group['name']).hex)
        group.setdefault('share_records', False)
    return config


def save_config(config_json):
    if not isinstance(config_json, str) or len(config_json) > 20000:
        raise ValueError('转达配置过大或格式错误。')
    value = json.loads(config_json)
    if not isinstance(value, dict) or set(value) != {'enabled', 'groups'} or type(value['enabled']) is not bool:
        raise ValueError('转达配置需包含开关和组列表。')
    groups = value['groups']
    if not isinstance(groups, list) or len(groups) > 32:
        raise ValueError('最多配置32个转达组。')
    known = members()
    existing = {g['id']:g for g in load_config().get('groups', [])}
    seen_names = set()
    seen_ids = set()
    clean = []
    for group in groups:
        if (not isinstance(group, dict) or not {'name','members'} <= set(group)
                or set(group) - {'id','name','members','share_records'}):
            raise ValueError('转达组格式错误。')
        name, keys = group['name'], group['members']
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 40
                or any(unicodedata.category(c).startswith('C') for c in name)):
            raise ValueError('组名需为1至40字，不能含控制字符。')
        name = name.strip()
        if name in seen_names:
            raise ValueError('转达组名称不能重复。')
        if not isinstance(keys, list) or not 2 <= len(keys) <= 16 or any(not isinstance(k, str) or k not in known for k in keys):
            raise ValueError('每组需要2至16个已接入用户。')
        if len(set(keys)) != len(keys):
            raise ValueError('组内成员不能重复。')
        sharing = group.get('share_records', False)
        if type(sharing) is not bool:
            raise ValueError('共享转达记录开关必须为布尔值。')
        group_id = group.get('id')
        if group_id and (not isinstance(group_id,str) or group_id not in existing):
            raise ValueError('转达组标识已失效，请刷新后重试。')
        if not group_id:
            group_id = next((gid for gid,g in existing.items() if g['name']==name), None) or uuid.uuid4().hex
        if group_id in seen_ids:
            raise ValueError('转达组标识不能重复。')
        seen_ids.add(group_id)
        seen_names.add(name)
        clean.append({'id':group_id,'name': name, 'members': keys,'share_records':sharing})
    result = {'enabled': value['enabled'], 'groups': clean}
    save_json(STATE_DIR / 'relay.local.json', result)
    return result


def permitted(config, sender, recipient, known):
    return (config.get('enabled') is True and sender != recipient and sender in known and recipient in known
            and any(sender in g.get('members', []) and recipient in g.get('members', [])
                    for g in config.get('groups', [])))


def targets(sender):
    config = load_config()
    known = members()
    return [{'recipient_key': key, 'name': row['name']} for key, row in known.items()
            if permitted(config, sender, key, known)]


def relay_content(sender_name, content):
    if not isinstance(content, str) or not content.strip() or len(content) > 1000:
        raise ValueError('转达内容需为1至1000字。')
    if any(unicodedata.category(c).startswith('C') and c != '\n' for c in content):
        raise ValueError('转达内容不能包含控制字符。')
    return f'【{sender_name} 托我转达】\n{content.strip()}'
