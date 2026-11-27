"""Окно приложения на pywebview, его геометрия и своя рамка.

Окно — тот же `index.html`, что раньше открывался в браузере, только теперь
в WebView2 без адресной строки и вкладок. Движок предустановлен на всех
Windows 11 и на Windows 10 начиная с обновления 2018 года, ставить нечего.

Здесь же лежит вся возня с координатами: сохранённое положение нельзя брать
на веру, потому что монитор, на котором окно стояло вчера, сегодня может быть
отключён — и окно откроется там, где его никто не увидит.

И здесь же `Frame` — то, что приходится вернуть окну после `frameless=True`.
Макеты просят свою титульную строку, а pywebview вместе с системной снимает
ещё две вещи, о которых никто не просил: рамку изменения размера и правильный
разворот. Обе возвращаются руками, см. `Frame.dress`.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import sys

from .paths import ICO_PATH

log = logging.getLogger("vahta")

MIN_WIDTH, MIN_HEIGHT = 900, 620
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1180, 820

# GetSystemMetrics: границы всех мониторов вместе взятых
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
# сколько окна должно остаться на экране, чтобы его можно было ухватить мышью
VISIBLE_EDGE = 120

GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
# SetWindowPos: размер, положение и z-порядок не трогаем, но рамку перечитать
SWP_FRAME_ONLY = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
# SetWindowPos: поставить куда сказано, не трогая z-порядок и фокус
SWP_PUT = 0x0004 | 0x0010
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR = 34, 35

# WM_SETICON: своя иконка окна вместо иконки python.exe
WM_SETICON = 0x0080
ICON_SMALL, ICON_BIG = 0, 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXICON, SM_CYICON = 11, 12
SM_CXSMICON, SM_CYSMICON = 49, 50


def _virtual_screen() -> tuple[int, int, int, int] | None:
    try:
        metrics = ctypes.windll.user32.GetSystemMetrics
        width = metrics(SM_CXVIRTUALSCREEN)
        height = metrics(SM_CYVIRTUALSCREEN)
        if width <= 0 or height <= 0:
            return None
        return metrics(SM_XVIRTUALSCREEN), metrics(SM_YVIRTUALSCREEN), width, height
    except (AttributeError, OSError):  # не Windows или отказали в вызове
        return None


def geometry(saved: dict) -> dict:
    """Аргументы окна из сохранённого — или значения по умолчанию.

    Положение отдаётся только если окно действительно видно: pywebview без
    координат откроет по центру, и это правильный ответ на «монитора больше
    нет», «разрешение сменилось», «в файле мусор».
    """
    size = {
        "width": max(MIN_WIDTH, int(saved.get("width") or DEFAULT_WIDTH)),
        "height": max(MIN_HEIGHT, int(saved.get("height") or DEFAULT_HEIGHT)),
    }
    # разворот сюда не попадает нарочно: pywebview развернул бы окно до того,
    # как `Frame.dress` подгонит границы, и оно легло бы поверх панели задач.
    # Разворачиваем сами, уже показанным — см. `Tray.on_shown`
    x, y = saved.get("x"), saved.get("y")
    if x is None or y is None:
        return size

    screen = _virtual_screen()
    if screen is None:
        return size
    left, top, width, height = screen
    fits = (
        left - size["width"] + VISIBLE_EDGE <= x <= left + width - VISIBLE_EDGE
        and top <= y <= top + height - VISIBLE_EDGE
    )
    if not fits:
        log.info("Сохранённое положение окна вне экранов, открываю по центру")
        return size
    return {**size, "x": int(x), "y": int(y)}


def to_front(title: str) -> bool:
    """Поднять окно с таким заголовком поверх всех и отдать ему фокус.

    Нужно, когда «Вахту» просят открыть, а окно уже есть — просто свёрнуто или
    закрыто чужими окнами. pywebview дескриптор наружу не отдаёт, поэтому ищем
    по заголовку: своё окно у нас одно.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        handle = user32.FindWindowW(None, title)
        if not handle:
            return False
        user32.ShowWindow(handle, 9)  # SW_RESTORE — развернуть свёрнутое
        user32.SetForegroundWindow(handle)
        return True
    except (AttributeError, OSError):
        return False


class Frame:
    """Своя рамка поверх `frameless=True`.

    `frameless` снимает с окна системную титульную строку — этого мы и хотели,
    строка нарисована в макетах своя. Заодно снимается то, о чём не просили:

    - **окно перестаёт тянуться за края.** Возвращается стилем `WS_THICKFRAME`:
      рамка снова есть, но рисовать в ней нечего — заголовка нет, остаётся
      полоска границы и невидимая зона захвата по периметру.
    - **разворот ложится поверх панели задач.** Проверено спайком: развёрнутое
      окно вылезает на весь монитор и ещё на 8 px за край. Лечится
      `MaximizedBounds` — рабочей областью того экрана, где окно сейчас.
    - **возврат из разворота съедает рамку.** WinForms пересчитывает границы,
      считая окно по-прежнему безрамочным, и окно каждый раз возвращается на
      двойную ширину рамки меньше — за круг 16 px при 125%, и это копится до
      минимального размера. Поэтому прямоугольник снимается перед разворотом
      и возвращается руками сразу после `restore()`.

    Скруглённые углы Windows 11 просятся туда же: атрибут DWM, на Windows 10
    он просто возвращает ошибку, и это нормально — там углы прямые.

    Всё держится на `window.native` — форме WinForms. Её нет до того, как окно
    показано, поэтому `dress()` зовётся из события `shown`.
    """

    def __init__(self, window) -> None:
        self.window = window
        self._screen = None  # к какому экрану подогнан MaximizedBounds
        self._normal = None  # прямоугольник до разворота, в него и вернёмся

    @property
    def hwnd(self) -> int:
        return int(self.window.native.Handle.ToInt64())

    def dress(self) -> None:
        """Вернуть окну рамку и скруглить углы. Не вышло — жить дальше."""
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            handle = self.hwnd
            style = user32.GetWindowLongW(handle, GWL_STYLE)
            user32.SetWindowLongW(handle, GWL_STYLE, style | WS_THICKFRAME)
            user32.SetWindowPos(handle, 0, 0, 0, 0, 0, SWP_FRAME_ONLY)
        except Exception:
            log.exception("Рамку окна вернуть не вышло — размер будет не менять")
        try:
            pref = ctypes.c_int(DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref)
            )
        except (AttributeError, OSError):  # Windows 10: углы остаются прямыми
            pass
        self.wear_icon()
        self.fit_screen()

    def wear_icon(self) -> None:
        """Надеть иконку окна: панель задач, alt-tab, угол своей строки.

        Через WM_SETICON, а не через форму WinForms: у окна и так всё делается
        ctypes, а `System.Drawing.Icon` потянул бы за собой ещё одну сборку .NET
        в бандл. Размеры берём у системы: у большой и маленькой иконок они
        разные и зависят от масштаба экрана.
        """
        if sys.platform != "win32" or not ICO_PATH.exists():
            return
        try:
            user32 = ctypes.windll.user32
            for which, cx, cy in (
                (ICON_SMALL, SM_CXSMICON, SM_CYSMICON),
                (ICON_BIG, SM_CXICON, SM_CYICON),
            ):
                handle = user32.LoadImageW(
                    0, str(ICO_PATH), IMAGE_ICON,
                    user32.GetSystemMetrics(cx), user32.GetSystemMetrics(cy),
                    LR_LOADFROMFILE,
                )
                if handle:
                    user32.SendMessageW(self.hwnd, WM_SETICON, which, handle)
        except Exception:
            log.exception("Иконку окна поставить не вышло")

    def paint(self, caption, border) -> None:
        """Покрасить рамку окна: `caption` и `border` — тройки 0–255 от страницы.

        Верхние 8 px окна рисует не WebView2, а DWM: там неклиентская область
        с рамкой изменения размера, и наша страница до неё не достаёт. Убрать
        эту область можно только своей оконной процедурой (`WM_NCCALCSIZE`), а
        своя оконная процедура на Python вешает мост pywebview — проверено
        спайком, `evaluate_js` после неё не возвращается. Поэтому полосу не
        убирают, а красят в цвет титульной строки: тогда она перестаёт читаться
        как полоса и просто продолжает строку вверх. Бока и низ при этом
        занимают ровно 1 px — это и есть «граница окна» из макетов.

        Цвета приходят со страницы, а не заводятся здесь вторыми: тема живёт
        в CSS, и второй список значений однажды разошёлся бы с первым.

        Атрибуты появились в Windows 11. На Windows 10 вызов просто вернёт
        ошибку — там же, где не бывает и скруглённых углов.
        """
        try:
            for attr, color in ((DWMWA_CAPTION_COLOR, caption), (DWMWA_BORDER_COLOR, border)):
                red, green, blue = (max(0, min(255, int(v))) for v in color[:3])
                ref = ctypes.c_uint(red | green << 8 | blue << 16)  # COLORREF: 0x00BBGGRR
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    self.hwnd, attr, ctypes.byref(ref), ctypes.sizeof(ref)
                )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            log.debug("Рамку окрасить не вышло: %s", exc)

    def fit_screen(self) -> None:
        """Подогнать границы разворота под экран, на котором окно сейчас.

        Зовётся при каждом перемещении: развернуть окно могут не только нашей
        кнопкой, но и `Win+↑`, а к тому моменту думать уже поздно.
        """
        try:
            from System.Windows.Forms import Screen  # pywebview уже поднял pythonnet

            form = self.window.native
            screen = Screen.FromHandle(form.Handle)
            if screen.DeviceName == self._screen:
                return
            form.MaximizedBounds = screen.WorkingArea
            self._screen = screen.DeviceName
        except Exception as exc:
            log.debug("Границы разворота не подогнаны: %s", exc)

    def is_maximized(self) -> bool:
        try:
            return bool(ctypes.windll.user32.IsZoomed(self.hwnd))
        except (AttributeError, OSError):
            return False

    def maximize(self) -> None:
        self._normal = self._rect()
        self.fit_screen()
        self.window.maximize()

    def restore(self) -> None:
        self.window.restore()
        if self._normal is None:
            return
        # `restore()` внутри — синхронный Invoke: когда он вернулся, WinForms
        # уже успел ошибиться, и наш прямоугольник ложится последним
        try:
            ctypes.windll.user32.SetWindowPos(self.hwnd, 0, *self._normal, SWP_PUT)
        except (AttributeError, OSError):
            pass
        self._normal = None

    def toggle_maximize(self) -> bool:
        """Развернуть или вернуть. Отдаёт новое состояние — его рисует кнопка."""
        if self.is_maximized():
            self.restore()
            return False
        self.maximize()
        return True

    def _rect(self) -> tuple[int, int, int, int] | None:
        """Прямоугольник окна на экране: левый верхний угол, ширина, высота."""
        try:
            box = ctypes.wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(box)):
                return None
            return box.left, box.top, box.right - box.left, box.bottom - box.top
        except (AttributeError, OSError):
            return None


def remember(saved: dict, *, width=None, height=None, x=None, y=None, maximized=False) -> None:
    """Запомнить геометрию. Развёрнутое и свёрнутое окно не запоминаем.

    У развёрнутого размер и положение — экрана, а не человека: запиши их, и
    после «восстановить» окно останется во весь экран уже без возврата.
    Поэтому запоминается только сам факт разворота, а прежняя геометрия
    остаётся лежать нетронутой — к ней окно и вернётся.
    """
    saved["maximized"] = bool(maximized)
    if maximized:
        return
    if width and height and width >= MIN_WIDTH // 2 and height >= MIN_HEIGHT // 2:
        saved["width"], saved["height"] = int(width), int(height)
    if x is not None and y is not None:
        # свёрнутое окно Windows уносит в координаты вроде -32000
        if x > -30000 and y > -30000:
            saved["x"], saved["y"] = int(x), int(y)
