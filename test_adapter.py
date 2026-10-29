import asyncio
import importlib
import json
import io
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock, patch

package=types.ModuleType('ilink_tests')
package.__path__=[str(Path(__file__).parent)]
sys.modules['ilink_tests']=package
runtime=importlib.import_module('ilink_tests.runtime_v2')
accounts=importlib.import_module('ilink_tests.accounts')
media=importlib.import_module('ilink_tests.media')
transport=importlib.import_module('ilink_tests.transport')
nicknames=importlib.import_module('ilink_tests.nicknames')
profiles=importlib.import_module('ilink_tests.profiles')
bindings=importlib.import_module('ilink_tests.bindings')
context_hooks=importlib.import_module('ilink_tests.context_hooks')
images=importlib.import_module('ilink_tests.images')
relay=runtime.relay
reply_contexts=importlib.import_module('ilink_tests.reply_contexts')
authorization=importlib.import_module('ilink_tests.authorization')

def test_picture(format='PNG'):
    stream=io.BytesIO()
    images.Image.new('RGB',(4,4),'blue').save(stream,format=format)
    return stream.getvalue()

class NicknameTests(unittest.TestCase):
    def test_mapping_is_scoped_validated_and_does_not_change_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            records={'a':{'allow_from':['owner']},'b':{'allow_from':['owner']}}
            with patch.object(nicknames,'STATE_DIR',Path(tmp)),patch.object(nicknames,'load_accounts',return_value=records),patch.object(nicknames,'settings',return_value={}):
                row=nicknames.list_users()['users'][0]
                nicknames.set_nickname(row['key'],'Andself')
                self.assertEqual(nicknames.display_name('a','owner'),'Andself')
                self.assertEqual(nicknames.display_name('b','owner'),'微信用户-'+nicknames.user_key('b','owner')[:8])
                self.assertEqual(nicknames.list_users()['users'][0]['user_id'],'owner')
                with self.assertRaises(ValueError):nicknames.set_nickname('unknown','test')
                with self.assertRaises(ValueError):nicknames.set_nickname(row['key'],'bad\nname')
                with self.assertRaises(ValueError):nicknames.set_nickname(row['key'],'x'*41)
                nicknames.set_nickname(row['key'],'')
                self.assertEqual(nicknames.display_name('a','owner'),'微信用户-'+nicknames.user_key('a','owner')[:8])
                self.assertNotEqual(nicknames.display_name('a','owner'),nicknames.display_name('b','owner'))

class SpeakerIdentityTests(unittest.TestCase):
    def test_structured_introduction_is_not_duplicated_as_unattributed_text(self):
        description='他叫小林，是我的同学'
        identity={'introduction':{'subject':{'name':'小林'},'introduced_by':{'name':'小陈'},
                                  'original_description':description,
                                  'first_person_in_original_description':'introduced_by'}}
        items=context_hooks.add_background([],description=description,identity=identity)
        text=items[0]['parts'][0]['text']
        self.assertEqual(text.count(description),1)
        self.assertNotIn('administrator_introduction',text)

    def test_introduction_preserves_original_and_separates_author_from_subject(self):
        cfg={'admin_account_id':'admin-bot','admin_user_id':'admin-user'}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(profiles,'STATE_DIR',Path(tmp)),patch.object(profiles,'settings',return_value=cfg),patch.object(nicknames,'display_name',side_effect=lambda a,u: '小陈' if a=='admin-bot' else '小林'):
                original='他叫小林，是我的朋友，我喜欢的游戏他也玩'
                profiles.save_profile('guest-bot','guest-user',original)
                value=profiles.identity_context('guest-bot','guest-user')
                intro=value['introduction']
                self.assertEqual(intro['original_description'],original)
                self.assertEqual(intro['introduced_by']['name'],'小陈')
                self.assertEqual(intro['subject']['name'],'小林')
                self.assertNotEqual(intro['introduced_by']['identity_key'],intro['subject']['identity_key'])
                # A later administrator change must not change the original author.
                cfg['admin_account_id']='new-admin'
                self.assertEqual(profiles.identity_context('guest-bot','guest-user')['introduction']['introduced_by']['identity_key'],intro['introduced_by']['identity_key'])

    def test_speaker_rules_are_request_local_and_do_not_rewrite_user_text(self):
        item={'item_type':'UserMessageItem','parts':[{'type':'text','text':'我喜欢DOTA，他说“我不喜欢”'}]}
        result=context_hooks.add_background([item],identity={'current_message_sender':{'name':'小林'}})
        self.assertIs(result[-1],item)
        text=result[0]['parts'][0]['text']
        self.assertIn('不是你（MaiBot）的自述',text)
        self.assertIn('小林',text)
        self.assertEqual(item['parts'][0]['text'],'我喜欢DOTA，他说“我不喜欢”')

class PolicyTests(unittest.TestCase):
    def test_remove_exact_account_overrides_legacy_and_preserves_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(accounts,'STATE_DIR',root),patch.object(accounts,'SETTINGS',root/'settings.local.json'),patch.object(accounts,'ACCOUNT_FILE',root/'legacy.local.json'):
                transport.save_json(accounts.ACCOUNT_FILE,{'account_id':'old','token':'test-secret','allow_from':['owner'],'base_url':transport.BASE_URL})
                transport.save_json(accounts.SETTINGS,{'primary_account_id':'old','admin_account_id':'main','admin_user_id':'admin'})
                transport.save_json(root/'accounts'/ (accounts.key('new')+'.local.json'),{'account_id':'new','token':'test','allow_from':['owner']})
                transport.save_json(root/'accounts'/ (accounts.key('main')+'.local.json'),{'account_id':'main','token':'test','allow_from':['admin']})
                self.assertEqual(accounts.remove_user(nicknames.user_key('old','owner')),('old','owner'))
                self.assertNotIn('old',accounts.load_accounts())
                self.assertIn('new',accounts.load_accounts())
                self.assertEqual(accounts.settings()['primary_account_id'],'old')
                self.assertTrue(accounts.ACCOUNT_FILE.exists())
                with self.assertRaises(ValueError):accounts.remove_user(nicknames.user_key('main','admin'))
                with self.assertRaises(ValueError):accounts.remove_user(nicknames.user_key('old','owner'))

    def test_voice_transcript_and_missing_transcript_preserve_policy(self):
        account = {'allow_from': ['owner']}
        message = {'from_user_id': 'owner', 'message_type': 1, 'context_token': 'test',
                   'item_list': [{'type': 3, 'voice_item': {'text': '语音测试'}}]}
        self.assertEqual(transport.allowed_text(message, account), ('owner', '语音测试'))
        message['item_list'][0]['voice_item'] = {}
        self.assertEqual(transport.allowed_text(message, account), ('owner', ''))
        message['from_user_id'] = 'stranger'
        self.assertIsNone(transport.allowed_text(message, account))
        message['from_user_id'] = 'owner'
        message['group_id'] = 'group'
        self.assertIsNone(transport.allowed_text(message, account))

    def test_admin_requires_account_and_owner(self):
        cfg={'admin_account_id':'mainbot','admin_user_id':'main'}
        self.assertTrue(accounts.is_admin(cfg,'mainbot','main'))
        self.assertFalse(accounts.is_admin(cfg,'guestbot','main'))
        self.assertFalse(accounts.is_admin(cfg,'mainbot','guest'))
        self.assertFalse(accounts.is_admin({},'mainbot','main'))

    def test_sessions_are_isolated_and_original_is_preserved(self):
        self.assertEqual(accounts.session_user('primary','owner','primary'),'owner')
        self.assertNotEqual(accounts.session_user('a','same','primary'),accounts.session_user('b','same','primary'))

    def test_guests_cannot_take_over_admin_and_registration_preserves_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(accounts,'STATE_DIR',root),patch.object(accounts,'SETTINGS',root/'settings.local.json'),patch.object(accounts,'ACCOUNT_FILE',root/'old.json'):
                def response(bot,owner):
                    return {'ilink_bot_id':bot,'ilink_user_id':owner,'bot_token':'test','baseurl':transport.BASE_URL}
                accounts.register(response('original','guest'))
                self.assertFalse(accounts.settings().get('admin_user_id'))
                accounts.register(response('mainbot','main'),admin=True)
                accounts.register(response('friend','friend'))
                self.assertEqual(len(accounts.load_accounts()),3)
                self.assertTrue(accounts.is_admin(accounts.settings(),'mainbot','main'))
                with self.assertRaises(ValueError): accounts.register(response('mainbot','main'))
                with self.assertRaises(ValueError): accounts.register(response('friend','stranger'))
                self.assertEqual(len(accounts.load_accounts()),3)

    def test_private_whitelist_and_group_rejection(self):
        msg={'from_user_id':'owner','message_type':1,'context_token':'ctx','item_list':[{'type':1,'text_item':{'text':'hello'}}]}
        self.assertEqual(transport.allowed_text(msg,{'allow_from':['owner']}),('owner','hello'))
        self.assertIsNone(transport.allowed_text(msg,{'allow_from':[]}))
        self.assertIsNone(transport.allowed_text({**msg,'group_id':'group'},{'allow_from':['owner']}))
        for url in ['http://ilinkai.weixin.qq.com','https://weixin.qq.com.evil.example','https://user@ilinkai.wechat.com']:
            with self.assertRaises(ValueError):transport.official_url(url)

class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        inst=runtime.ILinkPlugin()
        inst._ctx=types.SimpleNamespace(logger=logging.getLogger('test'))
        inst.contexts={}
        inst.invite_task=None
        inst.invite_started=0
        inst.admin_results={}
        inst.invite_drafts={}
        inst.latest_text={}
        inst.latest_inbound={}
        inst.bindings=bindings.BindingManager()
        inst.invitation_lock=asyncio.Lock()
        inst.relay_lock=asyncio.Lock()
        inst.relay_results={}
        inst.relay_last={}
        return inst

    async def test_reply_uses_original_account_and_context(self):
        inst=self.instance()
        inst.contexts={'sessionA':('contextA',time.time(),'a','ownerA'),'sessionB':('contextB',time.time(),'b','ownerB')}
        records={'a':{'account_id':'a','allow_from':['ownerA']},'b':{'account_id':'b','allow_from':['ownerB']}}
        with patch.object(runtime,'load_accounts',return_value=records),patch.object(runtime,'text_message',return_value='sent') as send:
            await inst.reply('sessionB','hi')
            self.assertEqual(send.call_args.args[:3],(records['b'],'ownerB','contextB'))
            with self.assertRaises(RuntimeError):await inst.reply('missing','hi')
            inst.contexts['sessionB']=('ctx',time.time()-reply_contexts.MAX_AGE-1,'b','ownerB')
            with self.assertRaises(RuntimeError):await inst.reply('sessionB','hi')

    async def test_expired_gateway_blocks_text_and_image_before_media_preparation(self):
        inst=self.instance()
        inst.contexts={'expired':('ctx',time.time()-86400,'bot','owner')}
        with patch.object(runtime,'outbound_image') as prepare,patch.object(runtime,'text_message') as send:
            for segment in ({'type':'text','data':'hello'},{'type':'image','data':'unused'}):
                result=await inst.outbound({'message_info':{'user_info':{'user_id':'expired'}},'raw_message':[segment]})
                self.assertFalse(result['success'])
                self.assertIn('24小时',result['error'])
            prepare.assert_not_called()
            send.assert_not_called()

    async def test_context_hooks_resolve_real_session_and_preserve_hook_payload(self):
        inst=self.instance()
        uid=accounts.session_user('guestbot','guest','primary')
        stream={'session_id':'real-session','platform':'weixin_ilink','is_group_session':False,
                'account_id':'primary','user_id':uid}
        inst._ctx.chat=types.SimpleNamespace(get_private_streams=AsyncMock(return_value=[stream]))
        items=[{'item_type':'UserMessageItem','meta':{'item_id':'m'},'parts':[{'type':'text','text':'你好'}]}]
        cfg={'primary_account_id':'primary','admin_account_id':'main','admin_user_id':'admin'}
        with patch.object(runtime,'settings',return_value=cfg),patch.object(runtime,'load_accounts',return_value={'guestbot':{'allow_from':['guest']}}),patch.object(runtime,'description_for',return_value='他叫小周，我的朋友'):
            result=await inst.planner_background(items=items,session_id='real-session',item_schema_version=1,tool_definitions=[{'name':'reply'}])
            payload=result['modified_kwargs']
            self.assertEqual(payload['tool_definitions'],[{'name':'reply'}])
            self.assertEqual(payload['item_schema_version'],1)
            self.assertEqual(payload['items'][-1],items[0])
            self.assertNotIn('plugin_options',json.dumps(payload['items'],ensure_ascii=False))
            result=await inst.replyer_background(items=items,session_id='other-session',request_type='reply')
            self.assertNotIn('modified_kwargs',result)
            stream['is_group_session']=True
            result=await inst.planner_background(items=items,session_id='real-session')
            self.assertNotIn('modified_kwargs',result)

    async def test_invite_requires_question_then_original_description(self):
        inst=self.instance()
        inst.invite=AsyncMock()
        account={'account_id':'main','allow_from':['admin']}
        cfg={'primary_account_id':'primary','admin_account_id':'main','admin_user_id':'admin'}
        with patch.object(runtime,'settings',return_value=cfg),patch.object(runtime,'load_accounts',return_value={'main':account}):
            first=await inst.manage_invitation(account,'admin','uid','/invite','编造的介绍')
            self.assertIn('这位朋友是谁',first)
            self.assertIsNone(inst.invite_task)
            inst.latest_text['uid']='小林，我的同学，喜欢露营'
            rejected=await inst.manage_invitation(account,'admin','uid','/invite','模型编造的资料')
            self.assertIn('原话',rejected)
            self.assertIsNone(inst.invite_task)
            await inst.manage_invitation(account,'admin','uid','/invite',inst.latest_text['uid'])
            await inst.invite_task
            inst.invite.assert_awaited_once_with('uid','main','admin','小林，我的同学，喜欢露营')
            self.assertNotIn('uid',inst.invite_drafts)

    async def test_guest_invitation_does_not_generate_qr(self):
        inst=self.instance()
        inst.reply=AsyncMock()
        cfg={'admin_account_id':'main','admin_user_id':'admin'}
        with patch.object(runtime,'new_qr') as generate, patch.object(runtime,'settings',return_value=cfg):
            await inst.command({'account_id':'guest'},'guest','uid','邀请好友',cfg)
            generate.assert_not_called()
        inst.reply.assert_awaited_once()
        self.assertIsNone(inst.invite_task)

    async def test_duplicate_and_cancel_invite(self):
        inst=self.instance()
        inst.reply=AsyncMock()
        inst.invite_task=asyncio.create_task(asyncio.sleep(30))
        cfg={'admin_account_id':'main','admin_user_id':'admin'}
        with patch.object(runtime,'settings',return_value=cfg):
            await inst.command({'account_id':'main'},'admin','uid','邀请好友',cfg)
            self.assertFalse(inst.invite_task.done())
            await inst.command({'account_id':'main'},'admin','uid','取消邀请',cfg)
        self.assertTrue(inst.invite_task.cancelled())

    async def test_extension_rejects_forged_identity_and_guest(self):
        inst=self.instance()
        inst.manage_invitation=AsyncMock(return_value='权限不足')
        chat={'platform':'weixin_ilink','is_group_chat':False,'user_id':'guest'}
        records={'guestbot':{'account_id':'guestbot','allow_from':['guest']}}
        uid=accounts.session_user('guestbot','guest','primary')
        inst.contexts[uid]=('ctx',time.time(),'guestbot','guest')
        chat['user_id']=uid
        cfg={'primary_account_id':'primary','admin_account_id':'main','admin_user_id':'admin'}
        with patch.object(runtime,'load_accounts',return_value=records),patch.object(runtime,'settings',return_value=cfg):
            result=await inst.invitation_extension(phase='before_send',chat=chat,
                parameters={'operation':'invite','user_id':'admin'})
            inst.manage_invitation.assert_not_awaited()
            self.assertIn('无法执行',result['messages'][0]['segments'][0]['data'])
            await inst.invitation_extension(phase='before_send',chat={**chat,'user_id':'admin'},parameters={'operation':'invite'})
            inst.manage_invitation.assert_not_awaited()
            # Real guest still reaches the common executor, which independently denies permissions.
            inst.manage_invitation=runtime.ILinkPlugin.manage_invitation.__get__(inst)
            result=await inst.invitation_extension(phase='before_send',chat=chat,parameters={'operation':'invite'})
            self.assertIn('权限不足',result['messages'][0]['segments'][0]['data'])

    async def test_extension_admin_retry_and_revocation(self):
        inst=self.instance()
        uid=accounts.session_user('main','admin','primary')
        inst.contexts[uid]=('ctx',time.time(),'main','admin')
        cfg={'primary_account_id':'primary','admin_account_id':'main','admin_user_id':'admin'}
        records={'main':{'account_id':'main','allow_from':['admin']}}
        chat={'platform':'weixin_ilink','is_group_chat':False,'user_id':uid}
        inst.manage_invitation=AsyncMock(return_value='开始生成')
        with patch.object(runtime,'load_accounts',return_value=records),patch.object(runtime,'settings',return_value=cfg):
            await inst.invitation_extension(phase='prepare',chat=chat,parameters={'operation':'invite'})
            inst.manage_invitation.assert_not_awaited()
            for _ in range(2):
                await inst.invitation_extension(phase='before_send',chat=chat,parameters={'operation':'invite'},reply_id='reply1')
            inst.manage_invitation.assert_awaited_once()
            inst.manage_invitation=runtime.ILinkPlugin.manage_invitation.__get__(inst)
            cfg['admin_user_id']='changed'
            result=await inst.invitation_extension(phase='before_send',chat=chat,parameters={'operation':'invite'},reply_id='reply1')
            self.assertIn('权限不足',result['messages'][0]['segments'][0]['data'])

class ImageTests(unittest.TestCase):
    def test_download_decrypts_all_key_encodings_without_bearer(self):
        plain=test_picture()
        raw_key=b'0123456789abcdef'
        padder=media.padding.PKCS7(128).padder()
        padded=padder.update(plain)+padder.finalize()
        encryptor=media.Cipher(media.algorithms.AES(raw_key),media.modes.ECB()).encryptor()
        encrypted=encryptor.update(padded)+encryptor.finalize()
        for source in ({'aeskey':raw_key.hex()}, {'media':{'aes_key':media.base64.b64encode(raw_key).decode()}},
                       {'media':{'aes_key':media.base64.b64encode(raw_key.hex().encode()).decode()}}):
            image={**source,'media':{**source.get('media',{}),'encrypt_query_param':'test'}}
            response=Mock()
            response.__enter__=Mock(return_value=response)
            response.__exit__=Mock(return_value=False)
            response.read.return_value=encrypted
            opener=Mock();opener.open.return_value=response
            with patch.object(images.urllib.request,'build_opener',return_value=opener):
                self.assertEqual(images.download_image({'image_item':image}),plain)
                self.assertNotIn('Authorization',opener.open.call_args.args[0].headers)

    def test_download_rejects_untrusted_url_and_size(self):
        for url in ('http://novac2c.cdn.weixin.qq.com/x','https://127.0.0.1/x','https://weixin.qq.com.evil.test/x','https://user@cdn.wechat.com/x'):
            with self.assertRaises(ValueError):images.cdn_url(url)
        with self.assertRaises(ValueError):images.validate_image(b'not an image')
        with self.assertRaises(ValueError):images.outbound_image({'type':'image','data':'C:/private-file.png'})

    def test_inbound_images_and_text_preserve_order_and_whitelist(self):
        message={'message_type':1,'from_user_id':'owner','context_token':'ctx','item_list':[
            {'type':1,'text_item':{'text':'看这张图'}},{'type':2,'image_item':{}}]}
        with patch.object(images,'download_image',return_value=test_picture()):
            segments=images.inbound_segments(message)
            self.assertEqual([s['type'] for s in segments],['text','image'])
            self.assertTrue(segments[1]['binary_data_base64'])
        self.assertIsNotNone(transport.allowed_text(message,{'allow_from':['owner']}))
        self.assertIsNone(transport.allowed_text(message,{'allow_from':[]}))
        self.assertEqual(transport.allowed_text({**message,'item_list':[{'type':2}]},{'allow_from':['owner']}),('owner',''))

    def test_outbound_emoji_preserves_gif_bytes(self):
        plain=test_picture('GIF')
        segment={'type':'emoji','binary_data_base64':media.base64.b64encode(plain).decode()}
        self.assertEqual(images.outbound_image(segment),plain)
        with patch.object(images,'upload_media',return_value=({'encrypt_type':1},128)) as upload,patch.object(images,'send_items',return_value='sent') as send:
            images.send_image({'allow_from':['owner']},'owner','ctx',plain)
            self.assertEqual(upload.call_args.args[2],plain)
            self.assertEqual(upload.call_args.args[3],1)
            self.assertEqual(send.call_args.args[3][0]['type'],2)

class ImageRoutingTests(unittest.IsolatedAsyncioTestCase):
    instance=RoutingTests.instance
    async def test_outbound_mixed_reply_uses_actual_account_in_order(self):
        inst=self.instance()
        inst.contexts={'uid':('ctx',time.time(),'friendbot','owner')}
        inst.reply=AsyncMock(return_value='text-sent')
        image={'type':'emoji','binary_data_base64':media.base64.b64encode(test_picture('GIF')).decode()}
        message={'message_info':{'additional_config':{'platform_io_target_user_id':'uid'}},
                 'raw_message':[{'type':'text','data':'好耶'},image]}
        record={'account_id':'friendbot','allow_from':['owner']}
        with patch.object(runtime,'load_accounts',return_value={'friendbot':record}),patch.object(runtime,'send_image',return_value='image-sent') as send:
            result=await inst.outbound(message)
            self.assertTrue(result['success'])
            inst.reply.assert_awaited_once_with('uid','好耶')
            self.assertEqual(send.call_args.args[:3],(record,'owner','ctx'))
        inst.reply.reset_mock()
        message['raw_message'][1]={'type':'image','data':'missing'}
        with self.assertRaises(ValueError):await inst.outbound(message)
        inst.reply.assert_not_awaited()

class ReplyContextTests(unittest.TestCase):
    def test_24_hour_boundary_future_and_invalid_timestamps(self):
        now=1000000
        self.assertTrue(reply_contexts.active(now-86399,now))
        self.assertFalse(reply_contexts.active(now-86400,now))
        self.assertFalse(reply_contexts.active(now+1,now))
        self.assertFalse(reply_contexts.active(float('nan'),now))
        self.assertFalse(reply_contexts.active(None,now))

    def test_message_creation_time_not_poll_time_opens_window(self):
        now=time.time()
        old=now-90000
        stamp=reply_contexts.message_time({'create_time_ms':old*1000})
        self.assertAlmostEqual(stamp,old,delta=0.001)
        self.assertFalse(reply_contexts.active(stamp))
        self.assertEqual(reply_contexts.message_time({}),0)
        self.assertEqual(reply_contexts.message_time({'create_time_ms':(now+1000)*1000}),0)

    def test_legacy_unknown_and_transport_blocks_before_network(self):
        account={'account_id':'bot','token':'credential','allow_from':['owner'],'base_url':'https://ilinkai.weixin.qq.com'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(reply_contexts,'STATE_DIR',Path(tmp)),patch.object(transport,'request',return_value={}) as network:
            path=Path(tmp)/'reply-contexts'/(nicknames.user_key('bot','owner')+'.local.json')
            transport.save_json(path,{'user_id':'owner','context_token':'old','received_at':time.time(),
                'credential_fingerprint':reply_contexts.fingerprint(account)})
            self.assertEqual(reply_contexts.window(account,'owner')['status'],'unknown')
            with self.assertRaises(RuntimeError):transport.api(account,'sendmessage',{'msg':{'to_user_id':'owner','context_token':'old'}})
            self.assertEqual(reply_contexts.restore({'bot':account},'primary'),{})
            reply_contexts.remember(account,'owner','ctx',time.time()-90000)
            self.assertEqual(reply_contexts.window(account,'owner')['status'],'expired')
            with self.assertRaises(RuntimeError):transport.api(account,'sendmessage',{'msg':{'to_user_id':'owner','context_token':'ctx'}})
            network.assert_not_called()
            reply_contexts.remember(account,'owner','fresh',time.time())
            transport.api(account,'sendmessage',{'msg':{'to_user_id':'owner','context_token':'fresh'}})
            self.assertEqual(network.call_count,1)
            # Outbound success must not extend the received-at timestamp.
            before=reply_contexts.window(account,'owner')['last_inbound_at']
            transport.api(account,'sendmessage',{'msg':{'to_user_id':'owner','context_token':'fresh'}})
            self.assertEqual(reply_contexts.window(account,'owner')['last_inbound_at'],before)

    def test_timestamp_and_token_are_scoped_to_account_and_credential(self):
        account={'account_id':'bot','token':'credential','allow_from':['owner'],'base_url':'https://ilinkai.weixin.qq.com'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(reply_contexts,'STATE_DIR',Path(tmp)):
            reply_contexts.remember(account,'owner','ctx',time.time()-7200)
            self.assertEqual(reply_contexts.window(account,'owner')['status'],'active')
            self.assertFalse(reply_contexts.remember(account,'owner','older',time.time()-8000))
            self.assertEqual(reply_contexts.window({**account,'account_id':'other'},'owner')['status'],'unknown')
            self.assertEqual(reply_contexts.window({**account,'token':'rotated'},'owner')['status'],'unknown')
            with self.assertRaises(RuntimeError):reply_contexts.require_window(account,'owner','different-token')

    def test_restart_restore_excludes_expired_rotated_and_disabled_accounts(self):
        account={'account_id':'bot','token':'credential','allow_from':['owner'],'base_url':'https://ilinkai.weixin.qq.com'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(reply_contexts,'STATE_DIR',Path(tmp)):
            reply_contexts.remember(account,'owner','reply-token',time.time())
            uid=accounts.session_user('bot','owner','primary')
            restored=reply_contexts.restore({'bot':account},'primary')
            self.assertEqual(restored[uid][0],'reply-token')
            self.assertEqual(restored[uid][2:],('bot','owner'))
            self.assertEqual(reply_contexts.restore({},'primary'),{})
            self.assertEqual(reply_contexts.restore({'bot':{**account,'token':'new-credential'}},'primary'),{})
            self.assertEqual(reply_contexts.restore({'bot':account},None),{})
            self.assertFalse(reply_contexts.remember(account,'owner','old-token',time.time()-3601))
            with patch.object(reply_contexts.time,'time',return_value=time.time()+reply_contexts.MAX_AGE+1):
                self.assertEqual(reply_contexts.restore({'bot':account},'primary'),{})
            with self.assertRaises(ValueError):reply_contexts.remember(account,'stranger','token',time.time())

class RelayPolicyTests(unittest.TestCase):
    def test_groups_are_explicit_non_transitive_and_default_off(self):
        known={'a':{},'b':{},'c':{},'admin':{}}
        config={'enabled':True,'groups':[{'members':['a','b']},{'members':['b','c']}]}
        self.assertTrue(relay.permitted(config,'a','b',known))
        self.assertFalse(relay.permitted(config,'a','c',known))
        self.assertFalse(relay.permitted(config,'admin','b',known))
        self.assertFalse(relay.permitted(config,'a','a',known))
        config['enabled']=False
        self.assertFalse(relay.permitted(config,'a','b',known))
        with tempfile.TemporaryDirectory() as tmp,patch.object(relay,'STATE_DIR',Path(tmp)):
            self.assertEqual(relay.load_config(),{'enabled':False,'groups':[]})

    def test_config_validates_members_and_preserves_disabled_groups(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(relay,'STATE_DIR',Path(tmp)),patch.object(relay,'members',return_value={'a':{},'b':{}}):
            value={'enabled':False,'groups':[{'name':'好友','members':['a','b']}]}
            saved=relay.save_config(json.dumps(value))
            self.assertFalse(saved['enabled'])
            self.assertEqual(saved['groups'][0]['members'],['a','b'])
            self.assertFalse(saved['groups'][0]['share_records'])
            self.assertTrue(saved['groups'][0]['id'])
            for invalid in ([{'name':'好友','members':['a']}],[{'name':'好友','members':['a','unknown']}],[{'name':'好友','members':['a','a']}]):
                with self.assertRaises(ValueError):relay.save_config(json.dumps({'enabled':True,'groups':invalid}))

class SharedRelayRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.state_patch=patch.object(relay,'STATE_DIR',Path(self.tmp.name));self.state_patch.start()
        self.known={key:{'name':key.upper()} for key in ('a','b','c','d')}
        self.member_patch=patch.object(relay,'members',return_value=self.known);self.member_patch.start()
        self.addCleanup(self.member_patch.stop);self.addCleanup(self.state_patch.stop);self.addCleanup(self.tmp.cleanup)

    def save(self,groups,enabled=True):
        return relay.save_config(json.dumps({'enabled':enabled,'groups':groups}))

    def test_default_off_no_backfill_and_exact_group_visibility(self):
        memory=runtime.relay_memory
        cfg=self.save([{'name':'组一','members':['a','b','c']},{'name':'组二','members':['b','d']}])
        self.assertIsNone(memory.record_attempt('a','b','关闭期间不记录'))
        self.assertFalse((relay.STATE_DIR/'relay-records.local.db').exists())
        cfg['groups'][0]['share_records']=True;self.save(cfg['groups'])
        event=memory.record_attempt('a','b','今晚八点开团')
        memory.finish(event,'submitted')
        self.assertEqual(memory.recent('c')[0]['content'],'今晚八点开团')
        self.assertEqual(memory.recent('c')[0]['status'],'submitted')
        self.assertEqual(len(memory.recent('a')),1)
        self.assertEqual(memory.recent('d'),[])
        self.assertEqual(memory.recent('unknown'),[])

    def test_disable_remove_rename_and_recreate_permissions(self):
        memory=runtime.relay_memory
        cfg=self.save([{'name':'原名','members':['a','b','c'],'share_records':True}])
        event=memory.record_attempt('a','b','共享正文');memory.finish(event,'unconfirmed')
        group=cfg['groups'][0];original_id=group['id']
        group['name']='新名';self.save([group])
        self.assertEqual(memory.recent('c')[0]['group'],'新名')
        self.assertEqual(memory.recent('c')[0]['status'],'unconfirmed')
        group['members']=['a','b'];self.save([group]);self.assertEqual(memory.recent('c'),[])
        group['share_records']=False;self.save([group]);self.assertEqual(memory.recent('a'),[])
        self.assertIsNone(memory.record_attempt('a','b','不共享的新消息'))
        group['share_records']=True;self.save([group],enabled=False);self.assertEqual(memory.recent('a'),[])
        self.save([])
        new=self.save([{'name':'新名','members':['a','b'],'share_records':True}])
        self.assertNotEqual(new['groups'][0]['id'],original_id)
        self.assertEqual(memory.recent('a'),[])

    def test_expiry_status_and_no_personal_context_in_record(self):
        memory=runtime.relay_memory
        self.save([{'name':'组','members':['a','b'],'share_records':True}])
        with patch.object(memory.time,'time',return_value=100):
            event=memory.record_attempt('a','b','仅保存明确转达正文')
            rows=memory.recent('a');self.assertEqual(rows[0]['status'],'attempting')
            self.assertNotIn('context_token',json.dumps(rows))
            self.assertNotIn('description',json.dumps(rows))
        with patch.object(memory.time,'time',return_value=100+memory.RETENTION_SECONDS+1):
            self.assertEqual(memory.recent('a'),[])
        with self.assertRaises(ValueError):memory.finish(event,'delivered')

class RelayExecutionTests(unittest.IsolatedAsyncioTestCase):
    instance=RoutingTests.instance

    async def test_remove_stops_polling_context_and_prunes_relay_groups(self):
        inst=self.instance()
        task=asyncio.create_task(asyncio.sleep(30))
        inst.tasks={'oldbot':task}
        inst.contexts={'old-session':('ctx',time.time(),'oldbot','owner'),'other':('ctx',time.time(),'newbot','owner')}
        inst.latest_text={'old-session':'test'}
        policy={'enabled':True,'groups':[{'name':'旧组','members':['old','b']},{'name':'保留组','members':['old','b','c']}]}
        with patch.object(runtime,'remove_user',return_value=('oldbot','owner')),patch.object(relay,'load_config',return_value=policy),patch.object(relay,'members',return_value={'b':{},'c':{}}),patch.object(relay,'save_config') as save:
            await inst.users_remove('old')
            self.assertTrue(task.cancelled())
            self.assertNotIn('old-session',inst.contexts)
            self.assertIn('other',inst.contexts)
            self.assertEqual(json.loads(save.call_args.args[0])['groups'],[{'name':'保留组','members':['b','c']}])

    async def test_standalone_tool_resolves_host_stream_ignoring_identity_arguments(self):
        inst=self.instance()
        stream={'session_id':'actual-stream','platform':'weixin_ilink','account_id':'primary',
                'user_id':'actual-user','is_group_session':False}
        inst._ctx.chat=types.SimpleNamespace(get_private_streams=AsyncMock(return_value=[stream]))
        inst.latest_inbound['actual-user']='actual-message'
        inst.execute_relay=AsyncMock(return_value='真实执行结果')
        with patch.object(runtime,'settings',return_value={'primary_account_id':'primary'}):
            result=await inst.weixin_relay(stream_id='actual-stream',recipient_key='target',content='晚上八点上线',
                                          user_id='forged-admin',platform='qq',account_id='forged-account')
            self.assertEqual(result['content'],'真实执行结果')
            self.assertEqual(inst.execute_relay.call_args.args[0]['user_id'],'actual-user')
            inst.execute_relay.reset_mock()
            result=await inst.weixin_relay(stream_id='missing',recipient_key='target',content='你好')
            self.assertFalse(result['success'])
            inst.execute_relay.assert_not_awaited()

    async def test_real_sender_target_account_and_retry_deduplication(self):
        inst=self.instance()
        uid=accounts.session_user('a','alice','primary')
        other_uid=accounts.session_user('b','bob','primary')
        inst.contexts={uid:('ctx-a',time.time(),'a','alice'),other_uid:('ctx-b',time.time(),'b','bob')}
        known={'key-a':{'account_id':'a','user_id':'alice','name':'小明'},'key-b':{'account_id':'b','user_id':'bob','name':'小周'}}
        policy={'enabled':True,'groups':[{'members':['key-a','key-b']}]}
        chat={'platform':'weixin_ilink','is_group_chat':False,'user_id':uid}
        inst.reply=AsyncMock(return_value='sent')
        inst._ctx.chat=types.SimpleNamespace(get_private_streams=AsyncMock(return_value=[
            {'session_id':'recipient-session','platform':'weixin_ilink','account_id':'primary',
             'user_id':other_uid,'is_group_session':False}]))
        inst._ctx.send=types.SimpleNamespace(text=AsyncMock(return_value={'sent':True,'message_id':'sent-id'}))
        with patch.object(runtime,'settings',return_value={'primary_account_id':'primary'}),patch.object(relay,'members',return_value=known),patch.object(relay,'load_config',return_value=policy),patch.object(relay,'user_key',side_effect=lambda a,u:'key-a' if a=='a' else 'key-b'),patch.object(runtime.relay_memory,'record_attempt',return_value='event-id') as record,patch.object(runtime.relay_memory,'finish') as finish:
            for _ in range(2):
                result=await inst.execute_relay(chat,{'recipient_key':'key-b','content':'晚上八点上线'},'reply1')
                self.assertIn('已向小周',result)
            inst._ctx.send.text.assert_awaited_once_with('【小明 托我转达】\n晚上八点上线','recipient-session',
                return_details=True,storage_message=True,sync_to_maisaka_history=True,maisaka_source_kind='weixin_relay')
            inst.reply.assert_not_awaited()
            record.assert_called_once_with('key-a','key-b','晚上八点上线')
            finish.assert_called_once_with('event-id','submitted')
            inst.relay_last.clear()
            inst._ctx.send.text.side_effect=RuntimeError('uncertain network result')
            for _ in range(2):
                result=await inst.execute_relay(chat,{'recipient_key':'key-b','content':'另一条消息'},'reply-failed')
                self.assertIn('不会自动重试',result)
            self.assertEqual(inst._ctx.send.text.await_count,2)
            self.assertEqual(record.call_count,2)
            finish.assert_called_with('event-id','unconfirmed')
            policy['enabled']=False
            result=await inst.execute_relay(chat,{'recipient_key':'key-b','content':'晚上八点上线'},'reply1')
            self.assertIn('无法转达',result)

    async def test_missing_recipient_context_or_forged_identity_cannot_send(self):
        inst=self.instance();inst.reply=AsyncMock()
        uid=accounts.session_user('a','alice','primary')
        inst.contexts={uid:('ctx',time.time(),'a','alice')}
        known={'key-a':{'account_id':'a','user_id':'alice','name':'A'},'key-b':{'account_id':'b','user_id':'bob','name':'B'}}
        policy={'enabled':True,'groups':[{'members':['key-a','key-b']}]}
        chat={'platform':'weixin_ilink','is_group_chat':False,'user_id':uid}
        with patch.object(runtime,'settings',return_value={'primary_account_id':'primary'}),patch.object(relay,'members',return_value=known),patch.object(relay,'load_config',return_value=policy),patch.object(relay,'user_key',return_value='key-a'):
            result=await inst.execute_relay(chat,{'recipient_key':'key-b','content':'你好'},'reply1')
            self.assertIn('24小时',result)
            result=await inst.execute_relay({**chat,'user_id':'forged-admin'},{'recipient_key':'key-b','content':'你好'},'reply2')
            self.assertIn('会话已失效',result)
            result=await inst.execute_relay(chat,{'recipient_key':'key-b','content':'你好','sender_id':uid},'reply3')
            self.assertIn('无法转达',result)
        inst.reply.assert_not_awaited()

class InvitationDraftTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        inst=RoutingTests().instance()
        inst.reply=AsyncMock()
        inst.invite_drafts={'uid':time.time()+600}
        inst.latest_text={'uid':'她叫小林，是我的同事'}
        inst.latest_inbound={'uid':'draft-message'}
        inst.invite=AsyncMock()
        return inst

    async def test_requested_introduction_is_consumed_as_form_input(self):
        inst=self.instance()
        cfg={'admin_account_id':'admin','admin_user_id':'owner'}
        account={'account_id':'admin','allow_from':['owner']}
        with patch.object(runtime,'settings',return_value=cfg),patch.object(runtime,'load_accounts',return_value={'admin':account}):
            consumed=await inst.consume_invitation_draft(account,'owner','uid','她叫小林，是我的同事')
            self.assertTrue(consumed)
            await inst.invite_task
            inst.invite.assert_awaited_once_with('uid','admin','owner','她叫小林，是我的同事')
            self.assertNotIn('uid',inst.invite_drafts)
            self.assertNotIn('uid',inst.latest_text)
            self.assertNotIn('uid',inst.latest_inbound)

    async def test_expired_form_and_cancel_do_not_start_invitation(self):
        cfg={'admin_account_id':'admin','admin_user_id':'owner'}
        account={'account_id':'admin','allow_from':['owner']}
        for value,deadline in [('介绍',time.time()-1),('取消邀请',time.time()+600)]:
            inst=self.instance();inst.invite_drafts['uid']=deadline
            with patch.object(runtime,'settings',return_value=cfg):
                self.assertTrue(await inst.consume_invitation_draft(account,'owner','uid',value))
            inst.invite.assert_not_awaited()
            self.assertNotIn('uid',inst.invite_drafts)

    async def test_failed_acknowledgment_does_not_fall_back_to_normal_chat(self):
        inst=self.instance();inst.reply.side_effect=RuntimeError('network unavailable')
        cfg={'admin_account_id':'admin','admin_user_id':'owner'}
        account={'account_id':'admin','allow_from':['owner']}
        with patch.object(runtime,'settings',return_value=cfg),patch.object(runtime,'load_accounts',return_value={'admin':account}):
            self.assertTrue(await inst.consume_invitation_draft(account,'owner','uid','她叫小林，是我的同事'))
            await inst.invite_task
        self.assertNotIn('uid',inst.latest_text)

    async def test_normal_chat_is_not_consumed_and_non_admin_cannot_supply_profile(self):
        inst=self.instance();account={'account_id':'ordinary'}
        with patch.object(runtime,'settings',return_value={'admin_account_id':'admin','admin_user_id':'owner'}):
            self.assertFalse(await inst.consume_invitation_draft(account,'guest','other','普通聊天'))
            self.assertFalse(await inst.consume_invitation_draft(account,'guest','uid','伪造资料'))
        inst.invite.assert_not_awaited();inst.reply.assert_not_awaited()

class ExpirationTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_expiry_is_distinct_and_cannot_register(self):
        with patch.object(authorization,'request',return_value={'status':'expired'}):
            with self.assertRaises(authorization.InvitationExpired):
                await authorization.wait_for_login({'qrcode':'test'})

    async def test_pairing_wait_is_bounded_by_total_deadline(self):
        async def wait_forever():
            await asyncio.Event().wait()
        with patch.object(authorization,'LOGIN_TIMEOUT',0.01),patch.object(authorization,'request',return_value={'status':'need_verifycode'}):
            with self.assertRaises(authorization.InvitationExpired):
                await authorization.wait_for_login({'qrcode':'test'},on_verify=wait_forever)

    async def test_webui_expiry_clears_qr_and_allows_new_attempt(self):
        manager=bindings.BindingManager()
        manager.state={'binding_id':'old','status':'creating','mode':'user','qr_image':''}
        cfg={'admin_account_id':'admin','admin_user_id':'owner'}
        with patch.object(bindings,'new_qr',return_value={'qrcode_img_content':'test'}),patch.object(bindings,'qr_image',return_value='image'),patch.object(bindings,'wait_for_login',AsyncMock(side_effect=authorization.InvitationExpired())),patch.object(bindings,'register') as register:
            manager.task=asyncio.create_task(manager.run(dict(manager.state),'介绍',cfg))
            await manager.task
            self.assertEqual(manager.state['status'],'expired')
            self.assertEqual(manager.state['qr_image'],'')
            register.assert_not_called()
        with patch.object(bindings,'settings',return_value=cfg),patch.object(bindings,'load_accounts',return_value={}),patch.object(manager,'run',AsyncMock()):
            result=await manager.start('user','介绍')
            self.assertNotEqual(result['binding_id'],'old')
            await manager.task

    async def test_chat_expiry_notifies_without_registering_or_automatic_retry(self):
        inst=RoutingTests().instance();inst.reply=AsyncMock()
        inst.contexts={'admin-uid':('context',time.time(),'admin','owner')}
        inst.invite_drafts={'admin-uid':time.time()+600}
        cfg={'admin_account_id':'admin','admin_user_id':'owner'}
        account={'account_id':'admin','allow_from':['owner']}
        with patch.object(runtime,'settings',return_value=cfg),patch.object(runtime,'load_accounts',return_value={'admin':account}),patch.object(runtime,'new_qr',return_value={'qrcode_img_content':'qr'}) as qr,patch.object(runtime,'send_qr'),patch.object(runtime,'wait_for_login',AsyncMock(side_effect=authorization.InvitationExpired())),patch.object(runtime,'register') as register:
            await inst.invite('admin-uid','admin','owner','介绍')
            self.assertIn('已过期',inst.reply.await_args.args[1])
            self.assertNotIn('admin-uid',inst.invite_drafts)
            qr.assert_called_once();register.assert_not_called()

class BindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_bootstrap_and_guest_description_guards(self):
        manager=bindings.BindingManager()
        cfg={'admin_account_id':'main','admin_user_id':'admin'}
        with patch.object(bindings,'settings',return_value=cfg):
            with self.assertRaises(ValueError):await manager.start('admin')
            with self.assertRaises(ValueError):await manager.start('user','')
        with patch.object(bindings,'settings',return_value={}):
            with self.assertRaises(ValueError):await manager.start('user','一位朋友')
        self.assertIsNone(manager.task)

    async def test_binding_confirmation_saves_profile_for_scanning_user(self):
        manager=bindings.BindingManager()
        manager.state={'binding_id':'test','mode':'user','status':'creating','expires_at':time.time()+300}
        cfg={'admin_account_id':'main','admin_user_id':'admin'}
        response={'ilink_bot_id':'friendbot','ilink_user_id':'friend','bot_token':'secret-test'}
        with patch.object(bindings,'new_qr',return_value={'qrcode_img_content':'https://example.test'}),patch.object(bindings,'qr_image',return_value='data:image/png;base64,test'),patch.object(bindings,'wait_for_login',new=AsyncMock(return_value=response)),patch.object(bindings,'settings',return_value=cfg),patch.object(bindings,'register',return_value={'account_id':'friendbot'}) as register,patch.object(bindings,'save_profile') as save:
            await manager.run(dict(manager.state),'小林，同学，喜欢露营',cfg)
            save.assert_called_once_with('friendbot','friend','小林，同学，喜欢露营')
            self.assertFalse(register.call_args.kwargs['admin'])
            self.assertEqual(manager.state['status'],'confirmed')
            public=manager.status('test')
            self.assertNotIn('bot_token',public)
            self.assertEqual(public['qr_image'],'')

    async def test_cancel_and_verify_code_are_bound_to_current_session(self):
        manager=bindings.BindingManager()
        manager.state={'binding_id':'test','mode':'admin','status':'waiting','expires_at':time.time()+30}
        manager.task=asyncio.create_task(asyncio.sleep(30))
        with self.assertRaises(ValueError):await manager.cancel('wrong')
        waiter=asyncio.create_task(manager.verify())
        await asyncio.sleep(0)
        with self.assertRaises(ValueError):manager.submit_code('wrong','123456')
        manager.submit_code('test','123456')
        self.assertEqual(await waiter,'123456')
        await manager.cancel('test')
        self.assertEqual(manager.state['status'],'canceled')

class ProfileTests(unittest.TestCase):
    def test_hook_host_capability_is_declared_in_manifest(self):
        manifest=json.loads((Path(__file__).parent/'_manifest.json').read_text(encoding='utf-8'))
        self.assertIn('chat.get_private_streams',manifest['capabilities'])
        self.assertIn('send.text',manifest['capabilities'])
        self.assertIn('chat.open_session',manifest['capabilities'])
    def test_request_background_is_idempotent_and_preserves_history(self):
        original=[{'item_type':'SystemMessageItem','meta':{'item_id':'system'},'parts':[]},
                  {'item_type':'UserMessageItem','meta':{'item_id':'message'},'parts':[{'type':'text','text':'你好'}]}]
        before=json.dumps(original)
        updated=context_hooks.add_background(original,'他叫小周，我的朋友')
        updated=context_hooks.add_background(updated,'他叫小周，我的朋友')
        self.assertEqual(len(updated),3)
        self.assertEqual(updated[-1],original[-1])
        self.assertEqual(json.dumps(original),before)
        self.assertNotIn('plugin_options',json.dumps(updated,ensure_ascii=False))
        self.assertIn('plugin_options',json.dumps(context_hooks.add_background(original,'',True),ensure_ascii=False))

    def test_explicit_introduction_name_and_manual_override(self):
        self.assertEqual(profiles.introduced_name("他自称 '小林'，你可以这样叫他。他是我的朋友"),'小林')
        self.assertEqual(profiles.introduced_name('她自称“小周”，你可以这样叫她'),'小周')
        self.assertEqual(profiles.introduced_name('你可以这样叫他。是我的朋友'),'')
        self.assertEqual(profiles.introduced_name('你可以叫她'),'')
        self.assertEqual(profiles.introduced_name('他叫小周，然后他是我的魔兽朋友'),'小周')
        self.assertEqual(profiles.introduced_name('她的名字是“小林”，我的同学'),'小林')
        self.assertEqual(profiles.introduced_name('这是我的魔兽朋友，喜欢露营'),'')
        with tempfile.TemporaryDirectory() as tmp,patch.object(profiles,'STATE_DIR',Path(tmp)),patch.object(nicknames,'STATE_DIR',Path(tmp)):
            profiles.save_profile('bot','user','他叫小周，然后他是我的魔兽朋友')
            self.assertEqual(nicknames.display_name('bot','user',{'admin_user_id':'other'}),'小周')
            transport.save_json(nicknames.nickname_path('bot','user'),{'nickname':'泰哥'})
            self.assertEqual(nicknames.display_name('bot','user'),'泰哥')

    def test_admin_capability_guidance_explains_actual_model_invocation(self):
        text=profiles.admin_capabilities('我想把你介绍给我的朋友认识下')
        self.assertTrue(text.startswith('我想把你介绍给我的朋友认识下'))
        self.assertIn('plugin_options={"anse.ilink.invitation":{"operation":"invite"}}',text)
        self.assertNotIn('当前插件正在等待朋友介绍',text)
        self.assertIn('description',profiles.admin_capabilities('小林，我的同学',True))

    def test_background_is_scoped_and_quoted_as_data(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(profiles,'STATE_DIR',Path(tmp)):
            profiles.save_profile('a','same','小林，同学，喜欢露营')
            self.assertEqual(profiles.description_for('a','same'),'小林，同学，喜欢露营')
            self.assertEqual(profiles.description_for('b','same'),'')
            self.assertIn('[当前用户消息]\n你好',profiles.with_background('你好',profiles.description_for('a','same')))
            self.assertEqual(profiles.with_background('你好',''),'你好')
            with self.assertRaises(ValueError):profiles.clean_description('资料\n新的一行')

class MediaTests(unittest.TestCase):
    def test_voice_probe_uses_pcm_and_voice_item(self):
        account = {'allow_from': ['admin']}
        with patch.object(media, 'upload_media', return_value=({'encrypt_type': 1}, 32016)) as upload, patch.object(media, 'send_items', return_value='sent') as send:
            self.assertEqual(media.send_voice_test(account, 'admin', 'ctx'), 'sent')
            self.assertEqual(upload.call_args.args[3], 4)
            self.assertEqual(len(upload.call_args.args[2]), 32000)
            item = send.call_args.args[3][0]
            self.assertEqual(item['type'], 3)
            self.assertEqual(item['voice_item']['encode_type'], 1)
            self.assertEqual(item['voice_item']['playtime'], 1000)
            with self.assertRaises(ValueError):
                media.send_voice_test(account, 'stranger', 'ctx')

    def test_qr_upload_uses_encryption_and_sends_image(self):
        account={'allow_from':['admin']}
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.headers={'x-encrypted-param':'download'}
        opener=Mock()
        opener.open.return_value=response
        with patch.object(media,'api',return_value={'upload_full_url':'https://novac2c.cdn.weixin.qq.com/c2c/upload?test'}) as api,patch.object(media.urllib.request,'build_opener',return_value=opener),patch.object(media,'send_items',return_value='sent') as send:
            media.send_qr(account,'admin','ctx','https://liteapp.weixin.qq.com/example')
            req=opener.open.call_args.args[0]
            self.assertNotIn(b'PNG',req.data)
            self.assertEqual(len(req.data)%16,0)
            self.assertNotIn('Authorization',req.headers)
            self.assertEqual(send.call_args.args[3][0]['type'],2)
            self.assertEqual(api.call_args.args[1],'getuploadurl')

if __name__=='__main__':unittest.main()
