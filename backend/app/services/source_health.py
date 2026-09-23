"""Resource failures are not automatically source-wide authentication failures."""
OPTIONAL = frozenset({'file', 'folder', 'page', 'page_detail', 'page_file', 'module', 'module_item',
    'announcement', 'calendar_event', 'assignment_group', 'assignment_detail', 'syllabus', 'course_metadata'})
KNOWN_CODES = frozenset({'permission_denied', 'resource_not_found', 'invalid_response', 'auth_required',
    'invalid_token', 'timeout', 'network_error', 'rate_limited', 'server_error'})


def classify_canvas_errors(errors, successful):
    if not errors:
        return 'HEALTHY'
    if any(code in {'auth_required', 'invalid_token'} for code in errors.values()):
        return 'AUTH_REQUIRED'
    if 'assignment' in errors or not successful:
        return 'ERROR'
    return 'DEGRADED'


def source_health_view(source):
    details = (source.metadata_json or {}).get('resource_errors') or []
    if not details and source.last_error:
        # Legacy records lack HTTP status: preserve that uncertainty explicitly.
        for entry in source.last_error.split(';'):
            kind, _, code = entry.strip().rpartition(':')
            if code in KNOWN_CODES:
                known_resource = kind in OPTIONAL or kind == 'assignment' or (
                    kind.startswith('page_file:') and kind.partition(':')[2].isdigit()
                )
                details.append({'resource': kind if known_resource else 'source',
                    'code':code, 'http_status':None, 'historical':True})
    message = None
    if source.state == 'DEGRADED':
        message = 'Some resources could not be read. Available course content remains usable.'
    elif source.state in {'AUTH_REQUIRED', 'EXPIRED', 'INVALID'}:
        message = 'Sign in again to resume source synchronization.'
    elif source.state == 'ERROR':
        message = 'Source synchronization failed. Check authentication and retry.'
    return {'health_summary': message, 'health_diagnostics': details}
