"""Запуск при входе в систему — через ключ Run в реестре пользователя.

HKCU, не HKLM: прав администратора не требует и не трогает других
пользователей машины.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .paths import FROZEN

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "Vahta"


# Windows запускает с этим флагом — по нему приложение отличает вход в систему
# от того, что человек сам ткнул в ярлык, и решает, показывать ли окно.
FLAG = "--autostart"


def _command() -> str:
    if FROZEN:
        return f'"{Path(sys.executable).resolve()}" {FLAG}'
    # из исходников — тем же интерпретатором, но без консольного окна
    root = Path(__file__).resolve().parent.parent
    quiet = Path(sys.executable).with_name("pythonw.exe")
    exe = quiet if quiet.exists() else Path(sys.executable)
    return f'"{exe}" "{root / "main.py"}" {FLAG}'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value, _ = winreg.QueryValueEx(key, NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
