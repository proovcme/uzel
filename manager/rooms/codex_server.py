"""Owned Codex App Server sessions over private stdio; never `codex exec`."""
import collections
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import config


def binary(app=None):
    configured = config.setting('CODEX_BINARY', '')
    candidates = ([str(Path(configured).expanduser())] if configured else [])
    if app:
        candidates.append(str(Path(app)/'Contents/Resources/codex-cli/bin/codex'))
    if not configured:
        candidates.append(shutil.which('codex'))
    return next((p for p in candidates if p and os.path.isfile(p) and os.access(p, os.X_OK)), None)


class ServerError(RuntimeError):
    pass


class AppServer:
    """One reader, bounded frames, correlated RPCs and explicit process ownership."""
    def __init__(self, executable, cwd, cancelled=None, room=None):
        self.cancelled = cancelled or threading.Event()
        self.room = room
        self.events = collections.deque()
        self.inbox = queue.Queue(maxsize=128)
        self.serial = 0
        env = {k:v for k,v in os.environ.items() if k not in
               ('CODEX_THREAD_ID','CODEX_APP_TOOLS_PIPE_PATH','CODEX_APP_TOOLS_CALLER_HOST_ID',
                'OPENAI_API_KEY','CODEX_API_KEY')}
        self.process = subprocess.Popen([executable,'app-server','--listen','stdio://'],
            cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.reader = threading.Thread(target=self._read,daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(2*1024*1024+1)
                if not line: break
                if len(line)>2*1024*1024: break
                try: value = json.loads(line)
                except ValueError: break
                if not isinstance(value,dict): break
                # Output deltas are private conversation data; the room is the UI.
                if value.get('method','').endswith('/delta'): continue
                try: self.inbox.put(value,timeout=1)
                except queue.Full: break
        finally:
            try:self.inbox.put(None,timeout=1)
            except queue.Full:pass

    def send(self, value):
        if self.process.poll() is not None: raise ServerError('Codex App Server завершился')
        try:
            self.process.stdin.write((json.dumps(value,ensure_ascii=False)+'\n').encode())
            self.process.stdin.flush()
        except (OSError,ValueError): raise ServerError('Связь с Codex App Server прервана') from None

    def receive(self, timeout):
        end=time.monotonic()+timeout
        while not self.cancelled.is_set():
            try: value=self.inbox.get(timeout=min(.2,max(.001,end-time.monotonic())))
            except queue.Empty:
                if time.monotonic()>=end:return None
                continue
            if value is None:raise ServerError('Codex App Server завершился')
            if 'method' in value and 'id' in value:
                params=value.get('params',{})
                allowed=re.fullmatch(r'Allow the uzel_session MCP server to run tool "(room_list|room_join|room_read|room_post|room_leave|room_status|room_wait|room_search)"\?',params.get('message',''))
                if (self.room and value['method']=='mcpServer/elicitation/request'
                        and params.get('serverName')=='uzel_session' and params.get('mode')=='form'
                        and params.get('requestedSchema')=={'type':'object','properties':{}}
                        and params.get('_meta',{}).get('codex_approval_kind')=='mcp_tool_call'
                        and allowed and params.get('_meta',{}).get('tool_params',{}).get('room',self.room)==self.room):
                    # The owner's launch authorizes these scoped room tools only.
                    # Never persist an "always" grant or accept a generic form.
                    self.send(dict(id=value['id'],result=dict(action='accept',content={})))
                    continue
                # A room never authorizes file edits, commands or new permissions.
                self.send(dict(id=value['id'],error=dict(code=-32601,message='UZEL room client does not authorize this operation')))
                raise ServerError('Codex запросил неподдерживаемое действие: '+str(value['method'])[:100])
            return value
        raise ServerError('Запуск отменён')

    def request(self, method, params, timeout=30):
        self.serial+=1; key=self.serial
        self.send(dict(id=key,method=method,params=params))
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            value=self.receive(max(.001,end-time.monotonic()))
            if value is None:break
            if value.get('id')==key:
                if 'error' in value:
                    # Never echo provider errors that may contain prompts or paths.
                    raise ServerError('Codex отклонил '+method+'; проверьте вход, модель и версию App Server')
                return value.get('result',{})
            if 'method' in value:
                if len(self.events)>=128:raise ServerError('Очередь событий Codex переполнена')
                self.events.append(value)
        raise ServerError('Codex не ответил на '+method+' за отведённое время')

    def initialize(self):
        self.request('initialize',dict(clientInfo=dict(name='uzel',title='UZEL',version='0.2.0')))
        self.send(dict(method='initialized',params={}))

    def require_subscription(self):
        account=self.request('account/read',dict(refreshToken=False)).get('account')
        if not account or account.get('type')!='chatgpt':
            raise ServerError('Войдите в Codex через ChatGPT. Этот запуск использует подписку, а не API-ключ')

    def close(self):
        if self.process.poll() is None:
            try:os.killpg(self.process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:os.killpg(self.process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                self.process.wait(timeout=3)
        for stream in (self.process.stdin,self.process.stdout):
            try:stream.close()
            except OSError:pass
        self.reader.join(timeout=2)


def models(executable):
    config.DATA_ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    server=AppServer(executable,config.DATA_ROOT)
    try:
        server.initialize();server.require_subscription(); result=[]; cursor=None
        for _ in range(5):
            page=server.request('model/list',dict(limit=50,**({'cursor':cursor} if cursor else {})))
            for m in page.get('data',[]):
                result.append(dict(id=m['model'],name=m.get('displayName',m['model']),default=bool(m.get('isDefault'))))
            cursor=page.get('nextCursor')
            if not cursor:break
        preferred=config.setting('CODEX_MODEL','')
        chosen=next((m['id'] for m in result if m['id']==preferred),None)
        chosen=chosen or next((m['id'] for m in result if m['id'].endswith('-luna')),None)
        chosen=chosen or next((m['id'] for m in result if m['default']),None)
        return dict(models=result,default=chosen)
    finally:server.close()


def participate(job, cancelled, register, on_started, snapshot):
    server=AppServer(job['binary'],job['workspace'],cancelled,room=job['room'])
    register(server.process,True)
    try:
        server.initialize();server.require_subscription()
        started=server.request('thread/start',dict(cwd=job['workspace'],model=job['model'],
            approvalPolicy='on-request',sandbox='read-only',serviceName='uzel-rooms',
            config={'mcp_servers':{'uzel_session':dict(job['server'],required=True)},
                    'features.shell_tool':False,'apps._default.enabled':False,'web_search':'disabled'},
            developerInstructions='Участвуй только через uzel_session. Не используй файлы, команды, другие подключения или историю.'))
        thread=started['thread']['id']
        prompt=job['prompt']; attempts=0; previous_pending=None
        while not cancelled.is_set():
            current=snapshot()
            if current['status']!='running':return
            pending=next(m for m in current['members'] if m['id']==job['member'])['turn_id']
            attempts=attempts+1 if pending and pending==previous_pending else 0
            if attempts>=3:raise ServerError('Codex завершает сеанс без ответа в свой ход; попробуйте другую модель')
            previous_pending=pending
            turn=server.request('turn/start',dict(threadId=thread,
                input=[dict(type='text',text=prompt)],effort='low'))['turn']['id']
            on_started(thread,job['model'])
            completed=False
            while not cancelled.is_set():
                if snapshot()['status']!='running':return
                event=server.events.popleft() if server.events else server.receive(1)
                if not event:continue
                if event.get('method')=='turn/completed' and event.get('params',{}).get('threadId')==thread:
                    value=event['params']['turn']
                    if value.get('status')!='completed':raise ServerError('Сеанс Codex завершился с ошибкой или был прерван')
                    completed=True;break
                if event.get('method')=='error' and not event.get('params',{}).get('willRetry',False):
                    raise ServerError('Codex сообщил об ошибке генерации; проверьте вход и лимиты подписки')
            if not completed:return
            # Clients can finish a turn early. Continue only when this member has
            # another pending room turn; do not wake a model just to poll status.
            while not cancelled.wait(.25):
                room=snapshot()
                if room['status']!='running':return
                member=next(m for m in room['members'] if m['id']==job['member'])
                if not member['enabled']:return
                if member['state']=='waiting_external':break
            prompt='Сейчас ТВОЙ ход: participant.next_action=reply. Прочитай новые сообщения через uzel_session и сразу вызови room_post с текущим turn_id. Не жди новых сообщений. После ответа жди room_wait до следующего своего хода или остановки. Не используй другие инструменты.'
    finally:
        server.close();register(server.process,False)
