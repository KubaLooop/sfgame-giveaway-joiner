"""Čistá (bez I/O) logika: pozná start GA ze spamu, výhru a konec GA."""
from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from .config import Config
from .irc import ChatEvent

CANDIDATE_RE = re.compile(r"^!([^\s!]{2,30})$")
AUTH_BADGES = frozenset({"broadcaster", "moderator", "lead_moderator"})
HOST_BADGES = AUTH_BADGES | {"vip"}

AUTHORITY_TRIGGER_WINDOW = 15.0   # jak dlouho po zprávě moda/streamera čekat na opakování
LEARN_WINDOW = 3.0                # odpověď command bota do X s => je to command


# ------------------------------------------------------------------ signály
@dataclass
class GiveawayStart:
    word: str          # tak, jak ho píše většina chatu (bez "!")
    key: str           # lowercase
    users: int
    first_seen: float
    reason: str


@dataclass
class Winner:
    text: str
    sender: str


@dataclass
class Closed:
    text: str
    index: int | None
    total: int | None


@dataclass
class Announcement:
    sender: str
    text: str
    raw: str


@dataclass
class CommandLearned:
    key: str
    bot: str


# ------------------------------------------------------------------ detektor
class Detector:
    def __init__(self, cfg: Config, my_login: str = "", my_display: str = "", learned: set | None = None):
        self.cfg = cfg
        self.learned: set[str] = set(learned or ())
        self.hits: dict[str, deque] = defaultdict(deque)        # key -> (ts, login, raw)
        self.first_seen: dict[str, float] = {}
        self.authority_words: dict[str, tuple[float, str]] = {}  # key -> (ts, login)
        self.triggered_at: dict[str, float] = {}
        self.recent_candidates: deque = deque()                  # (ts, key, login)
        self._last_gc = 0.0
        self._nick_re: re.Pattern | None = None
        self.my_login = ""
        self.set_identity(my_login, my_display)
        self.reload_patterns()

    # -------------------------------------------------------------- setup
    def set_identity(self, login: str, display: str = "") -> None:
        self.my_login = (login or "").lower()
        names = {n for n in {login, display} if n}
        if not names:
            self._nick_re = None
            return
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        self._nick_re = re.compile(rf"(?<![\w])@?(?:{alt})(?![\w])", re.IGNORECASE)

    def reload_patterns(self) -> None:
        self.blacklist = {w.lower().lstrip("!") for w in self.cfg.command_blacklist}
        self.command_bots = {b.lower() for b in self.cfg.command_bots}
        self.giveaway_bots = {b.lower() for b in self.cfg.giveaway_bots}
        self.winner_re = self._compile(self.cfg.winner_regex, r"giveaway")
        self.closed_re = self._compile(self.cfg.closed_regex, r"giveaway\s*(\d+)\s*/\s*(\d+)\s*closed")

    @staticmethod
    def _compile(pattern: str, fallback: str) -> re.Pattern:
        try:
            return re.compile(pattern, re.IGNORECASE)
        except re.error:
            return re.compile(fallback, re.IGNORECASE)

    # -------------------------------------------------------------- helpers
    def mentions_me(self, text: str) -> bool:
        return bool(self._nick_re and self._nick_re.search(text))

    def is_authority(self, ev: ChatEvent) -> bool:
        return (ev.kind == "announcement" or bool(ev.badges & AUTH_BADGES)
                or ev.login in self.giveaway_bots)

    def is_command(self, key: str) -> bool:
        return key in self.blacklist or key in self.learned

    def _prune(self, dq: deque, cutoff: float) -> None:
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _gc(self, now: float) -> None:
        if now - self._last_gc < 5:
            return
        self._last_gc = now
        cutoff = now - self.cfg.window_s
        for key in list(self.hits):
            self._prune(self.hits[key], cutoff)
            if not self.hits[key]:
                del self.hits[key]
                self.first_seen.pop(key, None)
        for key, (ts, _) in list(self.authority_words.items()):
            if now - ts > AUTHORITY_TRIGGER_WINDOW:
                del self.authority_words[key]
        self._prune(self.recent_candidates, now - LEARN_WINDOW)

    def recent_users(self, key: str, seconds: float, now: float) -> int:
        dq = self.hits.get(key)
        if not dq:
            return 0
        return len({login for ts, login, _ in dq if ts >= now - seconds})

    def _in_cooldown(self, key: str, now: float) -> bool:
        ts = self.triggered_at.get(key)
        return ts is not None and now - ts < self.cfg.word_cooldown_s

    # -------------------------------------------------------------- main
    def feed(self, ev: ChatEvent) -> list:
        out: list = []
        if ev.kind not in ("privmsg", "announcement"):
            return out
        # Shared chat: zprávy z jiného kanálu (jejich GA nás nezajímá).
        src, room = ev.tags.get("source-room-id"), ev.tags.get("room-id")
        if src and room and src != room:
            return out
        now = ev.ts
        self._gc(now)
        text = ev.clean_text
        login = ev.login
        authority = self.is_authority(ev)

        if ev.kind == "announcement":
            out.append(Announcement(ev.display or login, text, ev.raw))

        if authority:
            m = self.closed_re.search(text)
            if m:
                idx = tot = None
                try:
                    idx, tot = int(m.group(1)), int(m.group(2))
                except (IndexError, TypeError, ValueError):
                    pass
                out.append(Closed(text, idx, tot))
                return out
            if login != self.my_login and self.mentions_me(text) and self.winner_re.search(text):
                out.append(Winner(text, ev.display or login))
                return out

        if login in self.command_bots:
            # Učit se jen z běžných odpovědí, ne z announcementů (např. Nightbot hlásí výherce).
            if ev.kind == "privmsg":
                self._maybe_learn(now, login, out)
            return out
        if login == self.my_login:
            return out

        m = CANDIDATE_RE.match(text)
        if not m:
            return out
        raw = m.group(1)
        key = raw.lower()
        self._prune(self.recent_candidates, now - LEARN_WINDOW)
        self.recent_candidates.append((now, key, login))
        if key in self.blacklist:
            return out

        dq = self.hits[key]
        self._prune(dq, now - self.cfg.window_s)
        if not dq:
            self.first_seen[key] = now
        dq.append((now, login, raw))
        if ev.badges & HOST_BADGES or login in self.giveaway_bots:
            self.authority_words[key] = (now, login)

        if self._in_cooldown(key, now):
            return out

        users = {u for _, u, _ in dq}
        reason = None
        if key in self.learned:
            if len(users) >= self.cfg.learned_override_users:
                reason = f"spam naučeného commandu ({len(users)} lidí za {self.cfg.window_s:.0f}s)"
        elif len(users) >= self.cfg.min_users:
            reason = f"spam ({len(users)} lidí za {self.cfg.window_s:.0f}s)"
        elif key in self.authority_words:
            a_ts, a_login = self.authority_words[key]
            if now - a_ts <= AUTHORITY_TRIGGER_WINDOW:
                followers = {u for ts, u, _ in dq if ts >= a_ts and u != a_login}
                if len(followers) >= self.cfg.authority_min_followers:
                    reason = f"{a_login} (mod/host) + {len(followers)} opakování"

        if reason:
            self.triggered_at[key] = now
            word = Counter(r for _, _, r in dq).most_common(1)[0][0]
            out.append(GiveawayStart(word, key, len(users), self.first_seen.get(key, now), reason))
        return out

    def _maybe_learn(self, now: float, bot: str, out: list) -> None:
        self._prune(self.recent_candidates, now - LEARN_WINDOW)
        keys = {k for _, k, _ in self.recent_candidates}
        if len(keys) != 1:
            return
        key = next(iter(keys))
        if self.is_command(key) or self._in_cooldown(key, now):
            return
        # Když to slovo zrovna spamuje hodně lidí, je to spíš GA než command.
        if self.recent_users(key, self.cfg.window_s, now) >= max(2, self.cfg.min_users // 2):
            return
        self.learned.add(key)
        out.append(CommandLearned(key, bot))
