"""Приёмка базы игр: вливается, своего не затирает и стоит недорого."""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import games  # noqa: E402
from vahta.store import Store  # noqa: E402


def check(label: str, condition: bool) -> bool:
    print(f"  {'ок  ' if condition else 'ПЛОХО'}  {label}")
    return condition


def run() -> bool:
    ok = True
    tmp = Path(__file__).with_name("_games_test.db")
    tmp.unlink(missing_ok=True)
    файл = Path(__file__).with_name("_games_test.json.gz")

    ok &= check("шаблон превращается в адрес", games.адрес_обложки(
        "https://x/ttv-boxart/263490_IGDB-{width}x{height}.jpg"
    ) == "https://x/ttv-boxart/263490_IGDB-120x160.jpg")

    with Store(tmp) as store:
        # игра, которую клиент уже знает своими глазами
        store.db.execute("INSERT INTO game (id, name, box_art) VALUES "
                         "('1', 'Overwatch', 'своя.jpg')")
        # и вторая — известная, но без обложки
        store.db.execute("INSERT INTO game (id, name, box_art) VALUES ('2', 'Rust', '')")

        свежая = [
            ("1", "Overwatch 2", "каталог.jpg"),
            ("2", "Rust", "каталог-раст.jpg"),
            ("3", "Path of Exile", "каталог-пое.jpg"),
        ]
        added = store.add_games(свежая)
        ok &= check("новая игра добавилась", added == 1)

        было = {r["id"]: r for r in store.db.execute("SELECT * FROM game")}
        ok &= check("название из кампании не затёрлось",
                    было["1"]["name"] == "Overwatch")
        ok &= check("своя обложка не затёрлась",
                    было["1"]["box_art"] == "своя.jpg")
        ok &= check("пустая обложка заполнилась из каталога",
                    было["2"]["box_art"] == "каталог-раст.jpg")
        ok &= check("новая приехала целиком",
                    было["3"]["name"] == "Path of Exile")

        ok &= check("повторный залив ничего не добавляет",
                    store.add_games(свежая) == 0)

        ok &= check("названия отдаются для подсказки, по алфавиту",
                    store.game_names() == ["Overwatch", "Path of Exile", "Rust"])

        # файл целиком: читается, распаковывается, вливается
        файл.write_bytes(gzip.compress(json.dumps({
            "built": "2026-07-31",
            "games": [{"id": "4", "name": "Dota 2",
                       "art": "https://x/4-{width}x{height}.jpg"}],
        }).encode("utf8")))
        ok &= check("файл влился", games.влить(store, файл) == 1)
        row = store.db.execute("SELECT * FROM game WHERE id = '4'").fetchone()
        ok &= check("обложка из файла разложилась",
                    row["box_art"] == "https://x/4-120x160.jpg")

        ok &= check("битый файл не роняет запуск",
                    games.влить(store, Path(__file__)) == 0)
        ok &= check("отсутствующий файл не роняет запуск",
                    games.влить(store, файл.with_name("нет-такого.gz")) == 0)
        ok &= check("дата сборки читается", games.собрана(файл) == "2026-07-31")
        ok &= check("у битого файла даты нет", games.собрана(Path(__file__)) == "")

        # обновление с сервера: до сети дело доходит только когда есть что качать
        # и оно правда свежее. Сама закачка — это `update.fetch`, она своя
        class ServiceStub:
            def __init__(self, **поля):
                self.__dict__.update(поля)

        никуда = dict(games_url="", games_sha256="", games_have="")
        ok &= check("без адреса на сервер не ходим", not asyncio.run(games.обновить(
            ServiceStub(games_built="2026-08-01", **никуда))))
        ok &= check("без даты на сервере не ходим", not asyncio.run(games.обновить(
            ServiceStub(games_built="", games_url="http://x/games.json.gz",
                        games_sha256="ab", games_have=""))))
        ok &= check("своя база свежее — не качаем", not asyncio.run(games.обновить(
            ServiceStub(games_built="2026-07-31", games_url="http://x/games.json.gz",
                        games_sha256="ab", games_have="2026-08-01"))))
        ok &= check("ровесница — тоже не качаем", not asyncio.run(games.обновить(
            ServiceStub(games_built="2026-07-31", games_url="http://x/games.json.gz",
                        games_sha256="ab", games_have="2026-07-31"))))

    # цена на запуске: настоящая база, пустая таблица
    настоящая = Path(__file__).resolve().parent.parent / "vahta" / games.ИМЯ
    tmp.unlink(missing_ok=True)
    if настоящая.is_file():
        with Store(tmp) as store:
            начало = time.monotonic()
            сколько = games.влить(store, настоящая)
            прошло = time.monotonic() - начало
        print(f"      {сколько} игр за {прошло * 1000:.0f} мс")
        ok &= check("вливается быстрее четверти секунды", прошло < 0.25)
    else:
        print("      настоящей базы нет — прогон на ней пропущен")

    tmp.unlink(missing_ok=True)
    Path(str(tmp) + "-wal").unlink(missing_ok=True)
    Path(str(tmp) + "-shm").unlink(missing_ok=True)
    файл.unlink(missing_ok=True)
    return ok


if __name__ == "__main__":
    print("База игр:")
    sys.exit(0 if run() else 1)
