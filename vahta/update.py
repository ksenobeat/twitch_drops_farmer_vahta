"""Автообновление: скачать новую версию и отдать её инсталлеру.

Половина работы была сделана раньше и не здесь. О свежей версии рассказывают
релизы на GitHub (`releases.check`), `web.py` сравнивает её с нашей. Инсталлер с 0.3.0 умеет ставиться поверх —
тот же `AppId`, прежние галочки, `[InstallDelete]` для `_internal` — и сам
просит запущенную «Вахту» выйти (`--quit`), а после установки поднимает её
обратно. То есть «поставить» решено давно; здесь только «скачать» и «выбрать
момент».

Три решения, которые тут главные:

- **Ставим на границе ломтя, а не когда скачалось.** Фарм идёт часами, и
  обновление посреди просмотра стоило бы человеку минут, которые Twitch
  не вернёт. Автопилот отдаёт управление ломтями (`service.autopilot`), и
  вот в этот промежуток обновление и вклинивается: просмотр в этот момент
  не идёт, а следующий начнётся уже в новой версии.
- **Хэш — это проверка целостности, а не подпись.** GitHub, который отдал
  ссылку, отдаёт и хэш (`digest` файла релиза), поэтому от взломанного
  репозитория он не спасает; от битой закачки, оборванного соединения
  и подменённого зеркала — да. Настоящая защита здесь — HTTPS.
- **Не встало — значит просто не встало.** Инсталлер не запустился, файл
  не скачался, хэш не сошёлся — «Вахта» продолжает работать в прежней версии.
  Единственное, чего нельзя, — выйти самим в надежде, что нас поднимут:
  просить нас выйти будет инсталлер, и только когда он к этому готов.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
from pathlib import Path

import httpx

from . import games
from . import runtime
from . import releases

log = logging.getLogger("vahta")

# Раз в шесть часов: версии выходят раз в недели, а у API GitHub без токена
# лимит — 60 запросов в час на IP, и делить его с другими программами незачем.
CHECK_EVERY = 6 * 3600

# Первая проверка — не сразу: старт и так занят входом в Twitch и первым обходом.
FIRST_DELAY = 150
# Файл — десятки мегабайт, и качается он в фоне: спешить некуда, а рвать
# закачку по таймауту пинга (восемь секунд) было бы просто глупо.
TIMEOUT = 300

# Версии, на которых мы уже пробовали и получили ничего. Второй раз за тот же
# запуск не пробуем: иначе неудачная установка превратилась бы в скачивание
# сорока мегабайт каждый час до конца времён.
_tried: set[str] = set()


def folder(data_dir: Path) -> Path:
    return data_dir / "updates"


def tidy(where: Path, keep: Path | None = None) -> None:
    """Убрать инсталлеры, которые больше не нужны.

    Правило простое: в папке либо ровно один файл — тот, что ждёт установки, —
    либо ничего. Каждая версия весит за двадцать мегабайт, и за десяток
    обновлений в папке с данными скопилась бы четверть гигабайта, которую
    никто не просил хранить. Обновились — прежний инсталлер уже не нужен,
    а свежее ждать нечего.

    Не убралось — не беда: файл занят антивирусом, папка только для чтения.
    Скажем в отладочный лог и попробуем в следующий раз.
    """
    if not where.is_dir():
        return
    for path in list(where.glob("*.exe")) + list(where.glob("*.part")):
        if keep is not None and path == keep:
            continue
        try:
            path.unlink()
            log.info("Убрал ненужный инсталлер: %s", path.name)
        except OSError as exc:
            log.debug("Инсталлер %s не убрался: %s", path.name, exc)


async def fetch(url: str, sha256: str, into: Path, что: str = "Обновление") -> Path | None:
    """Скачать и проверить. None, если что-то не сошлось.

    Файл пишется рядом под временным именем и переименовывается только после
    сверки хэша: оборванная закачка не должна остаться лежать как готовая
    к установке.

    `что` — только ради лога: этим же качается база игр, и строка «обновление
    не сошлось по хэшу» про восемьдесят килобайт каталога сбивала бы с толку
    ровно в тот момент, когда лог читают.
    """
    into.parent.mkdir(parents=True, exist_ok=True)
    tmp = into.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url) as answer:
                answer.raise_for_status()
                with tmp.open("wb") as file:
                    async for chunk in answer.aiter_bytes(65536):
                        digest.update(chunk)
                        file.write(chunk)
    except Exception as exc:
        log.warning("%s не скачалось: %s: %s", что, type(exc).__name__, exc)
        tmp.unlink(missing_ok=True)
        return None

    got = digest.hexdigest()
    if got.lower() != sha256.strip().lower():
        log.warning("%s не сошлось по хэшу: ждали %s, получили %s", что, sha256, got)
        tmp.unlink(missing_ok=True)
        return None

    tmp.replace(into)
    return into


def verified(setup: Path, sha256: str) -> bool:
    """Проверять и сохранённый файл: имя не гарантирует его содержимое."""
    try:
        with setup.open("rb") as file:
            got = hashlib.file_digest(file, "sha256").hexdigest()
        return got == sha256.strip().lower()
    except OSError:
        return False


def install(setup: Path, sha256: str = "") -> bool:
    """Запустить инсталлер и уйти с дороги.

    Тихо и без вопросов: человек уже согласился на обновление — переключателем
    в настройках или кнопкой в плашке. `/NORESTART` обязателен: перезагружать
    чужой компьютер ради нашей программы недопустимо, а инсталлеру для
    обновления перезагрузка и не нужна.

    Выйти сами не пробуем. Инсталлер попросит об этом сам (`--quit`) — и только
    после того, как убедится, что может продолжать; выйдя раньше, мы рисковали
    бы остаться и закрытыми, и не обновлёнными.
    """
    if runtime.SERVER_MODE:
        return False
    if not verified(setup, sha256):
        log.warning("Инсталлер не прошёл проверку перед запуском: %s", setup.name)
        return False
    try:
        subprocess.Popen(
            [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL"],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log.info("Инсталлер запущен: %s", setup.name)
        return True
    except OSError as exc:
        log.warning("Инсталлер не запустился: %s: %s", type(exc).__name__, exc)
        return False


async def prepare(service) -> Path | None:
    """Скачать обновление, если GitHub про него рассказал и оно новее нашей.

    Ничего не ставит: решение о моменте принимает автопилот, а он смотрит
    на `service.update_ready`.
    """
    if runtime.SERVER_MODE:
        return None
    from .web import _newer  # там же, где решается, показывать ли плашку

    version = service.latest_version
    if not version or not _newer(version):
        # Свежее ждать нечего — значит и хранить в папке нечего. Сюда же
        # приходит только что обновившаяся «Вахта»: инсталлер, которым её
        # и поставили, лежит тут же и больше не нужен
        tidy(folder(service.data_dir))
        return None
    if version in _tried:
        return None
    if not service.update_url or not service.update_sha256:
        return None  # релиз есть, а инсталлера с хэшем в нём нет — только плашка

    into = folder(service.data_dir) / f"Vahta-Setup-{version}.exe"
    if verified(into, service.update_sha256):
        tidy(into.parent, keep=into)
        return into

    log.info("Качаю обновление %s", version)
    got = await fetch(service.update_url, service.update_sha256, into)
    if got is not None:
        # прежние версии больше не понадобятся: ставить будем эту
        tidy(got.parent, keep=got)
    return got


async def loop(service) -> None:
    """Время от времени спрашивать GitHub и качать обновление заранее.

    Заранее — потому что момент установки короткий (граница ломтя), и качать
    сорок мегабайт в этот момент значит его пропустить. Выключенное
    «Обновлять автоматически» не спрашивает ничего: проверить можно кнопкой.
    """
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            if service.settings is None or service.settings.auto_update:
                if await releases.check(service):
                    service.update_ready = await prepare(service)
                    # заодно и база игр: она лежит в тех же релизах
                    await games.обновить(service)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # обновление не должно ничего ломать
            log.warning("Проверка обновления сорвалась: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(CHECK_EVERY)


def take(service) -> Path | None:
    """Забрать готовое обновление, пометив версию как испробованную.

    Зовётся из автопилота в тот момент, когда просмотр не идёт. Пометка
    ставится до установки, а не после: если инсталлер не сработает, второй
    попытки в этом запуске не будет — и это лучше, чем цикл из перезапусков.
    """
    if service.settings is not None and not service.settings.auto_update:
        return None
    setup = service.update_ready
    if setup is None or not setup.exists():
        return None
    service.update_ready = None
    _tried.add(service.latest_version)
    return setup
