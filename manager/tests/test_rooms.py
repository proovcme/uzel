"""Synthetic dispatcher fixtures; these tests do not claim real model acceptance."""
import json
import http.client
import os
import subprocess
import sys
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
from rooms.paging import PAGE_BYTES, encoded_size
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
    def test_mcp_wait_releases_mutex_and_returns_own_turn(self):
        key=self.room();member=self.rooms.add_member(key,dict(name='External',kind='mcp'))
        token=member['invite_token'];self.rooms.store.update(key,status='running')
        ready=threading.Event()
        def grant_turn():
            with self.rooms.mu:
                self.rooms.store.member_state(key,member['member']['id'],'waiting_external','real-turn')
            ready.set()
        timer=threading.Timer(.1,grant_turn);timer.start()
        try:
            value=self.rooms.agent(token,'room_wait',dict(timeout=2))
            self.assertTrue(ready.is_set())
            self.assertEqual(value['participant']['turn_id'],'real-turn')
            self.assertFalse(value['timed_out'])
        finally:timer.join()
    def test_mcp_wait_timeout_stop_scope_and_revocation(self):
        key=self.room();member=self.rooms.add_member(key,dict(name='External',kind='mcp'));token=member['invite_token']
        self.rooms.store.update(key,status='running')
        started=time.monotonic();value=self.rooms.agent(token,'room_wait',dict(timeout=1))
        self.assertTrue(value['timed_out']);self.assertGreaterEqual(time.monotonic()-started,.9)
        self.rooms.store.update(key,status='stopped')
        self.assertFalse(self.rooms.agent(token,'room_wait',{})['timed_out'])
        for timeout in (0,26,True,1.5):
            with self.subTest(timeout=timeout),self.assertRaises(ValueError):self.rooms.agent(token,'room_wait',dict(timeout=timeout))
        with self.assertRaises(PermissionError):self.rooms.agent(token,'room_wait',dict(room='other'))
        self.rooms.store.update(key,status='running')
        timer=threading.Timer(.1,lambda:self.rooms.remove_member(key,member['member']['id']));timer.start()
        try:
            with self.assertRaises(PermissionError):self.rooms.agent(token,'room_wait',dict(timeout=2))
        finally:timer.join()
    def test_mcp_pages_are_small_lossless_and_chronological(self):
        key=self.room();other=self.room(title='Not in this credential')
        token=self.rooms.add_member(key,dict(name='External',kind='mcp'))['invite_token']
        self.rooms.post_user(other,dict(content='Not visible in this room'))
        for i in range(106):self.rooms.post_user(key,dict(content=f'{i}: '+('Привет 🌲 "\\\n\t' * 7)))
        self.rooms.post_user(key,dict(content='Long: '+('🌲я"\\\n'*2200)))
        expected=self.rooms.store.agent_messages(key,0,1000)[0]
        cursor={};pieces={};order=[];page_count=0
        while True:
            value=self.rooms.agent(token,'room_read',cursor)
            page_count+=1
            self.assertLessEqual(encoded_size(value),PAGE_BYTES)
            with patch.object(mcp,'call',return_value=value):
                wire=mcp.dispatch(dict(jsonrpc='2.0',id=99,method='tools/call',
                    params=dict(name='room_read',arguments=cursor)),'unused','unused')
            self.assertLessEqual(len(json.dumps(wire,ensure_ascii=False).encode()),4096)
            self.assertEqual(value['total_messages'],len(expected))
            self.assertNotIn('members',value['room'])
            self.assertNotIn('config',value['participant'])
            for message in value['messages']:
                mid=message['id']
                if mid not in pieces:pieces[mid]='';order.append(mid)
                self.assertEqual(message['offset'],len(pieces[mid]))
                pieces[mid]+=message['content']
            if not value['has_more']:break
            self.assertNotEqual(cursor,value['next_cursor'])
            cursor=value['next_cursor']
            self.assertLess(page_count,200)
        self.assertEqual(order,[0]+[m['id'] for m in expected])
        self.assertEqual(pieces.pop(0),self.rooms.store.get(key)['topic'])
        self.assertEqual(pieces,{m['id']:m['content'] for m in expected})
        empty=self.rooms.agent(token,'room_read',value['next_cursor'])
        self.assertEqual(empty['messages'],[])
        self.assertFalse(empty['has_more'])
    def test_mcp_streaming_cursor_waits_for_final_content(self):
        key=self.room();token=self.rooms.add_member(key,dict(name='External',kind='mcp'))['invite_token']
        initial=self.rooms.agent(token,'room_read',{})
        cursor=initial['next_cursor']
        pending=self.rooms.store.append(key,'Alpha','Alpha','Partial','streaming')
        later=self.rooms.store.append(key,'Beta','Beta','After pending')
        waiting=self.rooms.agent(token,'room_read',cursor)
        self.assertEqual(waiting['messages'],[])
        self.assertEqual(waiting['next_cursor'],cursor)
        self.assertTrue(waiting['waiting_for_message'])
        self.assertFalse(waiting['has_more'])
        self.rooms.store.revise(key,pending['id'],'Partial completed','done')
        done=self.rooms.agent(token,'room_read',cursor)
        self.assertEqual([m['content'] for m in done['messages']],['Partial completed','After pending'])
        self.assertEqual(done['next_cursor'],dict(after=later['id'],offset=0))
    def test_mcp_long_topic_and_invalid_page_cursors(self):
        key=self.rooms.store.create(dict(title='🌲'*120,topic='🌲"\\\n'*500))['id']
        token=self.rooms.add_member(key,dict(name='🌲'*80,kind='mcp'))['invite_token']
        cursor={};topic=''
        for _ in range(20):
            value=self.rooms.agent(token,'room_read',cursor)
            self.assertLessEqual(encoded_size(value),PAGE_BYTES)
            topic+=''.join(m['content'] for m in value['messages'])
            if not value['has_more']:break
            cursor=value['next_cursor']
        self.assertEqual(topic,self.rooms.store.get(key)['topic'])
        self.assertLess(encoded_size(self.rooms.agent(token,'room_status',{})),PAGE_BYTES)
        for args in ({'after':True},{'after':-2},{'offset':-1},{'offset':99999},
                     {'after':99999,'offset':1},{'limit':0},{'limit':9},{'limit':True}):
            with self.subTest(args=args),self.assertRaises(ValueError):self.rooms.agent(token,'room_read',args)
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
    def test_packaged_stdio_wait_read_post_and_private_connection(self):
        key=self.rooms.store.create(dict(title='Packaged stdio fixture'))['id']
        member=self.rooms.add_member(key,dict(name='Desktop',kind='mcp'));token=member['invite_token']
        self.rooms.post_user(key,dict(content='Synthetic initial question'))
        self.rooms.store.update(key,status='running')
        self.rooms.store.member_state(key,member['member']['id'],'waiting_external','fixture-turn')
        connection=Path(self.tmp.name)/'connection.json'
        connection.write_text(json.dumps(dict(manager_url=f'http://127.0.0.1:{self.port}',room_token=token)))
        connection.chmod(0o600)
        requests=[dict(jsonrpc='2.0',id=1,method='initialize'),
            dict(jsonrpc='2.0',id=2,method='tools/list')]
        for number,(name,args) in enumerate((('room_wait',{}),('room_read',{}),
            ('room_post',dict(content='Native bridge fixture reply',turn_id='fixture-turn'))),3):
            requests.append(dict(jsonrpc='2.0',id=number,method='tools/call',params=dict(name=name,arguments=args)))
        env={k:v for k,v in os.environ.items() if not k.startswith('UZEL_ROOM')}
        env['UZEL_ROOMS_PLUGIN_CONFIG']=str(connection)
        start=Path(__file__).resolve().parents[2]/'plugins/uzel-rooms/server/start.py'
        result=subprocess.run([sys.executable,str(start)],input=''.join(json.dumps(r)+'\n' for r in requests),
            text=True,capture_output=True,env=env,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn(token,result.stdout+result.stderr)
        replies=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies],[1,2,3,4,5])
        self.assertTrue(any(t['name']=='room_wait' for t in replies[1]['result']['tools']))
        self.assertFalse(json.loads(replies[2]['result']['content'][0]['text'])['timed_out'])
        page=json.loads(replies[3]['result']['content'][0]['text'])
        self.assertFalse(page['has_more']);self.assertEqual(page['total_messages'],1)
        self.assertFalse(replies[4]['result'].get('isError',False))
        self.assertEqual(self.rooms.store.messages(key)[-1]['content'],'Native bridge fixture reply')


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
        self.assertEqual(names,{'room_list','room_join','room_read','room_post','room_leave','room_status','room_wait'})
        reply=mcp.dispatch(dict(jsonrpc='2.0',id=1,method='initialize'),'unused','unused')
        self.assertEqual(reply['result']['capabilities'],{'tools':{}})
    def test_mcp_paging_arguments_and_compact_wire_result(self):
        args=dict(after=17,offset=250,limit=2)
        value=dict(messages=[dict(content='Привет 🌲')],next_cursor=dict(after=18,offset=0),has_more=False)
        with patch.object(mcp,'call',return_value=value) as call:
            reply=mcp.dispatch(dict(jsonrpc='2.0',id=3,method='tools/call',
                params=dict(name='room_read',arguments=args)),'unused','unused')
        self.assertEqual(call.call_args.args[-1],args)
        self.assertEqual(json.loads(reply['result']['content'][0]['text']),value)
        read=next(t for t in mcp.tools() if t['name']=='room_read')
        self.assertIn('next_cursor',read['description'])
        self.assertEqual(read['inputSchema']['properties']['after']['minimum'],-1)
    def test_packaged_bridge_matches_manager(self):
        root=Path(__file__).resolve().parents[2]
        self.assertEqual((root/'plugins/uzel-rooms/server/rooms_mcp.py').read_bytes(),
                         (root/'manager/rooms/mcp.py').read_bytes())
        manifest=json.loads((root/'plugins/uzel-rooms/plugin.json').read_text())
        compat=json.loads((root/'plugins/uzel-rooms/.codex-plugin/plugin.json').read_text())
        self.assertEqual(manifest['version'],compat['version'])
        self.assertEqual(manifest['version'],mcp.dispatch(dict(jsonrpc='2.0',id=1,method='initialize'),'','')['result']['serverInfo']['version'])
        catalog=json.loads((root/'.agents/plugins/marketplace.json').read_text())
        self.assertTrue((root/catalog['plugins'][0]['source']['path']/'plugin.json').is_file())


if __name__=='__main__':unittest.main()
