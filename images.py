"""Bounded iLink image transport, preserving original GIF bytes for outbound."""
import base64
import io
import urllib.parse
import urllib.request
from PIL import Image
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .media import send_items, upload_media
from .transport import NoRedirect

MAX_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 4
MAX_TOTAL_BYTES = 10 * 1024 * 1024


def probe_images():
    """Synthetic static + two-frame GIF for an administrator's delivery test."""
    png = io.BytesIO()
    first = Image.new('RGB', (128, 128), '#3977cc')
    first.save(png, format='PNG')
    gif = io.BytesIO()
    first.save(gif, format='GIF', save_all=True,
               append_images=[Image.new('RGB', (128, 128), '#e9b44c')], duration=500, loop=0)
    return [png.getvalue(), gif.getvalue()]


def validate_image(plain):
    if not plain or len(plain) > MAX_BYTES:
        raise ValueError('图片为空或超过8MB限制')
    try:
        with Image.open(io.BytesIO(plain)) as image:
            if image.format not in ('PNG', 'JPEG', 'GIF', 'WEBP', 'BMP'):
                raise ValueError()
            if image.width * image.height > 20_000_000 or getattr(image, 'n_frames', 1) > 200:
                raise ValueError()
            image.verify()
    except Exception:
        raise ValueError('图片格式不支持、内容损坏或尺寸/帧数过大') from None
    return plain


def cdn_url(value):
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not (host.endswith('.weixin.qq.com') or host.endswith('.wechat.com'))):
        raise ValueError('不允许的图片下载地址')
    return value


def image_key(image):
    if image.get('aeskey'):
        try:
            key = bytes.fromhex(image['aeskey'])
        except (ValueError, TypeError):
            raise ValueError('图片密钥格式无效') from None
    else:
        encoded = image.get('media', {}).get('aes_key')
        if not encoded:
            return None
        try:
            key = base64.b64decode(encoded, validate=True)
            if len(key) == 32:
                key = bytes.fromhex(key.decode('ascii'))
        except (ValueError, UnicodeError, TypeError):
            raise ValueError('图片密钥格式无效') from None
    if len(key) != 16:
        raise ValueError('图片密钥长度无效')
    return key


def download_image(item):
    image = item.get('image_item') or {}
    media = image.get('media') or {}
    url = media.get('full_url')
    if not url and media.get('encrypt_query_param'):
        url = 'https://novac2c.cdn.weixin.qq.com/c2c/download?' + urllib.parse.urlencode(
            {'encrypted_query_param': media['encrypt_query_param']})
    if not url:
        raise ValueError('图片缺少CDN下载引用')
    url = cdn_url(url)
    key = image_key(image)
    req = urllib.request.Request(url)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            raw = response.read(MAX_BYTES + 33)
    except Exception:
        raise RuntimeError('图片下载失败，请稍后重发') from None
    if len(raw) > MAX_BYTES + 16:
        raise ValueError('图片超过8MB限制')
    if key:
        try:
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            padded = decryptor.update(raw) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            raw = unpadder.update(padded) + unpadder.finalize()
        except ValueError:
            raise ValueError('图片解密失败，请重新发送') from None
    return validate_image(raw)


def inbound_segments(message):
    segments = []
    count = 0
    total = 0
    for item in message.get('item_list', []):
        kind = item.get('type')
        if kind in (1, 3):
            text = (item.get('text_item' if kind == 1 else 'voice_item') or {}).get('text', '')
            if text:
                segments.append({'type': 'text', 'data': text})
        elif kind == 2:
            count += 1
            if count > MAX_IMAGES:
                raise ValueError('每条消息最多处理4张图片，请分开发送')
            plain = download_image(item)
            total += len(plain)
            if total > MAX_TOTAL_BYTES:
                raise ValueError('单条消息图片总大小超过10MB，请分开发送')
            segments.append({'type': 'image', 'data': '',
                             'binary_data_base64': base64.b64encode(plain).decode()})
    return segments


def outbound_segments(value):
    if isinstance(value, list):
        return [segment for item in value for segment in outbound_segments(item)]
    if isinstance(value, dict):
        if value.get('type') == 'seglist':
            return outbound_segments(value.get('data', []))
        if value.get('type') in ('text', 'image', 'emoji'):
            return [value]
    return []


def outbound_image(segment):
    encoded = segment.get('binary_data_base64')
    if not encoded and str(segment.get('data', '')).startswith('data:image/'):
        encoded = segment['data'].split(',', 1)[-1]
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError('图片缺少二进制数据或超过8MB限制')
    try:
        plain = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError('图片数据无效') from None
    return validate_image(plain)


def send_image(account, user, context, plain):
    if user not in account.get('allow_from', []) or not context:
        raise ValueError('Unapproved image recipient or missing context')
    validate_image(plain)
    media, size = upload_media(account, user, plain, 1)
    return send_items(account, user, context, [{'type': 2, 'image_item': {
        'media': media, 'mid_size': size}}])
