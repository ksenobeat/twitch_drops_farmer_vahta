"""Приложение: окно, иконка в трее, служба и веб в фоне.

Консоли нет, батника нет, браузера тоже нет — окно своё, на WebView2.

Два решения, из которых следует всё устройство этого файла:

**Окно создаётся по требованию, а не при запуске.** Открытое окно стоит около
460 МБ (свои процессы 140, WebView2 сверх того ~320), и скрытие не возвращает
ничего, а уничтожение возвращает всё. «Вахта» живёт в трее сутками, поэтому
крестик окно уничтожает, а главный поток возвращается к ожиданию следующего
«открыть». Проверено спайком: pywebview спокойно переживает повторный
`webview.start()` в том же процессе.

**Экземпляр один.** Второй запуск будит первый и уходит — см. `single.py`.

Потоки, и порядок тут не случаен:

    главный поток   ожидание и webview.start() — окно иначе не работает
    свой поток      pystray.run_detached() — свой цикл сообщений
    свой поток      uvicorn
    свой поток      служба (см. service.py)
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import threading
import time
import webbrowser

import uvicorn

from . import autostart, mark, single
from .paths import DATA_DIR
from .window import MIN_HEIGHT, MIN_WIDTH, Frame, geometry, remember, to_front

log = logging.getLogger("vahta")

HOST = "127.0.0.1"
PORT = 8420
URL = f"http://{HOST}:{PORT}"
TITLE = "Вахта"

# Ключ «выйди, если запущена». Им пользуется инсталлер перед обновлением:
# «Вахта» держит свой exe открытым, а знать про порт и HTTP инсталлеру незачем
QUIT_FLAG = "--quit"

# Своё имя для панели задач. Без него Windows считает окно окном python.exe:
# группирует «Вахту» с любыми другими питоньими окнами и показывает чужую
# иконку. Ставится один раз и до появления первого окна
APP_ID = "Kseno.Vahta"

# Как часто трей переспрашивает службу о прогрессе. Полоса знака рисуется
# шагами в 1/16, засчитанные минуты приходят раз в минуту — чаще незачем
ICON_INTERVAL = 20


class TitlebarApi:
    """Кнопки своей титульной строки — из JS как `window.pywebview.api.*`.

    Системной строки у окна нет, поэтому свернуть, развернуть и закрыть больше
    некому. Методы зовутся из потока pywebview, а не из главного: всё, что они
    трогают, у pywebview потокобезопасно (`Invoke` на форму).

    **Ссылка на трей — с подчёркиванием, и это обязательно.** Строя мост,
    pywebview обходит все публичные атрибуты этого объекта вглубь и делает
    вызываемым из страницы всё, что нашёл. Через `Tray` наружу утекли бы служба
    и сервер, а обход формы WinForms и вовсе уходит в бесконечность
    (`native.AccessibilityObject.Bounds.Empty.Empty…`) и топит консоль
    ошибками. Всё, что начинается с `_`, обход пропускает.
    """

    def __init__(self, tray: "Tray") -> None:
        self._tray = tray

    def minimize(self) -> None:
        if self._tray.window is not None:
            self._tray.window.minimize()

    def toggle_maximize(self) -> bool:
        return bool(self._tray.frame and self._tray.frame.toggle_maximize())

    def is_maximized(self) -> bool:
        return bool(self._tray.frame and self._tray.frame.is_maximized())

    def set_frame_colors(self, caption, border) -> None:
        """Цвета рамки окна берутся со страницы: тема живёт в CSS."""
        if self._tray.frame is not None:
            self._tray.frame.paint(caption, border)

    def close(self) -> None:
        """То же, что системный крестик: дальше решает `on_closing`."""
        if self._tray.window is not None:
            self._tray.window.destroy()


class Tray:
    def __init__(self, *, autostarted: bool = False) -> None:
        from . import web
        from .service import Service

        self.icon = None
        self.window = None
        self.frame = None
        self.autostarted = autostarted
        self.quitting = False
        self.wanted = threading.Event()  # «покажи окно» — из трея или второго запуска
        self.icon_state: tuple[float, bool] | None = None  # что уже нарисовано

        self.service = Service()
        # уведомления показывает иконка трея — Windows сама превращает её
        # всплывающую подсказку в тост и кладёт его в центр уведомлений
        self.service.notifier = self.toast
        # второй запуск стучится сюда через /api/window/show
        self.service.open_ui = self.open_ui
        # а инсталлер перед обновлением — сюда, через /api/quit
        self.service.quit_ui = self.quit
        self.service.start()
        web.attach(self.service)

        self.server = uvicorn.Server(
            uvicorn.Config(
                web.api,
                host=HOST,
                port=PORT,
                log_level="warning",
                access_log=False,
                # свои форматтеры uvicorn лезут в stdout.isatty(), а его тут нет
                log_config=None,
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    # --- трей ----------------------------------------------------------------

    def toast(self, title: str, text: str) -> None:
        """Уведомление Windows. До появления иконки показывать нечем."""
        if self.icon is not None:
            self.icon.notify(text, title)

    def watch_icon(self) -> None:
        """Держать иконку в трее живой: средняя полоса знака = прогресс смены.

        Спрашиваем службу сами, а не просим её звать нас: `live` меняется
        в десятке мест автопилота, и обвешивать каждое обратным вызовом ради
        картинки в панели — дороже, чем раз в двадцать секунд спросить.
        Перерисовываем только когда картинка правда другая: каждое присвоение
        `icon.icon` — это Shell_NotifyIcon, а он мигает.
        """
        while not self.quitting:
            try:
                state = asyncio.run(self.service.call(
                    lambda s: (
                        s.live.current / s.live.required if s.live.required else 0.0,
                        bool(s.plan and s.plan.shortfalls),
                    )
                ))
                step = (round(state[0] * mark.STEPS), state[1])
                if self.icon is not None and step != self.icon_state:
                    self.icon_state = step
                    self.icon.icon = mark.tray(64, state[0], state[1])
            except Exception as exc:  # иконка не повод ронять фарм
                log.debug("Иконку обновить не вышло: %s", exc)
            time.sleep(ICON_INTERVAL)

    def open_ui(self, *_) -> None:
        """Показать окно. Есть — поднять поверх всех, нет — попросить создать."""
        if self.window is not None:
            to_front(TITLE)
        else:
            self.wanted.set()

    def open_data(self, *_) -> None:
        import os

        os.startfile(DATA_DIR)  # noqa: S606 — своя же папка с данными

    def toggle_autostart(self, icon, item) -> None:
        autostart.set_enabled(not item.checked)

    def quit(self, *_) -> None:
        """Настоящий выход. Крестик сюда ведёт, только если так выбрано."""
        self.quitting = True
        self.server.should_exit = True
        if self.window is not None:
            self.remember()
            self.window.destroy()  # отпустит webview.start() на главном потоке
        if self.icon is not None:
            self.icon.stop()
        self.wanted.set()  # разбудить главный поток, если он ждёт

    def menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem("Открыть Вахту", self.open_ui, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Папка с данными", self.open_data),
            pystray.MenuItem(
                "Запускать при входе в систему",
                self.toggle_autostart,
                checked=lambda item: autostart.is_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.quit),
        )

    # --- окно ----------------------------------------------------------------

    @property
    def settings(self):
        return self.service.settings

    @property
    def saved_window(self) -> dict:
        return self.settings.window if self.settings else {}

    def on_closing(self) -> bool:
        """Крестик: либо уничтожить окно и остаться в трее, либо выйти совсем.

        Скрывать окно смысла нет — спрятанное оно держит те же ~460 МБ.
        """
        self.remember()
        if self.settings and not self.settings.close_to_tray:
            # человек выбрал «выходить по крестику». Окно pywebview уничтожит
            # само, поэтому здесь только помечаемся — иначе получилось бы
            # два уничтожения одного окна навстречу друг другу
            self.quitting = True
            self.server.should_exit = True
            if self.icon is not None:
                self.icon.stop()
        return True

    def remember(self, *_) -> None:
        """Снять текущую геометрию окна. На диск она уходит при выходе."""
        w = self.window
        if w is None:
            return
        try:
            remember(
                self.saved_window,
                width=w.width, height=w.height, x=w.x, y=w.y,
                maximized=bool(self.frame and self.frame.is_maximized()),
            )
        except Exception as exc:  # окно уже уничтожают — геометрия не важнее выхода
            log.debug("Геометрию снять не вышло: %s", exc)

    def on_shown(self, *_) -> None:
        """Окно показано — только теперь есть форма, а значит и рамка.

        Разворот тоже здесь, а не в аргументах окна: сначала надо подогнать
        границы под экран, иначе развёрнутое ляжет поверх панели задач.
        """
        self.frame.dress()
        if self.saved_window.get("maximized"):
            self.frame.maximize()

    def on_moved(self, *_) -> None:
        """Перетащили окно: запомнить и переспросить, на каком оно экране."""
        self.remember()
        if self.frame is not None:
            self.frame.fit_screen()

    def open_window(self) -> None:
        import webview

        self.window = webview.create_window(
            TITLE, URL,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            background_color="#100e17",  # тёмный фон до загрузки — вместо белой вспышки
            # титульная строка своя, нарисована в макетах; easy_drag выключен,
            # иначе окно поедет от нажатия в любом месте, а не только за строку
            frameless=True,
            easy_drag=False,
            js_api=TitlebarApi(self),
            **geometry(self.saved_window),
        )
        self.frame = Frame(self.window)
        self.window.events.shown += self.on_shown
        self.window.events.closing += self.on_closing
        self.window.events.resized += self.remember
        self.window.events.moved += self.on_moved

    def show_at_start(self) -> bool:
        """Показывать ли окно сразу после запуска."""
        if not self.autostarted:
            return True  # человек сам ткнул в ярлык — он хочет увидеть окно
        return bool(self.settings and self.settings.window_on_autostart)

    # --- запуск --------------------------------------------------------------

    def run(self) -> None:
        import pystray

        self.thread.start()
        self.icon = pystray.Icon("vahta", mark.tray(64, 0.0), TITLE, self.menu())
        # свой цикл сообщений в своём потоке: главный нужен окну
        self.icon.run_detached()
        threading.Thread(target=self.watch_icon, daemon=True).start()

        if self.show_at_start():
            self.wanted.set()

        try:
            self.loop()
        finally:
            if self.settings:
                self.settings.save()
            # служба гасится последней: ей нужно дописать сессию и закрыть базу
            self.service.stop()

    def loop(self) -> None:
        """Главный поток: ждём просьбы показать окно, показываем, ждём снова.

        `webview.start()` возвращает управление, когда окно уничтожено, — и
        вместе с ним уходит вся память WebView2. Пока окна нет, «Вахта» стоит
        столько же, сколько служба с треем.
        """
        import webview

        while not self.quitting:
            self.wanted.wait()
            self.wanted.clear()
            if self.quitting:
                return
            try:
                self.open_window()
                # Профиль WebView2 держим рядом со своими данными, а не во
                # временной папке: иначе движок пересоздаёт кэши при каждом
                # открытии окна, а открывают его часто.
                webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"))
            except Exception:
                # WebView2 нет или не поднялся: не повод терять фарм —
                # показываем интерфейс в браузере и живём одним треем
                log.exception("Окно не открылось, показываю интерфейс в браузере")
                self.window = self.frame = None
                webbrowser.open(URL)
                self.wanted.wait()  # дальше только выход из трея
                return
            self.window = self.frame = None


def _attach_streams() -> None:
    """В оконном режиме stdout и stderr равны None.

    Любой print и любая библиотека, которая на них рассчитывает, роняет
    приложение. Подставляем файл, заодно ловим чужие сообщения — WebView2,
    например, ругается в stderr при закрытии, и это нормально.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    sink = open(DATA_DIR / "vahta-output.log", "a", encoding="utf8", buffering=1)
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def _name_in_taskbar() -> None:
    """Своё имя приложения для Windows.

    Без него панель задач считает окно окном python.exe: «Вахта» группируется
    с чужими питоньими окнами и берёт их иконку. Зовётся до первого окна —
    после Windows уже решила, к какой группе оно относится.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError) as exc:
        log.debug("Имя для панели задач не поставилось: %s", exc)


def run() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _attach_streams()
    _name_in_taskbar()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        # С датой, в отличие от консоли: файл живёт неделями и дописывается
        # каждым запуском, а по одному времени сутки не отличить. Разбирая
        # вечер 31 июля, мы дважды приняли позавчерашние строки за сегодняшние —
        # время совпало. День записи стоит шести символов в строке.
        # Порядок тот же, что и везде в «Вахте»: «до 01.08 11:44»
        datefmt="%d.%m %H:%M:%S",
        filename=str(DATA_DIR / "vahta.log"),
        filemode="a",
        encoding="utf8",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # `--quit` — просьба выйти, а не запуск. Ею пользуется инсталлер перед
    # обновлением: exe знает и порт, и адрес, а инсталлеру знать их незачем.
    # Некого просить — значит уже никого нет, и это тоже успех.
    if QUIT_FLAG in sys.argv:
        if single.claim():
            log.info("Просили выйти, но «Вахта» и не запущена")
            return
        log.info("Просили выйти — передаю запущенному экземпляру")
        if not single.farewell(URL):
            log.warning("Запущенный экземпляр не вышел за отведённое время")
            raise SystemExit(1)
        return

    if not single.claim():
        # «Вахта» уже запущена: показываем её окно и тихо уходим
        log.info("Экземпляр уже работает, открываю её окно")
        single.wake(URL)
        return
    if single.port_taken(HOST, PORT):
        single.complain(
            f"Порт {PORT} занят другой программой — «Вахта» не сможет "
            "показать интерфейс.\n\nЗакрой программу, которая его держит, "
            "и запусти заново."
        )
        return

    try:
        Tray(autostarted=autostart.FLAG in sys.argv).run()
    except Exception:
        log.exception("Приложение упало")
        raise


if __name__ == "__main__":
    sys.exit(run())
