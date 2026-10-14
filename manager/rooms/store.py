"""Additive room tables in the existing private workspace database."""
import hashlib
import json
import secrets
import time
import uuid


DEFAULT_LIMITS = dict(duration=300, max_turns=60, empty_turns=8,
                      context_messages=24, context_chars=24000, turn_timeout=120)


def text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'Invalid {name}')
    return value.strip()


def limits(value):
    if not isinstance(value, dict) or set(value) - DEFAULT_LIMITS.keys():
        raise ValueError('Invalid discussion limits')
    result = dict(DEFAULT_LIMITS, **value)
    bounds = dict(duration=(1, 7200), max_turns=(1, 500), empty_turns=(1, 100),
                  context_messages=(1, 80), context_chars=(1000, 80000), turn_timeout=(1, 300))
    for key, number in result.items():
        low, high = bounds[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError(f'Invalid limit: {key}')
    return result


class RoomStore:
    def __init__(self, workspace):
        self.workspace = workspace
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, topic TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, settings TEXT NOT NULL,
                    remaining REAL NOT NULL, turns INTEGER NOT NULL DEFAULT 0,
                    empty INTEGER NOT NULL DEFAULT 0, cursor INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS room_members (
                    id TEXT PRIMARY KEY, room TEXT NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, config TEXT NOT NULL, token_hash TEXT UNIQUE,
                    enabled INTEGER NOT NULL, state TEXT NOT NULL, turn_id TEXT);
                CREATE TABLE IF NOT EXISTS room_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
                    author_id TEXT NOT NULL, author TEXT NOT NULL, content TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, turn_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS room_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
                    kind TEXT NOT NULL, value TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS room_message_cursor ON room_messages(room,id);
                CREATE INDEX IF NOT EXISTS room_event_cursor ON room_events(room,id);
                CREATE INDEX IF NOT EXISTS room_member_room ON room_members(room);
                CREATE UNIQUE INDEX IF NOT EXISTS room_member_name ON room_members(room,name) WHERE enabled=1;
            ''')
            # Never silently restart conversations after a process exit.
            db.execute("UPDATE rooms SET status='paused',reason='restart' WHERE status='running'")
            db.execute("UPDATE room_messages SET state='interrupted' WHERE state='streaming'")
            db.execute("UPDATE room_members SET state='waiting',turn_id=NULL WHERE state IN ('responding','queued','waiting_external')")

    def connect(self):
        return self.workspace.connect()

    @staticmethod
    def event(db, room, kind, value):
        db.execute('INSERT INTO room_events(room,kind,value,created) VALUES (?,?,?,?)',
                   (room, kind, json.dumps(value, ensure_ascii=False), time.time()))
        # A bounded replay buffer; full conversations remain in room_messages.
        db.execute('DELETE FROM room_events WHERE room=? AND id < '
                   '(SELECT COALESCE(MAX(id),0)-1000 FROM room_events WHERE room=?)', (room, room))

    def create(self, spec):
        title = text(spec.get('title'), 'room name', 120)
        topic = text(spec.get('topic', title), 'topic', 2000)
        settings = limits(spec.get('limits', {}))
        key = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO rooms(id,title,topic,status,reason,settings,remaining,updated) '
                       'VALUES (?,?,?,?,?,?,?,?)', (key,title,topic,'idle','',json.dumps(settings),settings['duration'],time.time()))
        return self.get(key)

    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT id,title,status,reason,updated FROM rooms ORDER BY updated DESC').fetchall()
        return [dict(row) for row in rows]

    def get(self, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM rooms WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('Room not found')
            result = dict(row)
            result['limits'] = json.loads(result.pop('settings'))
            result['members'] = [self.member(row) for row in db.execute(
                'SELECT * FROM room_members WHERE room=? ORDER BY rowid', (key,))]
            result['event_cursor'] = db.execute('SELECT COALESCE(MAX(id),0) FROM room_events WHERE room=?',(key,)).fetchone()[0]
        return result

    @staticmethod
    def member(row):
        result = dict(row)
        result.pop('token_hash', None)
        result['config'] = json.loads(result['config'])
        result['enabled'] = bool(result['enabled'])
        return result

    def add_member(self, room, spec, prepared):
        self.get(room)
        name = text(spec.get('name'), 'participant name', 80)
        key = uuid.uuid4().hex
        token = secrets.token_urlsafe(32) if spec['kind'] == 'mcp' else None
        with self.connect() as db:
            if db.execute('SELECT COUNT(*) FROM room_members WHERE room=? AND enabled=1',(room,)).fetchone()[0] >= 12:
                raise ValueError('Room participant limit: 12')
            if db.execute('SELECT 1 FROM room_members WHERE room=? AND name=? AND enabled=1',(room,name)).fetchone():
                raise ValueError('Participant name already in use')
            db.execute('INSERT INTO room_members VALUES (?,?,?,?,?,?,?,?,?)',
                (key, room, name, spec['kind'], json.dumps(prepared),
                 hashlib.sha256(token.encode()).hexdigest() if token else None, 1, 'waiting', None))
            member = self.member(db.execute('SELECT * FROM room_members WHERE id=?',(key,)).fetchone())
            self.event(db, room, 'member', member)
        return dict(member=member, invite_token=token)

    def authenticate(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise PermissionError('Participant credential required')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute('SELECT * FROM room_members WHERE token_hash=?', (digest,)).fetchone()
        if row is None:
            raise PermissionError('Unknown participant credential')
        return self.member(row)

    def member_state(self, room, key, state, turn_id=None):
        with self.connect() as db:
            db.execute('UPDATE room_members SET state=?,turn_id=? WHERE room=? AND id=?', (state,turn_id,room,key))
            self.event(db, room, 'member_state', dict(id=key,state=state,turn_id=turn_id))

    def membership(self, room, key, enabled, revoke=False):
        with self.connect() as db:
            row = db.execute('SELECT * FROM room_members WHERE room=? AND id=?',(room,key)).fetchone()
            if row is None:
                raise ValueError('Participant not found')
            if enabled and not row['enabled']:
                if db.execute('SELECT COUNT(*) FROM room_members WHERE room=? AND enabled=1',(room,)).fetchone()[0] >= 12:
                    raise ValueError('Room participant limit: 12')
                if db.execute('SELECT 1 FROM room_members WHERE room=? AND name=? AND enabled=1',(room,row['name'])).fetchone():
                    raise ValueError('Participant name already in use')
            db.execute('UPDATE room_members SET enabled=?,state=?,turn_id=NULL,token_hash=? WHERE id=?',
                       (int(enabled),'waiting' if enabled else 'left',None if revoke else row['token_hash'],key))
            self.event(db,room,'membership',dict(id=key,enabled=enabled))

    def update(self, room, **fields):
        allowed = {'status','reason','remaining','turns','empty','cursor','settings'}
        if not fields or fields.keys() - allowed:
            raise ValueError('Invalid room update')
        with self.connect() as db:
            db.execute('UPDATE rooms SET '+','.join(k+'=?' for k in fields)+',updated=? WHERE id=?',
                       (*fields.values(),time.time(),room))
            self.event(db,room,'state',fields)

    def append(self, room, author_id, author, content, state='done', turn_id=None):
        with self.connect() as db:
            cursor = db.execute('INSERT INTO room_messages(room,author_id,author,content,state,created,turn_id) VALUES (?,?,?,?,?,?,?)',
                (room,author_id,author,content,state,time.time(),turn_id))
            message = dict(db.execute('SELECT * FROM room_messages WHERE id=?',(cursor.lastrowid,)).fetchone())
            db.execute('UPDATE rooms SET updated=? WHERE id=?',(time.time(),room))
            self.event(db,room,'message',message)
        return message

    def revise(self, room, message, content, state):
        with self.connect() as db:
            db.execute('UPDATE room_messages SET content=?,state=? WHERE room=? AND id=?',(content,state,room,message))
            value = dict(db.execute('SELECT * FROM room_messages WHERE room=? AND id=?',(room,message)).fetchone())
            self.event(db,room,'message',value)

    def messages(self, room, after=0, count=100, before=None):
        if type(after) is not int or after < 0 or type(count) is not int or not 1 <= count <= 100:
            raise ValueError('Invalid history cursor')
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError('Invalid history cursor')
        self.get(room)
        with self.connect() as db:
            if after:
                rows = db.execute('SELECT * FROM room_messages WHERE room=? AND id>? ORDER BY id LIMIT ?', (room,after,count)).fetchall()
            else:
                rows = db.execute('SELECT * FROM room_messages WHERE room=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?', (room,before,before,count)).fetchall()[::-1]
        return [dict(row) for row in rows]

    def agent_messages(self, room, after, count):
        # Unlike UI history's tail-first default, MCP pages always move forward.
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM room_messages WHERE room=?', (room,)).fetchone()[0]
            rows = db.execute('SELECT id,author,content,state FROM room_messages '
                              'WHERE room=? AND id>? ORDER BY id LIMIT ?', (room,after,count)).fetchall()
        return [dict(row) for row in rows], total

    def events(self, room, after):
        with self.connect() as db:
            minimum = db.execute('SELECT MIN(id) FROM room_events WHERE room=?',(room,)).fetchone()[0]
            rows = db.execute('SELECT * FROM room_events WHERE room=? AND id>? ORDER BY id LIMIT 200',(room,after)).fetchall()
        return dict(reset=bool(minimum and after < minimum-1), events=[dict(id=r['id'],kind=r['kind'],value=json.loads(r['value'])) for r in rows])

    def export(self, room):
        snapshot = self.get(room)
        with self.connect() as db:
            rows = db.execute('SELECT author,content,state FROM room_messages WHERE room=? ORDER BY id',(room,)).fetchall()
        result = '# '+snapshot['title']+'\n\n'+snapshot['topic']+'\n'
        for row in rows:
            result += '\n## '+row['author']+'\n\n'+row['content']+'\n'
            if row['state'] != 'done':
                result += '\n_'+row['state']+'_\n'
        return result
