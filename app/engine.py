"""Stavový automat: IDLE -> ENTERING -> ARMED -> (WON) -> IDLE.

Engine nedělá žádné I/O ani nespí. Volající mu předává eventy (`handle_event`)
a pravidelně `tick(now)`, který vrátí zprávy k odeslání. Díky tomu jde stejný kód
použít live, v replay režimu i v testech.

Zapojování umí víc slov najednou: když chat nejdřív spamuje podvrh (!slovo1) a pak
se objeví skutečné slovo (!slovo2), zapojí se appka i do nového. Nejnovější slovo má
přednost, starší se opustí, jakmile ho přestane spamovat dost lidí.
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field

from .config import Config
from .detector import Announcement, Closed, CommandLearned, Detector, GiveawayStart, Winner
from .irc import DEDUPE_SUFFIX, ChatEvent
from .phrases import PhrasePicker

log = logging.getLogger("sfg")

IDLE, ENTERING, ARMED, WON = "IDLE", "ENTERING", "ARMED", "WON"
STAT_NAMES = ("detected", "entered", "wins", "sent", "closed")


def _l(cat: str) -> dict:
    return {"extra": {"cat": cat}}


@dataclass
class Stats:
    detected: int = 0
    entered: int = 0
    wins: int = 0
    sent: int = 0
    closed: int = 0


@dataclass
class Outgoing:
    due: float
    text: str
    kind: str   # entry | win


@dataclass
class Entry:
    """Zapojování do jednoho konkrétního !slova."""
    word: str
    key: str
    first_seen: float
    started: float
    next_due: float
    sent: int = 0
    done: bool = False


@dataclass
class Engine:
    cfg: Config
    detector: Detector
    phrases: PhrasePicker
    rng: random.Random = field(default_factory=random.Random)
    on_learned: object = None          # callable(set[str]) – uložení naučených commandů
    on_stat: object = None             # callable(name) – perzistentní statistiky
    on_win: object = None              # callable(text) – záznam výhry

    def __post_init__(self):
        self.state = IDLE
        self.stats = Stats()
        self.slow = 0
        self.entries: list[Entry] = []
        self.word = ""                  # poslední / hlavní slovo (pro zobrazení)
        self.armed_until = 0.0
        self.last_sent = -1e9
        self.last_text = ""
        self.last_win = -1e9
        self.outbox: list[Outgoing] = []
        self.paused_until = 0.0
        self.banned = False
        self.can_send = True            # False = anonymní připojení

    # ------------------------------------------------------------ helpers
    def _stat(self, name: str) -> None:
        setattr(self.stats, name, getattr(self.stats, name) + 1)
        if callable(self.on_stat):
            self.on_stat(name)

    def min_interval(self) -> float:
        return max(self.slow + 0.4, 1.2)

    def active_entries(self) -> list[Entry]:
        return [e for e in self.entries if not e.done]

    def status(self, now: float) -> dict:
        active = self.active_entries()
        remaining = 0.0
        words = [e.word for e in sorted(active, key=lambda e: -e.started)]
        if active:
            remaining = max(0.0, max(e.first_seen for e in active) + self.cfg.entry_duration_s - now)
        return {
            "state": self.state, "word": self.word, "words": words, "remaining": remaining,
            "slow": self.slow, "banned": self.banned,
            "paused": max(0.0, self.paused_until - now),
            "stats": vars(self.stats).copy(),
        }

    # ------------------------------------------------------------ input
    def handle_event(self, ev: ChatEvent) -> None:
        now = ev.ts
        if ev.kind == "roomstate":
            if "slow" in ev.tags:
                try:
                    new = int(ev.tags["slow"] or 0)
                except ValueError:
                    new = self.slow
                if new != self.slow:
                    log.info(f"Slow mode: {new}s", **_l("info"))
                self.slow = new
            return
        if ev.kind == "notice":
            self._on_notice(ev, now)
            return
        if ev.kind == "clearchat" and ev.login and ev.login == self.detector.my_login:
            dur = ev.tags.get("ban-duration")
            if dur:
                self.paused_until = now + float(dur)
                log.error(f"Dostal jsi timeout na {dur}s – psaní pozastaveno.", **_l("error"))
            else:
                self.banned = True
                log.error("Dostal jsi BAN na kanálu – psaní vypnuto.", **_l("error"))
            return

        for sig in self.detector.feed(ev):
            if isinstance(sig, GiveawayStart):
                self._on_start(sig, now)
            elif isinstance(sig, Winner):
                self._on_win(sig, now)
            elif isinstance(sig, Closed):
                self._on_closed(sig, now)
            elif isinstance(sig, Announcement):
                log.info(f"ANNOUNCEMENT {sig.sender}: {sig.text}", **_l("ann"))
                log.debug(f"RAW ANNOUNCEMENT: {sig.raw}", **_l("raw"))
            elif isinstance(sig, CommandLearned):
                log.info(f"Naučený command: !{sig.key} (odpověděl {sig.bot}) – nebude spouštět GA",
                         **_l("info"))
                if callable(self.on_learned):
                    self.on_learned(set(self.detector.learned))

    def _on_notice(self, ev: ChatEvent, now: float) -> None:
        mid = ev.msg_id
        if mid == "msg_duplicate":
            log.warning(f"Twitch zahodil zprávu jako duplikát ({ev.text})", **_l("warn"))
        elif mid in ("msg_slowmode", "msg_ratelimit"):
            log.warning(f"Rate-limit/slow mode: {ev.text}", **_l("warn"))
            self.last_sent = now
        elif mid == "msg_timedout":
            m = re.search(r"(\d+)", ev.text)
            secs = int(m.group(1)) if m else 60
            self.paused_until = now + secs
            log.error(f"Timeout: {ev.text}", **_l("error"))
        elif mid in ("msg_banned", "msg_channel_suspended"):
            self.banned = True
            log.error(f"Nelze psát: {ev.text}", **_l("error"))
        elif mid in ("msg_followersonly", "msg_followersonly_zero", "msg_subsonly", "msg_emoteonly",
                     "msg_verified_email", "msg_requires_verified_phone_number", "msg_r9k"):
            log.error(f"Zpráva odmítnuta ({mid}): {ev.text}", **_l("error"))
        elif ev.text:
            log.info(f"NOTICE {mid}: {ev.text}", **_l("info"))

    def _on_start(self, sig: GiveawayStart, now: float) -> None:
        if any(e.key == sig.key and not e.done for e in self.entries):
            return
        others = [e.word for e in self.active_entries()]
        self._stat("detected")
        self.entries.append(Entry(sig.word, sig.key, sig.first_seen, now,
                                  next_due=now + self.rng.uniform(0.3, 1.2)))
        self.entries = [e for e in self.entries if not e.done or now - e.started < 600]
        self.word = sig.word
        self.state = ENTERING
        self.outbox = [o for o in self.outbox if o.kind != "entry"]
        extra = f" (souběžně s {', '.join('!' + w for w in others)} – nové slovo má přednost)" if others else ""
        log.info(f"GA DETEKOVÁNA: !{sig.word} – {sig.reason}{extra}", **_l("detect"))

    def _on_win(self, sig: Winner, now: float) -> None:
        if now - self.last_win < 45:
            log.info(f"(opakované oznámení výhry ignorováno) {sig.text}", **_l("win"))
            return
        self.last_win = now
        self._stat("wins")
        self.state = WON
        self.outbox = [o for o in self.outbox if o.kind != "entry"]
        for e in self.active_entries():
            self._finish(e, now, "výhra", quiet=True)
        due = now + self.rng.uniform(self.cfg.win_delay_min, self.cfg.win_delay_max)
        for _ in range(max(1, self.cfg.win_messages)):
            self.outbox.append(Outgoing(due, self.phrases.pick(), "win"))
            due += self.slow + self.rng.uniform(1.0, 2.0)
        log.info(f"VÝHRA! {sig.sender}: {sig.text}", **_l("win"))
        if callable(self.on_win):
            self.on_win(sig.text)

    def _on_closed(self, sig: Closed, now: float) -> None:
        """„Giveaway X/Y Closed“ = konec přihlášek, teprve pak se losuje."""
        self._stat("closed")
        self.outbox = [o for o in self.outbox if o.kind != "entry"]
        active = self.active_entries()
        if active:
            for e in active:
                self._finish(e, now, f"přihlášky uzavřeny ({sig.text})")
        elif self.state == IDLE:
            log.info(f"Přihlášky uzavřeny ({sig.text}) – do této GA jsem zapojený nebyl.", **_l("warn"))
        else:
            log.info(f"Přihlášky uzavřeny: {sig.text}", **_l("detect"))

    def _finish(self, e: Entry, now: float, reason: str, quiet: bool = False) -> None:
        e.done = True
        if e.sent:
            self._stat("entered")
        if not quiet:
            log.info(f"Konec zapojování !{e.word} ({reason}), odesláno {e.sent}x.", **_l("detect"))
        if not self.active_entries() and self.state == ENTERING:
            self.state = ARMED
            self.armed_until = now + self.cfg.armed_duration_s
            log.info("Čekám na losování…", **_l("detect"))

    # ------------------------------------------------------------ time
    def _update_entries(self, now: float) -> None:
        active = self.active_entries()
        if not active:
            return
        newest = max(active, key=lambda e: e.started)
        strong = max(2, self.cfg.min_users // 2)
        for e in active:
            elapsed = now - e.first_seen
            users = self.detector.recent_users(e.key, 8, now)
            reason = None
            if e.sent >= self.cfg.max_entry_messages:
                reason = f"odesláno max {e.sent} zpráv"
            elif elapsed >= self.cfg.entry_max_s:
                reason = "tvrdý časový strop"
            elif elapsed >= self.cfg.entry_duration_s and users < strong:
                reason = f"uplynulo {self.cfg.entry_duration_s:.0f}s"
            elif now - e.started >= 15 and users < 2:
                reason = "spam utichl"
            elif e is not newest and now - newest.started >= 3 and users < self.cfg.min_users:
                reason = f"chat přešel na !{newest.word}"
            if reason:
                self._finish(e, now, reason)

    def _interval(self, e: Entry, now: float) -> float:
        """Rozprostře max_entry_messages zpráv rovnoměrně do zbytku minuty, ale nikdy pod slow mode."""
        left_msgs = max(1, self.cfg.max_entry_messages - e.sent)
        left_time = max(10.0, e.first_seen + self.cfg.entry_duration_s - now)
        spread = left_time / left_msgs * self.rng.uniform(0.85, 1.15)
        return max(self.slow + self.rng.uniform(0.5, 1.5), spread)

    def tick(self, now: float) -> list[tuple[str, str]]:
        """Vrátí max. jednu zprávu [(text, kind)], kterou je teď potřeba odeslat."""
        self._update_entries(now)
        if self.state == ARMED and now > self.armed_until:
            self.state = IDLE
        if self.state == WON and not any(o.kind == "win" for o in self.outbox) and now - self.last_win > 30:
            self.state = ARMED if now < self.armed_until else IDLE

        if self.banned or now < self.paused_until:
            return []
        if now - self.last_sent < self.min_interval():
            return []

        due = sorted((o for o in self.outbox if o.due <= now), key=lambda o: o.due)
        if due:
            o = due[0]
            self.outbox.remove(o)
            return [self._emit(o.text, o.kind, now)]

        ready = [e for e in self.active_entries() if now >= e.next_due]
        if ready:
            e = max(ready, key=lambda x: x.started)        # nejnovější slovo má přednost
            text = "!" + e.word
            if self.cfg.dedupe_suffix and text == self.last_text:
                text += DEDUPE_SUFFIX
            e.sent += 1
            e.next_due = now + self._interval(e, now)
            return [self._emit(text, "entry", now)]
        return []

    def _emit(self, text: str, kind: str, now: float) -> tuple[str, str]:
        self.last_sent = now
        self.last_text = text
        self._stat("sent")
        return text, kind
