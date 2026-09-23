"""Small, loopback-only HTTP adapter for the private notification center."""
from http import HTTPStatus
import re
import secrets
from urllib.parse import parse_qs


def _local_request(handler, *, write=False):
    hosts=handler.headers.get_all('Host', [])
    port=handler.server.server_address[1]
    permitted={f'127.0.0.1:{port}',f'localhost:{port}'}
    if port==80: permitted.update({'localhost','127.0.0.1'})
    if len(hosts)!=1 or hosts[0].lower() not in permitted: return False
    if handler.client_address[0] not in {'127.0.0.1','::1'}: return False
    if handler.headers.get('Sec-Fetch-Site')=='cross-site': return False
    if not write: return True
    origins=handler.headers.get_all('Origin', [])
    tokens=handler.headers.get_all('X-CSRF-Token', [])
    service=handler.server.notifications
    return (len(origins)==1 and origins[0]=='http://'+hosts[0] and len(tokens)==1
            and service is not None and tokens[0].isascii() and secrets.compare_digest(tokens[0],service.csrf_token))


def handle_get(handler, parsed):
    if not _local_request(handler):
        handler._json(HTTPStatus.FORBIDDEN,{'error':'forbidden','message':'仅允许本机同源访问'})
        return
    if parsed.path=='/notifications':
        if handler._reject_unexpected_query(parsed.query): return
        from .notification_page import NOTIFICATION_PAGE
        handler._send(HTTPStatus.OK,NOTIFICATION_PAGE.encode('utf-8'),'text/html; charset=utf-8')
        return
    if parsed.path!='/api/notifications':
        handler._json(HTTPStatus.NOT_FOUND,{'error':'not_found'})
        return
    if handler.server.notifications is None:
        handler._json(HTTPStatus.SERVICE_UNAVAILABLE,{'error':'notification_unavailable','message':'通知模块未就绪；原行情服务不受影响'})
        return
    try:
        query=parse_qs(parsed.query,keep_blank_values=True)
        if not query.keys()<={'limit','offset'} or any(len(v)!=1 or not re.fullmatch('[0-9]{1,6}',v[0]) for v in query.values()):
            raise ValueError
        limit=int(query.get('limit',['50'])[0]); offset=int(query.get('offset',['0'])[0])
        result=handler.server.notifications.snapshot(limit,offset)
        handler._json(HTTPStatus.OK,result)
    except (ValueError,OSError):
        handler._json(HTTPStatus.BAD_REQUEST,{'error':'invalid_query','message':'通知记录或分页参数不可用'})


def handle_post(handler, parsed):
    if not _local_request(handler,write=True):
        handler._json(HTTPStatus.FORBIDDEN,{'error':'forbidden','message':'通知设置仅允许本机同源确认操作'})
        return
    if handler._reject_unexpected_query(parsed.query): return
    service=handler.server.notifications
    actions={'/api/notifications/config','/api/notifications/connection-test','/api/notifications/test-email',
             '/api/notifications/enabled','/api/notifications/replay'}
    if parsed.path not in actions:
        handler._json(HTTPStatus.NOT_FOUND,{'error':'not_found'})
        return
    try:
        payload=handler._read_json_object()
        if parsed.path.endswith('/config'):
            result=service.configure(payload)
        elif parsed.path.endswith('/enabled'):
            handler._require_fields(payload,{'enabled','confirmed'})
            if payload['confirmed'] is not True: raise ValueError('需要明确确认')
            result=service.set_enabled(payload['enabled'])
        elif parsed.path.endswith('/replay'):
            handler._require_fields(payload,{'symbol','date'})
            result=service.replay(payload['symbol'],payload['date'])
        else:
            handler._require_fields(payload,{'confirmed'})
            if payload['confirmed'] is not True: raise ValueError('需要明确确认')
            key=handler._idempotency_key()
            result=service.test_connection(key) if parsed.path.endswith('/connection-test') else service.test_email(key)
        handler._json(HTTPStatus.ACCEPTED if result.get('queued') else HTTPStatus.OK,result)
    except ValueError as error:
        # Third-party protocol/config exceptions may contain supplied input.
        # Only fixed, credential-free messages cross the HTTP boundary.
        if parsed.path.endswith('/replay'):
            safe_reasons={
                '存在未解除的分钟质量隔离问题，不能回放','该日没有通过校验的分钟历史',
                '不能回放未来日期','回放日期无效','回放日期不是交易日或为已配置休市日',
                '历史事务尚未完成，不能只读回放','只能回放已持有ETF',
            }
            reason=str(error)
            message=reason if reason in safe_reasons else '回放不可用：历史缺失、质量隔离未解除或日期/分钟校验未通过。'
            handler._json(HTTPStatus.UNPROCESSABLE_ENTITY,{'error':'replay_unavailable','message':message})
            return
        handler._json(HTTPStatus.UNPROCESSABLE_ENTITY,{'error':'invalid_request','message':'操作未完成：请检查字段、确认状态、测试间隔及邮箱配置；只读模式不能发信'})
    except OSError:
        handler._json(HTTPStatus.SERVICE_UNAVAILABLE,{'error':'notification_storage','message':'通知存储不可用，未执行该操作'})
