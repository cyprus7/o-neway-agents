"""Finite Telegram methods; no ambient headers, payload logs or token diagnostics."""
import asyncio
from dataclasses import dataclass
import json
import logging
import re

import httpx


class _RedactBotURL(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self._tokens = {token}

    def add_token(self, token):
        self._tokens.add(token)

    def filter(self, record):
        rendered = record.getMessage()
        for token in self._tokens:
            rendered = rendered.replace(token, "[redacted]")
        record.msg = re.sub(r"/bot[0-9]+:[^/\s?\"']+", "/bot[redacted]", rendered)
        record.args = ()
        return True


@dataclass(frozen=True)
class Delivery:
    outcome: str
    retry_after: int = 0


class TelegramClient:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client, self._token = client, token
        self._send_lock = asyncio.Lock()
        # Parent logger filters do not run on records emitted by children.
        names = {"httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                 "httpcore.proxy", "httpcore.http_proxy", "httpcore.socks_proxy"}
        names.update(name for name in logging.Logger.manager.loggerDict if name.startswith(("httpcore.", "httpx.")))
        for name in names:
            logger = logging.getLogger(name)
            filters = [f for f in logger.filters if isinstance(f, _RedactBotURL)]
            if filters:
                for filter_ in filters:
                    filter_.add_token(token)
            else:
                logger.addFilter(_RedactBotURL(token))

    async def _call(self, method, payload, *, timeout):
        # Construct Request directly to avoid inherited auth/cookies/default
        # headers/query parameters, especially traceparent/baggage.
        request = httpx.Request("POST", "https://api.telegram.org/bot" + self._token + "/" + method,
                               json=payload, extensions={"timeout": httpx.Timeout(timeout).as_dict()})
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.send(request, stream=True, auth=None, follow_redirects=False)
                try:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 1024 * 1024:
                            return None, "delivery_unknown"
                    result = json.loads(content)
                    if not isinstance(result, dict):
                        return None, "delivery_unknown"
                    if response.status_code == 403 or result.get("error_code") == 403:
                        return None, "blocked"
                    if response.status_code == 409 or result.get("error_code") == 409:
                        return None, "conflict"
                    if result.get("ok") is True and response.status_code == 200:
                        return result.get("result"), "ok"
                    if result.get("ok") is False:
                        parameters = result.get("parameters", {})
                        wait = parameters.get("retry_after", 2) if isinstance(parameters, dict) else 2
                        if type(wait) is not int:
                            wait = 2
                        if result.get("error_code") == 429 or response.status_code >= 500:
                            return max(1, min(wait, 300)), "retryable"
                        return None, "failed"
                    return None, "delivery_unknown"
                finally:
                    await response.aclose()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, RecursionError):
            return None, "delivery_unknown"

    async def send_message(self, chat_id: int, text: str, *, web_app_url: str | None = None, guard=None) -> Delivery:
        async with self._send_lock:
            if guard is not None and not guard():
                return Delivery("canceled")
            return await self._send_message(chat_id, text, web_app_url=web_app_url)

    async def _send_message(self, chat_id: int, text: str, *, web_app_url: str | None = None) -> Delivery:
        if type(chat_id) is not int or chat_id <= 0 or not isinstance(text, str) or not 1 <= len(text) <= 1000:
            return Delivery("failed")
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if web_app_url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть поездку", "web_app": {"url": web_app_url}}]]}
        result, outcome = await self._call("sendMessage", payload, timeout=8)
        if outcome == "ok" and (not isinstance(result, dict) or type(result.get("message_id")) is not int or result["message_id"] <= 0):
            outcome = "delivery_unknown"
        return Delivery("sent" if outcome == "ok" else outcome, result if outcome == "retryable" else 0)

    async def get_updates(self, offset: int):
        result, outcome = await self._call("getUpdates", {"offset": offset, "limit": 20, "timeout": 20,
            "allowed_updates": ["message"]}, timeout=25)
        if outcome != "ok" or not isinstance(result, list) or len(result) > 20:
            return [], outcome if outcome != "ok" else "failed"
        return result, "ok"
