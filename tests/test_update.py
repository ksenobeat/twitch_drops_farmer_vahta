"""Автообновление: что скачано, то и ставится — и ничего другого.

Главная проверка здесь одна: файл с несошедшимся хэшем не должен доехать
до запуска. Всё остальное в этом модуле — обёртки вокруг `subprocess`
и `httpx`, а вот эта ветка решает, что именно выполнится на чужой машине.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import update  # noqa: E402
from vahta.settings import Settings  # noqa: E402

# Первая проверка ждёт первого пинга — в тесте ждать нечего
update.FIRST_DELAY = 0
# Пять минут на закачку — это про двадцать мегабайт по живой сети. Здесь же
# любое обращение к сети означает, что проверка пошла не туда, и ждать его
# пять минут вместо ответа «упало» — потерянное время
update.TIMEOUT = 3

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
    else:
        fail += 1
        print(f"  ПЛОХО {name}: {got!r} вместо {want!r}")


class FakeService:
    """Ровно то, что читает `prepare`, и ничего сверх."""

    def __init__(self, tmp: Path):
        self.data_dir = tmp
        self.settings = Settings(tmp / "settings.json")
        self.latest_version = "99.0.0"
        self.update_url = ""
        self.update_sha256 = ""
        self.update_ready: Path | None = None


with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    body = b"MZ" + b"\x00" * 4096  # что угодно: важен не exe, а его хэш
    digest = hashlib.sha256(body).hexdigest()

    class Serve(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Serve)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/Vahta-Setup.exe"

    # --- скачивание ---------------------------------------------------------
    into = tmp / "updates" / "Vahta-Setup-99.0.0.exe"
    got = asyncio.run(update.fetch(url, digest, into))
    check("файл скачался", got, into)
    check("и это ровно он", into.read_bytes(), body)

    # Хэш не сошёлся — файла быть не должно вовсе: иначе он остался бы лежать
    # готовым к установке, и следующий заход принял бы его за проверенный
    other = tmp / "updates" / "Vahta-Setup-98.0.0.exe"
    check("подменённый файл отбит", asyncio.run(update.fetch(url, "0" * 64, other)), None)
    check("и не остался на диске", other.exists(), False)
    check("огрызка тоже нет", list((tmp / "updates").glob("*.part")), [])

    check("мёртвый адрес не роняет",
          asyncio.run(update.fetch("http://127.0.0.1:1/нет", digest, other)), None)

    # и `server_close`, а не только `shutdown`: без него порт остаётся
    # занятым, и обращение к нему не отказывает, а висит до таймаута
    server.shutdown()
    server.server_close()

    # --- подготовка ---------------------------------------------------------
    service = FakeService(tmp)
    check("без адреса качать нечего", asyncio.run(update.prepare(service)), None)

    service.update_url, service.update_sha256 = url, digest
    service.latest_version = "0.0.1"
    check("старая версия не качается", asyncio.run(update.prepare(service)), None)
    # Сюда же приходит только что обновившаяся «Вахта»: сервер больше не
    # обещает ничего нового, а инсталлер, которым её поставили, лежит в папке.
    # Каждая версия весит за двадцать мегабайт — за десяток обновлений
    # накопилась бы четверть гигабайта, которую никто не просил хранить
    check("и папка убрана целиком", list((tmp / "updates").iterdir()), [])

    into.write_bytes(body)  # как будто скачали в прошлый раз
    (tmp / "updates" / "Vahta-Setup-98.0.0.exe").write_bytes(b"old version")
    (tmp / "updates" / "Vahta-Setup-97.0.0.part").write_bytes(b"half done")
    service.latest_version = "99.0.0"
    check("скачанное в прошлый раз берётся с диска",
          asyncio.run(update.prepare(service)), into)
    check("а прежние инсталлеры и огрызки убраны",
          sorted(p.name for p in (tmp / "updates").iterdir()), [into.name])

    # --- момент установки ---------------------------------------------------
    service.update_ready = into
    check("автопилот забирает готовое", update.take(service), into)
    check("и второй раз оно не отдаётся", update.take(service), None)
    check("версия помечена испробованной",
          asyncio.run(update.prepare(service)), None)

    # Не встало — «Вахта» остаётся работающей. Запускаем заведомо несуществующее:
    # никакого исключения наружу, только «нет» и строчка в логе.
    check("пропавший инсталлер не роняет", update.install(tmp / "нет-такого.exe"), False)

print(f"\n{ok} проверок прошло, {fail} упало")
sys.exit(1 if fail else 0)
