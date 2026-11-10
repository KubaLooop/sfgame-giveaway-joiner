"""Síťová část: asyncio vlákno s Twitch IRC přes WebSocket, ticker pro engine."""
from __future__ import annotations

import asyncio
import logging
import queue
import random
import threading
import time

import websockets

from . import phrases
from .auth import TokenStore, ensure_valid
from .config import Config, load_learned, save_learned
from .detector import Detector
from .engine import Engine
from .irc import DEDUPE_SUFFIX, ChatEvent, parse_badges, parse_line, to_event
from .logsetup import RawLog
from .stats import StatsStore

log = logging.getLogger("sfg")
IRC_URI = "wss://irc-ws.chat.twitch.tv:443"


def _l(cat: str) -> dict:
    return {"extra": {"cat": cat}}


def pretty(text: str) -> str:
    return text.replace(DEDUPE_SUFFIX, " ⟨neviditelný znak⟩")


class Reconnect(Exception):
    pass


class BotRunner(threading.Thread):
    def __init__(self, cfg: Config, store: TokenStore, ui_q: queue.Queue | None = None):
        super().__init__(daemon=True, name="sfg-bot")
        self.cfg = cfg
        self.store = store
        self.ui_q = ui_q
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stop_evt: asyncio.Event | None = None
        self._stopping = False
        self.ws = None
        self.channel = cfg.channel.lower().lstrip("#").strip()
        self.detector = Detector(cfg, learned=load_learned())
        self.stats = StatsStore()
        self.engine = Engine(cfg, self.detector, phrases.PhrasePicker(phrases.load()), on_learned=save_learned,
                             on_stat=self._persist_stat, on_win=self._persist_win)
        self.raw = RawLog(cfg.raw_log, cfg.log_retention_days)
        self.sim_until = 0.0
        self._pending_confirm = ""
        self.login = ""
        self.me = {"display": "", "color": "", "badges": []}   # jak nás vidí Twitch (z USERSTATE)

    # ------------------------------------------------------------ statistiky
    def _simulating(self) -> bool:
        return time.time() < self.sim_until

    def _persist_stat(self, name: str) -> None:
        if not self._simulating():
            self.stats.add(name)

    def _persist_win(self, text: str) -> None:
        if not self._simulating():
            self.stats.log_win(text)

    # ------------------------------------------------------------ thread API (volá GUI)
    def _ui(self, item) -> None:
        if self.ui_q is not None:
            self.ui_q.put(item)

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("Bot spadl")
        finally:
            self.raw.close()
            self._ui(("stopped",))

    def _call(self, fn, *args) -> None:
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        self._stopping = True
        self._call(self._do_stop)

    def reload_settings(self) -> None:
        def _reload():
            self.detector.reload_patterns()
            self.engine.phrases.set(phrases.load())
            self.raw.enabled = self.cfg.raw_log
            self.raw.retention_days = self.cfg.log_retention_days
            self.detector.learned = load_learned()
            if not self.login:
                self.detector.set_identity(self.cfg.watch_nick)
            log.info("Nastavení načteno.", **_l("info"))
        self._call(_reload)

    def send_manual(self, text: str) -> None:
        self._call(lambda: asyncio.ensure_future(self._send_manual(text)))

    def simulate(self, what: str) -> None:
        self._call(lambda: asyncio.ensure_future(self._simulate(what)))

    # ------------------------------------------------------------ async
    def _do_stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close())

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop_evt = asyncio.Event()
        if self._stopping:
            return
        ticker = asyncio.create_task(self._ticker())
        backoff = 1
        while not self._stop_evt.is_set():
            started = time.time()
            try:
                await self._session()
            except Reconnect:
                log.info("Twitch požádal o reconnect.", **_l("info"))
            except Exception as e:
                if self._stop_evt.is_set():
                    break
                log.warning(f"Spojení spadlo: {e!r}", **_l("warn"))
            if self._stop_evt.is_set():
                break
            if time.time() - started > 60:
                backoff = 1
            log.info(f"Znovu se připojím za {backoff}s…", **_l("info"))
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        ticker.cancel()
        log.info("Zastaveno.", **_l("info"))

    async def _session(self) -> None:
        tok = await asyncio.to_thread(ensure_valid, self.cfg.client_id, self.store)
        if tok:
            nick, self.login = tok.login, tok.login
            self.engine.can_send = True
            self.detector.set_identity(tok.login)
        else:
            nick = f"justinfan{random.randint(10000, 99999)}"
            self.login = ""
            self.engine.can_send = False
            self.detector.set_identity(self.cfg.watch_nick)
            log.info("Nepřihlášen – připojuji se anonymně (jen čtení).", **_l("warn"))
        self._ui(("identity", self.login))

        async with websockets.connect(IRC_URI, max_size=2 ** 20, open_timeout=15) as ws:
            self.ws = ws
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            if tok:
                await ws.send(f"PASS oauth:{tok.access_token}")
            await ws.send(f"NICK {nick}")
            await ws.send(f"JOIN #{self.channel}")
            reval = asyncio.create_task(self._revalidate()) if tok else None
            try:
                async for frame in ws:
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", "replace")
                    for line in frame.split("\r\n"):
                        if line:
                            await self._on_line(ws, line, nick)
            finally:
                self.ws = None
                if reval:
                    reval.cancel()

    async def _on_line(self, ws, line: str, nick: str) -> None:
        now = time.time()
        self.raw.write(now, line)
        if line.startswith("PING"):
            await ws.send("PONG" + line[4:])
            return
        msg = parse_line(line)
        if msg is None:
            return
        cmd = msg.command
        if cmd == "001":
            log.info(f"Připojeno k Twitchi jako {nick}.", **_l("info"))
        elif cmd == "JOIN" and msg.nick.lower() == nick.lower():
            log.info(f"Sleduji kanál #{self.channel}. Čekám na giveaway…", **_l("info"))
        elif cmd in ("USERSTATE", "GLOBALUSERSTATE"):
            self._update_me(msg.tags, channel=(cmd == "USERSTATE"))
            if cmd == "USERSTATE" and self._pending_confirm:
                log.info(f"✓ Twitch zprávu přijal: {pretty(self._pending_confirm)}", **_l("send"))
                self._pending_confirm = ""
        elif cmd == "NOTICE" and ("authentication failed" in msg.trailing.lower()
                                  or "improperly formatted auth" in msg.trailing.lower()):
            log.error("Twitch odmítl přihlášení – zkusím obnovit token.", **_l("error"))
            raise Reconnect()
        elif cmd == "RECONNECT":
            raise Reconnect()
        ev = to_event(msg, now, line)
        if ev is not None:
            if ev.kind == "notice" and self._pending_confirm:
                self._pending_confirm = ""
            self._chat_event(ev)
            self.engine.handle_event(ev)

    def _update_me(self, tags: dict, channel: bool) -> None:
        display = tags.get("display-name", "")
        if display:
            self.me["display"] = display
            if self.login:
                self.detector.set_identity(self.login, display)
        if tags.get("color"):
            self.me["color"] = tags["color"]
        if channel:                       # odznaky v tomhle kanálu (sub, VIP…)
            self.me["badges"] = sorted(parse_badges(tags.get("badges", "")))

    def _chat_event(self, ev: ChatEvent) -> None:
        """Pošle zprávu do záložky Chat v GUI (neukládá se nikam)."""
        if ev.kind not in ("privmsg", "announcement"):
            return
        src, room = ev.tags.get("source-room-id"), ev.tags.get("room-id")
        self._ui(("chat", {
            "ts": ev.ts, "name": ev.display or ev.login, "login": ev.login,
            "color": ev.tags.get("color", ""), "badges": sorted(ev.badges), "text": ev.text,
            "kind": ev.kind, "own": bool(self.login) and ev.login == self.login,
            "shared": bool(src and room and src != room),
        }))

    def _own_chat(self, text: str, auto: bool) -> None:
        self._ui(("chat", {"ts": time.time(), "name": self.me["display"] or self.login, "login": self.login,
                           "color": self.me["color"], "badges": self.me["badges"],
                           "text": pretty(text), "kind": "privmsg", "own": True,
                           "auto": auto, "shared": False}))

    async def _send_manual(self, text: str) -> None:
        text = " ".join(text.split())[:500]
        if not text:
            return
        if not self.engine.can_send or self.ws is None:
            log.warning("Zprávu nejde poslat – nejsi přihlášený nebo appka není připojená.", **_l("warn"))
            return
        await self.ws.send(f"PRIVMSG #{self.channel} :{text}")
        self.engine.last_sent = time.time()      # ať automatika nepřekročí slow mode
        self._pending_confirm = text
        log.info(f"Ruční zpráva odeslána: {text}", **_l("send"))
        self._own_chat(text, auto=False)

    async def _revalidate(self) -> None:
        while True:
            await asyncio.sleep(3600)
            tok = await asyncio.to_thread(ensure_valid, self.cfg.client_id, self.store)
            if not tok:
                log.error("Přihlášení vypršelo – přihlas se znovu.", **_l("error"))
                self._ui(("identity", ""))
                if self.ws is not None:
                    await self.ws.close()
                return

    async def _ticker(self) -> None:
        last_status = 0.0
        while True:
            now = time.time()
            try:
                for text, kind in self.engine.tick(now):
                    await self._send(text, kind, now)
            except Exception:
                log.exception("Chyba v tickeru")
            if now - last_status >= 0.5:
                st = self.engine.status(now)
                st["persist"] = self.stats.snapshot()
                self._ui(("status", st))
                last_status = now
            await asyncio.sleep(0.2)

    async def _send(self, text: str, kind: str, now: float) -> None:
        label = "ZAPOJENÍ" if kind == "entry" else "ODPOVĚĎ NA VÝHRU"
        if now < self.sim_until:
            log.info(f"[SIMULACE] {label}: {pretty(text)}", **_l("send"))
            return
        if self.cfg.dry_run:
            log.info(f"[DRY-RUN] {label} by poslal: {pretty(text)}", **_l("send"))
            return
        if not self.engine.can_send or self.ws is None:
            log.warning(f"Nemůžu psát (nepřihlášen / odpojen): {pretty(text)}", **_l("warn"))
            return
        await self.ws.send(f"PRIVMSG #{self.channel} :{text}")
        self._pending_confirm = text
        log.info(f"{label} odesláno: {pretty(text)}", **_l("send"))
        self._own_chat(text, auto=True)

    # ------------------------------------------------------------ simulace (jen dry-run)
    def _sim_event(self, ev: ChatEvent) -> None:
        self._chat_event(ev)
        self.engine.handle_event(ev)

    async def _simulate(self, what: str) -> None:
        self.sim_until = time.time() + 150
        room = {"room-id": "sim"}
        if what == "ga":
            word = "Test" + "".join(random.choice("abcdefghjkmnpqrstuvwxyz") for _ in range(5))
            log.info(f"[SIMULACE] 25 s spamu !{word} od falešných diváků…", **_l("info"))
            end = time.time() + 25
            i = 0
            while time.time() < end:
                self._sim_event(ChatEvent("privmsg", login=f"sim_user{i % 40}",
                                                   display=f"sim_user{i % 40}", text="!" + word,
                                                   tags=room, ts=time.time()))
                i += 1
                await asyncio.sleep(0.4)
        elif what == "win":
            me = self.login or self.cfg.watch_nick
            if not me:
                log.warning("Pro simulaci výhry se přihlas nebo vyplň 'Hlídaný nick' v nastavení.", **_l("warn"))
                return
            self._sim_event(ChatEvent("announcement", login="nightbot", display="Nightbot",
                                               badges=frozenset({"moderator", "partner"}),
                                               text=f"Congratulations, {me}! You won the giveaway!",
                                               tags=room, ts=time.time(), msg_id="announcement"))
        elif what == "closed":
            self._sim_event(ChatEvent("announcement", login="spidderofficial",
                                               display="SpidderOfficial", badges=frozenset({"moderator"}),
                                               text="Giveaway 1/3 Closed", tags=room,
                                               ts=time.time(), msg_id="announcement"))
