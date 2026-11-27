"""Собрать базу игр Twitch: категории, обложки и иконки.

    env\\Scripts\\python.exe tools\\games.py           — каталог
    env\\Scripts\\python.exe tools\\games.py --icons   — ещё и иконки (брошено, см. ниже)

Делает два файла. `tools/games.json` — наша рабочая копия, читаемая глазами:
в ней всё, что удалось узнать, включая адреса иконок и промахи. `vahta/games.json.gz`
— то, что едет к человеку: только id, название и обложка. Разделение не ради
красоты — адреса иконок клиенту не нужны ни на что, а лишнее в базе живёт вечно.

**Иконки брошены 31 июля 2026 и по умолчанию не ищутся.** Автомат нашёл 66 из 86,
а оставшиеся шестнадцать требовали рисования запасных картинок руками — при том,
что увидят их только те, у чьего бота владелец с Telegram Premium. Работа не сошлась
с пользой. Код и найденные адреса оставлены на месте: вернуться к ним — это `--icons`,
а не писать заново. База игр живёт своей жизнью, она нужна поиску игры при вводе.

Пять решений, которые тут главные:

- **`games/top` — это не каталог.** Курсор кончается на 3751 категории: это те,
  у кого прямо сейчас есть эфир. Полный каталог Twitch отдаёт только перебором
  `search/categories` (по одной букве «a» — шесть тысяч и не конец), он на сотни
  тысяч и устаревает с первого дня. Поэтому возим живых плюс всех, у кого когда-либо
  были дропсы, а редкую игру, которую сейчас никто не стримит, клиент добирает
  поиском по мере ввода.
- **Обложка не собирается из id.** `box_art_url` — шаблон, и у части игр в пути
  стоит суффикс: `263490_IGDB-{width}x{height}.jpg` у Rust против
  `21779-{width}x{height}.jpg` у League of Legends. Храним шаблон целиком.
- **Пустой `igdb_id` — это «Twitch ни с чем не связал».** У `Just Chatting` он пуст,
  у League of Legends — `115`. Соблазнительно считать пустоту признаком служебной
  категории, но это неправда: пусто и у `World of Tanks Console`, и у `Kirka.io`,
  которые игры. Признак годится только на то, чтобы понимать, есть ли у нас id
  для поиска иконки, — и такие идут в отдельный список отчёта.
- **Иконки ищем по id, а не по названию.** На восьмидесяти трёх играх с дропсами
  название совпало точно лишь у шестидесяти трёх: «Mir Tankov» и «Special Events»
  находят что угодно. Цепочка идёт по id: категория Twitch → `igdb_id` (его отдаёт
  сам Helix) → IGDB `external_games` → appid в Steam → SteamGridDB. По названию
  ищем только там, где цепочка оборвалась, и сравниваем по буквам и цифрам:
  посимвольно — теряется «HITMAN: World of Assassination», по первому попаданию —
  приезжает чужая игра.
- **Что не нашлось — доводится руками,** через `tools/icons_manual.json`
  (id категории Twitch → id игры на SteamGridDB). Автомат под это не написать:
  решить, что «Mir Tankov» — это World of Tanks, может только человек.

Ключи берутся из `data/keys.json` или из окружения (`TW_ID`, `TW_SECRET`, `SGDB`).
В клиент они не едут: сборка забирает `vahta/` и статику, а `data/` — папка
пользователя.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

РАБОЧАЯ = КОРЕНЬ / "tools" / "games.json"
РУЧНАЯ = КОРЕНЬ / "tools" / "icons_manual.json"
ГОТОВАЯ = КОРЕНЬ / "vahta" / "games.json.gz"

HELIX = "https://api.twitch.tv/helix"
IGDB = "https://api.igdb.com/v4"
SGDB = "https://www.steamgriddb.com/api/v2"

STEAM = 1  # external_game_source: поле `category` в v4 больше не фильтрует

# Базы «Вахты», из которых берётся история: какие игры вообще видели с дропсами.
# Своей копии у скрипта нет и заводить её незачем — клиент копит это сам.
БАЗЫ = [
    КОРЕНЬ / "data" / "vahta.db",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Vahta" / "data" / "vahta.db",
]


def ключи() -> dict:
    """Окружение важнее файла: так разовый запуск с чужим ключом не требует правок."""
    из_файла = {}
    path = КОРЕНЬ / "data" / "keys.json"
    if path.exists():
        из_файла = json.loads(path.read_text("utf8"))
    got = {
        "tw_id": os.environ.get("TW_ID") or из_файла.get("twitch_id", ""),
        "tw_secret": os.environ.get("TW_SECRET") or из_файла.get("twitch_secret", ""),
        "sgdb": os.environ.get("SGDB") or из_файла.get("steamgriddb", ""),
    }
    if not got["tw_id"] or not got["tw_secret"]:
        sys.exit("Нет ключей Twitch: положи их в data/keys.json или в TW_ID/TW_SECRET")
    return got


def токен(k: dict) -> str:
    r = httpx.post("https://id.twitch.tv/oauth2/token", timeout=30, data={
        "client_id": k["tw_id"], "client_secret": k["tw_secret"],
        "grant_type": "client_credentials"})
    r.raise_for_status()
    return r.json()["access_token"]


def каталог(http: httpx.Client) -> dict[str, dict]:
    """Все категории, у которых сейчас есть эфир. Идём курсором до конца.

    Страховка от вечного цикла — не счётчик страниц, а повтор: если страница
    не принесла ни одной новой категории трижды подряд, курсор крутится на месте.
    Ограничивать числом страниц нельзя — каталог растёт, и потолок однажды
    молча срезал бы хвост.
    """
    игры: dict[str, dict] = {}
    курсор, страниц, впустую, начало = "", 0, 0, time.monotonic()
    while True:
        params = {"first": 100} | ({"after": курсор} if курсор else {})
        r = http.get(f"{HELIX}/games/top", params=params)
        r.raise_for_status()
        тело = r.json()
        строки = тело.get("data") or []
        было = len(игры)
        for row in строки:
            игры[row["id"]] = {
                "id": row["id"],
                "name": row["name"],
                "art": row.get("box_art_url", ""),
                "igdb": row.get("igdb_id", ""),
            }
        страниц += 1
        впустую = впустую + 1 if len(игры) == было else 0
        if впустую > 2:
            print("  курсор крутится на месте, останавливаюсь")
            break
        курсор = (тело.get("pagination") or {}).get("cursor", "")
        if not курсор or not строки:
            break
        if страниц % 20 == 0:
            print(f"  {страниц} страниц, {len(игры)} категорий")
    print(f"каталог: {len(игры)} категорий за {страниц} страниц, "
          f"{time.monotonic() - начало:.0f} с")
    return игры


ЖИВОЙ = "http://127.0.0.1:8420/api/campaigns"


def свои(игры: dict[str, dict]) -> list[str]:
    """Игры, которые «Вахта» видела с дропсами. Их иконки нужны в первую очередь.

    Источника два, и оба нужны. В таблице `game` лежит только то, что клиент
    планировал, — у Ксена это одиннадцать игр против восьмидесяти трёх в живом
    каталоге дропсов: остальные он видел, но не заводил. А в каталоге, наоборот,
    только сегодняшние кампании — то, что кончилось месяц назад, там не найти.
    Сложение даёт и историю, и сегодняшний день.

    База открывается только на чтение: клиент может работать прямо сейчас,
    и мешать ему мы не вправе. Каталог берётся у него же, если он отвечает:
    свой запрос к Twitch потребовал бы его токена, а это уже чужие ключи.
    """
    были: list[str] = []
    try:
        строки = httpx.get(ЖИВОЙ, timeout=10).json().get("campaigns") or []
        print(f"  живой каталог: {len(строки)} кампаний")
        for row in строки:
            были.append(row["game_id"])
            игры.setdefault(row["game_id"], {
                "id": row["game_id"], "name": row["game"],
                "art": row.get("box_art", ""), "igdb": ""})
    except Exception as exc:
        print(f"  живой каталог недоступен ({type(exc).__name__}) — "
              f"«Вахта» не запущена, беру только историю из базы")

    for path in БАЗЫ:
        if not path.exists():
            continue
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = db.execute("SELECT id, name, box_art FROM game").fetchall()
            db.close()
        except sqlite3.Error as exc:
            print(f"  база {path} не открылась: {exc}")
            continue
        print(f"  {path}: {len(rows)} игр")
        for gid, name, art in rows:
            были.append(gid)
            # Игры может не быть в каталоге: дропсы идут и у тех, кого сейчас
            # никто не стримит. Тогда наша строка — единственное, что о ней есть
            игры.setdefault(gid, {"id": gid, "name": name, "art": art or "", "igdb": ""})
    return sorted(set(были))


def steam_appid(http: httpx.Client, k: dict, tok: str, igdb_ids: list[str]) -> dict[str, str]:
    """igdb_id -> appid в Steam. Пачками: у IGDB четыре запроса в секунду."""
    найдено: dict[str, str] = {}
    head = {"Client-ID": k["tw_id"], "Authorization": f"Bearer {tok}"}
    for i in range(0, len(igdb_ids), 100):
        пачка = igdb_ids[i:i + 100]
        r = http.post(f"{IGDB}/external_games", headers=head, content=(
            f"fields game,uid; where game = ({','.join(пачка)}) "
            f"& external_game_source = {STEAM}; limit 500;"))
        if r.status_code != 200:
            print(f"  IGDB отказал: {r.status_code} {r.text[:120]}")
            continue
        for row in r.json():
            найдено.setdefault(str(row["game"]), row["uid"])
    return найдено


def ключ_названия(name: str) -> str:
    """Название без того, чем каталоги расходятся: регистр, знаки, пробелы.

    Twitch пишет «HITMAN World of Assassination», SteamGridDB —
    «HITMAN: World of Assassination». Требовать совпадения посимвольно значит
    терять такие, а брать первое попадание — получать чужую игру. Сравнение
    по буквам и цифрам держит середину: «Overwatch» и «Overwatch 2» остаются
    разными, а двоеточие перестаёт быть разницей.
    """
    return "".join(c for c in name.casefold() if c.isalnum())


def лучшая_иконка(rows: list[dict]) -> str:
    """Из всех иконок игры — самая пригодная.

    Нужен квадрат в PNG: телеграмному эмодзи нужен ровно 100x100, а `.ico`
    SteamGridDB отдаёт с нулевыми размерами, и что там внутри — неизвестно.
    Официальная лучше самодельной: её человек уже видел в лаунчере и узнает.
    """
    годные = [
        r for r in rows
        if (r.get("mime") or "").endswith("png")
        and r.get("width") and r["width"] == r.get("height")
    ]
    if not годные:
        return ""
    годные.sort(key=lambda r: (r.get("style") != "official", -r["width"]))
    return годные[0].get("url", "")


def иконки(http: httpx.Client, k: dict, tok: str, игры: dict[str, dict],
           нужны: list[str]) -> tuple[int, dict[str, list[str]]]:
    """Найти иконки перечисленным играм.

    Возвращает счёт и три списка промахов, потому что лечатся они по-разному:
    «не нашлась» — это ручная привязка в `icons_manual.json`, «иконки нет» —
    привязка не поможет, нужен запасной рисунок, а «не игра» не лечится вовсе
    и в отчёте стоит только чтобы не искать его снова.
    """
    if not k["sgdb"]:
        print("Ключа SteamGridDB нет — иконки пропускаю")
        return 0, {}
    ручные = json.loads(РУЧНАЯ.read_text("utf8")) if РУЧНАЯ.exists() else {}
    head = {"Authorization": f"Bearer {k['sgdb']}"}

    есть_igdb = [игры[g]["igdb"] for g in нужны if игры.get(g, {}).get("igdb")]
    appids = steam_appid(http, k, tok, есть_igdb) if есть_igdb else {}
    print(f"  appid в Steam нашёлся у {len(appids)} из {len(есть_igdb)}")

    нашли, промахи, пустые, без_igdb = 0, [], [], []
    for gid in нужны:
        игра = игры.get(gid)
        if not игра:
            continue
        sgdb_id = ручные.get(gid, "")
        if not sgdb_id and appids.get(игра["igdb"]):
            r = http.get(f"{SGDB}/games/steam/{appids[игра['igdb']]}", headers=head)
            if r.status_code == 200:
                sgdb_id = (r.json().get("data") or {}).get("id", "")
        if not sgdb_id:
            # цепочка оборвалась — пробуем по названию, но только точное совпадение:
            # приблизительное даёт чужую игру, а чужая иконка хуже, чем никакой
            r = http.get(f"{SGDB}/search/autocomplete/{quote(игра['name'])}",
                         headers=head)
            строки = r.json().get("data") or [] if r.status_code == 200 else []
            свой = ключ_названия(игра["name"])
            точное = next((s for s in строки
                           if ключ_названия(s["name"]) == свой), None)
            sgdb_id = точное["id"] if точное else ""
        if not sgdb_id:
            # Пустой `igdb_id` — это либо служебная категория (`Special Events`),
            # либо игра, которую Twitch просто ни с чем не связал
            # (`World of Tanks Console`). Разделяем не потому, что вторых
            # не надо привязывать, а потому, что у них другой шанс на успех:
            # искать по названию тут нечего, id взять неоткуда
            (без_igdb if not игра["igdb"] else промахи).append(игра["name"])
            continue
        r = http.get(f"{SGDB}/icons/game/{sgdb_id}", headers=head)
        url = лучшая_иконка(r.json().get("data") or []) if r.status_code == 200 else ""
        игра["sgdb"] = sgdb_id
        if url:
            игра["icon"] = url
            нашли += 1
        else:
            пустые.append(f"{игра['name']} (sgdb {sgdb_id})")
    return нашли, {"нет на SteamGridDB — привязать руками": промахи,
                   "есть, но иконок не залито — нужен запасной рисунок": пустые,
                   "без igdb_id у Twitch — среди них есть и игры, и служебные категории":
                       без_igdb}


def main() -> None:
    ap = argparse.ArgumentParser(description="Собрать базу игр Twitch")
    ap.add_argument("--icons", action="store_true",
                    help="искать иконки на SteamGridDB (брошено, по умолчанию нет)")
    args = ap.parse_args()

    k = ключи()
    tok = токен(k)
    head = {"Client-Id": k["tw_id"], "Authorization": f"Bearer {tok}"}

    with httpx.Client(headers=head, timeout=60) as http:
        игры = каталог(http)
        print("история «Вахты»:")
        нужны = свои(игры)
        print(f"  игр с дропсами за всё время: {len(нужны)}")

        нашли, промахи = 0, {}
        if args.icons:
            print("иконки:")
            нашли, промахи = иконки(http, k, tok, игры, нужны)
            print(f"  иконка нашлась у {нашли} из {len(нужны)}")

    # Найденное прошлым разом переносим, даже когда иконки не искали: рабочий
    # файл пишется заново каждый запуск, и обычный обход каталога иначе стирал бы
    # шестьдесят шесть адресов, которые собирались вручную и по одному
    if РАБОЧАЯ.exists():
        прежние = {g["id"]: g for g in
                   json.loads(РАБОЧАЯ.read_text("utf8")).get("games", [])}
        for gid, игра in игры.items():
            было = прежние.get(gid, {})
            for поле in ("icon", "sgdb"):
                if было.get(поле) and not игра.get(поле):
                    игра[поле] = было[поле]

    строки = sorted(игры.values(), key=lambda g: g["name"].lower())
    РАБОЧАЯ.write_text(json.dumps(
        {"built": date.today().isoformat(), "games": строки},
        ensure_ascii=False, indent=1), "utf8")

    # к человеку едет только нужное человеку: адреса иконок и id SteamGridDB —
    # это наша кухня, и в базе на каждой машине им делать нечего
    лёгкие = [{"id": g["id"], "name": g["name"], "art": g["art"]} for g in строки]
    ГОТОВАЯ.write_bytes(gzip.compress(json.dumps(
        {"built": date.today().isoformat(), "games": лёгкие},
        ensure_ascii=False, separators=(",", ":")).encode("utf8"), 9))

    print(f"\n{РАБОЧАЯ.relative_to(КОРЕНЬ)} — {РАБОЧАЯ.stat().st_size / 1_000_000:.1f} МБ")
    print(f"{ГОТОВАЯ.relative_to(КОРЕНЬ)} — "
          f"{ГОТОВАЯ.stat().st_size / 1000:.0f} КБ, {len(лёгкие)} игр")
    # Имя с датой нужно «Вахте»: по нему она сравнивает базу в релизе со своей
    # (см. `vahta/releases.py`). Релиз `games` — отдельный пререлиз, чтобы
    # не стать «последним» вместо версии приложения
    имя = f"games-{date.today().isoformat()}.json.gz"
    print(f"\nРаздать: cp {ГОТОВАЯ.relative_to(КОРЕНЬ)} {имя}")
    print(f"  gh release upload games {имя} --clobber")
    print('  (первый раз: gh release create games --prerelease --title "База игр" '
          '--notes "База игр Twitch для подсказок")')
    for заголовок, список in промахи.items():
        if список:
            print(f"\n{заголовок} ({len(список)}):")
            for line in sorted(список):
                print(f"  {line}")


if __name__ == "__main__":
    main()
