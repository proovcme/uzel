"""Small, lossless MCP pages, including text too large for a single tool result."""
import json


# Budget the escaped JSON text inside an MCP content block, not Python characters.
# Leave room for the JSON-RPC envelope and content block on the stdio wire.
PAGE_BYTES = 3800


def encoded_size(value):
    inner = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return len(json.dumps(inner, ensure_ascii=False).encode('utf-8'))


def summary(snapshot):
    return {key: snapshot[key] for key in ('id', 'title', 'status', 'reason', 'turns', 'remaining', 'limits', 'web_search')}


def participant(member, snapshot=None):
    value={key: member[key] for key in ('id', 'name', 'enabled', 'state', 'turn_id')}
    value['next_action']=('stop' if not member['enabled'] or snapshot and snapshot['status']!='running'
        else 'reply' if member['state']=='waiting_external' and member['turn_id'] else 'wait')
    return value


def read_page(store, snapshot, member, args):
    after, offset, limit = args.get('after', -1), args.get('offset', 0), args.get('limit', 8)
    if type(after) is not int or after < -1 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid history cursor')
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError('Invalid page limit')
    rows, total = store.agent_messages(snapshot['id'], max(0, after), limit+1)
    if after == -1:
        rows.insert(0, dict(id=0, author='Topic', content=snapshot['topic'], state='done'))
    result = dict(participant=participant(member,snapshot), room=summary(snapshot), messages=[],
                  next_cursor=dict(after=after, offset=offset), has_more=False,
                  total_messages=total, waiting_for_message=False)
    if offset and (not rows or offset >= len(rows[0]['content'])):
        raise ValueError('Invalid text offset')
    for row in rows:
        if row['state'] == 'streaming':
            # Its content is still changing; retry the same cursor after waiting.
            result['waiting_for_message'] = True
            break
        if len(result['messages']) == limit:
            result['has_more'] = True
            break
        content = row['content']
        entry = dict(id=row['id'], author=row['author'], state=row['state'],
                     offset=offset, content='', complete=False)
        result['messages'].append(entry)
        old_cursor = dict(result['next_cursor'])

        def fit(end):
            entry.update(content=content[offset:end], complete=end == len(content))
            result['next_cursor'] = (dict(after=row['id'], offset=0) if entry['complete']
                                     else dict(after=old_cursor['after'], offset=end))
            result['has_more'] = True  # Conservative size, even for the last row.
            return encoded_size(result) <= PAGE_BYTES

        low, high = offset, len(content)
        while low < high:
            middle = (low+high+1)//2
            if fit(middle): low = middle
            else: high = middle-1
        if not fit(low) or (low == offset and offset < len(content)):
            result['messages'].pop()
            result['next_cursor'] = old_cursor
            result['has_more'] = True
            break
        if not entry['complete']:
            break
        offset = 0
        result['has_more'] = False
    return result
