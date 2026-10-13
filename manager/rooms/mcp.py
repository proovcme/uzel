"""Dependency-free MCP stdio bridge; a credential grants one participant/room."""
import json
import os
import sys
import urllib.request
from urllib.parse import urlsplit


DESCRIPTIONS = {
    'room_list':'List the room granted by this participant credential.',
    'room_join':'Rejoin your room as your own participant.',
    'room_read':'Read recent messages after a message cursor and inspect your pending turn.',
    'room_post':'Reply to your pending turn. Use PASS if you have nothing to add.',
    'room_leave':'Leave your room.',
    'room_status':'Read participant state and discussion limits.'}


def tools():
    result = []
    for name, description in DESCRIPTIONS.items():
        properties = dict(room={'type':'string'})
        if name == 'room_read': properties['after'] = {'type':'integer','minimum':0}
        if name == 'room_post': properties.update(content={'type':'string','maxLength':16000},turn_id={'type':'string'})
        result.append(dict(name=name,description=description,inputSchema=dict(type='object',
            properties=properties,additionalProperties=False,required=['content','turn_id'] if name == 'room_post' else [])))
    return result


def call(base, token, name, arguments):
    if name not in DESCRIPTIONS or not isinstance(arguments,dict):
        raise ValueError('Unknown tool or invalid arguments')
    allowed = {'room'} | ({'after'} if name == 'room_read' else {'content','turn_id'} if name == 'room_post' else set())
    if arguments.keys()-allowed: raise ValueError('Unexpected tool arguments')
    payload = json.dumps(dict(operation=name,arguments=arguments)).encode()
    request = urllib.request.Request(base+'/api/rooms/agent',data=payload,method='POST',
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    # No proxy or redirects: the capability cannot leave the local manager.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): return None
    with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=15) as response:
        return json.load(response)


def dispatch(request, base, token):
    if not isinstance(request,dict) or request.get('jsonrpc') != '2.0':
        raise ValueError('Invalid JSON-RPC request')
    key, method = request.get('id'), request.get('method')
    if key is None: return None
    if method == 'initialize':
        result = dict(protocolVersion='2024-11-05',capabilities={'tools':{}},
                      serverInfo={'name':'uzel-rooms','version':'0.1.0'})
    elif method == 'ping': result = {}
    elif method == 'tools/list': result = {'tools':tools()}
    elif method == 'tools/call':
        params = request.get('params',{})
        try:
            value = call(base,token,params.get('name'),params.get('arguments',{}))
            result = dict(content=[dict(type='text',text=json.dumps(value,ensure_ascii=False))])
        except Exception:
            result = dict(isError=True,content=[dict(type='text',text='Room operation refused or manager unavailable. Check the scoped credential and active turn.')])
    else:
        return dict(jsonrpc='2.0',id=key,error=dict(code=-32601,message='Method not found'))
    return dict(jsonrpc='2.0',id=key,result=result)


def main():
    base = os.environ.get('UZEL_ROOMS_URL','http://127.0.0.1:1924').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise SystemExit('UZEL_ROOMS_URL must be a loopback HTTP origin')
    token = os.environ.get('UZEL_ROOM_TOKEN','')
    if not 20 <= len(token) <= 100: raise SystemExit('UZEL_ROOM_TOKEN required')
    while True:
        raw = sys.stdin.buffer.readline(1024*1024+1)
        if not raw: break
        if len(raw) > 1024*1024: raise SystemExit('MCP request too large')
        try:
            response = dispatch(json.loads(raw),base,token)
        except Exception:
            response = dict(jsonrpc='2.0',id=None,error=dict(code=-32700,message='Invalid request'))
        if response is not None:
            print(json.dumps(response,ensure_ascii=False),flush=True)


if __name__ == '__main__': main()
