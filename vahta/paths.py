"""Где что лежит.

Из исходников и из собранного exe пути разные, и путать их нельзя: данные
внутри бандла PyInstaller живут во временной папке и стираются при выходе.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)


def resources() -> Path:
    """Папка с ресурсами (статика веб-интерфейса)."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Папка с данными пользователя — токены, база, лицензия.

    В собранном виде — рядом с exe, если туда можно писать (портативный режим),
    иначе в профиле пользователя.
    """
    if configured := os.environ.get("VAHTA_DATA_DIR"):
        return Path(configured).expanduser().resolve()
    if not FROZEN:
        return Path(__file__).resolve().parent.parent / "data"

    beside = Path(sys.executable).resolve().parent / "data"
    try:
        beside.mkdir(parents=True, exist_ok=True)
        probe = beside / ".write-test"
        probe.write_text("", "utf8")
        probe.unlink()
        return beside
    except OSError:
        # Program Files и подобные — пишем в профиль
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "Vahta"


DATA_DIR = data_dir()
DB_PATH = DATA_DIR / "vahta.db"
STATIC_DIR = resources() / "vahta" / "static"
# иконка приложения: её просит окно (панель задач, alt-tab), сборка (exe)
# и инсталлер. Лежит рядом со статикой — та и так едет внутрь бандла
ICO_PATH = STATIC_DIR / "vahta.ico"
