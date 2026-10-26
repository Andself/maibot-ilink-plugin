import asyncio
import time
import urllib.parse
from .transport import BASE_URL, official_url, request

LOGIN_TIMEOUT = 300


class InvitationExpired(RuntimeError):
    """The local wait window or official QR authorization has expired."""


def new_qr():
    result = request(BASE_URL, 'ilink/bot/get_bot_qrcode?bot_type=3', {'local_token_list': []})
    if not result.get('qrcode') or not result.get('qrcode_img_content'):
        raise RuntimeError('Official QR response incomplete')
    return result


async def wait_for_login(qr, on_verify=None):
    # Includes slow network calls and waiting for a phone pairing code.
    try:
        async with asyncio.timeout(LOGIN_TIMEOUT):
            return await _poll_login(qr, on_verify)
    except TimeoutError as exc:
        raise InvitationExpired('邀请二维码已过期') from exc


async def _poll_login(qr, on_verify=None):
    deadline = time.monotonic() + LOGIN_TIMEOUT
    base = BASE_URL
    code = ''
    while time.monotonic() < deadline:
        endpoint = 'ilink/bot/get_qrcode_status?' + urllib.parse.urlencode({'qrcode': qr['qrcode']})
        if code:
            endpoint += '&' + urllib.parse.urlencode({'verify_code': code})
        try:
            response = await asyncio.to_thread(request, base, endpoint)
        except RuntimeError:
            await asyncio.sleep(3)
            continue
        status = response.get('status')
        if status == 'confirmed':
            response['baseurl'] = official_url(response.get('baseurl') or base)
            return response
        if status == 'scaned_but_redirect' and response.get('redirect_host'):
            host = response['redirect_host']
            base = official_url(host if host.startswith('https://') else 'https://' + host)
        elif status == 'need_verifycode':
            if on_verify is None:
                raise RuntimeError('Phone pairing code required; use the local login entry instead')
            code = await on_verify()
        elif status == 'expired':
            raise InvitationExpired('邀请二维码已过期')
        elif status in ('verify_code_blocked', 'binded_redirect'):
            raise RuntimeError('Authorization ended: ' + status)
        await asyncio.sleep(2)
    raise InvitationExpired('邀请二维码已过期')
