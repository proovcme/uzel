"""Synthetic dispatcher fixtures; these tests do not claim real model acceptance."""
import json
import http.client
from http.server import ThreadingHTTPServer
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
from store import Store
from rooms import Rooms
from rooms.adapters import prepare, decode_sse, Invocation
from rooms.store import limits, RoomStore
from rooms import mcp
import server


class ScriptedInvocation:
    calls = []
    script = None
    def __init__(self,manager,member,request_id,cancelled):
        self.member,self.cancelled=member,cancelled
    def cancel(self): self.cancelled.set()
    def stream(self,messages):
        self.calls.append((self.member['name'],messages))
        if type(self).script:
            yield from type(self).script(self,messages)
        else:
            yield self.member['name']+': test reply'


class RoomsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.patch=patch.object(config,'DATA_ROOT',Path(self.tmp.name));self.patch.start()
        self.workspace=Store()
        self.manager=Mock();self.manager.room_binding.return_value={'mode':'model','model':'synthetic'}
        ScriptedInvocation.calls=[];ScriptedInvocation.script=None
        self.rooms=Rooms(self.manager,self.workspace,ScriptedInvocation)
    def tearDown(self):
        self.rooms.shutdown();self.patch.stop();self.tmp.cleanup()
    def room(self,title='Test room',**settings):
        room=self.rooms.store.create(dict(title=title,limits=dict(duration=30,max_turns=4,**settings)))
        for name in ('Alpha','Beta'):
            self.rooms.add_member(room['id'],dict(name=name,kind='managed'))
        self.rooms.post_user(room['id'],dict(content='Synthetic discussion topic'))
        return room['id']
    def wait(self,key,status='stopped',timeout=4):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=self.rooms.snapshot(key)
            if value['status']==status and key not in self.rooms.runs:return value
            time.sleep(.02)
        self.fail('Discussion did not settle: '+str(self.rooms.snapshot(key)))
    def test_auto_exchange_and_turn_limit(self):
        key=self.room();self.rooms.control(key,dict(action='start'));value=self.wait(key)
        self.assertEqual(value['reason'],'turn_limit')
        self.assertEqual([x['author'] for x in value['messages']],['Вы','Alpha','Beta','Alpha','Beta'])
        self.assertIn('Alpha',ScriptedInvocation.calls[1][1][1]['content'])
    def test_all_pass_waits_and_user_message_resumes(self):
        ScriptedInvocation.script=lambda self,messages:iter(['PASS'])
        key=self.room();self.rooms.control(key,dict(action='start'));value=self.wait(key,'paused')
        self.assertEqual(value['reason'],'all_pass');self.assertEqual(len(ScriptedInvocation.calls),2)
        self.rooms.post_user(key,dict(content='A fresh question'));self.wait(key,'paused')
        self.assertEqual(len(ScriptedInvocation.calls),4)
    def test_error_does_not_lose_history_or_stop_other_member(self):
        def script(actor,messages):
            if actor.member['name']=='Alpha':raise OSError('synthetic unavailable')
            yield 'A useful reply'
        ScriptedInvocation.script=script
        key=self.room();self.rooms.control(key,dict(action='start'));value=self.wait(key)
        self.assertEqual(value['messages'][1]['state'],'error')
        self.assertEqual(value['messages'][2]['content'],'A useful reply')
    def test_pause_cancels_and_preserves_partial_then_resume(self):
        def script(actor,messages):
            yield 'Partial reply'
            actor.cancelled.wait(3)
        ScriptedInvocation.script=script
        key=self.room();self.rooms.control(key,dict(action='start'))
        until=time.monotonic()+1
        while not ScriptedInvocation.calls and time.monotonic()<until:time.sleep(.01)
        self.rooms.control(key,dict(action='pause'));value=self.wait(key,'paused')
        self.assertEqual(value['messages'][-1]['content'],'Partial reply')
        self.assertEqual(value['messages'][-1]['state'],'interrupted')
        ScriptedInvocation.script=None
        self.rooms.control(key,dict(action='resume'));value=self.wait(key)
        self.assertGreater(len(value['messages']),3)
    def test_duration_cancels_active_turn(self):
        def script(actor,messages):
            yield 'Partial';actor.cancelled.wait(4)
        ScriptedInvocation.script=script
        key=self.room();self.rooms.configure(key,dict(limits=dict(duration=1,max_turns=4)))
        self.rooms.control(key,dict(action='start'));value=self.wait(key)
        self.assertEqual(value['reason'],'time_limit')
        self.assertEqual(value['messages'][-1]['state'],'interrupted')
    def test_restart_preserves_history_without_auto_restart(self):
        key=self.room();self.rooms.store.update(key,status='running')
        self.rooms.store.append(key,'test','Alpha','Incomplete','streaming')
        new=RoomStore(self.workspace);value=new.get(key)
        self.assertEqual((value['status'],value['reason']),('paused','restart'))
        self.assertEqual(new.messages(key)[-1]['state'],'interrupted')
    def test_additive_schema_preserves_regular_chat(self):
        self.workspace.put(dict(id='normal',kind='chat',model='test',value=dict(title='Existing',messages=[])))
        self.room();RoomStore(self.workspace)
        self.assertEqual(self.workspace.get('normal')['value']['title'],'Existing')
    def test_context_is_bounded_with_authors_and_no_other_room(self):
        a=self.room(title='Room A');b=self.room(title='Room B')
        self.rooms.post_user(a,dict(content='Exclusive room A text'))
        self.rooms.post_user(b,dict(content='Exclusive room B text'))
        self.rooms.control(a,dict(action='start'));self.wait(a)
        self.assertNotIn('Exclusive room B',str(ScriptedInvocation.calls))
        snapshot=self.rooms.store.get(a);snapshot['limits'].update(context_messages=1,context_chars=1000)
        history=[dict(author='Вы',content='x'*16000,state='done')]
        context=Rooms.context(snapshot,snapshot['members'][0],history)
        self.assertLess(len(context[1]['content']),1000);self.assertIn('опущено',context[1]['content'])
    def test_rooms_share_serial_turn_slot(self):
        active=[0,0];mu=threading.Lock()
        def script(actor,messages):
            with mu: active[0]+=1;active[1]=max(active)
            try:time.sleep(.03);yield 'reply'
            finally:
                with mu:active[0]-=1
        ScriptedInvocation.script=script
        a=self.room();b=self.room(title='Other');self.rooms.control(a,dict(action='start'));self.rooms.control(b,dict(action='start'))
        self.wait(a);self.wait(b);self.assertEqual(active[1],1)
    def test_mcp_identity_scope_late_turn_and_revocation(self):
        key=self.room();other=self.room(title='Private other room')
        result=self.rooms.add_member(key,dict(name='External',kind='mcp'));token=result['invite_token'];member=result['member']
        with self.assertRaises(PermissionError):self.rooms.agent(token,'room_read',dict(room=other))
        with self.assertRaises(ValueError):self.rooms.agent(token,'room_post',dict(content='late',turn_id='stale'))
        self.rooms.store.update(key,status='running');self.rooms.store.member_state(key,member['id'],'waiting_external','own-turn')
        with self.assertRaises(PermissionError):self.rooms.agent(token,'room_post',dict(content='fake',turn_id='own-turn',author='Alpha'))
        self.rooms.agent(token,'room_post',dict(content='Real scoped post',turn_id='own-turn'))
        self.assertEqual(self.rooms.store.messages(key)[-1]['author'],'External')
        with self.assertRaises(ValueError):self.rooms.agent(token,'room_post',dict(content='again',turn_id='own-turn'))
        self.rooms.remove_member(key,member['id'])
        with self.assertRaises(PermissionError):self.rooms.agent(token,'room_status',{})
    def test_mcp_actual_queue_wait_and_reply(self):
        key=self.room();first=self.rooms.store.get(key)['members'][1]
        self.rooms.remove_member(key,first['id'])
        result=self.rooms.add_member(key,dict(name='External',kind='mcp'));token=result['invite_token']
        self.rooms.control(key,dict(action='start'))
        until=time.monotonic()+2
        while time.monotonic()<until:
            state=self.rooms.agent(token,'room_status',{})['participant']
            if state['turn_id']:
                self.rooms.agent(token,'room_post',dict(content='PASS',turn_id=state['turn_id']));break
            time.sleep(.01)
        self.rooms.control(key,dict(action='stop'));self.wait(key)
        self.assertIn('External',[m['author'] for m in self.rooms.store.messages(key)])
    def test_export_and_pagination(self):
        key=self.room()
        for i in range(105):self.rooms.post_user(key,dict(content='Message '+str(i)))
        tail=self.rooms.store.messages(key)
        self.assertEqual(len(tail),100)
        head=self.rooms.store.messages(key,before=tail[0]['id'])
        self.assertEqual(len(head),6)
        self.assertIn('Message 104',self.rooms.store.export(key))
    def test_cannot_start_only_passive_mcp(self):
        key=self.rooms.store.create(dict(title='Passive'))['id']
        for name in ('One','Two'):self.rooms.add_member(key,dict(name=name,kind='mcp'))
        self.rooms.post_user(key,dict(content='topic'))
        with self.assertRaises(ValueError):self.rooms.control(key,dict(action='start'))
    def test_removed_name_can_be_reused_without_old_token(self):
        key=self.room();p=self.rooms.add_member(key,dict(name='External',kind='mcp'))
        self.rooms.remove_member(key,p['member']['id']);q=self.rooms.add_member(key,dict(name='External',kind='mcp'))
        self.assertNotEqual(p['invite_token'],q['invite_token'])
    def test_duplicate_name_and_rejoin_cannot_exceed_member_limit(self):
        key=self.room()
        with self.assertRaises(ValueError):self.rooms.add_member(key,dict(name='Alpha',kind='managed'))
        external=self.rooms.add_member(key,dict(name='External',kind='mcp'))
        self.rooms.agent(external['invite_token'],'room_leave',{})
        for i in range(10):self.rooms.add_member(key,dict(name='Other '+str(i),kind='mcp'))
        with self.assertRaises(ValueError):self.rooms.agent(external['invite_token'],'room_join',{})
    def test_removing_queued_member_prevents_invocation(self):
        key=self.room()
        beta=self.rooms.store.get(key)['members'][0]
        self.rooms.turn_lock.acquire()
        try:
            self.rooms.control(key,dict(action='start'))
            until=time.monotonic()+1
            while self.rooms.store.get(key)['members'][0]['state']!='queued' and time.monotonic()<until:time.sleep(.01)
            self.rooms.remove_member(key,beta['id'])
        finally:self.rooms.turn_lock.release()
        self.wait(key,'paused')
        self.assertEqual(ScriptedInvocation.calls,[])
    def test_pause_transport_exception_is_interrupted(self):
        def script(actor,messages):
            yield 'Partial'
            actor.cancelled.wait(3)
            raise OSError('socket closed by cancellation')
        ScriptedInvocation.script=script
        key=self.room();self.rooms.control(key,dict(action='start'))
        until=time.monotonic()+1
        while not ScriptedInvocation.calls and time.monotonic()<until:time.sleep(.01)
        self.rooms.control(key,dict(action='pause'));value=self.wait(key,'paused')
        self.assertEqual(value['messages'][-1]['state'],'interrupted')
    def test_managed_uses_existing_stream_and_pinned_binding(self):
        p=dict(kind='managed',config=dict(binding={'mode':'model','model':'same'}))
        self.manager.stream_chat.return_value=iter([b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',b'data: [DONE]\n\n'])
        # A real generator supports close, like Manager.stream_chat.
        self.manager.stream_chat.return_value=(x for x in self.manager.stream_chat.return_value)
        result=''.join(Invocation(self.manager,p,'own-request',threading.Event()).stream([dict(role='user',content='Hi')]))
        self.assertEqual(result,'Hello')
        self.assertEqual(self.manager.stream_chat.call_args.args[0]['room_binding'],p['config']['binding'])


class RoomHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.data=patch.object(config,'DATA_ROOT',Path(self.tmp.name));self.data.start()
        self.rooms=Rooms(Mock(),Store(),ScriptedInvocation)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.http.daemon_threads=True
        self.port=self.http.server_port
        self.patches=[patch.object(config,'MANAGER_PORT',self.port),patch.object(server,'MANAGER',Mock(rooms=self.rooms),create=True),patch.object(server.Handler,'log_message')]
        for p in self.patches:p.start()
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join()
        self.rooms.shutdown()
        for p in reversed(self.patches):p.stop()
        self.data.stop();self.tmp.cleanup()
    def request(self,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        connection.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),dict({'Content-Type':'application/json'},**(headers or {})))
        response=connection.getresponse();result=(response.status,json.loads(response.read()));connection.close();return result
    def test_owner_route_and_participant_scope(self):
        status,room=self.request('/api/rooms',dict(title='HTTP fixture'))
        self.assertEqual(status,201)
        token=self.rooms.add_member(room['id'],dict(name='External',kind='mcp'))['invite_token']
        auth={'Authorization':'Bearer '+token}
        self.assertEqual(self.request('/api/rooms',headers=auth)[0],403)
        self.assertEqual(self.request('/api/rooms/'+room['id']+'/control',dict(action='start'),auth)[0],403)
        status,value=self.request('/api/rooms/agent',dict(operation='room_list',arguments={}),auth)
        self.assertEqual(status,200);self.assertEqual([r['id'] for r in value['rooms']],[room['id']])
    def test_cross_origin_and_host_cannot_create_room(self):
        self.assertEqual(self.request('/api/rooms',dict(title='Blocked'),{'Origin':'https://example.invalid'})[0],403)
        self.assertEqual(self.request('/api/rooms',dict(title='Blocked'),{'Host':'example.invalid'})[0],403)
        self.assertEqual(self.rooms.store.list(),[])


class AdapterContractTests(unittest.TestCase):
    def test_endpoint_and_remote_consent(self):
        self.assertEqual(prepare(dict(kind='openai',endpoint='http://127.0.0.1:8000/v1',model='test'))['model'],'test')
        for endpoint in ('http://remote.example/v1','https://remote.example/v1','http://user:secret@localhost/v1','http://localhost/v1?x=1','file:///tmp/test'):
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):prepare(dict(kind='openai',endpoint=endpoint,model='test'))
        self.assertTrue(prepare(dict(kind='openai',endpoint='https://remote.example/v1',model='test',allow_remote=True))['allow_remote'])
    def test_codex_disabled_and_never_launched(self):
        with self.assertRaises(ValueError):prepare(dict(kind='codex',allow_remote=True))
    def test_sse_fragments_utf8_and_truncation(self):
        data=('data: '+json.dumps(dict(choices=[dict(delta=dict(content='Привет'))]),ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode()
        self.assertEqual(''.join(decode_sse([bytes([b]) for b in data])),'Привет')
        with self.assertRaises(RuntimeError):list(decode_sse([b'data: {"choices":[]}\n\n']))
    def test_invalid_limits_and_mcp_schema(self):
        for value in ({'duration':False},{'max_turns':0},{'context_chars':1},{'unknown':3}):
            with self.subTest(value=value),self.assertRaises(ValueError):limits(value)
        names={t['name'] for t in mcp.tools()}
        self.assertEqual(names,{'room_list','room_join','room_read','room_post','room_leave','room_status'})
        reply=mcp.dispatch(dict(jsonrpc='2.0',id=1,method='initialize'),'unused','unused')
        self.assertEqual(reply['result']['capabilities'],{'tools':{}})


if __name__=='__main__':unittest.main()
