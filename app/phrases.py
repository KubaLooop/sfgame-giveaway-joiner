"""Slovník odpovědí při výhře. Uživatelská kopie žije v %APPDATA%/SfgGiveaway/phrases.txt."""
from __future__ import annotations

import os
import random
import shutil

from .config import app_dir, resource_path

FALLBACK = ["NICE!!!", "YESSS!", "FINALLY", "LETS GOOO", "THANK YOU!!", "here!!"]


def user_phrases_path() -> str:
    return os.path.join(app_dir(), "phrases.txt")


def ensure_user_file() -> str:
    path = user_phrases_path()
    if not os.path.exists(path):
        src = resource_path(os.path.join("data", "phrases.txt"))
        if os.path.exists(src):
            shutil.copyfile(src, path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(FALLBACK) + "\n")
    return path


def parse(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load() -> list[str]:
    try:
        with open(ensure_user_file(), encoding="utf-8") as f:
            items = parse(f.read())
        return items or list(FALLBACK)
    except Exception:
        return list(FALLBACK)


class PhrasePicker:
    """Náhodný výběr bez opakování posledních použitých frází."""

    def __init__(self, phrases: list[str], rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.set(phrases)

    def set(self, phrases: list[str]) -> None:
        self.phrases = list(phrases) or list(FALLBACK)
        self._recent: list[str] = []

    def pick(self) -> str:
        memory = min(len(self.phrases) - 1, 5)
        pool = [p for p in self.phrases if p not in self._recent[-memory:]] if memory > 0 else self.phrases
        choice = self.rng.choice(pool or self.phrases)
        self._recent.append(choice)
        self._recent = self._recent[-10:]
        return choice
