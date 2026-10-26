"""Send invitation QR as an encrypted image via the official CDN protocol."""
import base64
import hashlib
import io
import math
import struct
import secrets
import urllib.parse
import urllib.request
import uuid
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .transport import NoRedirect, api

import qrcode


def text_message(account, user, context, text):
    return send_items(account, user, context, [{'type': 1, 'text_item': {'text': text}}])


def send_items(account, user, context, items):
    if user not in account.get('allow_from', []) or not context:
        raise ValueError('Unapproved recipient or missing context')
    client_id = 'maibot-' + uuid.uuid4().hex
    api(account, 'sendmessage', {'msg': {'from_user_id': '', 'to_user_id': user,
        'client_id': client_id, 'message_type': 2, 'message_state': 2,
        'context_token': context, 'item_list': items}})
    return client_id


def send_qr(account, user, context, content):
    if user not in account.get('allow_from', []) or not context:
        raise ValueError('Unapproved invitation recipient')
    stream = io.BytesIO()
    qrcode.make(content).save(stream, format='PNG')
    plain = stream.getvalue()
    media, size = upload_media(account, user, plain, 1)
    return send_items(account, user, context, [{'type': 2, 'image_item': {
        'media': media, 'mid_size': size}}])


def send_voice_test(account, user, context):
    """Experimental one-second PCM tone; does not invoke a paid TTS service."""
    if user not in account.get('allow_from', []) or not context:
        raise ValueError('Unapproved voice recipient')
    rate = 16000
    plain = b''.join(struct.pack('<h', int(4000 * math.sin(2 * math.pi * 440 * i / rate)
                    * min(1, i / 160, (rate - 1 - i) / 160))) for i in range(rate))
    media, _ = upload_media(account, user, plain, 4)
    return send_items(account, user, context, [{'type': 3, 'voice_item': {
        'media': media, 'encode_type': 1, 'bits_per_sample': 16,
        'sample_rate': rate, 'playtime': 1000}}])


def upload_media(account, user, plain, media_type):
    if user not in account.get('allow_from', []):
        raise ValueError('Unapproved media recipient')
    key = secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    filekey = secrets.token_hex(16)
    result = api(account, 'getuploadurl', {'filekey': filekey, 'media_type': media_type,
        'to_user_id': user, 'rawsize': len(plain), 'rawfilemd5': hashlib.md5(plain).hexdigest(),
        'filesize': len(ciphertext), 'no_need_thumb': True, 'aeskey': key.hex()})
    url = result.get('upload_full_url')
    if not url and result.get('upload_param'):
        url = 'https://novac2c.cdn.weixin.qq.com/c2c/upload?' + urllib.parse.urlencode(
            {'encrypted_query_param': result['upload_param'], 'filekey': filekey})
    parsed = urllib.parse.urlsplit(url or '')
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Invalid CDN upload URL')
    if not (host.endswith('.weixin.qq.com') or host.endswith('.wechat.com')):
        raise ValueError('Unexpected CDN host')
    req = urllib.request.Request(url, data=ciphertext, headers={'Content-Type': 'application/octet-stream'})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            download = response.headers.get('x-encrypted-param')
    except Exception:
        raise RuntimeError('iLink media upload failed') from None
    if not download:
        raise RuntimeError('Missing CDN media reference')
    # Official implementation base64-encodes the ASCII hex key, not the raw bytes.
    return {'encrypt_query_param': download,
            'aes_key': base64.b64encode(key.hex().encode()).decode(), 'encrypt_type': 1}, len(ciphertext)
