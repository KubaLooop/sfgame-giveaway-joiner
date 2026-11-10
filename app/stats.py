"""Trvalé statistiky (celkem + po dnech), přežijí restart appky."""
from __future__ import annotations

import json
import os
import time

from .config import app_dir

NAMES = ("detected", "entered", "wins", "sent", "closed")


def _empty() -> dict:
    return {n: 0 for n in NAMES}


class StatsStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(app_dir(), "stats.json")
        self.totals = _empty()
        self.days: dict[str, dict] = {}
        self.wins_log: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        self.totals.update({k: int(v) for k, v in data.get("totals", {}).items() if k in NAMES})
        self.days = {d: {**_empty(), **v} for d, v in data.get("days", {}).items()}
        self.wins_log = list(data.get("wins", []))[-200:]

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"totals": self.totals, "days": self.days, "wins": self.wins_log}, f, indent=2,
                      ensure_ascii=False)
        os.replace(tmp, self.path)

    @staticmethod
    def today() -> str:
        return time.strftime("%Y-%m-%d")

    def add(self, name: str, n: int = 1) -> None:
        if name not in NAMES:
            return
        self.totals[name] += n
        day = self.days.setdefault(self.today(), _empty())
        day[name] += n
        self.save()

    def log_win(self, text: str) -> None:
        self.wins_log.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text})
        self.wins_log = self.wins_log[-200:]
        self.save()

    def snapshot(self) -> dict:
        return {"totals": dict(self.totals), "today": dict(self.days.get(self.today(), _empty()))}
