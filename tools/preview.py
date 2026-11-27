"""Посмотреть на интерфейс, не поднимая фарм.

Настоящее окно и настоящий `index.html`, но вместо Twitch — выдуманный JSON.
Нужно, чтобы вёрстку можно было проверить на непустых данных: службу ради этого
поднимать не надо, аккаунт Twitch не трогается, ничего не пишется в базу.

Ради чего именно: у экранов есть состояния, до которых на живых данных
не дотянуться по заказу — конфликт дедлайнов, кампания без привязанного
аккаунта, игра без замеров, пауза. Здесь они просто лежат в `FAKE`.

    env\\Scripts\\python.exe tools\\preview.py
    env\\Scripts\\python.exe tools\\preview.py --serve

Правь `FAKE`, чтобы увидеть другое состояние. Закрывается своим крестиком.

С `--serve` окна нет — поднимается только выдуманный сервер, и страницу можно
открыть в любом браузере. Титульной строки там не будет (её вешает мост
pywebview), но разметку и числа так можно мерить чем угодно.
"""

import http.server
import json
import sys
import threading
from pathlib import Path

import webview

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

from vahta.app import TitlebarApi  # noqa: E402
from vahta.window import Frame  # noqa: E402

PORT = 8477
ROOT = str(КОРЕНЬ / "vahta" / "static")

NOW = {
    "game": "THE FINALS", "campaign": "S11 LAUNCH DROPS", "channel": "sha0lelu",
    "viewers": 812, "current": 178, "required": 240, "boxArt": "", "note": "в эфире",
    "farming": True,
    # эти три поля служба присылает всегда; без них правая колонка «Сейчас»
    # падала на выдуманных данных, а сам экран оставался пустым
    "paused_since": "", "paused_until": "",
    # лента уведомлений: первый опрос её только запоминает, поэтому тост
    # из превью сам не выскочит — в браузере зови toast("Дроп забран", "…")
    "events": [],
    "today": [
        {"game": "THE FINALS", "game_id": "1", "box_art": "", "drop": "S11 Weapon Charm",
         "at": "2026-07-29T16:40:00+03:00"},
        {"game": "Overwatch", "game_id": "2", "box_art": "", "drop": "OWCS Souvenir Pack",
         "at": "2026-07-29T13:14:00+03:00"},
    ],
    "shell": {
        "shift": 13200,          # три часа сорок
        "traffic": 4.2,
        "deadline": "2026-07-30T09:00:00+03:00",
        "counts": {"sched": 1, "games": 10, "cat": 105, "inv": 2},
        "twitch": "drops_farm",
        "telegram": True,
        "theme": "system",
        # False — и на старте поздоровается мастер первого запуска
        "onboarded": True,
        "version": "0.1.0",
        # непустое — и в статусной строке появится плашка «вышла версия»
        "update": "",
        # куда ведёт плашка: адрес инсталлера с сервера
        "update_url": "",
        # True вместе с непустым `update` — в «Настройках» панель обновлений
        # покажет «ждёт паузы в фарме», а плашка — «ставлю версию»
        "update_ready": False,
    },
}
FAKE = {
    "/api/now": NOW,
    "/api/plan": {
        "starts_at": "2026-07-29T21:48:00+03:00",
        "ends_at": "2026-08-12T21:48:00+03:00", "feasible": False,
        # Блоки пересекаются, и так и должно быть: эфир делится, а не
        # резервируется — все кампании идут с самого начала, каждая со своей
        # долей календаря (`minutes` = span × share). Кампания с редким эфиром
        # тянется неделю, но занимает в ней свои десять процентов
        "blocks": [
            {"game": "THE FINALS", "game_id": "1", "box_art": "", "campaign": "S11 LAUNCH DROPS",
             "start": "2026-07-29T21:48:00+03:00", "end": "2026-07-30T05:04:00+03:00",
             "minutes": 240, "share": 0.55},
            {"game": "Overwatch", "game_id": "2", "box_art": "", "campaign": "OWCS MSC Day 1",
             "start": "2026-07-29T21:48:00+03:00", "end": "2026-07-30T06:58:00+03:00",
             "minutes": 110, "share": 0.2},
            {"game": "Marvel Rivals", "game_id": "3", "box_art": "", "campaign": "Ignite Stage 1",
             "start": "2026-07-29T21:48:00+03:00", "end": "2026-07-31T13:48:00+03:00",
             "minutes": 360, "share": 0.15},
            {"game": "Stalzone", "game_id": "5", "box_art": "", "campaign": "Summer 2026, Week 3",
             "start": "2026-07-29T21:48:00+03:00", "end": "2026-08-05T03:48:00+03:00",
             "minutes": 900, "share": 0.1},
        ],
        "tasks": [
            {"game": "THE FINALS", "game_id": "1", "box_art": "", "campaign": "S11 LAUNCH DROPS",
             "minutes": 240, "release": "2026-07-20T00:00:00+03:00",
             "deadline": "2026-08-02T22:00:00+03:00", "availability": 0.8, "confident": True},
            {"game": "Overwatch", "game_id": "2", "box_art": "", "campaign": "OWCS MSC Day 1",
             "minutes": 110, "release": "2026-07-20T00:00:00+03:00",
             # запас в три часа — это «в обрез»: порог шесть. Меньше нуля здесь
             # быть не может, иначе кампания была бы в `shortfalls`
             "deadline": "2026-07-30T09:00:00+03:00", "availability": 0.7, "confident": True},
            {"game": "Stalzone", "game_id": "5", "box_art": "", "campaign": "Summer 2026, Week 3",
             "minutes": 900, "release": "2026-07-20T00:00:00+03:00",
             "deadline": "2026-08-06T12:00:00+03:00", "availability": 0.25, "confident": True},
            {"game": "Marvel Rivals", "game_id": "3", "box_art": "", "campaign": "Ignite Stage 1",
             "minutes": 360, "release": "2026-07-20T00:00:00+03:00",
             "deadline": "2026-08-02T20:00:00+03:00", "availability": 0.6, "confident": False},
            {"game": "Escape from Tarkov", "game_id": "4", "box_art": "", "campaign": "Streamer Drops",
             "minutes": 840, "release": "2026-07-20T00:00:00+03:00",
             "deadline": "2026-08-01T12:00:00+03:00", "availability": 0.3, "confident": True},
        ],
        "shortfalls": [
            {"game": "Escape from Tarkov", "campaign": "Streamer Drops",
             "deadline": "2026-08-01T12:00:00+03:00", "missing": 360,
             # «не влезает» — про последнюю веху: два дропа из трёх и так наши
             "reachable": 2, "drops": 3,
             "competitors": ["Delta Force"],
             # Стороны вилки «что важнее» — числами, слова навешивает экран.
             # Сторон ровно столько, сколько правда меняют исход: каждая
             # проверена пересчётом. Отказа от самой «Streamer Drops» здесь нет
             # и быть не может — два дропа из трёх и так будут забраны
             "options": [
                 {"drop_id": "a", "drop_campaign": "Season 6", "drop_game": "Delta Force",
                  "gain": 3, "keep": 2, "total": 3, "rival_keeps": 4, "rival_total": 4},
             ]},
        ],
        # Фармить нельзя вовсе: по замерам канала с этой кампанией не бывает.
        # В плане её нет намеренно, но видно её и причину — по ней понятно,
        # есть ли смысл ждать
        "waiting": [
            {"game": "Overwatch", "game_id": "2", "box_art": "", "campaign": "OW 2026 S3 Midseason",
             "minutes": 600, "deadline": "2026-08-04T09:58:00+03:00",
             "reason": "идёт только на своих каналах, и их нет в эфире"},
        ],
        "unconfident": ["Marvel Rivals"],
        "unlinked": [
            {"game": "Delta Force", "game_id": "9", "box_art": "", "campaign": "Season 6",
             "minutes": 180, "deadline": "2026-08-10T12:00:00+03:00",
             "link": "https://www.twitch.tv/drops/campaigns"},
        ],
        "done": [
            {"game": "Rust", "game_id": "8", "box_art": "", "campaign": "Twitch Rivals",
             "minutes": 0, "deadline": "2026-08-05T12:00:00+03:00", "link": ""},
        ],
    },
    "/api/settings": {"notifications": True, "close_to_tray": True,
                      "window_on_autostart": False, "autostart": True,
                      "onboarded": True, "theme": "system",
                      "farm_unlinked": False, "auto_update": True,
                      "available": True, "windowed": True},
    # игры, по которым на аккаунте уже идёт прогресс: шаг «игры» в мастере
    # первого запуска предлагает их списком. Две уже в вотчлисте — так видно,
    # что предлагается только новое
    "/api/inventory/games": {"games": [
        {"game_id": "2", "name": "Overwatch", "slug": "overwatch-2", "box_art": "",
         "campaigns": 2, "watched": True},
        {"game_id": "3", "name": "Marvel Rivals", "slug": "marvel-rivals", "box_art": "",
         "campaigns": 4, "watched": True},
        {"game_id": "8", "name": "Arena Breakout: Infinite",
         "slug": "arena-breakout-infinite", "box_art": "", "campaigns": 1,
         "watched": False},
        {"game_id": "11", "name": "League of Legends", "slug": "league-of-legends",
         "box_art": "", "campaigns": 1, "watched": False},
    ]},
    "/api/telegram": {"configured": True, "hint": "…4242", "owner": 1,
                      "username": "vahta_bot", "running": True},
    # три состояния входа. Чтобы посмотреть ожидание кода — убрать login и
    # раскомментировать pending; истёкший код (login-error.png) — login пустой,
    # pending нет, error «Код истёк, начни заново»
    "/api/twitch": {"login": "drops_farm", "user_id": "12345678", "pending": None, "error": ""},
    # "/api/twitch": {"login": "", "user_id": "", "error": "", "pending": {
    #     "user_code": "HXRT LQ9F", "verification_uri": "https://twitch.tv/activate",
    #     "expires_at": "2026-07-29T21:39:00+03:00"}},
    "/api/watchlist": [
        {"game_id": "2", "name": "Overwatch", "box_art": "", "priority": 1,
         "availability": 0.95, "samples": 22, "added": "2026-07-01T10:00:00+00:00"},
        {"game_id": "4", "name": "Escape from Tarkov", "box_art": "", "priority": 2,
         "availability": 0.3, "samples": 14, "added": "2026-07-02T10:00:00+00:00"},
        {"game_id": "1", "name": "THE FINALS", "box_art": "", "priority": 3,
         "availability": 0.87, "samples": 30, "added": "2026-07-03T10:00:00+00:00"},
        {"game_id": "9", "name": "Delta Force", "box_art": "", "priority": 4,
         "availability": 0.62, "samples": 11, "added": "2026-07-04T10:00:00+00:00"},
        {"game_id": "7", "name": "Rust", "box_art": "", "priority": 5,
         "availability": None, "samples": 0, "added": "2026-07-05T10:00:00+00:00"},
        {"game_id": "5", "name": "Stalzone", "box_art": "", "priority": 6,
         "availability": 0.25, "samples": 7, "added": "2026-07-06T10:00:00+00:00"},
        {"game_id": "3", "name": "Marvel Rivals", "box_art": "", "priority": 7,
         "availability": 0.6, "samples": 3, "added": "2026-07-07T10:00:00+00:00"},
        {"game_id": "6", "name": "Zenless Zone Zero", "box_art": "", "priority": 8,
         "availability": 0.4, "samples": 9, "added": "2026-07-08T10:00:00+00:00"},
    ],
    # каталог: привязанные вперемешку с непривязанными — на экране их
    # различает колонка, а не отдельный режим. Rust в этот список не попадает
    # нарочно: он есть в вотчлисте, и строка «активных кампаний нет» берётся
    # оттуда — это состояние иначе не увидеть
    "/api/campaigns": {
        "at": "2026-07-29T21:32:00+03:00",
        "total": 9,
        "campaigns": [
            {"id": "c1", "name": "Biohazard Loot Rush Drops", "game": "Arena Breakout: Infinite",
             "game_id": "11", "box_art": "", "publisher": "Level Infinite", "starts_at": "",
             "ends_at": "2026-08-18T20:00:00+03:00", "linked": True, "link_url": "",
             "details_url": "https://www.twitch.tv/drops/campaigns", "watched": True},
            {"id": "c2", "name": "Season 7 Drops", "game": "Delta Force", "game_id": "9",
             "box_art": "", "publisher": "Team Jade", "starts_at": "",
             "ends_at": "2026-08-10T20:00:00+03:00", "linked": False,
             "link_url": "https://www.deltaforce.com/link", "details_url": "", "watched": True},
            {"id": "c3", "name": "Streamer Drops", "game": "Escape from Tarkov", "game_id": "4",
             "box_art": "", "publisher": "Battlestate Games", "starts_at": "",
             "ends_at": "2026-08-01T12:00:00+03:00", "linked": True, "link_url": "",
             "details_url": "https://www.twitch.tv/drops/campaigns", "watched": True},
            {"id": "c4", "name": "FH · Y10 Season 2 — II", "game": "For Honor", "game_id": "12",
             "box_art": "", "publisher": "Ubisoft", "starts_at": "",
             "ends_at": "2026-08-13T20:00:00+03:00", "linked": True, "link_url": "",
             "details_url": "https://www.twitch.tv/drops/campaigns", "watched": False},
            {"id": "c5", "name": "V4.4 Drops", "game": "Honkai: Star Rail", "game_id": "13",
             "box_art": "", "publisher": "Cognosphere", "starts_at": "",
             "ends_at": "2026-08-12T20:00:00+03:00", "linked": True, "link_url": "",
             "details_url": "https://www.twitch.tv/drops/campaigns", "watched": False},
            {"id": "c6", "name": "Ignite Stage 1 Finals", "game": "Marvel Rivals", "game_id": "3",
             "box_art": "", "publisher": "NetEase", "starts_at": "",
             "ends_at": "2026-08-02T20:00:00+03:00", "linked": True, "link_url": "",
             "details_url": "https://www.twitch.tv/drops/campaigns", "watched": True},
            {"id": "c7", "name": "OW 2026 S3 Midseason", "game": "Overwatch", "game_id": "2",
             "box_art": "", "publisher": "Blizzard", "starts_at": "",
             "ends_at": "2026-07-31T20:00:00+03:00", "linked": True, "link_url": "",
             "details_url": "https://www.twitch.tv/drops/campaigns", "watched": True},
            {"id": "c8", "name": "S11 LAUNCH DROPS", "game": "THE FINALS", "game_id": "1",
             "box_art": "", "publisher": "Nexon", "starts_at": "",
             "ends_at": "2026-08-02T22:00:00+03:00", "linked": True, "link_url": "",
             "details_url": "https://www.twitch.tv/drops/campaigns", "watched": True},
            {"id": "c9", "name": "Summer 2026, Week 3", "game": "Stalzone", "game_id": "5",
             "box_art": "", "publisher": "Vostok Games", "starts_at": "",
             "ends_at": "2026-08-06T12:00:00+03:00", "linked": True, "link_url": "",
             "details_url": "https://www.twitch.tv/drops/campaigns", "watched": True},
        ],
    },
    "/api/inventory": {
        "totals": {"drops": 35, "games": 5, "minutes": 10860, "minutes_week": 1860,
                   "rewards": 6, "new": 2},
        # непустое — Twitch не ответил и показывается своя копия
        "stale": "",
        "at": "2026-07-29T22:40:00+03:00",
        # награды приходят из инвентаря Twitch: у части игра неизвестна вовсе
        # (`game` там всегда пустой), и это состояние иначе не увидеть
        "rewards": [
            {"id": "b1", "name": "OWCS Souvenir Pack", "image": "", "count": 1,
             "at": "2026-07-28T22:14:00+03:00", "linked": True, "link_url": "",
             "game": "Overwatch", "game_id": "2", "new": True},
            {"id": "b2", "name": "Twitch Shards", "image": "", "count": 12,
             "at": "2026-07-27T16:40:00+03:00", "linked": True, "link_url": "",
             "game": "Stalzone", "game_id": "5", "new": True},
            {"id": "b3", "name": "Greenroom Glitch P90", "image": "", "count": 1,
             "at": "2026-06-28T23:10:00+03:00", "linked": False, "link_url": "",
             "game": "", "game_id": ""},
            {"id": "b4", "name": "Ignite Nameplate", "image": "", "count": 2,
             "at": "2026-05-19T23:48:00+03:00", "linked": True, "link_url": "",
             "game": "Marvel Rivals", "game_id": "3"},
            {"id": "b5", "name": "Alligator Skin", "image": "", "count": 1,
             "at": "2025-11-02T14:20:00+03:00", "linked": True, "link_url": "",
             "game": "", "game_id": ""},
            {"id": "b6", "name": "Ration Box", "image": "", "count": 3,
             "at": "", "linked": False, "link_url": "", "game": "", "game_id": ""},
        ],
        "games": [
            {"game_id": "2", "name": "Overwatch", "box_art": "", "drops": 12, "minutes": 3840,
             "campaigns": 4, "claims": [
                 {"name": "OWCS Souvenir Pack", "campaign": "OWCS MSC Day 1",
                  "at": "2026-07-28T22:14:00+03:00", "minutes": 120},
                 {"name": "Player Icon: Kiriko", "campaign": "OWCS MSC Day 1",
                  "at": "2026-07-28T20:02:00+03:00", "minutes": 60},
                 {"name": "Spray: Hanamura", "campaign": "S3 Midseason",
                  "at": "2026-07-24T19:31:00+03:00", "minutes": 180}]},
            {"game_id": "1", "name": "THE FINALS", "box_art": "", "drops": 9, "minutes": 2700,
             "campaigns": 3, "claims": [
                 {"name": "S11 Weapon Charm", "campaign": "S11 LAUNCH DROPS",
                  "at": "2026-07-27T16:40:00+03:00", "minutes": 240}]},
            {"game_id": "5", "name": "Stalzone", "box_art": "", "drops": 6, "minutes": 1920,
             "campaigns": 2, "claims": [
                 {"name": "Ration Box", "campaign": "Summer 2026, Week 2",
                  "at": "2026-07-22T11:05:00+03:00", "minutes": 300}]},
            {"game_id": "3", "name": "Marvel Rivals", "box_art": "", "drops": 5, "minutes": 1380,
             "campaigns": 2, "claims": [
                 {"name": "Ignite Nameplate", "campaign": "Ignite Stage 1",
                  "at": "2026-07-19T23:48:00+03:00", "minutes": 360}]},
            # часы есть, дропов нет — это состояние иначе не увидеть
            {"game_id": "6", "name": "Zenless Zone Zero", "box_art": "", "drops": 3,
             "minutes": 1020, "campaigns": 1, "claims": []},
        ],
    },
}

# Что отвечать на POST: тела не читаем, важен только вид ответа
FAKE_POST = {
    "/api/inventory/export": {"name": "inventory-2026-07-29.csv", "rows": 6},
    "/api/watchlist/inventory": {"added": 2, "limited": False, "limit": 2},
    "/api/inventory/seen": {"seen": 2},
    "/api/data/open": {"ok": True},
    "/api/pause": {"until": "2026-07-29T23:00:00+03:00"},
    "/api/resume": {"ok": True},
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *a):
        pass

    def json(self, payload) -> None:
        body = json.dumps(payload).encode("utf8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in FAKE:
            self.json(FAKE[self.path])
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        # тело не читаем — здесь важно только то, что кнопка получает ответ
        self.json(FAKE_POST.get(self.path, {"ok": True}))


class FakeTray:
    """Ровно то, чем TitlebarApi пользуется, и ничего сверх."""

    window = None
    frame = None


сервер = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

if "--serve" in sys.argv:
    print(f"http://127.0.0.1:{PORT}/index.html")
    сервер.serve_forever()
    sys.exit()

threading.Thread(target=сервер.serve_forever, daemon=True).start()

tray = FakeTray()
window = webview.create_window(
    "Вахта", f"http://127.0.0.1:{PORT}/index.html",
    width=1180, height=820, min_size=(900, 620),
    background_color="#100e17", frameless=True, easy_drag=False,
    js_api=TitlebarApi(tray),
)
tray.window = window
tray.frame = Frame(window)
window.events.shown += lambda *_: tray.frame.dress()
webview.start(private_mode=False)
