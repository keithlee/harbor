"""Explicit model fallback policy and sanitized provider rejection detection."""
import json
import re


def validate_fallbacks(primary, models):
    if not isinstance(models, (list, tuple)) or len(models) > 8:
        raise ValueError('fallback_models must be an ordered list of at most eight models')
    if any(not isinstance(m, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', m) for m in models):
        raise ValueError('fallback_models contains an invalid model identifier')
    if len(set(models)) != len(models) or primary in models:
        raise ValueError('fallback_models must be distinct from each other and the primary model')
    return tuple(models)


def model_rejection(value, depth=0):
    """Accept only structured provider errors, never assistant/tool prose."""
    if depth > 4:
        return False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            return False
    if not isinstance(value, dict):
        return False
    if value.get('type') == 'turn.failed':
        return model_rejection(value.get('error'), depth + 1)
    if value.get('type') == 'error' and isinstance(value.get('message'), str):
        return model_rejection(value['message'], depth + 1)
    if 'message' in value and set(value) == {'message'}:
        return model_rejection(value['message'], depth + 1)
    error = value.get('error')
    if not isinstance(error, dict):
        return False
    status, error_type = value.get('status'), error.get('type')
    if status is not None and (type(status) is not int or status not in (400, 404)):
        return False
    if error_type is not None and error_type not in ('invalid_request_error', 'model_not_found', 'unsupported_model'):
        return False
    code = error.get('code')
    if code is not None and not isinstance(code, str):
        return False
    if code in ('model_not_found', 'unsupported_model'):
        return True
    return (value.get('status') == 400 and error.get('type') == 'invalid_request_error'
            and bool(re.search(r"\bmodel\b.*\b(?:not supported|unsupported|does not exist)\b", str(error.get('message', '')), re.I)))


def rejection_in_stderr(text):
    for line in text.splitlines():
        # Plain codex exec writes ERROR: followed by the provider JSON.
        if line.startswith('ERROR: '):
            line = line[7:]
        if model_rejection(line):
            return True
    return False


def rejected_events(events):
    return any(e.type in {'error', 'turn.failed'} and model_rejection(e.data) for e in events)


def selected_model(db, project, ticket, run_id, primary, fallbacks):
    from harbor.workflow_store import initialize
    initialize(db)
    rows = db.execute("SELECT payload FROM workflow_receipts WHERE project=? AND ticket=? AND run_id=? AND kind='model_fallback' ORDER BY id DESC", (project, ticket, run_id))
    for row in rows:
        receipt = json.loads(row[0])
        if receipt.get('primary') == primary and receipt.get('selected') in fallbacks:
            return receipt['selected']
    return primary


def record_fallback(db, project, ticket, run_id, primary, rejected, selected):
    from harbor.workflow_store import record
    record(db, {'project':project,'ticket':ticket,'run_id':run_id,'hook':'model_fallback',
                'primary':primary,'rejected':rejected,'selected':selected,'reason':'model_unavailable'})
