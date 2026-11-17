"""Read-only, version-bound Linear reply markers."""
import hashlib
import json


def pending(raw):
    comments = raw.get('comments',{})
    comments = comments.get('nodes',[]) if isinstance(comments,dict) else comments
    if not isinstance(comments,list):
        raise ValueError('Linear comments unavailable')
    result=[]
    for comment in comments:
        body=comment.get('body','')
        if '<!-- harbor:linear-reply:' in body or body.startswith('<!-- harbor:'):
            continue
        digest=hashlib.sha256(json.dumps({k:comment.get(k) for k in ('id','body','updatedAt')},sort_keys=True).encode()).hexdigest()
        marker=f'<!-- harbor:linear-reply:{digest} -->'
        # A separate, later comment must carry the marker, not the source.
        if not any(c.get('id') != comment.get('id') and c.get('createdAt','') >= comment.get('createdAt','') and marker in c.get('body','') for c in comments):
            result.append({'id':comment.get('id'),'marker':marker})
    return result
