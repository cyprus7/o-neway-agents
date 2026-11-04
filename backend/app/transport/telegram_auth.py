"""Server-verified Telegram Mini App login; raw launch data never leaves this edge.

HMAC construction: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import JSONResponse

from .auth import AUTH_COOKIE, error, origin_allowed, set_credentials

BODY_LIMIT = 16 * 1024
INIT_DATA_LIMIT = 8 * 1024
MAX_USER_ID = 2**52 - 1


class InvalidInitData(ValueError):
    pass


class ExpiredInitData(ValueError):
    def __init__(self, subject: str):
        super().__init__('telegram_launch_expired')
        self.subject = subject


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInitData()
        result[key] = value
    return result


def _json(value):
    return json.loads(value, object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(InvalidInitData()))


def verify_init_data(raw, token, *, now=None):
    """Return only the signed numeric subject, with no username-based identity."""
    if not isinstance(raw, str) or not raw or len(raw.encode('utf-8')) > INIT_DATA_LIMIT:
        raise InvalidInitData()
    if re.search(r'%(?![0-9a-fA-F]{2})', raw):
        raise InvalidInitData()
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=40)
        fields = _unique_object(pairs)
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key) for key in fields):
            raise InvalidInitData()
        supplied = fields.pop('hash')
        if not re.fullmatch(r'[0-9a-fA-F]{64}', supplied):
            raise InvalidInitData()
        check = '\n'.join(f'{key}={value}' for key,value in sorted(fields.items()))
        secret = hmac.new(b'WebAppData', token.encode('utf-8'), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode('utf-8'), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, bytes.fromhex(supplied)):
            raise InvalidInitData()
        stamp = fields['auth_date']
        if not re.fullmatch(r'[0-9]{1,13}', stamp):
            raise InvalidInitData()
        current = int(time.time()) if now is None else now
        if int(stamp) > current + 30:
            raise InvalidInitData()
        user = _json(fields['user'])
        subject = user.get('id') if isinstance(user, dict) else None
        if type(subject) is not int or not 0 < subject <= MAX_USER_ID:
            raise InvalidInitData()
        if int(stamp) < current - 300:
            raise ExpiredInitData(str(subject))
        return str(subject)
    except ExpiredInitData:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidInitData() from None


def install_telegram_auth(app):
    @app.post('/api/auth/telegram')
    async def telegram_login(request: Request):
        if not origin_allowed(request):
            return error(403, 'origin_rejected', 'Недопустимый источник запроса.')
        token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        match = re.fullmatch(r'([0-9]+):([^\s:]+)', token)
        if not match or len(match[1]) > 20 or int(match[1]) <= 0:
            return error(503, 'telegram_unavailable', 'Вход через Telegram не настроен.')
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > BODY_LIMIT:
                return error(413, 'request_too_large', 'Запрос слишком большой.')
            body.extend(chunk)
        try:
            payload = _json(body)
            if not isinstance(payload, dict) or set(payload) != {'init_data'}:
                raise InvalidInitData()
            subject = verify_init_data(payload['init_data'], token)
        except ExpiredInitData as expired:
            store = request.app.state.store
            access = store.resolve_access_session(request.cookies.get(AUTH_COOKIE, ''))
            csrf = request.headers.get('x-csrf-token', '')
            issuer = 'telegram:' + str(int(match[1]))
            if (access is not None and csrf
                    and hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(), access.csrf_hash)
                    and store.identity_matches(access.principal.user_id, issuer, expired.subject)):
                user = store.get_user(access.principal.user_id)
                if user is not None:
                    return JSONResponse({'user': {'id': user.user_id, 'kind': user.kind}, 'csrf_token': csrf},
                                        headers={'Cache-Control': 'no-store'})
            return error(401, 'telegram_launch_expired',
                         'Срок запуска Telegram истёк. Закройте мини-приложение и откройте его снова из бота.')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return error(401, 'telegram_init_data_invalid', 'Не удалось подтвердить вход через Telegram.')
        store = request.app.state.store
        try:
            with store.transaction():
                user = store.resolve_identity('telegram:' + str(int(match[1])), subject)
                credentials = store.create_access_session(user.user_id)
        except KeyError:
            return error(401, 'telegram_init_data_invalid', 'Не удалось подтвердить вход через Telegram.')
        response = JSONResponse({'user': {'id': user.user_id, 'kind': user.kind},
                                 'csrf_token': credentials.csrf_token})
        set_credentials(response, request, credentials, partitioned=True)
        return response
