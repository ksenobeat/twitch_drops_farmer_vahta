"""Локальная база: история фарма и статистика доступности эфира.

Twitch помнит инвентарь плохо и недолго, поэтому всё, что было отфармлено,
копим у себя. Заодно с первого дня собираем, когда по игре вообще есть
живой канал с дропсами — без этих данных планировщик не сможет отличить
«успею» от «не успею».
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .drops import Campaign, Drop

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS game (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    box_art  TEXT
);

CREATE TABLE IF NOT EXISTS campaign (
    id         TEXT PRIMARY KEY,
    game_id    TEXT NOT NULL REFERENCES game(id),
    name       TEXT NOT NULL,
    starts_at  TEXT,
    ends_at    TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

-- имя не 'drop': это зарезервированное слово SQL
-- `benefit_ids` — id наград за дроп. Единственный мост между инвентарём Twitch
-- и игрой: у награды в инвентаре `game` всегда пустой, а id совпадает с id
-- benefit'а, и его игру мы знаем по кампании
CREATE TABLE IF NOT EXISTS drop_item (
    id               TEXT PRIMARY KEY,
    campaign_id      TEXT NOT NULL REFERENCES campaign(id),
    name             TEXT NOT NULL,
    required_minutes INTEGER NOT NULL,
    benefits         TEXT NOT NULL DEFAULT '[]',
    benefit_ids      TEXT NOT NULL DEFAULT '[]'
);

-- то, что реально забрано; строка появляется один раз и не меняется
CREATE TABLE IF NOT EXISTS claim (
    drop_id     TEXT PRIMARY KEY REFERENCES drop_item(id),
    campaign_id TEXT NOT NULL,
    game_id     TEXT NOT NULL,
    claimed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drop_progress (
    drop_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    current_minutes INTEGER NOT NULL,
    claimed INTEGER NOT NULL DEFAULT 0,
    granted INTEGER NOT NULL DEFAULT 0
);

-- инвентарь Twitch, своей копией. Ради того же, ради чего и всё остальное
-- в этой базе: Twitch помнит инвентарь плохо и недолго, а без сети или
-- с протухшим токеном не отдаёт вовсе — и тогда экран показывал бы пустоту
-- вместо того, что у человека есть.
--
-- `id` — это id benefit'а кампании: своего id у награды в инвентаре нет.
-- `awarded_at` — последняя выдача (`lastAwardedAt`), а `count` — за всё время,
-- поэтому «когда получена впервые» отсюда узнать нельзя, и врать об этом нельзя.
-- `seen` — показывали ли награду человеку: из него и получается «что нового
-- с прошлого раза»
--
-- `game_note` — игра, вписанная руками. Автоматика знает игру только у наград
-- из кампаний, которые «Вахта» когда-то видела: после одного обхода это 20
-- наград из 137, остальные из кампаний, которых давно нет. Человек помнит
-- лучше, поэтому его пометка сильнее подстановки и не стирается обходами.
CREATE TABLE IF NOT EXISTS reward (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    image      TEXT NOT NULL DEFAULT '',
    awarded_at TEXT,
    count      INTEGER NOT NULL DEFAULT 1,
    linked     INTEGER NOT NULL DEFAULT 0,
    link_url   TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 0,
    game_note  TEXT NOT NULL DEFAULT ''
);

-- сколько минут и на каком канале отсмотрено
CREATE TABLE IF NOT EXISTS watch_session (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    channel     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    minutes     INTEGER NOT NULL DEFAULT 0
);

-- игры, за которыми следим; ключ — id игры, а не название:
-- «Overwatch 2» в каталоге и «Overwatch» в кампании — одна и та же игра
CREATE TABLE IF NOT EXISTS watchlist (
    game_id   TEXT PRIMARY KEY,
    game_name TEXT NOT NULL,
    slug      TEXT NOT NULL,
    priority  INTEGER NOT NULL,
    added_at  TEXT NOT NULL
);

-- `available` — был ли канал, на котором можно фармить **нужную кампанию**;
-- `air` — был ли по игре эфир вообще. Это разные вещи, и путать их дорого:
-- у Overwatch стримы идут круглосуточно, а обе его кампании — только на своих
-- каналах, и «доступность 0» читалась как «игру никто не стримит».
-- NULL в `air` — замер из версии, которая эфир не мерила.
CREATE TABLE IF NOT EXISTS uptime_sample (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id    TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    available  INTEGER NOT NULL,
    air        INTEGER
);

-- кампании, от которых отказались при разборе конфликта
CREATE TABLE IF NOT EXISTS skipped (
    campaign_id TEXT PRIMARY KEY,
    decided_at  TEXT NOT NULL
);

-- кампании, отложенные до срока: Twitch считает время, но дроп не отдаёт.
-- Отличается от skipped тем, что решение временное и приняла его программа
CREATE TABLE IF NOT EXISTS held (
    campaign_id TEXT PRIMARY KEY,
    until       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_claim_game ON claim(game_id, claimed_at);
CREATE INDEX IF NOT EXISTS ix_session_game ON watch_session(game_id, started_at);
CREATE INDEX IF NOT EXISTS ix_uptime_game ON uptime_sample(game_id, sampled_at);
"""


# Схема умеет только создавать то, чего нет: у уже установленных «Вахт» таблицы
# созданы прошлой версией, и новый столбец в CREATE TABLE до них не доедет.
# Поэтому столбцы, добавленные позже, доливаются здесь — по одной строке
# на столбец, и каждая должна пережить повторный запуск.
MIGRATIONS = [
    ("uptime_sample", "air", "ALTER TABLE uptime_sample ADD COLUMN air INTEGER"),
    (
        "drop_item",
        "benefit_ids",
        "ALTER TABLE drop_item ADD COLUMN benefit_ids TEXT NOT NULL DEFAULT '[]'",
    ),
    ("reward", "game_note", "ALTER TABLE reward ADD COLUMN game_note TEXT NOT NULL DEFAULT ''"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, statement in MIGRATIONS:
            known = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in known:
                self.db.execute(statement)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- запись ----------

    def knows_campaign(self, campaign_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM campaign WHERE id = ?", (campaign_id,)
        ).fetchone()
        return row is not None

    def game_names(self) -> list[str]:
        """Названия всех известных игр — для подсказки при вводе.

        Отдаём списком целиком, а не по букве запроса: четыре тысячи названий
        это шестьдесят килобайт, один раз за сеанс, а поиск по мере ввода
        браузер делает сам и без сети. Ручка «дай по префиксу» стоила бы
        запроса на каждую букву ровно затем же.
        """
        return [row["name"] for row in
                self.db.execute("SELECT name FROM game ORDER BY name")]

    def add_games(self, rows: list[tuple[str, str, str]]) -> int:
        """Влить каталог игр: (id, название, обложка). Сколько прибавилось.

        Своего не трогаем. Название, которое клиент взял из живой кампании,
        остаётся: у одной игры имена в каталоге и в кампании расходятся
        («Overwatch 2» против «Overwatch»), и то, что человек видел на экране,
        важнее слепка. Обложка заполняется только когда своей нет — отсюда
        `COALESCE` в обратном против `remember` порядке.

        Всё одной транзакцией: база открыта в автокоммите, и четыре тысячи
        строк превратились бы в четыре тысячи записей на диск.
        """
        before = self.db.execute("SELECT COUNT(*) FROM game").fetchone()[0]
        self.db.execute("BEGIN")
        try:
            self.db.executemany(
                "INSERT INTO game (id, name, box_art) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "box_art = COALESCE(NULLIF(game.box_art, ''), excluded.box_art)",
                rows,
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.db.execute("SELECT COUNT(*) FROM game").fetchone()[0] - before

    def remember(self, campaign: Campaign, box_art: str = "") -> bool:
        """Запомнить кампанию и её дропы. True, если видим её впервые."""
        now = _now()
        is_new = not self.knows_campaign(campaign.id)
        box_art = box_art or campaign.box_art
        self.db.execute(
            "INSERT INTO game (id, name, box_art) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
            "box_art = COALESCE(NULLIF(excluded.box_art, ''), game.box_art)",
            (campaign.game_id, campaign.game, box_art),
        )
        self.db.execute(
            "INSERT INTO campaign (id, game_id, name, starts_at, ends_at, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen, "
            "name = excluded.name, ends_at = excluded.ends_at",
            (
                campaign.id,
                campaign.game_id,
                campaign.name,
                _iso(campaign.starts_at),
                _iso(campaign.ends_at),
                now,
                now,
            ),
        )
        self.db.executemany(
            "INSERT INTO drop_item "
            "(id, campaign_id, name, required_minutes, benefits, benefit_ids) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
            "required_minutes = excluded.required_minutes, benefits = excluded.benefits, "
            # пустым не перетираем: инвентарь отдаёт дропы без benefit'ов,
            # и знание, добытое из деталей кампании, терять из-за этого незачем
            "benefit_ids = COALESCE(NULLIF(excluded.benefit_ids, '[]'), drop_item.benefit_ids)",
            [
                (
                    d.id,
                    campaign.id,
                    d.name,
                    d.required_minutes,
                    json.dumps(d.benefits),
                    json.dumps(d.benefit_ids),
                )
                for d in campaign.drops
            ],
        )
        return is_new

    # ---------- вотчлист ----------

    def watchlist(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT w.*, g.box_art FROM watchlist w "
            "LEFT JOIN game g ON g.id = w.game_id "
            "ORDER BY w.priority, w.game_name"
        ).fetchall()

    def watch_add(self, game_id: str, name: str, slug: str) -> bool:
        """Добавить игру в конец вотчлиста. False, если она уже там."""
        row = self.db.execute(
            "SELECT COALESCE(MAX(priority), 0) + 1 AS next FROM watchlist"
        ).fetchone()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO watchlist (game_id, game_name, slug, priority, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (game_id, name, slug, row["next"], _now()),
        )
        return cur.rowcount > 0

    def watch_find(self, name: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM watchlist WHERE game_id = ? OR game_name = ? COLLATE NOCASE",
            (name, name),
        ).fetchone()

    def watch_remove(self, game_id: str) -> bool:
        cur = self.db.execute("DELETE FROM watchlist WHERE game_id = ?", (game_id,))
        return cur.rowcount > 0

    def watch_move(self, game_id: str, position: int) -> None:
        """Поставить игру на нужное место, остальные сдвинуть."""
        order = [row["game_id"] for row in self.watchlist() if row["game_id"] != game_id]
        order.insert(max(0, position - 1), game_id)
        self.db.executemany(
            "UPDATE watchlist SET priority = ? WHERE game_id = ?",
            [(i, gid) for i, gid in enumerate(order, start=1)],
        )

    def record_claim(self, campaign: Campaign, drop: Drop) -> bool:
        """Отметить дроп забранным. False, если он уже был записан раньше."""
        self.remember(campaign)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO claim (drop_id, campaign_id, game_id, claimed_at) "
            "VALUES (?, ?, ?, ?)",
            (drop.id, campaign.id, campaign.game_id, _now()),
        )
        return cur.rowcount > 0

    def restore_progress(self, campaign: Campaign) -> None:
        """Twitch omits completed campaigns; exact drop IDs retain our evidence."""
        saved = {row['drop_id']: row for row in self.db.execute(
            'SELECT * FROM drop_progress WHERE campaign_id = ?', (campaign.id,))}
        claimed = {row[0] for row in self.db.execute(
            'SELECT drop_id FROM claim WHERE campaign_id = ?', (campaign.id,))}
        for drop in campaign.drops:
            if row := saved.get(drop.id):
                drop.current_minutes = max(drop.current_minutes, row['current_minutes'])
                drop.claimed = drop.claimed or bool(row['claimed'])
                drop.granted = drop.granted or bool(row['granted'])
            drop.claimed = drop.claimed or drop.id in claimed

    def remember_progress(self, campaign: Campaign) -> None:
        self.db.executemany(
            'INSERT INTO drop_progress VALUES (?, ?, ?, ?, ?) '
            'ON CONFLICT(drop_id) DO UPDATE SET '
            'current_minutes = MAX(drop_progress.current_minutes, excluded.current_minutes), '
            'claimed = MAX(drop_progress.claimed, excluded.claimed), '
            'granted = MAX(drop_progress.granted, excluded.granted)',
            [(d.id, campaign.id, d.current_minutes, int(d.claimed), int(d.granted))
             for d in campaign.drops if d.current_minutes or d.claimed])

    def session_start(self, campaign: Campaign, channel: str) -> int:
        now = _now()
        cur = self.db.execute(
            "INSERT INTO watch_session (game_id, campaign_id, channel, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (campaign.game_id, campaign.id, channel, now, now),
        )
        return int(cur.lastrowid or 0)

    def session_tick(self, session_id: int, minutes: int = 1) -> None:
        self.db.execute(
            "UPDATE watch_session SET minutes = minutes + ?, ended_at = ? WHERE id = ?",
            (minutes, _now(), session_id),
        )

    def record_uptime(self, game_id: str, available: bool, air: bool | None = None) -> None:
        """Замер: `available` — можно ли фармить нужную кампанию, `air` — был ли
        по игре эфир вообще. `None` — эфир не мерили, и врать об этом не надо."""
        self.db.execute(
            "INSERT INTO uptime_sample (game_id, sampled_at, available, air) "
            "VALUES (?, ?, ?, ?)",
            (game_id, _now(), int(available), None if air is None else int(air)),
        )

    # ---------- чтение ----------

    def games(self) -> list[sqlite3.Row]:
        """Сводка по играм: сколько забрано, сколько часов ушло."""
        return self.db.execute(
            """
            SELECT g.id, g.name, g.box_art,
                   (SELECT COUNT(*) FROM claim c WHERE c.game_id = g.id) AS drops,
                   (SELECT COUNT(DISTINCT c.campaign_id) FROM claim c
                     WHERE c.game_id = g.id) AS campaigns,
                   (SELECT COALESCE(SUM(s.minutes), 0) FROM watch_session s
                     WHERE s.game_id = g.id) AS minutes,
                   (SELECT MAX(c.claimed_at) FROM claim c WHERE c.game_id = g.id) AS last_claim
              FROM game g
             WHERE drops > 0 OR minutes > 0
             ORDER BY drops DESC, minutes DESC
            """
        ).fetchall()

    def claims(self, game_id: str | None = None, since: str | None = None) -> list[sqlite3.Row]:
        """Забранные дропы, свежие сверху. `since` — с какого момента (ISO)."""
        sql = """
            SELECT c.claimed_at, d.name AS drop_name, d.required_minutes,
                   cm.name AS campaign_name, g.name AS game_name, c.game_id
              FROM claim c
              JOIN drop_item d ON d.id = c.drop_id
              JOIN campaign cm ON cm.id = c.campaign_id
              JOIN game g ON g.id = c.game_id
        """
        where, params = [], []
        if game_id:
            where.append("c.game_id = ?")
            params.append(game_id)
        if since:
            where.append("c.claimed_at >= ?")
            params.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY c.claimed_at DESC"
        return self.db.execute(sql, tuple(params)).fetchall()

    # ---------- инвентарь ----------

    def remember_rewards(self, rewards: list) -> list[str]:
        """Обновить свою копию инвентаря. Возвращает id тех наград, что новые.

        Новой считается та, которой у нас не было, и та, у которой выросло
        `count`: «Twitch Shards ×12» вместо ×11 — это тоже новая награда,
        просто с прежним именем.

        **Первое знакомство новостями не считается.** Когда таблица пуста, мы
        видим не 137 новых наград, а исходное состояние: человек их давно
        получил, и рассказывать ему о них — значит соврать в первый же запуск.
        """
        now = _now()
        first_run = not self.db.execute("SELECT 1 FROM reward LIMIT 1").fetchone()
        known = {
            row["id"]: row["count"]
            for row in self.db.execute("SELECT id, count FROM reward")
        }
        fresh = [
            item.id
            for item in rewards
            if not first_run and item.count > known.get(item.id, 0)
        ]
        self.db.executemany(
            "INSERT INTO reward "
            "(id, name, image, awarded_at, count, linked, link_url, "
            " first_seen, last_seen, seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
            "image = COALESCE(NULLIF(excluded.image, ''), reward.image), "
            "awarded_at = excluded.awarded_at, count = excluded.count, "
            "linked = excluded.linked, link_url = excluded.link_url, "
            "last_seen = excluded.last_seen, "
            # выросло количество — награда снова новая, даже если её уже видели
            "seen = CASE WHEN excluded.count > reward.count THEN 0 ELSE reward.seen END",
            [
                (
                    item.id,
                    item.name,
                    item.image,
                    _iso(item.awarded_at),
                    item.count,
                    int(item.linked),
                    item.link_url,
                    now,
                    now,
                    int(first_run),
                )
                for item in rewards
            ],
        )
        return fresh

    def rewards(self) -> list[sqlite3.Row]:
        """Своя копия инвентаря, свежие сверху. Без даты выдачи — в конце."""
        return self.db.execute(
            "SELECT * FROM reward ORDER BY awarded_at IS NULL, awarded_at DESC, name"
        ).fetchall()

    def unseen_rewards(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM reward WHERE seen = 0 ORDER BY awarded_at DESC"
        ).fetchall()

    def mark_rewards_seen(self) -> int:
        cur = self.db.execute("UPDATE reward SET seen = 1 WHERE seen = 0")
        return cur.rowcount

    def name_reward_game(self, reward_id: str, game: str) -> bool:
        """Вписать игру награде руками. Пустая строка снимает пометку.

        Живёт рядом с наградой, а не вместо подстановки: обход её не трогает
        (`remember_rewards` этот столбец не перечисляет), и человек всегда
        может вернуть автоматику, стерев поле.
        """
        cur = self.db.execute(
            "UPDATE reward SET game_note = ? WHERE id = ?", (game.strip()[:80], reward_id)
        )
        return cur.rowcount > 0

    def games_by_benefit(self) -> dict[str, tuple[str, str]]:
        """id награды -> (id игры, название игры).

        Инвентарь Twitch не говорит, к какой игре относится награда: `game`
        у всех до единой пустой. Но id награды совпадает с id benefit'а дропа,
        а его игру мы знаем по кампании, которую когда-то видели. Отсюда и
        предел: подставится игра только у наград из знакомых кампаний.
        """
        out: dict[str, tuple[str, str]] = {}
        for row in self.db.execute(
            "SELECT d.benefit_ids, c.game_id, g.name FROM drop_item d "
            "JOIN campaign c ON c.id = d.campaign_id "
            "JOIN game g ON g.id = c.game_id"
        ):
            for benefit in json.loads(row["benefit_ids"] or "[]"):
                out[benefit] = (row["game_id"], row["name"])
        return out

    def totals(self) -> sqlite3.Row:
        return self.db.execute(
            """
            SELECT (SELECT COUNT(*) FROM claim) AS drops,
                   (SELECT COUNT(DISTINCT game_id) FROM claim) AS games,
                   (SELECT COALESCE(SUM(minutes), 0) FROM watch_session) AS minutes,
                   (SELECT COALESCE(SUM(minutes), 0) FROM watch_session
                     WHERE started_at >= ?) AS minutes_week
            """,
            ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds"),),
        ).fetchone()

    # ---------- решения по конфликтам ----------

    def skip(self, campaign_id: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO skipped (campaign_id, decided_at) VALUES (?, ?)",
            (campaign_id, _now()),
        )

    def unskip(self, campaign_id: str) -> bool:
        cur = self.db.execute("DELETE FROM skipped WHERE campaign_id = ?", (campaign_id,))
        return cur.rowcount > 0

    def skipped(self) -> set[str]:
        return {
            row["campaign_id"] for row in self.db.execute("SELECT campaign_id FROM skipped")
        }

    def hold(self, campaign_id: str, minutes: int, reason: str = "") -> str:
        """Отложить кампанию до срока: смотреть её сейчас бессмысленно.

        Не то же самое, что `skip`: там человек отказался насовсем, здесь
        программа сама решила подождать — и сама вернётся, когда срок выйдет.
        """
        until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(
            timespec="seconds"
        )
        self.db.execute(
            "INSERT OR REPLACE INTO held (campaign_id, until, reason) VALUES (?, ?, ?)",
            (campaign_id, until, reason),
        )
        return until

    def held(self) -> dict[str, dict]:
        """Отложенные прямо сейчас: id → {until, reason}. Просроченные забываем."""
        self.db.execute("DELETE FROM held WHERE until <= ?", (_now(),))
        return {
            row["campaign_id"]: {"until": row["until"], "reason": row["reason"]}
            for row in self.db.execute("SELECT campaign_id, until, reason FROM held")
        }

    def unhold(self, campaign_id: str) -> bool:
        cur = self.db.execute("DELETE FROM held WHERE campaign_id = ?", (campaign_id,))
        return cur.rowcount > 0

    def box_art(self, game_id: str) -> str:
        row = self.db.execute(
            "SELECT box_art FROM game WHERE id = ?", (game_id,)
        ).fetchone()
        return (row["box_art"] or "") if row else ""

    def sample_count(self, game_id: str, days: int = 14) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM uptime_sample WHERE game_id = ? AND sampled_at >= ?",
            (game_id, since),
        ).fetchone()
        return int(row["n"])

    def availability(self, game_id: str, days: int = 14) -> float:
        """Доля времени, когда по игре был канал с нужной кампанией.
        -1, если данных нет.

        Это и есть основа прогноза для планировщика: календарного времени до
        дедлайна почти всегда хватает, а вот эфирного — далеко не всегда.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        row = self.db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(available), 0) AS up "
            "FROM uptime_sample WHERE game_id = ? AND sampled_at >= ?",
            (game_id, since),
        ).fetchone()
        return row["up"] / row["n"] if row["n"] else -1.0

    def air(self, game_id: str, days: int = 14) -> float:
        """Доля времени, когда по игре был эфир вообще. -1, если не мерили.

        От `availability` отличается тем, что не спрашивает про кампанию. Разница
        между ними — это и есть «стримят, но не то»: у Overwatch эфир
        круглосуточный, а обе кампании идут только на своих каналах. Пока эти два
        факта были одним числом, ноль читался как «игру никто не стримит»,
        и план обещал фармить её по 14 минут в сутки до самого дедлайна.

        Считается по замерам, где эфир мерили: `COUNT(air)` пропускает NULL,
        то есть замеры версий, которые этого не умели.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        row = self.db.execute(
            "SELECT COUNT(air) AS n, COALESCE(SUM(air), 0) AS up "
            "FROM uptime_sample WHERE game_id = ? AND sampled_at >= ?",
            (game_id, since),
        ).fetchone()
        return row["up"] / row["n"] if row["n"] else -1.0
