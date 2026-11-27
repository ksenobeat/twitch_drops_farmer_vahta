"""Релизы GitHub → поля службы: что ставить, откуда и с каким хэшем.

Сеть здесь не нужна: проверяется только разбор ответа. Главное — инсталлер
без хэша не должен стать «готовым к скачиванию», а черновик или пререлиз —
«свежей версией».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import releases  # noqa: E402

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
    else:
        fail += 1
        print(f"  ПЛОХО {name}: {got!r} вместо {want!r}")


SHA = "a" * 64
GSHA = "b" * 64


def asset(name, digest=None):
    return {"name": name, "browser_download_url": f"https://dl.example/{name}",
            "digest": digest}


def fresh():
    return SimpleNamespace(latest_version="x", release_page="x", update_url="x",
                           update_sha256="x", games_built="x", games_url="x",
                           games_sha256="x")


# --- обычный релиз --------------------------------------------------------
s = fresh()
releases.apply(s, {
    "tag_name": "v0.6.0", "html_url": "https://github.com/o/r/releases/tag/v0.6.0",
    "assets": [asset("Vahta-Setup-0.6.0.exe", f"sha256:{SHA}"),
               asset("games-2026-11-01.json.gz", f"sha256:{GSHA}")],
}, None)
check("версия без v", s.latest_version, "0.6.0")
check("страница релиза", s.release_page, "https://github.com/o/r/releases/tag/v0.6.0")
check("инсталлер найден", s.update_url, "https://dl.example/Vahta-Setup-0.6.0.exe")
check("хэш инсталлера", s.update_sha256, SHA)
check("база игр из релиза", s.games_built, "2026-11-01")
check("хэш базы", s.games_sha256, GSHA)

# --- инсталлер без хэша — только плашка -----------------------------------
s = fresh()
releases.apply(s, {"tag_name": "0.6.1", "html_url": "h",
                   "assets": [asset("Vahta-Setup-0.6.1.exe")]}, None)
check("версия видна", s.latest_version, "0.6.1")
check("без хэша не качаем", (s.update_url, s.update_sha256), ("", ""))
check("базы нет — поля пустые", s.games_built, "")

# --- пререлиз не считается свежей версией ----------------------------------
s = fresh()
releases.apply(s, {"tag_name": "v9.0.0", "prerelease": True,
                   "assets": [asset("Vahta-Setup-9.0.0.exe", f"sha256:{SHA}")]}, None)
check("пререлиз не версия", s.latest_version, "")
check("и не обновление", s.update_url, "")

# --- база игр: берётся самая свежая из двух релизов ------------------------
s = fresh()
releases.apply(
    s,
    {"tag_name": "v0.6.0", "assets": [asset("games-2026-10-01.json.gz", f"sha256:{SHA}")]},
    {"tag_name": "games", "prerelease": True,
     "assets": [asset("games-2026-11-20.json.gz", f"sha256:{GSHA}"),
                asset("games-2026-09-01.json.gz", f"sha256:{SHA}")]},
)
check("свежая база из релиза games", s.games_built, "2026-11-20")
check("и её адрес", s.games_url, "https://dl.example/games-2026-11-20.json.gz")

# --- мусор вместо ответа ----------------------------------------------------
s = fresh()
releases.apply(s, None, "мусор")  # type: ignore[arg-type]
check("мусор — пустая версия", s.latest_version, "")
check("мусор — без обновления", s.update_url, "")
check("мусор — без базы", s.games_url, "")

# --- кривой digest не принимается ------------------------------------------
check("md5 не годится", releases._sha256({"digest": "md5:abc"}), "")
check("короткий sha не годится", releases._sha256({"digest": "sha256:abc"}), "")

print(f"releases: {ok} ок, {fail} плохо")
sys.exit(1 if fail else 0)
