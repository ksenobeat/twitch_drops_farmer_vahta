"""Telegram-бот. Работает локально через long polling — вебхук и белый IP не нужны.

Bot API достаточно прост, чтобы обойтись httpx, который уже есть в проекте.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta
from html import escape
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import httpx

from . import drops as dr
from . import planner

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger("vahta")

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 25
# Сколько ждать после отказа Telegram. `api.telegram.org` в этом дата-центре
# отваливается пачками, и без паузы цикл долбил его как мог
RETRY_PAUSE = 10
SPAN_RE = re.compile(r"^\s*(\d+)\s*([чhмm])", re.I)


def _hours(minutes: float) -> str:
    minutes = round(minutes)
    if minutes < 60:
        return f"{minutes} мин"
    rest = minutes % 60
    return f"{minutes // 60} ч" + (f" {rest} м" if rest else "")


def parse_span(text: str) -> timedelta | None:
    """«2ч», «30м», «90m» -> timedelta."""
    match = SPAN_RE.match(text)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(hours=amount) if unit in "чh" else timedelta(minutes=amount)


async def verify(token: str) -> dict | None:
    """Проверить токен через getMe. None — токен не годится."""
    if not token.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(API.format(token=token.strip(), method="getMe"))
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    return data.get("result") if data.get("ok") else None


class Config:
    """Токен и владелец. Владелец — первый, кто напишет /start."""

    def __init__(self, path: Path):
        self.path = path
        data = json.loads(path.read_text("utf8")) if path.exists() else {}
        self.token: str = data.get("token", "")
        self.chat_id: int | None = data.get("chat_id")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"token": self.token, "chat_id": self.chat_id}, indent=2), "utf8"
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def forget(self) -> None:
        self.token = ""
        self.chat_id = None
        self.path.unlink(missing_ok=True)

    @property
    def hint(self) -> str:
        """Хвост токена — показать, что он есть, не показывая его."""
        return f"…{self.token[-6:]}" if self.token else ""


class FarmErrorAlert:
    """One delivered alert per failure; retry failed delivery while it persists."""

    def __init__(self):
        self.clear()

    def clear(self) -> None:
        self.note = ""
        self.sent = False
        self.retry_at = 0.0

    async def notify(self, bot: "Bot | None", note: str) -> None:
        if note != self.note:
            self.clear()
            self.note = note
        if self.sent or bot is None or monotonic() < self.retry_at:
            return
        self.retry_at = monotonic() + 120
        try:
            self.sent = bool(await asyncio.wait_for(bot.farm_error(note), timeout=10))
        except Exception as exc:
            # HTTP exceptions may contain the Telegram token in their URL.
            log.warning("Telegram: уведомление об ошибке не доставлено (%s)",
                        type(exc).__name__)


class FarmProgressAlert:
    """Count time spent on watch attempts across slices of the same farm run."""

    def __init__(self):
        self.alert = FarmErrorAlert()
        self.reset()

    def reset(self):
        self.since: float | None = None
        self.elapsed = 0.0
        self.progress = {}
        self.alert.clear()

    def start_watch(self):
        self.since = monotonic()

    def end_watch(self):
        if self.since is not None:
            self.elapsed += monotonic() - self.since
            self.since = None

    def observe(self, key, current):
        previous = self.progress.get(key)
        self.progress[key] = current
        if self.since is None or (previous is not None and current > previous):
            self.since = monotonic()
            self.elapsed = 0.0
            self.alert.clear()

    async def check(self, bot):
        if self.since is not None and self.elapsed + (monotonic() - self.since) >= 900:
            await self.alert.notify(bot, "За 15 минут попыток просмотра Twitch не подтвердил прогресс. Проверяю каналы дальше.")


class Bot:
    def __init__(self, app: "Service", config: Config):
        self.app = app
        self.config = config
        self.http = httpx.AsyncClient(timeout=POLL_TIMEOUT + 10)
        self.offset = 0
        self.announced: set[str] = set()
        self.username = ""
        # о какой беде уже сказано и сколько раз она повторилась после этого
        self.said: tuple[str, str] | None = None
        self.repeats = 0

    async def close(self) -> None:
        await self.http.aclose()

    async def call(self, method: str, **payload) -> dict | None:
        """Позвать метод API. `None` — Telegram ответил отказом.

        Отличать отказ от пустого ответа обязательно: у `getUpdates` пустой
        результат — это норма («новостей нет»), а отказ — повод подождать.
        Пока и то и другое было пустым словарём, цикл на отказе крутился без
        паузы: в ночь на 30 июля `Bad Gateway` дал под тридцать строк в одну
        секунду, и лог стало нечем читать.
        """
        r = await self.http.post(
            API.format(token=self.config.token, method=method), json=payload
        )
        data = r.json()
        if not data.get("ok"):
            self._trouble(method, str(data.get("description") or r.status_code))
            return None
        self._recovered()
        return data.get("result") or {}

    def _trouble(self, method: str, reason: str) -> None:
        """Сказать о беде один раз, а не по строке на каждую попытку.

        Молчать совсем нельзя — невидимая беда хуже шумной, — поэтому говорим
        о первой и, когда всё кончится, о том, сколько раз она повторилась.
        """
        if (method, reason) == self.said:
            self.repeats += 1
            return
        self._recovered()
        self.said = (method, reason)
        log.warning("Telegram %s: %s", method, reason)

    def _recovered(self) -> None:
        if self.repeats:
            log.warning("Telegram: то же повторилось ещё %d раз", self.repeats)
        self.said = None
        self.repeats = 0

    async def say(self, text: str, buttons: list[list[dict]] | None = None) -> bool:
        """Написать владельцу. Молча ничего не делает, если владельца ещё нет."""
        if not self.config.chat_id:
            return False
        payload: dict = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", **payload) is not None

    # ---------- уведомления ----------

    async def farm_error(self, note: str, *, test: bool = False) -> bool:
        """Only a safe status summary, never raw exceptions or credentials."""
        if test:
            title = "ВАХТА: проверка уведомлений"
            action = "Это тест. Фарм продолжает работать."
        else:
            title = "ВАХТА: фарм требует внимания"
            action = ("Открой настройки ВАХТЫ и войди в Twitch заново."
                      if note == "нужен вход в Twitch" else
                      "Продолжу автоматические попытки восстановления.")
        return await self.say(
            f"<b>{title}</b>\n{escape(note)}\n{action}\n"
            "Текущее состояние: /status."
        )

    async def new_campaign(self, game: str, campaign: dr.Campaign) -> None:
        ends = campaign.ends_at.astimezone().strftime("%d.%m %H:%M") if campaign.ends_at else "?"
        await self.say(
            f"<b>Кампания открылась</b>\n{escape(game)} — {escape(campaign.name)}\n"
            f"До {ends}, {_hours(campaign.remaining_minutes)}, "
            f"{len(campaign.drops)} дропов. Уже в плане."
        )

    async def claimed(self, campaign: dr.Campaign, drop: dr.Drop) -> None:
        await self.say(
            f"<b>{'Пришёл сам' if drop.granted else 'Забрал'}</b>\n"
            f"{escape(drop.name)} — {escape(campaign.game)}\n"
            f"По кампании {sum(d.claimed for d in campaign.drops)}/{len(campaign.drops)}."
        )

    async def new_rewards(self, names: list[str]) -> None:
        """Награды, появившиеся в инвентаре сами.

        О забранном при нашем просмотре говорит `claimed` — сюда попадает только
        то, чего мы не забирали: выдача задним числом, дроп с другого устройства,
        награда за кампанию, которой мы не видели.
        """
        one = len(names) == 1
        await self.say(
            f"<b>{'Новая награда' if one else f'Новых наград: {len(names)}'}</b>\n"
            + "\n".join(escape(name) for name in names[:5])
            + (f"\n…и ещё {len(names) - 5}" if len(names) > 5 else "")
            + f"\n{'Появилась' if one else 'Появились'} в инвентаре Twitch."
        )

    async def conflict(self, gap: planner.Shortfall, plan: planner.Plan) -> None:
        """Показать конфликт и дать кнопки. Решение всегда за пользователем."""
        if gap.task.campaign_id in self.announced:
            return
        self.announced.add(gap.task.campaign_id)
        options = planner.resolutions(gap, plan)
        deadline = gap.task.deadline.astimezone().strftime("%d.%m %H:%M")
        # Нехватка — это про последнюю веху, а не про всю кампанию: дропы это
        # вехи одного потока просмотра, и ближние заберутся как обычно
        lines = [
            f"<b>{'Не успеваем' if gap.empty else 'Влезает не целиком'}: "
            f"{escape(gap.task.campaign)}</b>",
            f"{escape(gap.task.game)}, дедлайн {deadline}.",
            f"Не хватает <b>{_hours(gap.missing_minutes)}</b> эфира.",
        ]
        if gap.total:
            lines.append(
                f"Ни один из {gap.total} дропов не успеет."
                if gap.empty
                else f"Успеем <b>{gap.reachable} из {gap.total}</b> — не влезает последний."
            )
        if gap.competitors:
            lines.append("Окно делят: " + escape(", ".join(gap.competitors)))

        # Вилка «что важнее»: у обеих сторон своя цена, иначе выбор не понятен.
        # Числа те же, что на экране, — слова свои: в чат влезает больше.
        #
        # Начинаем строку с того, чем жертвуем, а не с того, что выигрываем:
        # выигрывает во всех вариантах одна и та же кампания, и варианты,
        # открывавшиеся её именем, читались как один и тот же дважды. Цена обеих
        # сторон остаётся на месте — она тут и есть весь смысл вопроса.
        buttons = []
        if options:
            lines.append("\n<b>Что важнее?</b>")
            for i, option in enumerate(options, start=1):
                lines.append(
                    f"{i}. Перестанем фармить <b>{escape(option.drop_game)}</b> — "
                    f"его {option.rival_keeps} из {option.rival_total} уйдут, "
                    f"зато {escape(gap.task.game)} возьмёт {option.gain} из "
                    f"{option.total} вместо {option.keep}."
                )
                buttons.append(
                    [
                        {
                            # У инлайн-кнопки одна строка, и цену обеих сторон
                            # в неё не уместить — в отличие от экрана, где она
                            # едет второй строкой внутри кнопки. Значит подпись
                            # должна называть то, чем варианты различаются, то
                            # есть жертву: имя победителя одинаково у всех
                            "text": f"{i}. Перестать фармить {option.drop_game}"[:60],
                            "callback_data": f"drop:{option.drop_campaign_id}",
                        }
                    ]
                )
            lines.append(
                f"Оставить как есть — {escape(gap.task.game)} возьмёт "
                f"{gap.reachable} из {gap.total}, остальные свои."
            )
            buttons.append([{"text": "Оставить как есть", "callback_data": "noop"}])
        await self.say("\n".join(lines), buttons)

    # ---------- команды ----------

    async def cmd_status(self) -> str:
        live = self.app.live
        if self.app.paused_until:
            until = self.app.paused_until.astimezone().strftime("%H:%M")
            return f"На паузе до {until}."
        if not live.game:
            return f"Не фармит: {escape(live.note)}"
        left = max(0, live.required - live.current)
        return (
            f"<b>{escape(live.game)}</b>\n{escape(live.campaign)}\n"
            f"Канал {escape(live.channel or '—')}, {live.viewers} зрителей\n"
            f"Прогресс {live.current}/{live.required} мин, осталось {left}."
        )

    async def cmd_plan(self) -> str:
        plan = self.app.plan or await self.app.replan()
        if not plan.blocks:
            return "План пуст: активных кампаний по вотчлисту нет."
        # Кампании идут вперемешку: пока по одной игре нет эфира, «Вахта» смотрит
        # другую. Поэтому у строки не окно, а объём работы и срок, к которому
        # она будет доделана
        lines = ["<b>В работе</b>"]
        for block in plan.blocks[:6]:
            till = block.end.astimezone().strftime("%d.%m %H:%M")
            lines.append(
                f"{escape(block.task.game)} — {escape(block.task.campaign)}: "
                f"{_hours(block.minutes)} эфира, до {till}"
            )
        if plan.shortfalls:
            lines.append(f"\nКонфликтов: {len(plan.shortfalls)}")
        return "\n".join(lines)

    async def cmd_watchlist(self) -> str:
        rows = self.app.store.watchlist()
        if not rows:
            return "Вотчлист пуст. Добавить: /add Rust"
        lines = ["<b>Вотчлист</b>"]
        for row in rows:
            share = self.app.store.availability(row["game_id"])
            mark = "нет замеров" if share < 0 else f"эфир {share * 100:.0f}%"
            lines.append(f"{row['priority']}. {escape(row['game_name'])} — {mark}")
        return "\n".join(lines)

    async def cmd_add(self, name: str) -> str:
        if not name:
            return "Так: /add Rust"
        from . import scanner

        game_id, canonical, slug = await scanner.resolve_game(self.app.tw, name)
        if not game_id:
            return f"Twitch не знает игру «{escape(name)}»"
        added = self.app.store.watch_add(game_id, canonical, slug)
        return (
            f"Добавил: {escape(canonical)}" if added else f"{escape(canonical)} уже в вотчлисте"
        )

    async def cmd_history(self, name: str) -> str:
        store = self.app.store
        if name:
            rows = [
                row
                for row in store.claims()
                if name.casefold() in row["game_name"].casefold()
            ]
            if not rows:
                return f"По «{escape(name)}» пока ничего не забрано"
            lines = [f"<b>{escape(rows[0]['game_name'])}</b>"]
            for row in rows[:20]:
                lines.append(f"{row['claimed_at'][:10]} — {escape(row['drop_name'])}")
            return "\n".join(lines)

        totals = store.totals()
        lines = [
            f"<b>Всего</b>: {totals['drops']} дропов, {_hours(totals['minutes'])} "
            f"по {totals['games']} играм"
        ]
        for row in store.games():
            lines.append(
                f"{escape(row['name'])} — {row['drops']} дропов, {_hours(row['minutes'])}"
            )
        return "\n".join(lines)

    async def cmd_pause(self, arg: str) -> str:
        if arg.lower() in ("off", "выкл", "стоп"):
            self.app.resume()
            return "Снял с паузы."
        span = parse_span(arg) or timedelta(hours=1)
        until = self.app.pause(span)
        return f"Пауза до {until.astimezone().strftime('%H:%M')}."

    async def handle(self, text: str) -> str:
        command, _, rest = text.partition(" ")
        command = command.split("@")[0].lower()
        rest = rest.strip()
        if command in ("/start", "/help"):
            return (
                "<b>Вахта</b>\n"
                "/status — что фармится сейчас\n"
                "/plan — очередь на ближайшее\n"
                "/watchlist — игры под наблюдением\n"
                "/add &lt;игра&gt; — добавить в вотчлист\n"
                "/history [игра] — что уже забрано\n"
                "/pause 2ч — пауза, /pause off — снять"
            )
        if command == "/status":
            return await self.cmd_status()
        if command == "/plan":
            return await self.cmd_plan()
        if command == "/watchlist":
            return await self.cmd_watchlist()
        if command == "/add":
            return await self.cmd_add(rest)
        if command == "/history":
            return await self.cmd_history(rest)
        if command == "/pause":
            return await self.cmd_pause(rest)
        return "Не знаю такой команды. /help"

    # ---------- цикл ----------

    async def _message(self, message: dict) -> None:
        chat_id = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return

        if self.config.chat_id is None:
            # первым написавшим становится владелец, остальные потом игнорируются
            self.config.chat_id = chat_id
            self.config.save()
            log.info("Telegram: владелец %s", chat_id)
        elif chat_id != self.config.chat_id:
            log.warning("Telegram: чужой чат %s, игнорирую", chat_id)
            return

        await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=await self.handle(text),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _callback(self, query: dict) -> None:
        chat_id = (query.get("message") or {}).get("chat", {}).get("id")
        if chat_id != self.config.chat_id:
            return
        data = query.get("data") or ""
        if data.startswith("drop:"):
            campaign_id = data[5:]
            self.app.store.skip(campaign_id)
            await self.call(
                "answerCallbackQuery", callback_query_id=query["id"], text="Принято"
            )
            await self.app.replan()
            await self.say("Пересчитал план с учётом решения.")
        else:
            await self.call(
                "answerCallbackQuery", callback_query_id=query["id"], text="Оставил как есть"
            )

    async def run(self) -> None:
        me = await self.call("getMe")
        if not me:
            log.error("Telegram: токен не принят, бот выключен")
            return
        self.username = me.get("username", "")
        log.info("Telegram: @%s на связи", self.username)
        if not self.config.chat_id:
            log.info("Telegram: напиши боту /start, чтобы он знал, кому писать")

        while True:
            try:
                updates = await self.call(
                    "getUpdates", offset=self.offset, timeout=POLL_TIMEOUT
                )
                if updates is None:
                    # Telegram отказал — подождать, а не долбить заново. Без
                    # этого `api.telegram.org`, который в этом дата-центре
                    # отваливается пачками, превращался в тридцать попыток
                    # в секунду
                    await asyncio.sleep(RETRY_PAUSE)
                    continue
                for update in updates or []:
                    self.offset = update["update_id"] + 1
                    if "message" in update:
                        await self._message(update["message"])
                    elif "callback_query" in update:
                        await self._callback(update["callback_query"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._trouble("связь", f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(RETRY_PAUSE)
