"""Logování: denní soubor + fronta pro GUI + volitelně konzole. Raw IRC log zvlášť."""
from __future__ import annotations

import logging
import os
import queue
import re
import sys
import time

from .config import logs_dir

log = logging.getLogger("sfg")


class QueueToGui(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__(logging.INFO)
        self.q = q

    def emit(self, record):
        try:
            cat = getattr(record, "cat", "info")
            if record.levelno >= logging.ERROR:
                cat = "error"
            text = time.strftime("%H:%M:%S", time.localtime(record.created)) + "  " + record.getMessage()
            self.q.put(("log", cat, text))
        except Exception:
            pass


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.log$")


def cleanup_old_logs(days: int, folder: str | None = None, today: str | None = None) -> list[str]:
    """Smaže denní logy (`YYYY-MM-DD.log`, `raw-YYYY-MM-DD.log`) starší než `days` dní."""
    if not days or days <= 0:
        return []
    folder = folder or logs_dir()
    today_t = time.mktime(time.strptime(today or time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    removed = []
    for name in os.listdir(folder):
        m = _DATE_RE.search(name)
        if not m:
            continue
        try:
            age_days = round((today_t - time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))) / 86400)
        except ValueError:
            continue
        if age_days >= days:
            try:
                os.remove(os.path.join(folder, name))
                removed.append(name)
            except OSError:
                pass            # soubor je zrovna otevřený apod. – zkusí se příště
    return removed


def setup(gui_queue: queue.Queue | None = None, console: bool = False, retention_days: int = 0) -> None:
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = logging.FileHandler(os.path.join(logs_dir(), time.strftime("%Y-%m-%d") + ".log"), encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)
    if gui_queue is not None:
        log.addHandler(QueueToGui(gui_queue))
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        sh.setLevel(logging.INFO)
        log.addHandler(sh)
    removed = cleanup_old_logs(retention_days)
    if removed:
        log.info(f"Smazány staré logy ({len(removed)} souborů, starší než {retention_days} dny).",
                 extra={"cat": "info"})


class RawLog:
    """Všechny syrové IRC řádky ve formátu `<epoch>\\t<line>` – vstup pro --replay."""

    def __init__(self, enabled: bool, retention_days: int = 0):
        self.enabled = enabled
        self.retention_days = retention_days
        self._f = None
        self._day = ""

    def write(self, ts: float, line: str) -> None:
        if not self.enabled:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day != self._day or self._f is None:
            self.close()
            if self._day:                 # přechod přes půlnoc -> uklidit staré logy
                cleanup_old_logs(self.retention_days)
            self._f = open(os.path.join(logs_dir(), f"raw-{day}.log"), "a", encoding="utf-8")
            self._day = day
        self._f.write(f"{ts:.3f}\t{line}\n")
        self._f.flush()

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None
