"""Сборка «Вахты».

    env\\Scripts\\python.exe build.py

Папкой, а не одним файлом. Onefile при каждом запуске распаковывает себя
во временную папку: старт две-три секунды и лишний износ диска, а «Вахту»
запускают каждый день и держат запущенной. Инсталлер всё равно кладёт
папку целиком, так что единственное, ради чего onefile был удобен —
«скачал и запустил», — закрывается инсталлером.

Статика веб-интерфейса и иконка едут внутрь бандла, `paths.resources()`
достаёт их оттуда.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Вахта"
DIST = ROOT / "dist" / NAME


def folder_size(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Нет PyInstaller. Поставь:")
        print(r"    env\Scripts\python.exe -m pip install pyinstaller")
        return 1

    for stale in (ROOT / "build", ROOT / "dist"):
        shutil.rmtree(stale, ignore_errors=True)

    # иконку пересобираем сами: она рисуется тем же кодом, что и знак в трее,
    # и разойтись с ним не должна
    sys.path.insert(0, str(ROOT))
    from vahta import __version__, mark
    from vahta.paths import ICO_PATH

    mark.save_ico(ICO_PATH)

    # версию инсталлер берёт отсюда, а не пишет у себя: второй список версий
    # однажды разошёлся бы с первым
    (ROOT / "installer" / "version.iss").write_text(
        f'#define AppVersion "{__version__}"\n', "utf8"
    )

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir",
        "--noconsole",
        "--name", NAME,
        "--icon", str(ICO_PATH),
        "--add-data", f"{ROOT / 'vahta' / 'static'}{os.pathsep}vahta/static",
        # база игр для подсказок при вводе. Собирается отдельно
        # (`tools/games.py`) и сама не обновляется: сборка не должна требовать
        # ни сети, ни ключей Twitch — поэтому дата базы просто печатается ниже,
        # чтобы забытое обновление было видно глазами
        "--add-data", f"{ROOT / 'vahta' / 'games.json.gz'}{os.pathsep}vahta",
        # uvicorn и fastapi подтягивают часть модулей динамически
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.wsproto_impl",
        "--hidden-import", "uvicorn.lifespan.on",
        # окно: pywebview выбирает бэкенд в рантайме и везёт с собой JS,
        # а winforms-бэкенд держится на pythonnet и его сборках .NET
        "--collect-all", "webview",
        "--collect-all", "clr_loader",
        "--collect-all", "pythonnet",
        "--collect-submodules", "vahta",
        str(ROOT / "main.py"),
    ]
    from vahta import games
    база = ROOT / "vahta" / games.ИМЯ
    if not база.is_file():
        print(f"Нет {база.name} — собери её: env\Scripts\python.exe tools\games.py")
        return 1
    print(f"База игр: от {games.собрана(база) or '?'}, "
          f"{база.stat().st_size / 1000:.0f} КБ")

    print("Собираю...")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode:
        return result.returncode

    exe = DIST / f"{NAME}.exe"
    if not exe.exists():
        print(f"PyInstaller отработал, но {exe} нет")
        return 1

    print(f"\nГотово: {DIST}  ({folder_size(DIST):.0f} МБ, {exe.name} внутри)")
    print("Данные лягут в папку data рядом с exe.")
    print("Инсталлер — installer/vahta.iss (нужен Inno Setup 6).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
