"""Переключатели, которые человек трогает сам.

Отдельно от `auth.json` и `telegram.json`: там секреты и
состояние, а тут предпочтения — их не жалко потерять и не страшно показать.
"""

from __future__ import annotations

import json
from pathlib import Path

THEMES = ("system", "dark", "light")


class Settings:
    def __init__(self, path: Path):
        self.path = path
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text("utf8"))
            except (ValueError, OSError):
                pass
        # по умолчанию включены: иначе человек про них не узнает,
        # а выключатель на виду
        self.notifications: bool = bool(data.get("notifications", True))
        # крестик прячет «Вахту» в трей, а не выходит: фарм должен пережить
        # закрытие окна, ради этого весь трей и существует
        self.close_to_tray: bool = bool(data.get("close_to_tray", True))
        # при входе в систему окно не лезет: человек включил автозапуск, чтобы
        # фармило, а не чтобы окно вылезало при каждой загрузке
        self.window_on_autostart: bool = bool(data.get("window_on_autostart", False))
        # «как в системе» — не «светлая»: у человека уже выбрана тема Windows,
        # и переспрашивать про неё в третий раз незачем
        self.theme: str = data.get("theme") if data.get("theme") in THEMES else "system"
        # мастер первого запуска показан и закрыт. Живёт здесь, а не рядом
        # с токеном: это предпочтение, и потерять его не страшно — в худшем
        # случае мастер поздоровается второй раз
        self.onboarded: bool = bool(data.get("onboarded", False))
        # фармить кампании без привязанного игрового аккаунта. По умолчанию нет:
        # один раз мы видели, что Twitch по такой не открывает дроп-сессию, —
        # но документация Twitch говорит обратное, и 21 награда в инвентаре
        # получена без привязки. Пока вопрос открыт, выбор за человеком, и по
        # умолчанию — то поведение, которое точно ничего не теряет впустую
        self.farm_unlinked: bool = bool(data.get("farm_unlinked", False))
        # ставить обновления самой. По умолчанию да: «Вахта» работает месяцами
        # без присмотра, и версия, о которой человек узнал из плашки и забыл, —
        # это версия, в которой так и живут все известные беды. Ставится
        # не когда скачалось, а между ломтями фарма, и только тихо
        self.auto_update: bool = bool(data.get("auto_update", True))
        # размер и положение окна; пусто — открыть по центру
        self.window: dict = data.get("window") or {}

    def save(self) -> None:
        """Запись через временный файл.

        Писать сюда могут двое: интерфейс — переключатели, окно — свою
        геометрию. Потерянное обновление тут не страшно (в худшем случае окно
        один раз откроется не там), а вот обрезанный на полуслове файл сделал
        бы настройки нечитаемыми — от этого и страхуемся.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2), "utf8")
        tmp.replace(self.path)

    def as_dict(self) -> dict:
        return {
            "notifications": self.notifications,
            "close_to_tray": self.close_to_tray,
            "window_on_autostart": self.window_on_autostart,
            "theme": self.theme,
            "onboarded": self.onboarded,
            "farm_unlinked": self.farm_unlinked,
            "auto_update": self.auto_update,
            "window": self.window,
        }

    def payload(self) -> dict:
        """То, что показывается в интерфейсе. Геометрия окна человеку не нужна."""
        return {
            "notifications": self.notifications,
            "close_to_tray": self.close_to_tray,
            "window_on_autostart": self.window_on_autostart,
            "theme": self.theme,
            "onboarded": self.onboarded,
            "farm_unlinked": self.farm_unlinked,
            "auto_update": self.auto_update,
        }
