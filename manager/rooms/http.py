"""Owner HTTP routes and a separate credential-scoped MCP bridge route."""
import json
import os
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .adapters import codex_available


class RoomHTTPMixin:
    def rooms_get(self, rooms):
        path = urlsplit(self.path).path
        if not path.startswith('/api/rooms'):
            return False
        if self.headers.get('Authorization'):
            raise PermissionError('Participant credentials are accepted only by the agent route')
        parts = path.split('/')[3:]
        query = parse_qs(urlsplit(self.path).query)
        if path == '/api/rooms':
            self._json(200,{'rooms':rooms.store.list()})
        elif path == '/api/rooms/adapters':
            self._json(200,dict(codex=codex_available(),gemini=False,
                managed_binding=rooms.manager.room_binding()))
        elif len(parts) == 1:
            self._json(200,rooms.snapshot(parts[0]))
        elif len(parts) == 2 and parts[1] == 'messages':
            self._json(200,dict(messages=rooms.store.messages(parts[0],
                after=int(query.get('after',['0'])[0]), before=int(query['before'][0]) if 'before' in query else None)))
        elif len(parts) == 2 and parts[1] == 'export':
            data = rooms.store.export(parts[0]).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','text/markdown; charset=utf-8')
            self.send_header('Content-Disposition','attachment; filename="room.md"')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.end_headers(); self.wfile.write(data)
        elif len(parts) == 2 and parts[1] == 'events':
            rooms.store.get(parts[0])
            cursor = int(self.headers.get('Last-Event-ID') or query.get('after',['0'])[0])
            if cursor < 0: raise ValueError('Invalid event cursor')
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream; charset=utf-8')
            self.send_header('Cache-Control','no-cache')
            self.send_header('Connection','close')
            self.end_headers(); self.close_connection = True
            until = time.monotonic()+30
            try:
                while time.monotonic() < until:
                    batch = rooms.store.events(parts[0],cursor)
                    if batch['reset']:
                        value = rooms.snapshot(parts[0]); cursor = value['event_cursor']
                        self.wfile.write(('id: '+str(cursor)+'\nevent: reset\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode())
                    else:
                        for event in batch['events']:
                            cursor = event['id']
                            self.wfile.write(('id: '+str(cursor)+'\nevent: room\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                    self.wfile.write(b': heartbeat\n\n'); self.wfile.flush()
                    time.sleep(.25)
            except (BrokenPipeError,ConnectionResetError):
                pass
        else:
            self._json(404,{'error':'Room route not found'})
        return True

    def rooms_post(self, rooms):
        path = urlsplit(self.path).path
        if not path.startswith('/api/rooms'):
            return False
        spec = self._body()
        if path == '/api/rooms/agent':
            authorization = self.headers.get('Authorization','')
            if not authorization.startswith('Bearer '):
                raise PermissionError('Participant credential required')
            self._json(200,rooms.agent(authorization[7:],spec.get('operation'),spec.get('arguments',{})))
            return True
        if self.headers.get('Authorization'):
            raise PermissionError('Participant credentials cannot control rooms')
        parts = path.split('/')[3:]
        if path == '/api/rooms':
            self._json(201,rooms.store.create(spec))
        elif len(parts) == 2 and parts[1] == 'members':
            result = rooms.add_member(parts[0],spec)
            if result['invite_token']:
                import config
                result['mcp_command'] = shlex.join(['env','UZEL_ROOM_TOKEN='+result['invite_token'],
                    f'UZEL_ROOMS_URL=http://127.0.0.1:{config.MANAGER_PORT}',sys.executable,
                    str(Path(__file__).resolve().parents[1]/'rooms_mcp.py')])
            self._json(201,result)
        elif len(parts) == 2 and parts[1] == 'remove-member':
            self._json(200,rooms.remove_member(parts[0],spec.get('id')))
        elif len(parts) == 2 and parts[1] == 'messages':
            self._json(201,rooms.post_user(parts[0],spec))
        elif len(parts) == 2 and parts[1] == 'control':
            self._json(200,rooms.control(parts[0],spec))
        elif len(parts) == 2 and parts[1] == 'settings':
            self._json(200,rooms.configure(parts[0],spec))
        else:
            self._json(404,{'error':'Room route not found'})
        return True
