import asyncio
import json
import time
from collections import deque
from maibot_sdk import API, Action, HookHandler, MaiBotPlugin, MessageGateway, ReplyExtension
from .accounts import is_admin, key, load_accounts, read_json, register, remove_user, session_user, settings
from .authorization import InvitationExpired, new_qr, wait_for_login
from .bindings import BindingManager
from .context_hooks import add_background
from .images import MAX_IMAGES, inbound_segments, outbound_image, outbound_segments, probe_images, send_image
from .media import send_qr, send_voice_test, text_message
from .nicknames import display_name, list_users, set_nickname
from .profiles import clean_description, description_for, identity_context, save_profile
from . import relay
from . import relay_memory
from .reply_contexts import MAX_AGE, active, message_time, remember, restore
from . import welcome
from .transport import STATE_DIR, allowed_text, api, save_json

GATEWAY = 'anse_ilink'


class ILinkPlugin(MaiBotPlugin):
    async def on_load(self):
        records = await asyncio.to_thread(load_accounts)
        config = await asyncio.to_thread(settings)
        self.contexts = await asyncio.to_thread(restore, records, config.get('primary_account_id'))
        self.seen = {}
        self.tasks = {}
        self.invite_task = None
        self.invite_started = 0
        self.admin_results = {}
        self.invite_drafts = {}
        self.latest_text = {}
        self.latest_inbound = {}
        self.bindings = BindingManager()
        self.invitation_lock = asyncio.Lock()
        self.relay_lock = asyncio.Lock()
        self.relay_results = {}
        self.relay_last = {}
        self.welcome_tasks = {}
        self.welcome_allowances = {}
        self.worker = asyncio.create_task(self.supervise())

    async def on_unload(self):
        await self.bindings.close()
        tasks = [self.worker, *self.tasks.values(), *self.welcome_tasks.values()]
        if self.invite_task:
            tasks.append(self.invite_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def on_config_update(self, scope, config_data, version):
        pass

    async def supervise(self):
        while True:
            accounts = await asyncio.to_thread(load_accounts)
            for account_id in list(self.tasks):
                if account_id not in accounts:
                    self.tasks.pop(account_id).cancel()
            for account_id in accounts:
                if account_id not in self.tasks or self.tasks[account_id].done():
                    self.tasks[account_id] = asyncio.create_task(self.monitor(account_id))
                for user in accounts[account_id].get('allow_from', []):
                    status = await asyncio.to_thread(welcome.state,account_id,user)
                    welcome_key = relay.user_key(account_id,user)
                    if status == 'pending' and (welcome_key not in self.welcome_tasks or self.welcome_tasks[welcome_key].done()):
                        self.welcome_tasks[welcome_key] = asyncio.create_task(self.welcome_user(account_id,user))
            await asyncio.sleep(3)

    async def welcome_user(self, account_id, user):
        uid = None
        try:
            config=await asyncio.to_thread(settings)
            primary=config.get('primary_account_id')
            if not primary:
                return
            uid=session_user(account_id,user,primary)
            context=self.contexts.get(uid)
            if not context or not active(context[1]):
                return  # Keep pending until a genuine inbound message opens the window.
            if not await asyncio.to_thread(welcome.claim,account_id,user):
                return
            records = await asyncio.to_thread(load_accounts)
            config = await asyncio.to_thread(settings)
            primary = config.get('primary_account_id')
            account = records.get(account_id)
            if not primary or not account or user not in account.get('allow_from', []) or is_admin(config,account_id,user):
                await asyncio.to_thread(welcome.finish,account_id,user,'skipped')
                return
            uid = session_user(account_id,user,primary)
            name = await asyncio.to_thread(display_name,account_id,user,config)
            text = welcome.greeting(name)
            await self.ctx.gateway.update_state(gateway_name=GATEWAY,ready=True,platform='weixin_ilink',account_id=primary)
            opened = await self.ctx.chat.open_session(platform='weixin_ilink',chat_type='private',user_id=uid,account_id=primary)
            if not isinstance(opened,dict) or not opened.get('success') or not opened.get('session_id'):
                raise RuntimeError('Welcome session unavailable')
            sent = await self.ctx.send.text(text,opened['session_id'],return_details=True,
                storage_message=True,sync_to_maisaka_history=True,maisaka_source_kind='weixin_welcome')
            if not isinstance(sent,dict) or sent.get('sent') is not True:
                raise RuntimeError('Welcome submission failed')
            await asyncio.to_thread(welcome.finish,account_id,user,'submitted')
            self.ctx.logger.info('iLink welcome submitted; account=%s',key(account_id)[:8])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(welcome.finish,account_id,user,'unconfirmed')
            self.ctx.logger.warning('iLink welcome unconfirmed; error_type=%s',type(exc).__name__)
        finally:
            if uid:
                self.welcome_allowances.pop(uid,None)

    async def monitor(self, account_id):
        cursor_file = STATE_DIR / 'cursors' / (key(account_id) + '.local.json')
        cursor = await asyncio.to_thread(read_json, cursor_file, {})
        # Import the original cursor exactly once; preserve original conversation identity.
        if not cursor and account_id == settings().get('primary_account_id'):
            old = await asyncio.to_thread(read_json, STATE_DIR / 'cursor.local.json', {})
            if old.get('account_id') == account_id:
                cursor = old
        seen = self.seen.setdefault(account_id, deque(cursor.get('seen', []), maxlen=2000))
        while True:
            try:
                account = (await asyncio.to_thread(load_accounts)).get(account_id)
                if not account:
                    return
                config = await asyncio.to_thread(settings)
                primary = config.get('primary_account_id')
                if not primary:
                    raise RuntimeError('Missing primary gateway identity')
                await self.ctx.gateway.update_state(gateway_name=GATEWAY, ready=True,
                    platform='weixin_ilink', account_id=primary)
                result = await asyncio.to_thread(api, account, 'getupdates', {'get_updates_buf': cursor.get('buf', '')})
                for incoming in result.get('msgs', []):
                    incoming_id=str(incoming.get('message_id') or incoming.get('client_id') or '')
                    incoming_user=incoming.get('from_user_id','')
                    if (incoming.get('message_type')==1 and not incoming.get('group_id')
                            and incoming_user in account.get('allow_from',[]) and incoming.get('context_token')
                            and incoming_id and incoming_id not in seen):
                        stamp=message_time(incoming)
                        if await asyncio.to_thread(remember,account,incoming_user,incoming['context_token'],stamp):
                            target_uid=session_user(account_id,incoming_user,primary)
                            self.contexts[target_uid]=(incoming['context_token'],stamp,account_id,incoming_user)
                    parsed = allowed_text(incoming, account)
                    if not parsed:
                        if (incoming.get('message_type') == 1 and not incoming.get('group_id')
                                and incoming.get('from_user_id') in account.get('allow_from', [])):
                            kinds = [item.get('type') for item in incoming.get('item_list', [])]
                            self.ctx.logger.info('iLink unsupported inbound item types=%s; account=%s', kinds, key(account_id)[:8])
                        continue
                    sender, text = parsed
                    msg_id = str(incoming.get('message_id') or incoming.get('client_id') or '')
                    if not msg_id or msg_id in seen:
                        continue
                    uid = session_user(account_id, sender, primary)
                    self.latest_text[uid] = text
                    self.latest_inbound[uid] = msg_id
                    has_image = any(item.get('type') == 2 for item in incoming.get('item_list', []))
                    # The explicitly requested introduction is form input, not a
                    # chat utterance. Consume it before gateway storage or memory.
                    if await self.consume_invitation_draft(account, sender, uid, text, has_image):
                        seen.append(msg_id)
                        continue
                    if not text and not has_image:
                        await self.reply(uid, '收到语音了，但微信接口没有提供转写文字，当前还无法识别这段语音。请先发送文字消息。')
                        self.ctx.logger.info('iLink voice received without transcript; account=%s', key(account_id)[:8])
                    elif not has_image and text.strip() == '/image-test':
                        if not is_admin(config, account_id, sender):
                            await self.reply(uid, '图片测试仅限管理员使用。')
                        else:
                            try:
                                for picture in await asyncio.to_thread(probe_images):
                                    await asyncio.to_thread(send_image, account, sender, incoming['context_token'], picture)
                                await self.reply(uid, '已发送一张蓝色测试图和一张蓝黄交替GIF。请确认两张都能看到，以及第二张是否会动；它们按图片发送，不代表微信原生收藏表情已支持。')
                            except (RuntimeError, ValueError) as exc:
                                await self.reply(uid, '图片测试失败：' + str(exc))
                    elif not has_image and text.strip() == '/voice-test':
                        if not is_admin(config, account_id, sender):
                            await self.reply(uid, '语音测试仅限管理员使用。')
                        else:
                            try:
                                await asyncio.to_thread(send_voice_test, account, sender, incoming['context_token'])
                                await self.reply(uid, '语音测试请求已被接口接受：应为1秒提示音。请确认是否出现可播放的语音气泡；接口接受不代表客户端一定能播放。')
                            except (RuntimeError, ValueError) as exc:
                                await self.reply(uid, '语音测试失败：' + str(exc))
                    elif not has_image and text.strip() in ('/invite', '/cancel-invite'):
                        await self.command(account, sender, uid, text.strip(), config)
                    else:
                        # A single logical MaiBot gateway multiplexes real iLink accounts.
                        # Session user IDs and transport destinations remain separate.
                        host_id = msg_id if account_id == primary else key(account_id) + ':' + msg_id
                        nickname = await asyncio.to_thread(display_name, account_id, sender, config)
                        try:
                            segments = await asyncio.to_thread(inbound_segments, incoming)
                        except (RuntimeError, ValueError) as exc:
                            await self.reply(uid, '这条图片暂时无法处理：' + str(exc))
                            seen.append(msg_id)
                            continue
                        message = {'message_id': host_id, 'platform': 'weixin_ilink', 'account_id': primary,
                            'message_info': {'platform': 'weixin_ilink', 'message_id': host_id, 'time': time.time(),
                                'user_info': {'platform': 'weixin_ilink', 'user_id': uid, 'user_nickname': nickname},
                                'group_info': None, 'additional_config': {'platform_io_target_user_id': uid}},
                            'message_segment': {'type': 'text', 'data': text},
                            'raw_message': segments,
                            'is_picture': has_image}
                        accepted = await self.ctx.gateway.route_message(gateway_name=GATEWAY, message=message)
                        if not accepted:
                            raise RuntimeError('MaiBot rejected inbound message')
                        self.ctx.logger.info('iLink private message accepted; image=%s; user=%s; account=%s', has_image, nickname, key(account_id)[:8])
                    seen.append(msg_id)
                if result.get('get_updates_buf'):
                    cursor['buf'] = result['get_updates_buf']
                cursor['seen'] = list(seen)
                await asyncio.to_thread(save_json, cursor_file, cursor)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 524 is a normal empty long-poll timeout on some official regions.
                if 'HTTP 524' not in str(exc):
                    self.ctx.logger.warning('iLink account %s polling: %s', key(account_id)[:8], type(exc).__name__)
                await asyncio.sleep(3600 if '-14' in str(exc) else 5)

    @HookHandler('maisaka.planner.before_request', name='planner_background')
    async def planner_background(self, items=None, session_id='', **kwargs):
        return await self.request_background(items, session_id, kwargs)

    @HookHandler('maisaka.replyer.before_model_request', name='replyer_background')
    async def replyer_background(self, items=None, session_id='', **kwargs):
        return await self.request_background(items, session_id, kwargs)

    async def request_background(self, items, session_id, payload=None):
        if not session_id or not isinstance(items, list):
            return {'action': 'continue'}
        # Resolve existing host sessions; never compute or fabricate a session ID.
        streams = await self.ctx.chat.get_private_streams(platform='weixin_ilink')
        # SDK 2.16 unwraps the host's {success, streams} response to a list.
        if not isinstance(streams, list):
            raise RuntimeError('Unexpected SDK private-stream response')
        stream = next((s for s in streams
                       if (s.get('session_id') or s.get('stream_id')) == session_id
                       and s.get('platform') == 'weixin_ilink' and not s.get('is_group_session')
                       and not s.get('group_id')), None)
        if not stream:
            return {'action': 'continue'}
        config = await asyncio.to_thread(settings)
        primary = config.get('primary_account_id')
        if not primary or stream.get('account_id') != primary:
            return {'action': 'continue'}
        uid = stream.get('user_id')
        records = await asyncio.to_thread(load_accounts)
        for account_id, account in records.items():
            for user in account.get('allow_from', []):
                if session_user(account_id, user, primary) != uid:
                    continue
                description = await asyncio.to_thread(description_for, account_id, user)
                administrator = is_admin(config, account_id, user)
                waiting = self.invite_drafts.get(uid, 0) > time.time()
                relay_targets = await asyncio.to_thread(relay.targets, relay.user_key(account_id, user))
                identity = await asyncio.to_thread(identity_context, account_id, user)
                shared_records = await asyncio.to_thread(relay_memory.recent, relay.user_key(account_id,user))
                updated = add_background(items, description, administrator, waiting, relay_targets, identity, shared_records)
                return {'action': 'continue', 'modified_kwargs': {
                    **(payload or {}), 'items': updated, 'session_id': session_id}}
        return {'action': 'continue'}

    async def reply(self, uid, text):
        context = self.contexts.get(uid)
        if not context or not active(context[1]):
            raise RuntimeError('微信24小时回复窗口已过期或未知，请让接收方先给Bot发一条消息。')
        account = (await asyncio.to_thread(load_accounts)).get(context[2])
        if not account or context[3] not in account.get('allow_from', []):
            raise ValueError('Reply identity is not allowed')
        return await asyncio.to_thread(text_message, account, context[3], context[0], text)

    @API('users.list', description='列出已接入的微信用户与本地昵称', public=False)
    async def users_list(self):
        return await asyncio.to_thread(list_users)

    @API('users.rename', description='更新本地用户昵称，不修改身份或权限', public=False)
    async def users_rename(self, user_key: str, nickname: str):
        return await asyncio.to_thread(set_nickname, user_key, nickname)

    @API('users.remove', public=False, description='从本地接入名单移除普通用户，保留历史与介绍')
    async def users_remove(self, user_key: str):
        async with self.relay_lock:
            account_id, user = await asyncio.to_thread(remove_user, user_key)
            policy = await asyncio.to_thread(relay.load_config)
            known = await asyncio.to_thread(relay.members)
            groups = []
            for group in policy.get('groups', []):
                remaining = [value for value in group['members'] if value in known]
                if len(remaining) >= 2:
                    groups.append({**group, 'members':remaining})
            await asyncio.to_thread(relay.save_config, json.dumps({'enabled':policy.get('enabled',False),'groups':groups}))
            task = self.tasks.pop(account_id, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for uid, context in list(self.contexts.items()):
                if context[2:] == (account_id, user):
                    self.contexts.pop(uid, None)
                    self.latest_text.pop(uid, None)
                    self.latest_inbound.pop(uid, None)
                    self.invite_drafts.pop(uid, None)
            return {'success':True}

    @API('relay.config', public=False, description='读取后台转达开关与组配置')
    async def relay_config(self):
        return await asyncio.to_thread(relay.load_config)

    @API('context.check', public=False, description='检查宿主会话权限与背景Hook，不返回用户资料')
    async def context_check(self):
        response = await self.ctx.chat.get_private_streams(platform='weixin_ilink')
        # Empty required arguments stop at host validation: no delivery or storage.
        # This exercises capability authorization in the real runner without a message.
        send_probe = await self.ctx.send.text('', '', return_details=True)
        open_available = None
        config = await asyncio.to_thread(settings)
        for stream in response:
            if (stream.get('platform') == 'weixin_ilink' and not stream.get('is_group_session')
                    and stream.get('account_id') == config.get('primary_account_id') and stream.get('user_id')):
                # Reopen the identical existing session; never create a test user.
                opened = await self.ctx.chat.open_session(platform='weixin_ilink',chat_type='private',
                    user_id=stream['user_id'],account_id=stream['account_id'],scope=stream.get('scope') or '')
                open_available = bool(isinstance(opened,dict) and opened.get('success') and opened.get('created') is False)
                break
        injected = 0
        relay_scopes = 0
        if not isinstance(response, list):
            raise RuntimeError('Unexpected SDK private-stream response')
        for stream in response:
            session_id = stream.get('session_id') or stream.get('stream_id')
            if not session_id:
                continue
            result = await self.request_background([], session_id, {})
            items = result.get('modified_kwargs', {}).get('items', [])
            if items:
                injected += 1
                if 'weixin_relay' in json.dumps(items):
                    relay_scopes += 1
        return {'host_query_success': True,
                'host_send_capability_available': isinstance(send_probe,dict) and send_probe.get('sent') is False,
                'host_open_existing_session_available': open_available,
                'background_scopes': injected, 'relay_scopes': relay_scopes,
                'valid_reply_scopes': sum(active(value[1]) for value in self.contexts.values())}

    @API('relay.save', public=False, description='仅WebUI设置转达开关与成员组')
    async def relay_save(self, config_json: str):
        async with self.relay_lock:
            return await asyncio.to_thread(relay.save_config, config_json)

    @ReplyExtension('relay', platforms='weixin_ilink', chat_scope='private',
        description='按当前用户的明确请求给同一转达组成员转告一条文字消息。收件人只能取当前上下文提供的recipient_key，不得猜测。对象重名或内容不明确先询问。执行层强制校验开关与双方同组权限。',
        parameters={'type':'object','properties':{
            'recipient_key':{'type':'string','minLength':64,'maxLength':64},
            'content':{'type':'string','minLength':1,'maxLength':1000}},
            'required':['recipient_key','content'],'additionalProperties':False})
    async def relay_extension(self, phase='', chat=None, parameters=None, reply_id='', **kwargs):
        if phase == 'prepare':
            return {'extra_prompt':'仅按用户明确要求转达；发送前插件检查权限和接收方会话。不要提前承诺成功。'}
        if phase != 'before_send':
            return {}
        async with self.relay_lock:
            result = await self.execute_relay(chat or {}, parameters or {}, reply_id)
        return {'messages':[{'segments':[{'type':'text','data':result}],'quote_previous':False}]}

    @Action('weixin_relay',
        description='给当前用户同组的微信朋友实际转达文字。用户说帮我告诉/转告某人时调用此工具，不能只口头答应。若返回缺少回复凭据，应准确说明需要接收方先发消息；不能说对方未绑定或微信未连接。',
        action_parameters={'recipient_key':'当前上下文可转达名单中的目标键，不得猜测',
                           'content':'用户明确要求转达的文字内容，最多1000字'},
        action_require=['用户明确请求给同组朋友转达消息时使用；对象或内容不清楚先询问'],
        associated_types=['text'])
    async def weixin_relay(self, stream_id='', recipient_key='', content='', **kwargs):
        # Compatibility Action is registered by SDK as a TOOL. Its stream_id is
        # unconditionally overwritten by the host execution context (not model args).
        # Ignore all caller-supplied user/account/platform fields and resolve the stream.
        streams = await self.ctx.chat.get_private_streams(platform='weixin_ilink')
        config = await asyncio.to_thread(settings)
        stream = next((s for s in streams if (s.get('session_id') or s.get('stream_id')) == stream_id
                       and s.get('platform') == 'weixin_ilink' and not s.get('is_group_session')
                       and not s.get('group_id') and s.get('account_id') == config.get('primary_account_id')), None)
        if not stream:
            return {'success':False,'content':'无法确认当前微信私聊会话，未发送。'}
        uid = stream.get('user_id', '')
        message_id = self.latest_inbound.get(uid)
        if not message_id:
            return {'success':False,'content':'请重新发送转达请求以恢复当前会话，未发送。'}
        async with self.relay_lock:
            result = await self.execute_relay(
                {'platform':'weixin_ilink','is_group_chat':False,'user_id':uid},
                {'recipient_key':recipient_key,'content':content}, 'tool:'+message_id)
        return {'success':True,'content':result}

    async def execute_relay(self, chat, parameters, reply_id):
        if (chat.get('platform') != 'weixin_ilink' or chat.get('is_group_chat') is not False
                or chat.get('group_id') or set(parameters) != {'recipient_key','content'}):
            return '无法转达：需要有效的微信私聊身份和明确的收件人与内容。'
        uid = chat.get('user_id', '')
        origin = self.contexts.get(uid)
        config = await asyncio.to_thread(settings)
        primary = config.get('primary_account_id')
        if (not origin or not active(origin[1]) or not primary
                or session_user(origin[2],origin[3],primary) != uid):
            return '当前会话已失效，请重新发起转达请求。'
        known = await asyncio.to_thread(relay.members)
        policy = await asyncio.to_thread(relay.load_config)
        sender = relay.user_key(origin[2],origin[3])
        target = parameters.get('recipient_key')
        if not isinstance(target,str) or not relay.permitted(policy,sender,target,known):
            return '无法转达：功能未开启，或你与接收人不在同一个可转达组。'
        if not reply_id:
            return '无法转达：缺少本次回复标识，请重新发起。'
        # Both the reply extension and standalone tool share per-inbound deduplication.
        cache_key = (uid,self.latest_inbound.get(uid,reply_id))
        if cache_key in self.relay_results:
            return self.relay_results[cache_key]
        recipient = known[target]
        target_uid = session_user(recipient['account_id'],recipient['user_id'],primary)
        destination = self.contexts.get(target_uid)
        if (not destination or not active(destination[1])
                or destination[2:] != (recipient['account_id'],recipient['user_id'])):
            return '对方账号已绑定，但24小时回复窗口已过期或未知，目前尚未发送。请让接收方先给自己的机器人发一条消息，再重新发起转达；不要将此原因描述为未绑定或微信未连接。'
        if time.monotonic()-self.relay_last.get(sender,0) < 10:
            return '转达过于频繁，请稍等10秒再试。'
        try:
            content = relay.relay_content(known[sender]['name'],parameters.get('content'))
        except ValueError as exc:
            return str(exc)
        streams = await self.ctx.chat.get_private_streams(platform='weixin_ilink')
        destination_stream = next((stream for stream in streams
            if stream.get('platform') == 'weixin_ilink' and stream.get('account_id') == primary
            and stream.get('user_id') == target_uid and not stream.get('is_group_session')
            and not stream.get('group_id') and (stream.get('session_id') or stream.get('stream_id'))), None)
        if not destination_stream:
            return '接收方尚无可用的系统聊天会话，未发送，请让对方先发一条消息。'
        destination_id = destination_stream.get('session_id') or destination_stream['stream_id']
        try:
            record_id = await asyncio.to_thread(relay_memory.record_attempt,sender,target,parameters['content'].strip())
        except Exception as exc:
            self.ctx.logger.warning('Shared relay record creation failed: %s',type(exc).__name__)
            return '共享转达记录暂时无法保存，本次尚未发送，请稍后重试。'
        self.relay_last[sender] = time.monotonic()
        # Remember attempts before network I/O; an uncertain result must not trigger duplicate delivery.
        self.relay_results[cache_key] = '这次转达已尝试提交，结果未确认；为避免重复发送，不会自动重试。'
        if len(self.relay_results) > 512:
            self.relay_results.pop(next(iter(self.relay_results)))
        try:
            # The host sends through our outbound gateway, stores the successful
            # bot message and synchronizes the recipient's live model history.
            sent = await self.ctx.send.text(content, destination_id, return_details=True,
                storage_message=True, sync_to_maisaka_history=True, maisaka_source_kind='weixin_relay')
            if not isinstance(sent,dict) or sent.get('sent') is not True:
                raise RuntimeError('Host relay send was not successful')
            result = f'已向{recipient["name"]}的微信会话提交转达；不代表对方已收到或阅读。'
        except Exception:
            result = '转达提交失败或结果未确认，请确认对方会话有效；为避免重复，本次不会自动重试。'
        try:
            await asyncio.to_thread(relay_memory.finish,record_id,'submitted' if result.startswith('已向') else 'unconfirmed')
        except Exception as exc:
            # The network operation may already have succeeded; never resend.
            self.ctx.logger.warning('Shared relay record status uncertain: %s',type(exc).__name__)
        self.relay_results[cache_key] = result
        self.ctx.logger.info('iLink relay attempted; submitted=%s', result.startswith('已向'))
        return result

    @API('binding.status', public=False, description='后台绑定状态，不返回账号凭据')
    async def binding_status(self, binding_id: str = ''):
        config = await asyncio.to_thread(settings)
        return {'has_admin': bool(config.get('admin_account_id') and config.get('admin_user_id')),
                'binding': self.bindings.status(binding_id)}

    @API('binding.start', public=False, description='仅WebUI后台发起扫码绑定')
    async def binding_start(self, mode: str, description: str = ''):
        async with self.invitation_lock:
            if self.invite_task and not self.invite_task.done():
                raise ValueError('聊天邀请正在进行，请先完成或取消。')
            return await self.bindings.start(mode, description)

    @API('binding.cancel', public=False, description='取消后台绑定等待')
    async def binding_cancel(self, binding_id: str):
        return await self.bindings.cancel(binding_id)

    @API('binding.code', public=False, description='提交微信配对验证码')
    async def binding_code(self, binding_id: str, code: str):
        return self.bindings.submit_code(binding_id, code)

    async def command(self, account, sender, uid, text, config):
        result = await self.manage_invitation(account, sender, uid, text)
        await self.reply(uid, result)

    async def consume_invitation_draft(self, account, sender, uid, text, has_image=False):
        if uid not in self.invite_drafts:
            return False
        config = await asyncio.to_thread(settings)
        if not is_admin(config, account['account_id'], sender):
            self.invite_drafts.pop(uid, None)
            return False
        value = text.strip()
        if value in ('取消', '取消邀请', '/cancel-invite', '算了', '先不邀请了'):
            result = await self.manage_invitation(account, sender, uid, '/cancel-invite')
        elif self.invite_drafts[uid] <= time.time():
            self.invite_drafts.pop(uid, None)
            result = '朋友介绍填写已超时，本条未写入聊天或人物记忆。需要时请重新发起邀请。'
        elif has_image or not value:
            result = '当前正在填写邀请资料，请只发送一句文字介绍；不想继续可发送“取消邀请”。'
        else:
            result = await self.manage_invitation(account, sender, uid, '/invite', value)
        # A failed acknowledgment must not send this input through the ordinary
        # chat gateway on a retry after the invitation task has consumed it.
        try:
            await self.reply(uid, result)
        except Exception as exc:
            self.ctx.logger.warning('Invitation form acknowledgment failed: %s', type(exc).__name__)
        # Do not keep the raw form input in reusable per-user buffers either.
        self.latest_text.pop(uid, None)
        self.latest_inbound.pop(uid, None)
        return True

    async def manage_invitation(self, account, sender, uid, text, description=''):
        async with self.invitation_lock:
            return await self._manage_invitation(account, sender, uid, text, description)

    async def _manage_invitation(self, account, sender, uid, text, description=''):
        # Always re-read permissions at execution time, including direct commands.
        config = await asyncio.to_thread(settings)
        if not is_admin(config, account['account_id'], sender):
            return '权限不足：只有管理员可以管理邀请。'
        if text in ('取消邀请', '/cancel-invite'):
            self.invite_drafts.pop(uid, None)
            if self.invite_task and not self.invite_task.done():
                self.invite_task.cancel()
                await asyncio.gather(self.invite_task, return_exceptions=True)
            return '本地邀请已取消；已发出的二维码不再由本服务接收。'
        if text not in ('邀请好友', '/invite'):
            return '未知的邀请操作。'
        if self.bindings.task and not self.bindings.task.done():
            return '后台扫码绑定正在进行，请先完成或取消。'
        account_count = len(await asyncio.to_thread(load_accounts))
        if self.invite_task and not self.invite_task.done():
            return '已有一份有效邀请，请先等待完成或取消邀请。'
        if time.monotonic() - self.invite_started < 30:
            return '请稍候再生成邀请。'
        if account_count >= 16:
            return '已达到16个账号的本地接入上限。'
        if not self.invite_drafts.get(uid, 0) > time.time():
            self.invite_drafts[uid] = time.time() + 600
            return ('好呀，这位朋友是谁呀？你平时怎么称呼，对方和你是什么关系，喜欢些什么？'
                    '下一条请用一句话介绍，我会把它作为本次邀请资料，不写入聊天记忆。'
                    '只有绑定成功才保存到对方资料；不想继续可发送“取消邀请”。')
        if not description:
            return '先告诉我这位朋友是谁吧，用一句话介绍称呼、关系或喜好就好。'
        try:
            description = clean_description(description)
        except ValueError as exc:
            return str(exc)
        if description != self.latest_text.get(uid, '').strip():
            return '请直接发来一句话介绍朋友，我会按你的原话保存，再生成二维码。'
        self.invite_started = time.monotonic()
        self.invite_drafts.pop(uid, None)
        self.invite_task = asyncio.create_task(self.invite(uid, account['account_id'], sender, description))
        return '已开始生成邀请二维码，稍后会单独发送。只有好友扫码授权成功才会完成接入。'

    @ReplyExtension('invitation', platforms='weixin_ilink', chat_scope='private',
        description='认识管理员介绍的新朋友。管理员说“我想把你介绍给我的朋友认识下”“介绍个朋友给你认识”“让我的朋友也来找你聊天”等语义相近表达时，选择本扩展operation=invite，首次会询问朋友是谁。明确邀请好友或生成邀请二维码也支持。随后介绍朋友时再次选invite，并将本条介绍原话完整填入description。取消或暂不介绍选cancel。仅讨论、引用、假设或无关聊天不调用。',
        parameters={'type': 'object', 'properties': {'operation': {
            'type': 'string', 'enum': ['invite', 'cancel'], 'description': '发起或取消邀请'},
            'description': {'type': 'string', 'maxLength': 500, 'description': '询问后，管理员本条介绍原话；首次请求留空。'}},
            'required': ['operation'], 'additionalProperties': False})
    async def invitation_extension(self, phase='', chat=None, parameters=None, reply_id='', **kwargs):
        # chat is supplied separately by the host; never accept identity from parameters.
        if phase == 'prepare':
            return {'extra_prompt': '邀请管理在发送前由插件校验和执行。不要声称好友已接入、二维码已发出或取消成功；以插件返回结果为准。'}
        if phase != 'before_send':
            return {}
        chat = chat or {}
        parameters = parameters or {}
        result = '无法执行：缺少可信的微信私聊身份。'
        uid = chat.get('user_id', '')
        context = self.contexts.get(uid)
        operation = parameters.get('operation')
        valid = (chat.get('platform') == 'weixin_ilink' and chat.get('is_group_chat') is False
                 and not chat.get('group_id') and context and active(context[1])
                 and 'operation' in parameters and set(parameters) <= {'operation', 'description'}
                 and operation in ('invite', 'cancel'))
        if valid:
            account = (await asyncio.to_thread(load_accounts)).get(context[2])
            if account and context[3] in account.get('allow_from', []):
                config = await asyncio.to_thread(settings)
                primary = config.get('primary_account_id')
                if session_user(context[2], context[3], primary) == uid:
                    cache_key = (uid, reply_id, operation)
                    cached = self.admin_results.get(cache_key) if reply_id else None
                    if cached and is_admin(config, context[2], context[3]):
                        result = cached
                    else:
                        result = await self.manage_invitation(account, context[3], uid,
                            '/invite' if operation == 'invite' else '/cancel-invite', parameters.get('description', ''))
                        if reply_id:
                            self.admin_results[cache_key] = result
                            if len(self.admin_results) > 256:
                                self.admin_results.pop(next(iter(self.admin_results)))
        return {'messages': [{'segments': [{'type': 'text', 'data': result}], 'quote_previous': False}]}

    async def invite(self, admin_uid, admin_account, admin_user, description=''):
        try:
            if not is_admin(settings(), admin_account, admin_user):
                return
            if len(load_accounts()) >= 16:
                await self.reply(admin_uid, '已达到16个账号的本地接入上限。')
                return
            qr = await asyncio.to_thread(new_qr)
            await self.reply(admin_uid, '请把二维码和本说明一起转给好友：扫码将授权你的微信机器人连接此 MaiBot，聊天会交给已配置的 AI 模型处理。二维码约5分钟有效，仅限一位好友使用。')
            account = load_accounts()[admin_account]
            context = self.contexts[admin_uid]
            await asyncio.to_thread(send_qr, account, admin_user, context[0], qr['qrcode_img_content'])
            response = await wait_for_login(qr)
            if not is_admin(settings(), admin_account, admin_user):
                raise RuntimeError('Administrator changed; invitation aborted')
            item = await asyncio.to_thread(register, response)
            if description:
                await asyncio.to_thread(save_profile, item['account_id'], response['ilink_user_id'], description)
            await asyncio.to_thread(welcome.queue,item['account_id'],response['ilink_user_id'])
            await self.reply(admin_uid, '好友授权成功，已加入私聊服务。好友可在自己的机器人会话发送“你好”。新账号没有管理员权限。')
        except asyncio.CancelledError:
            raise
        except InvitationExpired:
            self.invite_drafts.pop(admin_uid, None)
            await self.reply(admin_uid, '这次邀请二维码已过期，好友尚未完成绑定。'
                             '原二维码不能再用于本次接入；需要时请重新发起邀请并介绍朋友，我会生成新的二维码。')
        except Exception as exc:
            self.ctx.logger.warning('iLink invitation failed: %s', type(exc).__name__)
            await self.reply(admin_uid, '邀请未完成：' + (str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__))

    @MessageGateway(name=GATEWAY, route_type='duplex', platform='weixin_ilink', protocol='ilink',
                    description='微信官方 iLink 多账号私聊通道')
    async def outbound(self, message, route=None, metadata=None):
        info = message.get('message_info', {})
        if info.get('group_info'):
            return {'success': False}
        uid = (info.get('additional_config') or {}).get('platform_io_target_user_id')
        uid = uid or (info.get('user_info') or {}).get('user_id')
        segments = outbound_segments(message.get('raw_message') or [message.get('message_segment', {})])
        if not segments or uid not in self.contexts:
            return {'success': False,'error':'缺少有效微信回复窗口，请让接收方先给Bot发消息。'}
        if not active(self.contexts[uid][1]):
            return {'success':False,'error':'微信24小时回复窗口已过期或未知，尚未发送。'}
        # Validate all media before sending any text, avoiding silent image loss.
        prepared = []
        image_count = 0
        for segment in segments:
            if segment['type'] == 'text':
                if segment.get('data'):
                    prepared.append(('text', segment['data']))
            else:
                image_count += 1
                if image_count > MAX_IMAGES:
                    raise ValueError('每次回复最多发送4张图片或表情包')
                prepared.append(('image', await asyncio.to_thread(outbound_image, segment)))
        client_id = ''
        for kind, content in prepared:
            if kind == 'text':
                client_id = await self.reply(uid, content)
            else:
                context = self.contexts.get(uid)
                if not context or not active(context[1]):
                    raise RuntimeError('微信24小时回复窗口已过期或未知，请让接收方先给Bot发一条消息。')
                account = (await asyncio.to_thread(load_accounts)).get(context[2])
                if not account or context[3] not in account.get('allow_from', []):
                    raise ValueError('Reply identity is not allowed')
                client_id = await asyncio.to_thread(send_image, account, context[3], context[0], content)
        if not client_id:
            return {'success': False}
        self.ctx.logger.info('iLink private reply sent; images=%s', image_count)
        return {'success': True, 'external_message_id': client_id}

    @staticmethod
    def extract_text(value):
        if isinstance(value, list):
            return ''.join(ILinkPlugin.extract_text(x) for x in value)
        if isinstance(value, dict):
            if value.get('type') == 'text' and isinstance(value.get('data'), str):
                return value['data']
            if value.get('type') == 'seglist':
                return ILinkPlugin.extract_text(value.get('data', []))
        return ''


def create_plugin():
    return ILinkPlugin()
