"""Single-owner web sessions for the private server deployment."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "__Host-vahta"
SESSION_SECONDS = 30 * 24 * 3600
LOGIN_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход · Вахта</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;
place-items:center;background:#141714;color:#e4e8df;font:16px system-ui;padding:24px}
main{width:100%;max-width:420px;padding:32px;border:1px solid #394337;border-radius:16px}
h1{letter-spacing:.12em;font-size:30px}p{color:#b4bcae;line-height:1.55}
label{display:block;margin-bottom:8px}input,button{width:100%;padding:13px;border-radius:8px;
font:inherit}input{background:#20271e;color:inherit;border:1px solid #596552}
button{margin-top:18px;background:#c6e89e;border:0;color:#1e2818;cursor:pointer}
#error{color:#ffc7ac;min-height:24px}a{color:#c6e89e}</style>
<main><h1>ВАХТА</h1><p>Твоя служба Twitch-дропсов.</p>
<form id="login"><label for="password">Пароль ВАХТЫ</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button>Войти</button><p id="error" role="alert"></p></form>
<p>Это пароль панели. Вход в Twitch настраивается отдельно внутри.</p></main>
<script>document.getElementById('login').onsubmit=async(e)=>{e.preventDefault();
const b=e.target.querySelector('button');b.disabled=true;
try{const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({password:document.getElementById('password').value})});
if(r.ok){location.replace('/');return}document.getElementById('error').textContent=(await r.json()).error;
}catch(e){document.getElementById('error').textContent='Нет связи со службой'}b.disabled=false;};</script>
</html>"""


def origin(value: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise ValueError("credentials in origin")
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class Access:
    def __init__(self, public_url: str, password_file: Path):
        parsed = urlsplit(public_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("VAHTA_PUBLIC_URL must be an HTTPS origin")
        self.origin = origin(public_url)
        password = password_file.read_text("utf8").strip()
        if len(password) < 20:
            raise ValueError("Server password must have at least 20 characters")
        self.key = hashlib.sha256(password.encode()).digest()
        self.attempts: dict[str, tuple[float, int]] = {}

    def _signature(self, payload: str) -> str:
        return hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def session(self) -> str:
        payload = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
        return f"{payload}.{self._signature(payload)}"

    def authenticated(self, value: str) -> bool:
        try:
            payload, signature = value.rsplit(".", 1)
            expiry, nonce = payload.split(".")
            remaining = int(expiry) - time.time()
            return (len(nonce) == 32 and 0 < remaining <= SESSION_SECONDS
                    and hmac.compare_digest(signature, self._signature(payload)))
        except (ValueError, TypeError):
            return False

    async def handle(self, request: Request, call_next):
        # Health checks stay inside the container and reveal no account state.
        if (request.url.path == "/healthz" and request.method == "GET"
                and request.client and request.client.host in {"127.0.0.1", "::1"}
                and request.url.hostname in {"127.0.0.1", "localhost", "::1"}):
            return await call_next(request)
        try:
            if origin(str(request.base_url)) != self.origin:
                return JSONResponse({"error": "Недопустимый адрес сервера"}, 403)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                source = request.headers.get("origin")
                if (not source or origin(source) != self.origin
                        or request.headers.get("sec-fetch-site") == "cross-site"):
                    return JSONResponse({"error": "Запрос с другого сайта запрещён"}, 403)
        except ValueError:
            return JSONResponse({"error": "Недопустимый адрес"}, 403)
        path = request.url.path
        if path == "/auth/login" and request.method == "GET":
            response = HTMLResponse(LOGIN_PAGE)
        elif path == "/auth/login" and request.method == "POST":
            response = await self.login(request)
        elif not self.authenticated(request.cookies.get(COOKIE, "")):
            response = (JSONResponse({"error": "Нужен вход в ВАХТУ"}, 401)
                        if path.startswith("/api/") else RedirectResponse("/auth/login", 303))
        elif path == "/auth/logout" and request.method == "POST":
            response = JSONResponse({"ok": True})
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    async def login(self, request: Request):
        address = request.client.host if request.client else "unknown"
        now = time.monotonic()
        self.attempts = {ip: value for ip, value in self.attempts.items() if now - value[0] < 300}
        since, count = self.attempts.get(address, (now, 0))
        if count >= 10 or len(self.attempts) >= 256:
            return JSONResponse({"error": "Слишком много попыток. Подожди пять минут."}, 429)
        self.attempts[address] = (since, count + 1)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024:
                return JSONResponse({"error": "Слишком длинный запрос"}, 413)
        try:
            import json
            password = json.loads(raw).get("password", "")
            valid = isinstance(password, str) and hmac.compare_digest(
                hashlib.sha256(password.encode()).digest(), self.key)
        except (ValueError, AttributeError, UnicodeError):
            valid = False
        if not valid:
            return JSONResponse({"error": "Пароль не подошёл"}, 401)
        self.attempts.pop(address, None)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, self.session(), max_age=SESSION_SECONDS,
                            secure=True, httponly=True, samesite="strict")
        return response
