"""Local owner-approved login; --admin is never reachable from chat commands."""
import argparse
import asyncio
import sys
import types
from pathlib import Path

if not __package__:
    package = types.ModuleType('_ilink_local_login')
    package.__path__ = [str(Path(__file__).parent)]
    sys.modules[package.__name__] = package
    __package__ = package.__name__
from .accounts import register
from .authorization import new_qr, wait_for_login
from .transport import STATE_DIR, save_json
from .welcome import queue

OUTPUT = STATE_DIR / 'login'
import qrcode

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--admin', action='store_true', help='Bind scanning owner as administrator; local entry only')
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    qr = await asyncio.to_thread(new_qr)
    image = OUTPUT / ('admin-qr.png' if args.admin else 'login-qr.png')
    qrcode.make(qr['qrcode_img_content']).save(image)
    save_json(OUTPUT / 'login-status.local.json', {'status': 'waiting', 'mode': 'administrator' if args.admin else 'user'})
    print('Official QR ready. Mode:', 'ADMINISTRATOR' if args.admin else 'ordinary user', flush=True)
    print('Only authorize the intended WeChat account on your phone.', flush=True)
    async def verify():
        return await asyncio.to_thread(input, 'Enter the pairing code shown by official WeChat: ')
    status = await wait_for_login(qr, on_verify=verify)
    item = await asyncio.to_thread(register, status, admin=args.admin)
    if not args.admin:
        await asyncio.to_thread(queue, item['account_id'], status['ilink_user_id'])
    save_json(OUTPUT / 'login-status.local.json', {'status': 'confirmed', 'mode': 'administrator' if args.admin else 'user'})
    image.unlink(missing_ok=True)
    print('Authorization saved without replacing other accounts.', flush=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Canceled')
    except Exception as exc:
        save_json(OUTPUT / 'login-status.local.json', {'status': 'failed', 'error_type': type(exc).__name__})
        print('Login failed:', type(exc).__name__, flush=True)
        raise SystemExit(1)
