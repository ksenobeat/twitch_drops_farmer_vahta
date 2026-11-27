"""Один экземпляр на пользователя.

Второй запуск раньше поднимал вторую службу: у неё не получалось занять порт,
её uvicorn тихо умирал в потоке, а фарм — нет. В итоге два экземпляра смотрели
Twitch одним аккаунтом, а параллельный просмотр рассинхронит прогресс дропов.
Это не «некрасиво», это потерянные часы фарма.

Поэтому: первый запуск занимает именованный мьютекс, второй его не получает,
будит первого через его же HTTP и уходит. Отдельного канала связи не нужно —
порт всё равно слушается.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import urllib.error
import urllib.request

log = logging.getLogger("vahta")

# Без префикса Global\ имя живёт в сеансе: у каждого пользователя машины
# своя «Вахта» со своими данными, и мешать друг другу они не должны.
MUTEX = "Vahta.SingleInstance"
ERROR_ALREADY_EXISTS = 183

_handle = None  # держим до конца процесса: закроется — мьютекс освободится


def claim() -> bool:
    """True — мы единственные. False — «Вахта» уже запущена."""
    global _handle
    if sys.platform != "win32":
        return True
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX)
    if not handle:
        log.warning("Мьютекс не создался, проверку единственности пропускаю")
        return True
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        # Чужой мьютекс держать нельзя: пока открыта наша ссылка, он не
        # освободится и после выхода хозяина. `--quit` из-за этого ждал
        # собственную ссылку все 45 секунд и докладывал инсталлеру «не вышла»
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True


def _ask(url: str, path: str, timeout: float) -> bool:
    try:
        request = urllib.request.Request(f"{url}{path}", data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError) as exc:
        log.info("Запущенный экземпляр не ответил на %s: %s", path, exc)
        return False


def wake(url: str, timeout: float = 3.0) -> bool:
    """Попросить уже запущенный экземпляр показать окно."""
    # первый экземпляр может ещё подниматься и не слушать порт — он всё равно
    # откроет окно сам, так что неудача здесь не беда
    return _ask(url, "/api/window/show", timeout)


# Сколько ждать, пока «Вахта» правда выйдет. Не десять секунд, и это
# не запас на всякий случай: 31 июля в живом логе выход занял дольше —
# служба остановилась в 12:22:10, а мьютекс освободился уже после 12:22:20.
# Дольше всего гасится открытое окно WebView2, за ним трей и сервер.
#
# Цена ошибки несимметрична. Ждали мало — инсталлер решит, что «Вахта» жива,
# и **тихая** установка (а автообновление только такая и бывает) упрётся
# в AppMutex, где спросить человека некого. Ждали много — потеряли секунды
# один раз за обновление, и то лишь когда выход правда затянулся: ждём мы
# опросом и уходим сразу, как мьютекс отпущен.
QUIT_TIMEOUT = 45.0


def farewell(url: str, timeout: float = QUIT_TIMEOUT) -> bool:
    """Попросить уже запущенный экземпляр выйти совсем.

    Нужно инсталлеру: обновление ставится поверх запущенной «Вахты», а она
    держит свой exe и свои библиотеки открытыми. Просить об этом человека —
    значит превращать обновление в «закрой, потом поставь»; прибивать процесс —
    значит не дать ему закрыть базу и запомнить окно.
    """
    # На сам запрос столько ждать незачем: он либо отвечает сразу, либо
    # отвечать некому. Долгое ожидание нужно выходу, а не ответу
    if not _ask(url, "/api/quit", 10.0):
        return False
    # Ответ приходит до того, как процесс закончился: выход идёт своим чередом
    # в потоках трея и окна. Инсталлеру нужен освободившийся мьютекс, поэтому
    # ждём именно его, а не ответа.
    return released(timeout)


def released(timeout: float = QUIT_TIMEOUT) -> bool:
    """Дождаться, пока «Вахта» отпустит мьютекс, то есть правда вышла."""
    import time

    if sys.platform != "win32":
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX)
        already = ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        if not already:
            return True
        time.sleep(0.25)
    return False


def port_taken(host: str, port: int) -> bool:
    """Занят ли порт кем-то посторонним.

    Проверяется, только когда мьютекс наш: значит «Вахта» одна, и если порт
    всё равно занят — это чужая программа, и молча падать нельзя.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            return False
        except OSError:
            return True


def complain(text: str) -> None:
    """Сказать человеку то, что иначе увидел бы только лог.

    Окна ещё нет и может не быть вовсе, поэтому системное окно сообщения —
    единственный способ не промолчать.
    """
    if sys.platform != "win32":
        print(text)
        return
    ctypes.windll.user32.MessageBoxW(None, text, "Вахта", 0x10)  # MB_ICONERROR
