"""WebUI-only QR binding. Never exposes account credentials to the browser."""
import asyncio
import base64
import io
import time
import uuid
import qrcode
from .accounts import load_accounts, register, settings
from .authorization import InvitationExpired, LOGIN_TIMEOUT, new_qr, wait_for_login
from .profiles import clean_description, save_profile


def qr_image(content):
    stream = io.BytesIO()
    qrcode.make(content).save(stream, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()


class BindingManager:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.task = None
        self.state = None
        self.code = None

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def start(self, mode, description=''):
        async with self.lock:
            config = await asyncio.to_thread(settings)
            has_admin = bool(config.get('admin_account_id') and config.get('admin_user_id'))
            if mode not in ('admin', 'user'):
                raise ValueError('未知绑定模式。')
            if mode == 'admin' and has_admin:
                raise ValueError('已经绑定管理员，不能通过此入口替换。')
            if mode == 'user' and not has_admin:
                raise ValueError('请先绑定管理员。')
            if mode == 'user':
                description = clean_description(description)
            if self.task and not self.task.done():
                raise ValueError('已有绑定正在进行，请先完成或取消。')
            if len(await asyncio.to_thread(load_accounts)) >= 16:
                raise ValueError('已达到16个账号上限。')
            self.state = {'binding_id': uuid.uuid4().hex, 'mode': mode, 'status': 'creating',
                          'qr_image': '', 'message': '正在生成二维码…', 'expires_at': time.time()+300}
            self.code = None
            self.task = asyncio.create_task(self.run(dict(self.state), description, config))
            return dict(self.state)

    def status(self, binding_id=''):
        if not self.state or (binding_id and binding_id != self.state['binding_id']):
            return {'status': 'idle'}
        return dict(self.state)

    async def verify(self):
        self.state.update(status='need_code', message='请填写手机上显示的配对验证码。')
        self.code = asyncio.get_running_loop().create_future()
        return await asyncio.wait_for(self.code, max(1, self.state['expires_at']-time.time()))

    def submit_code(self, binding_id, code):
        if not self.state or self.state['binding_id'] != binding_id or not self.code or self.code.done():
            raise ValueError('当前没有等待验证码的绑定。')
        if not isinstance(code, str) or not code.isdigit() or not 4 <= len(code) <= 12:
            raise ValueError('验证码需为4至12位数字。')
        self.code.set_result(code)
        self.state.update(status='waiting', message='正在验证…')
        return self.status(binding_id)

    async def cancel(self, binding_id):
        if not self.state or self.state['binding_id'] != binding_id:
            raise ValueError('绑定已失效。')
        if self.state['status'] == 'saving':
            raise ValueError('扫码已确认，正在保存账号，请稍候。')
        await self.close()
        if self.state['status'] not in ('confirmed', 'error', 'expired'):
            self.state.update(status='canceled', qr_image='', message='已取消本地绑定等待。')
        return self.status(binding_id)

    async def run(self, initial, description, config):
        try:
            qr = await asyncio.to_thread(new_qr)
            picture = await asyncio.to_thread(qr_image, qr['qrcode_img_content'])
            self.state.update(status='waiting', qr_image=picture, expires_at=time.time()+LOGIN_TIMEOUT,
                              message='请用目标微信扫码并确认授权。')
            response = await wait_for_login(qr, on_verify=self.verify)
            current = await asyncio.to_thread(settings)
            if initial['mode'] == 'user' and any(current.get(k) != config.get(k)
                    for k in ('admin_account_id', 'admin_user_id')):
                raise ValueError('管理员已变更，绑定终止。')
            self.state.update(status='saving', qr_image='', message='扫码已确认，正在保存账号…')
            item = await asyncio.to_thread(register, response, admin=initial['mode'] == 'admin',
                                           bootstrap=initial['mode'] == 'admin')
            if description:
                await asyncio.to_thread(save_profile, item['account_id'], response['ilink_user_id'], description)
            self.state.update(status='confirmed', qr_image='', message='管理员绑定成功。' if initial['mode'] == 'admin'
                              else '绑定成功。请让用户在微信机器人会话中先发送一条消息，即可开始聊天。')
        except asyncio.CancelledError:
            raise
        except (InvitationExpired, TimeoutError):
            self.state.update(status='expired', qr_image='',
                              message='二维码已过期，本次未完成绑定。可保留介绍重新生成二维码。')
        except Exception as exc:
            # Do not send raw network errors, QR sessions or credentials to the UI.
            message = str(exc) if isinstance(exc, ValueError) else '绑定失败或二维码过期，请重新生成。'
            self.state.update(status='error', qr_image='', message=message)
