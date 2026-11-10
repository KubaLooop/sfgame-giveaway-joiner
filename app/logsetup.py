"""Logování: denní soubor + fronta pro GUI + volitelně konzole. Raw IRC log zvlášť."""
from __future__ import annotations

import logging
import os
import queue
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


def setup(gui_queue: queue.Queue | None = None, console: bool = False) -> None:
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


class RawLog:
    """Všechny syrové IRC řádky ve formátu `<epoch>\\t<line>` – vstup pro --replay."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._f = None
        self._day = ""

    def write(self, ts: float, line: str) -> None:
        if not self.enabled:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day != self._day or self._f is None:
            self.close()
            self._f = open(os.path.join(logs_dir(), f"raw-{day}.log"), "a", encoding="utf-8")
            self._day = day
        self._f.write(f"{ts:.3f}\t{line}\n")
        self._f.flush()

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None
