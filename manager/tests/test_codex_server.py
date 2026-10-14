"""Protocol fixtures use a tiny fake stdio server, never a model process."""
import tempfile
import threading
import unittest
from pathlib import Path
from rooms.codex_server import AppServer, ServerError, participate


class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.executable=self.root/'fixture'
        import sys
        self.executable.write_text('#!'+sys.executable+'\n'+'''import json,sys
assert sys.argv[1:]==['app-server','--listen','stdio://']
for line in sys.stdin:
 d=json.loads(line);method=d['method']
 if 'id' not in d:continue
 if method=='eof':break
 if method=='timeout':continue
 if method=='approval':
  print(json.dumps({'id':100,'method':'item/commandExecution/requestApproval','params':{}}),flush=True);continue
 if method.startswith('elicitation'):
  params={'serverName':'uzel_session' if method!='elicitation-other' else 'other','mode':'form',
   '_meta':{'codex_approval_kind':'mcp_tool_call','tool_params':{}},
   'message':'Allow the uzel_session MCP server to run tool "room_status"?',
   'requestedSchema':{'type':'object','properties':{}}}
  print(json.dumps({'id':100,'method':'mcpServer/elicitation/request','params':params}),flush=True)
  reply=json.loads(next(sys.stdin))
  if reply.get('result')!={'action':'accept','content':{}}:break
 if method=='initialize':result={}
 elif method=='account/read':result={'account':{'type':'chatgpt'}}
 elif method=='thread/start':
  assert d['params']['sandbox']=='read-only'
  assert d['params']['config']['mcp_servers']['uzel_session']['required'] is True
  result={'thread':{'id':'synthetic-thread'}}
 elif method=='turn/start':
  result={'turn':{'id':'synthetic-turn'}}
  print(json.dumps({'method':'turn/completed','params':{'threadId':'synthetic-thread','turn':{'id':'synthetic-turn','status':'completed'}}}),flush=True)
 else:result={'echo':method}
 print(json.dumps({'id':d['id'],'result':result}),flush=True)
''')
        self.executable.chmod(0o700)
    def tearDown(self):self.tmp.cleanup()
    def test_rpc_correlates_interleaved_events_and_reaps_owned_process(self):
        server=AppServer(str(self.executable),self.root)
        try:
            server.initialize()
            self.assertEqual(server.request('probe',{}),{'echo':'probe'})
            server.request('turn/start',{})
            self.assertEqual(server.events[0]['method'],'turn/completed')
            with self.assertRaises(ServerError):server.request('approval',{})
        finally:server.close()
        self.assertIsNotNone(server.process.poll())
    def test_eof_and_timeout_are_explicit(self):
        for method in ('eof','timeout'):
            with self.subTest(method=method):
                server=AppServer(str(self.executable),self.root)
                try:
                    server.initialize()
                    with self.assertRaises(ServerError):server.request(method,{},timeout=.1)
                finally:server.close()
    def test_only_authorized_room_tool_confirmation_is_accepted(self):
        for method,room,allowed in [('elicitation','fixture-room',True),
                                    ('elicitation-other','fixture-room',False),('elicitation',None,False)]:
            with self.subTest(method=method,room=room):
                server=AppServer(str(self.executable),self.root,room=room)
                try:
                    server.initialize()
                    if allowed:self.assertEqual(server.request(method,{}),{'echo':method})
                    else:
                        with self.assertRaises(ServerError):server.request(method,{})
                finally:server.close()
    def test_native_launch_uses_protocol_and_stops_with_room(self):
        cancel=threading.Event();calls=[];starts=[]
        job=dict(binary=str(self.executable),workspace=str(self.root),model='fixture-model',room='fixture-room',
                 prompt='Synthetic room participation',server={'command':'fixture'},member='member')
        reads=iter([{'status':'running','members':[{'id':'member','turn_id':'pending'}]},{'status':'stopped'}])
        participate(job,cancel,lambda process,add:calls.append((process,add)),
                    lambda *args:starts.append(args),lambda:next(reads))
        self.assertEqual(starts,[('synthetic-thread','fixture-model')])
        self.assertEqual([add for _,add in calls],[True,False])
        self.assertIsNotNone(calls[0][0].poll())
