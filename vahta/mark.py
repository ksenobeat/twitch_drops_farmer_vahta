"""Знак «Вахты»: иконка в трее, иконка приложения, файл `.ico`.

Рисуется кодом, а не берётся картинкой, и это не от бедности. Средняя полоса
знака — индикатор: она растёт по прогрессу смены и краснеет при конфликте
(`design/logo/vaxta-mark-progress.svg`), то есть иконка в трее меняется по
десять раз за смену. Хранить два десятка PNG ради этого незачем, а тащить
в зависимости растеризатор SVG — тем более: знак это три скруглённых
прямоугольника.

Геометрия — та же, что в `design/logo/*.svg`: холст 32×32, полосы 4 единицы
высотой, длина убывает, средняя акцентная, силуэт читается как V.

Два вида, и они разные не по прихоти:

- **трей** — голые полосы на прозрачном. Рядом с ним в панели такие же
  однотонные значки, и плитка с фоном выбивалась бы. Цвет приглушённой полосы
  выбран так, чтобы читаться и на тёмной панели, и на светлой.
- **приложение** — те же полосы на скруглённой плитке. Значок в панели задач,
  в alt-tab и в проводнике ложится на что угодно, и прозрачный знак там
  пропадал бы через раз.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# viewBox из design/logo: (x, y, ширина, высота) в единицах холста 32×32
UNIT = 32
BARS = ((6, 7, 20, 4), (9, 14, 14, 4), (12, 21, 8, 4))
RADIUS = 2

# tokens.md: accent тёмной темы, danger, и приглушённый цвет полос.
# Для трея приглушённый берётся не из темы, а средним между тёмной и светлой
# панелью: одна иконка живёт на обеих, и системе всё равно, что у нас в CSS
ACCENT = (169, 112, 255, 255)
DANGER = (255, 107, 139, 255)
TRAY_DIM = (140, 133, 160, 255)
TILE = (28, 24, 38, 255)
TILE_DIM = (216, 204, 242, 255)

# Полосу рисуем шагами в 1/16 длины: иконка 16 px, и более мелкий шаг она
# всё равно не покажет, а лишние перерисовки трея — это мигание в панели
STEPS = 16

# Сглаживания у GDI нет, поэтому рисуем крупно и уменьшаем
SUPERSAMPLE = 4


def _draw(size: int, dim, progress: float, bad: bool, tile) -> Image.Image:
    big = size * SUPERSAMPLE
    scale = big / UNIT
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if tile is not None:
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.22, fill=tile)

    share = max(0.0, min(1.0, progress))
    share = round(share * STEPS) / STEPS
    for index, (x, y, width, height) in enumerate(BARS):
        middle = index == 1
        if middle:
            # средняя полоса — индикатор. Меньше своей высоты не бывает:
            # прямоугольник короче скругления вырождается в точку
            width = max(height, width * share)
            colour = DANGER if bad else ACCENT
        else:
            colour = dim
        draw.rounded_rectangle(
            (x * scale, y * scale, (x + width) * scale - 1, (y + height) * scale - 1),
            radius=RADIUS * scale,
            fill=colour,
        )
    return image.resize((size, size), Image.LANCZOS)


def tray(size: int = 64, progress: float = 1.0, bad: bool = False) -> Image.Image:
    """Знак для иконки в трее: голые полосы, средняя показывает прогресс."""
    return _draw(size, TRAY_DIM, progress, bad, tile=None)


def app(size: int = 256) -> Image.Image:
    """Знак приложения: те же полосы на скруглённой плитке."""
    return _draw(size, TILE_DIM, 1.0, False, tile=TILE)


def save_ico(path: Path) -> Path:
    """Собрать `.ico` со всеми размерами, которые спрашивает Windows.

    16 — панель задач и заголовок, 32 — alt-tab, 48 — крупные значки
    проводника, 256 — плитки и «огромные значки». Каждый размер рисуется
    отдельно, а не пересчитывается из большого: у 16 px после уменьшения
    256-го полосы сливаются в кашу.
    """
    sizes = (16, 32, 48, 256)
    frames = [app(size) for size in sizes]
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(path, format="ICO", sizes=[(s, s) for s in sizes],
                    append_images=frames[:-1])
    return path
