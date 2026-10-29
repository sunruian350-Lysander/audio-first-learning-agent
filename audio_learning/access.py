"""Personal access gate for hosting without a separate password proxy."""

import base64
import binascii
import secrets

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class PersonalAccessMiddleware:
    """Protect the entire app; only the fixed read-only health response is public."""

    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        self.app = app
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")

    def authenticated(self, authorization: str) -> bool:
        if len(authorization) > 8192:
            return False
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(token, validate=True)
        except (ValueError, binascii.Error):
            return False
        username, separator, password = decoded.partition(b":")
        # Evaluate both comparisons, even when the username is incorrect.
        user_ok = secrets.compare_digest(username, self.username)
        password_ok = secrets.compare_digest(password, self.password)
        return bool(separator) & user_ok & password_ok

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.password:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # No WebSocket routes are used today; do not silently expose future ones.
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def private_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
            await send(message)

        health_check = scope["path"] == "/health" and scope["method"] in {"GET", "HEAD"}
        if not health_check and not self.authenticated(Headers(scope=scope).get("authorization", "")):
            response = JSONResponse(
                {"detail": "请使用网站访问账号和密码登录。"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Zouma", charset="UTF-8"'},
            )
            await response(scope, receive, private_response)
            return
        await self.app(scope, receive, private_response)
