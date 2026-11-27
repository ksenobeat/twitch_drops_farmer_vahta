"""Один экземпляр на пользователя.

Проверяется то, из-за чего это вообще написано: второй запуск не должен
подняться. Раньше он поднимался, не мог занять порт, терял свой веб-слой —
и продолжал фармить тем же аккаунтом Twitch параллельно с первым.
"""

from __future__ import annotations

import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import single  # noqa: E402

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
    else:
        fail += 1
        print(f"  ПЛОХО {name}: {got!r} вместо {want!r}")


# своё имя мьютекса: настоящее может быть занято живой «Вахтой»
single.MUTEX = "Vahta.Test.SingleInstance"

check("первый запуск проходит", single.claim(), True)
check("второй не проходит", single.claim(), False)
check("и третий тоже", single.claim(), False)

# --- занятость порта ---------------------------------------------------------
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken_port = busy.getsockname()[1]
    check("занятый порт виден", single.port_taken("127.0.0.1", taken_port), True)

# сокет закрыт — порт снова свободен
check("свободный порт виден", single.port_taken("127.0.0.1", taken_port), False)


# --- побудка -----------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    hits: list = []

    def do_POST(self):
        Handler.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}"

check("живой экземпляр разбудили", single.wake(url), True)
check("постучались куда надо", Handler.hits, ["/api/window/show"])
server.shutdown()

# порт свободен, стучаться некуда — но падать нельзя: первый экземпляр мог
# ещё не поднять сервер, и окно он всё равно откроет сам
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
check("мёртвый адрес не роняет", single.wake(f"http://127.0.0.1:{dead}", timeout=1), False)

print(f"\n{ok} проверок прошло, {fail} упало")
sys.exit(1 if fail else 0)
