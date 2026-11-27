"""Пересобрать `vahta/static/vahta.ico` из знака.

    env\\Scripts\\python.exe tools\\make_ico.py

Файл лежит в репозитории готовым: он нужен окну ещё до сборки, а в exe
попадает вместе со статикой. `build.py` зовёт то же самое перед упаковкой,
чтобы иконка приложения не разошлась со знаком.
"""

from __future__ import annotations

import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

from vahta import mark  # noqa: E402
from vahta.paths import ICO_PATH  # noqa: E402

if __name__ == "__main__":
    path = mark.save_ico(ICO_PATH)
    print(f"{path}  ({path.stat().st_size / 1024:.0f} КБ)")
