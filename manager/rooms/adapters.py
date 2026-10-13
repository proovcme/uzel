"""Invocation adapters. MCP membership is transport, not an invocation adapter."""
import json
import socket
import threading
import urllib.request
from urllib.parse import urlsplit

from transport import open_stream


def prepare(spec):
    kind = spec.get('kind')
    if kind not in ('managed', 'openai', 'mcp', 'codex'):
        raise ValueError('Unknown participant adapter')
    result = {}
    if kind == 'openai':
        endpoint = spec.get('endpoint', '')
        if not isinstance(endpoint, str) or len(endpoint) > 500:
            raise ValueError('Invalid participant endpoint')
        parsed = urlsplit(endpoint)
        local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') not in ('', '/v1')
                or (local and parsed.scheme != 'http')
                or (not local and (parsed.scheme != 'https' or spec.get('allow_remote') is not True))):
            raise ValueError('Use loopback HTTP or explicitly authorize a remote HTTPS endpoint')
        model = spec.get('model')
        if not isinstance(model, str) or not 1 <= len(model) <= 200:
            raise ValueError('Model identifier required')
        result.update(endpoint=endpoint.rstrip('/'), model=model, allow_remote=not local)
    if kind == 'codex':
        if spec.get('allow_remote') is not True:
            raise ValueError('Explicit authorization required for Codex subscription access')
        if not codex_available():
            raise ValueError('Automatic Codex adapter is disabled; connect an agent through MCP instead')
        result['allow_remote'] = True
    return result


def codex_available():
    # CLI integration is not shipped until a safe, stable invocation is verified.
    return False


def decode_sse(lines):
    """Parse complete SSE frames, independent of upstream line chunking."""
    import codecs
    decoder = codecs.getincrementaldecoder('utf-8')()
    buffer, frame, finished, size = '', [], False, 0
    for chunk in lines:
        size += len(chunk)
        if size > 2*1024*1024:
            raise RuntimeError('Participant stream exceeds limit')
        buffer += decoder.decode(chunk) if isinstance(chunk,bytes) else chunk
        while '\n' in buffer:
            line, buffer = buffer.split('\n',1)
            line = line.rstrip('\r')
            if not line:
                if not frame: continue
                payload = '\n'.join(frame); frame.clear()
                if payload == '[DONE]': return
                value = json.loads(payload)
                if value.get('error'): raise RuntimeError('Participant generation failed')
                for choice in value.get('choices',[]):
                    finished = finished or bool(choice.get('finish_reason'))
                    content = choice.get('delta',{}).get('content')
                    if isinstance(content,str): yield content
            elif line.startswith('data:'):
                frame.append(line[5:].lstrip())
    decoder.decode(b'',final=True)
    if buffer or frame or not finished:
        raise RuntimeError('Participant stream ended before completion')


class Invocation:
    def __init__(self, manager, participant, request_id, cancelled):
        self.manager, self.participant = manager, participant
        self.request_id, self.cancelled = request_id, cancelled
        self._mu = threading.Lock()
        self._socket = None

    def cancel(self):
        self.cancelled.set()
        with self._mu:
            sock = self._socket
        if self.participant['kind'] == 'managed':
            self.manager.cancel_chat(self.request_id)
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    def track_socket(self, sock):
        with self._mu:
            self._socket = sock
        if self.cancelled.is_set():
            self.cancel()

    def stream(self, messages):
        kind = self.participant['kind']
        if self.cancelled.is_set():
            return
        if kind == 'managed':
            stream = self.manager.stream_chat(dict(messages=messages, max_tokens=768,
                temperature=.7, request_id=self.request_id, room_binding=self.participant['config']['binding']))
            try:
                yield from decode_sse(stream)
            finally:
                stream.close()
        elif kind == 'openai':
            cfg = self.participant['config']
            base = cfg['endpoint']
            if not base.endswith('/v1'):
                base += '/v1'
            payload = dict(model=cfg['model'], messages=messages, max_tokens=768, temperature=.7, stream=True)
            request = urllib.request.Request(base+'/chat/completions', data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            with open_stream(request, self.track_socket, timeout=120, redirects=False) as response:
                yield from decode_sse(response)
        elif kind == 'codex':
            raise RuntimeError('Automatic Codex adapter is disabled')
        else:
            raise ValueError('MCP participants must post through their scoped credential')
