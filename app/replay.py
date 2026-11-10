"""Přehrání raw logu (`raw-YYYY-MM-DD.log`) přes detektor+engine – bez sítě, v simulovaném čase."""
from __future__ import annotations

import logging
import random

from . import phrases
from .config import Config
from .detector import Detector
from .engine import Engine
from .irc import DEDUPE_SUFFIX, parse_line, to_event

log = logging.getLogger("sfg")


class _ReplayClock(logging.Filter):
    """Přepíše čas log záznamů na čas z přehrávaného logu."""
    now = 0.0

    def filter(self, record):
        if self.now:
            record.created = self.now
        return True


def replay(path: str, cfg: Config, nick: str = "", step: float = 0.2) -> Engine:
    det = Detector(cfg, nick, learned=set())
    eng = Engine(cfg, det, phrases.PhrasePicker(phrases.load()), random.Random(0))
    clock = None
    count = 0
    rc = _ReplayClock()
    for h in log.handlers:
        h.addFilter(rc)
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            ts_s, _, line = raw.rstrip("\n").partition("\t")
            try:
                ts = float(ts_s)
            except ValueError:
                continue
            if clock is None:
                clock = ts
            while clock < ts:          # ticky mezi zprávami
                rc.now = clock
                _tick(eng, clock)
                clock += step
            rc.now = ts
            msg = parse_line(line)
            if msg is None:
                continue
            if msg.command == "GLOBALUSERSTATE" and nick:
                det.set_identity(nick, msg.tags.get("display-name", ""))
            ev = to_event(msg, ts, line)
            if ev is not None:
                eng.handle_event(ev)
                count += 1
    if clock is not None:
        for _ in range(int(120 / step)):
            _tick(eng, clock)
            clock += step
    log.info(f"Replay hotov: {count} eventů, statistiky: {vars(eng.stats)}", extra={"cat": "info"})
    return eng


def _tick(eng: Engine, now: float) -> None:
    for text, kind in eng.tick(now):
        log.info(f"[REPLAY] {kind}: {text.replace(DEDUPE_SUFFIX, ' ⟨inv⟩')}", extra={"cat": "send"})
