"""Геометрия окна.

Всё вокруг сохранённого положения: его нельзя брать на веру. Монитор, на
котором окно стояло вчера, сегодня может быть отключён, разрешение могло
смениться, а в файле мог оказаться мусор — и во всех случаях окно должно
открыться там, где его видно.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import window as win  # noqa: E402

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
    else:
        fail += 1
        print(f"  ПЛОХО {name}: {got!r} вместо {want!r}")


# --- значения по умолчанию --------------------------------------------------
g = win.geometry({})
check("без сохранённого — размер по умолчанию",
      (g["width"], g["height"]), (win.DEFAULT_WIDTH, win.DEFAULT_HEIGHT))
check("и без координат — окно встанет по центру", "x" in g, False)

check("слишком узкое подтягивается", win.geometry({"width": 10})["width"], win.MIN_WIDTH)
check("слишком низкое тоже", win.geometry({"height": 10})["height"], win.MIN_HEIGHT)
check("нули считаем «не задано»", win.geometry({"height": 0})["height"], win.DEFAULT_HEIGHT)

screen = win._virtual_screen()
if screen is None:
    print("  (не Windows — проверки положения пропущены)")
else:
    left, top, width, height = screen
    inside = {"width": 1000, "height": 700, "x": left + 50, "y": top + 50}
    check("положение на экране сохраняется", win.geometry(inside).get("x"), left + 50)

    for name, spot in (
        ("далеко влево", {"x": left - 9000, "y": top + 50}),
        ("далеко вправо", {"x": left + width + 500, "y": top + 50}),
        ("ниже всех экранов", {"x": left + 50, "y": top + height + 500}),
        ("выше всех экранов", {"x": left + 50, "y": top - 900}),
    ):
        check(f"вне экранов ({name}) — по центру",
              "x" in win.geometry({"width": 1000, "height": 700, **spot}), False)

    # окно, наполовину свисающее за правый край, — это нормально:
    # человек мог его так поставить, и ухватить мышью его ещё можно
    half = {"width": 1000, "height": 700, "x": left + width - 300, "y": top + 100}
    check("наполовину за краем — оставляем", "x" in win.geometry(half), True)

# --- запоминание -------------------------------------------------------------
saved: dict = {}
win.remember(saved, width=1200, height=800, x=100, y=60)
check("запомнили", saved,
      {"width": 1200, "height": 800, "x": 100, "y": 60, "maximized": False})

win.remember(saved, width=1300, height=850, x=-32000, y=-32000)
check("свёрнутое окно координаты не портит", (saved["x"], saved["y"]), (100, 60))
check("а размер обновился", (saved["width"], saved["height"]), (1300, 850))

win.remember(saved, width=5, height=5, x=200, y=120)
check("мусорный размер не запоминается", (saved["width"], saved["height"]), (1300, 850))
check("а положение — да", (saved["x"], saved["y"]), (200, 120))

win.remember(saved, width=None, height=None, x=None, y=None)
check("пустой вызов ничего не ломает", len(saved), 5)

# сохранённое переживает круг «запомнили → открыли»
check("круг сходится", win.geometry(saved).get("width"), 1300)

# --- развёрнутое окно --------------------------------------------------------
# у него размер экрана, а не человека: запиши его — и «восстановить» вернёт
# окно во весь экран, только уже навсегда
win.remember(saved, width=3440, height=1380, x=0, y=0, maximized=True)
check("развёрнутое размер не портит", (saved["width"], saved["height"]), (1300, 850))
check("и положение тоже", (saved["x"], saved["y"]), (200, 120))
check("но сам разворот запомнен", saved["maximized"], True)
# в аргументы pywebview разворот не отдаём: окно развернулось бы раньше, чем
# Frame подгонит границы, и легло бы поверх панели задач
check("в аргументы окна разворот не попадает", "maximized" in win.geometry(saved), False)
check("а размер там прежний", win.geometry(saved).get("width"), 1300)

win.remember(saved, width=1300, height=850, x=200, y=120)
check("вернули из разворота — флаг снят", saved["maximized"], False)

# --- мост титульной строки ---------------------------------------------------
# Строя мост, pywebview обходит все публичные атрибуты объекта вглубь и делает
# вызываемым из страницы всё, что нашёл. Стоит оставить здесь публичную ссылку
# на трей — и наружу уедут служба с сервером, а обход формы WinForms уйдёт
# в бесконечность и утопит консоль ошибками. Проверено на живом окне.
from vahta.app import TitlebarApi  # noqa: E402

api = TitlebarApi(tray=None)
public = sorted(n for n in dir(api) if not n.startswith("_"))
check("наружу торчат только кнопки", public,
      ["close", "is_maximized", "minimize", "set_frame_colors", "toggle_maximize"])
check("и все они вызываемые", all(callable(getattr(api, n)) for n in public), True)

# --- знак --------------------------------------------------------------------
# Иконка в трее рисуется на каждый шаг прогресса, поэтому проверяем не картинку
# целиком, а то, из-за чего она вообще перерисовывается: длину средней полосы
# и её цвет.
import tempfile  # noqa: E402

from vahta import mark  # noqa: E402


def middle_bar(image):
    """Ширина средней полосы в пикселях и её цвет — по строке через её центр."""
    size = image.width
    row = int(size * (14 + 2) / mark.UNIT)  # середина полосы y=14 h=4
    pixels = [image.getpixel((x, row)) for x in range(size)]
    lit = [(x, p) for x, p in enumerate(pixels) if p[3] > 200]
    return (len(lit), lit[len(lit) // 2][1][:3] if lit else None)


empty, _ = middle_bar(mark.tray(128, 0.0))
half, colour = middle_bar(mark.tray(128, 0.5))
full, _ = middle_bar(mark.tray(128, 1.0))
check("пустой прогресс — полоса не длиннее своей высоты", empty <= 128 * 4 // mark.UNIT + 2, True)
check("половина короче целого", half < full, True)
check("и длиннее пустого", half > empty, True)
check("полный прогресс — полоса во всю длину",
      abs(full - round(128 * 14 / mark.UNIT)) <= 2, True)
check("растёт слева направо", middle_bar(mark.tray(128, 0.25))[0] < half, True)

check("обычная полоса акцентная", colour, mark.ACCENT[:3])
check("при конфликте краснеет", middle_bar(mark.tray(128, 0.5, True))[1], mark.DANGER[:3])
# прогресс приходит долей, а не процентом — за границы выходить нельзя
check("больше единицы не бывает", middle_bar(mark.tray(128, 4.0))[0], full)
check("и меньше нуля тоже", middle_bar(mark.tray(128, -1.0))[0], empty)

check("у трея фон прозрачный", mark.tray(32).getpixel((0, 0))[3], 0)
check("у приложения — плитка", mark.app(32).getpixel((16, 3))[3] > 200, True)

with tempfile.TemporaryDirectory() as tmp:
    path = mark.save_ico(Path(tmp) / "vahta.ico")
    from PIL import Image  # noqa: E402

    with Image.open(path) as ico:
        check("в .ico все размеры", sorted(ico.info["sizes"]),
              [(16, 16), (32, 32), (48, 48), (256, 256)])
        # мелкие размеры рисуются заново, а не ужимаются из большого: у 16 px
        # после уменьшения 256-го полосы сливаются в кашу
        ico.size = (16, 16)
        check("16 px нарисован отдельно",
              list(ico.convert("RGBA").get_flattened_data()),
              list(mark.app(16).get_flattened_data()))

print(f"\n{ok} проверок прошло, {fail} упало")
sys.exit(1 if fail else 0)
