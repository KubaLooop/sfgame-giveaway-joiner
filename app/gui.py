"""CustomTkinter GUI."""
from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from tkinter import messagebox

import customtkinter as ctk

from . import logsetup, phrases
from .auth import TokenStore, ensure_valid, poll_device_token, start_device_flow
from .bot import BotRunner
from .config import APP_VERSION, Config, load_learned, logs_dir, save_learned
from .engine import ARMED, ENTERING, IDLE, WON
from .stats import StatsStore

log = logging.getLogger("sfg")

LOG_COLORS = {
    "detect": "#f0a030", "send": "#4cc38a", "ann": "#7aa7ff", "win": "#ffd700",
    "error": "#ff5c5c", "warn": "#e6c84f", "info": "#c8c8c8", "raw": "#808080",
}
MAX_LOG_LINES = 3000
MAX_CHAT_LINES = 600
BADGE_LABELS = {"broadcaster": "STREAMER", "lead_moderator": "MOD", "moderator": "MOD", "vip": "VIP",
                "partner": "✓", "staff": "STAFF"}
DEFAULT_NICK_COLORS = ["#FF4040", "#4f7dff", "#3cb371", "#d2691e", "#ff7f50", "#9acd32", "#ff4500",
                       "#2e8b57", "#daa520", "#ff69b4", "#5f9ea0", "#1e90ff", "#ba55d3", "#00c878"]


def _readable(color: str) -> str:
    """Twitch barvy nicků bývají na tmavém pozadí nečitelné – ztmavené zesvětlí."""
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return "#c8c8c8"
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    if lum < 0.45:
        k = 0.55
        r, g, b = (int(c + (255 - c) * k) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _split_list(text: str) -> list[str]:
    return [x.strip().lstrip("!") for x in text.replace("\n", ",").split(",") if x.strip()]


class DeviceDialog(ctk.CTkToplevel):
    def __init__(self, master, dev: dict, on_cancel):
        super().__init__(master)
        self.title("Přihlášení přes Twitch")
        self.geometry("420x260")
        self.resizable(False, False)
        self.transient(master)
        self.on_cancel = on_cancel
        url = dev.get("verification_uri", "https://www.twitch.tv/activate")
        code = dev.get("user_code", "")
        ctk.CTkLabel(self, text="1) Otevři stránku a přihlas se svým Twitch účtem\n2) Potvrď tento kód:",
                     justify="left").pack(pady=(18, 6))
        ctk.CTkLabel(self, text=code, font=ctk.CTkFont(size=34, weight="bold")).pack(pady=6)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=6)
        ctk.CTkButton(row, text="Otevřít twitch.tv/activate", command=lambda: webbrowser.open(url)).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Kopírovat kód", width=110, command=lambda: self._copy(code)).pack(side="left", padx=4)
        ctk.CTkLabel(self, text="Čekám na potvrzení… (okno se samo zavře)", text_color="gray").pack(pady=4)
        ctk.CTkButton(self, text="Zrušit", fg_color="gray30", command=self._cancel).pack(pady=6)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        webbrowser.open(url)

    def _copy(self, code: str):
        self.clipboard_clear()
        self.clipboard_append(code)

    def _cancel(self):
        self.on_cancel()
        self.destroy()


class App(ctk.CTk):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.store = TokenStore()
        self.q: queue.Queue = queue.Queue()
        self.runner: BotRunner | None = None
        self.login = ""
        self.prev_state = None
        self._login_cancel = False
        self.device_dialog = None
        self._restart_pending = False

        logsetup.setup(gui_queue=self.q)
        self.title(f"SFGame Giveaway Joiner v{APP_VERSION}")
        self.geometry("860x680")
        self.minsize(700, 520)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        threading.Thread(target=self._check_login, daemon=True).start()
        log.info(f"Data a logy: {os.path.dirname(logs_dir())}", extra={"cat": "info"})

    # ------------------------------------------------------------ layout
    def _build(self):
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.lbl_user = ctk.CTkLabel(top, text="Ověřuji přihlášení…", font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_user.pack(side="left", padx=10, pady=8)
        self.btn_login = ctk.CTkButton(top, text="Přihlásit přes Twitch", width=170, command=self._login_clicked)
        self.btn_login.pack(side="right", padx=8)

        row = ctk.CTkFrame(self)
        row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(row, text="Kanál:").pack(side="left", padx=(10, 4), pady=8)
        self.ent_channel = ctk.CTkEntry(row, width=170)
        self.ent_channel.insert(0, self.cfg.channel)
        self.ent_channel.pack(side="left")
        self.var_dry = ctk.BooleanVar(value=self.cfg.dry_run)
        ctk.CTkCheckBox(row, text="Jen sledovat (dry-run, nic nepíše)", variable=self.var_dry,
                        command=self._dry_toggled).pack(side="left", padx=16)
        self.btn_start = ctk.CTkButton(row, text="▶ Start", width=120, fg_color="#2d8a4e",
                                       hover_color="#256f3f", command=self._start_stop)
        self.btn_start.pack(side="right", padx=8)

        st = ctk.CTkFrame(self)
        st.pack(fill="x", padx=10, pady=4)
        self.lbl_state = ctk.CTkLabel(st, text="⏹ Zastaveno", font=ctk.CTkFont(size=22, weight="bold"))
        self.lbl_state.pack(anchor="w", padx=12, pady=(8, 0))
        self.lbl_stats = ctk.CTkLabel(st, text="", text_color="gray70")
        self.lbl_stats.pack(anchor="w", padx=12, pady=(0, 8))
        self._render_stats(StatsStore().snapshot())

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self._build_log_tab(self.tabs.add("Log"))
        self._build_chat_tab(self.tabs.add("Chat"))
        self._build_settings_tab(self.tabs.add("Nastavení"))

    def _build_log_tab(self, tab):
        self.txt = ctk.CTkTextbox(tab, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self.txt.pack(fill="both", expand=True)
        for cat, color in LOG_COLORS.items():
            self.txt.tag_config(cat, foreground=color)
        self.txt.configure(state="disabled")
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(bar, text="Test (jen v dry-runu):").pack(side="left", padx=(0, 6))
        for label, what in (("1) Simulovat GA", "ga"), ("2) Konec přihlášek", "closed"), ("3) Simulovat výhru", "win")):
            ctk.CTkButton(bar, text=label, width=130, fg_color="gray30",
                          command=lambda w=what: self._simulate(w)).pack(side="left", padx=3)
        ctk.CTkButton(bar, text="Složka s logy", width=110, command=self._open_logs).pack(side="right", padx=3)
        ctk.CTkButton(bar, text="Vyčistit", width=80, fg_color="gray30", command=self._clear_log).pack(side="right", padx=3)

    def _build_chat_tab(self, tab):
        self.chat = ctk.CTkTextbox(tab, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_config("time", foreground="gray50")
        self.chat.tag_config("badge", foreground="#a0a0a0")
        self.chat.tag_config("shared", foreground="gray55")
        self.chat.tag_config("ann", foreground="#ffffff", background="#3b2a66")
        self.chat.tag_config("hl", background="#4a3b00")
        self.chat.tag_config("own", background="#123a24")
        self.chat.configure(state="disabled")
        self._chat_tags: set = set()
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(6, 0))
        self.ent_chat = ctk.CTkEntry(bar, placeholder_text="Napiš zprávu do chatu… (Enter = odeslat; ruční zprávy jdou i v dry-runu)")
        self.ent_chat.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.ent_chat.bind("<Return>", lambda _e: self._send_chat())
        ctk.CTkButton(bar, text="Odeslat", width=90, command=self._send_chat).pack(side="left")
        self.var_autoscroll = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(bar, text="Posouvat", variable=self.var_autoscroll, width=90).pack(side="left", padx=(8, 0))

    def _send_chat(self):
        text = self.ent_chat.get().strip()
        if not text:
            return
        if not self.runner:
            messagebox.showinfo("Chat", "Nejdřív dej ▶ Start.")
            return
        if not self.login:
            messagebox.showwarning("Chat", "Pro psaní se musíš přihlásit přes Twitch.")
            return
        self.runner.send_manual(text)
        self.ent_chat.delete(0, "end")

    def _nick_tag(self, m: dict) -> str:
        color = m.get("color") or DEFAULT_NICK_COLORS[sum(map(ord, m.get("login") or "x")) % len(DEFAULT_NICK_COLORS)]
        color = _readable(color)
        tag = "nc_" + color.lstrip("#")
        if tag not in self._chat_tags:
            self.chat.tag_config(tag, foreground=color)
            self._chat_tags.add(tag)
        return tag

    def _append_chat(self, m: dict):
        t = self.chat
        t.configure(state="normal")
        start = t.index("end-1c")
        t.insert("end", time.strftime("%H:%M:%S ", time.localtime(m["ts"])), "time")
        if m.get("shared"):
            t.insert("end", "[sdílený chat] ", "shared")
        if m["kind"] == "announcement":
            t.insert("end", " 📢 ANNOUNCEMENT ", "ann")
            t.insert("end", " ")
        badges = [BADGE_LABELS[b] for b in m.get("badges", []) if b in BADGE_LABELS]
        if m.get("own"):
            badges.append("APPKA" if m.get("auto") else "TY")
        if badges:
            t.insert("end", "[" + "|".join(dict.fromkeys(badges)) + "] ", "badge")
        t.insert("end", m["name"], self._nick_tag(m))
        t.insert("end", ": " + m["text"] + "\n")
        if m.get("own"):
            t.tag_add("own", start, "end-1c")
        elif self.login and re.search(rf"(?<!\w){re.escape(self.login)}(?!\w)", m["text"], re.IGNORECASE):
            t.tag_add("hl", start, "end-1c")
        lines = int(t.index("end-1c").split(".")[0])
        if lines > MAX_CHAT_LINES + 100:
            t.delete("1.0", f"{lines - MAX_CHAT_LINES}.0")
        if self.var_autoscroll.get():
            t.see("end")
        t.configure(state="disabled")

    def _build_settings_tab(self, tab):
        sf = ctk.CTkScrollableFrame(tab)
        sf.pack(fill="both", expand=True)
        sf.grid_columnconfigure(1, weight=1)
        self.fields: dict = {}
        r = 0

        def entry(key, label, hint=""):
            nonlocal r
            ctk.CTkLabel(sf, text=label, anchor="w").grid(row=r, column=0, sticky="w", padx=6, pady=3)
            e = ctk.CTkEntry(sf, placeholder_text=hint)
            val = getattr(self.cfg, key)
            e.insert(0, ", ".join(val) if isinstance(val, list) else str(val))
            e.grid(row=r, column=1, sticky="ew", padx=6, pady=3)
            self.fields[key] = e
            r += 1

        def check(key, label):
            nonlocal r
            v = ctk.BooleanVar(value=getattr(self.cfg, key))
            ctk.CTkCheckBox(sf, text=label, variable=v).grid(row=r, column=0, columnspan=2, sticky="w", padx=6, pady=3)
            self.fields[key] = v
            r += 1

        def box(label, text, height=110):
            nonlocal r
            ctk.CTkLabel(sf, text=label, anchor="w").grid(row=r, column=0, columnspan=2, sticky="w", padx=6, pady=(8, 2))
            r += 1
            t = ctk.CTkTextbox(sf, height=height)
            t.insert("1.0", text)
            t.grid(row=r, column=0, columnspan=2, sticky="ew", padx=6, pady=3)
            r += 1
            return t

        entry("client_id", "Twitch Client ID")
        entry("watch_nick", "Hlídaný nick (bez přihlášení)", "jen pro testy výhry v dry-runu")
        entry("min_users", "Min. lidí se stejným !slovem")
        entry("window_s", "Okno detekce (s)")
        entry("entry_duration_s", "Zapojovat min. (s)")
        entry("max_entry_messages", "Max. zpráv na jednu GA")
        entry("win_delay_min", "Odpověď na výhru – min. zpoždění (s)")
        entry("win_delay_max", "Odpověď na výhru – max. zpoždění (s)")
        entry("win_messages", "Počet odpovědí na výhru")
        entry("winner_regex", "Regex textu výhry")
        entry("closed_regex", "Regex konce GA")
        entry("giveaway_bots", "Účty, které hlásí GA (čárkou)")
        entry("command_bots", "Command boti (čárkou)")
        check("dedupe_suffix", "Střídat zprávu s neviditelným znakem (obchází Twitch filtr duplicit)")
        check("raw_log", "Ukládat celý chat (raw log) – pro ladění")
        self.box_blacklist = box("Commandy, které nikdy nejsou GA (čárkou)", ", ".join(self.cfg.command_blacklist), 80)
        self.box_learned = box("Automaticky naučené commandy (smaž, pokud je tam omylem GA slovo)",
                               ", ".join(sorted(load_learned())), 60)
        with open(phrases.ensure_user_file(), encoding="utf-8") as f:
            self.box_phrases = box("Odpovědi při výhře (jedna na řádek)", f.read(), 160)
        ctk.CTkButton(sf, text="💾 Uložit nastavení", command=self._save_settings).grid(
            row=r, column=0, columnspan=2, pady=10)

    # ------------------------------------------------------------ actions
    def _save_settings(self):
        try:
            for key, w in self.fields.items():
                cur = getattr(self.cfg, key)
                if isinstance(w, ctk.BooleanVar):
                    setattr(self.cfg, key, bool(w.get()))
                    continue
                raw = w.get().strip()
                if isinstance(cur, list):
                    setattr(self.cfg, key, [x.lower() for x in _split_list(raw)])
                elif isinstance(cur, bool):
                    pass
                elif isinstance(cur, int):
                    setattr(self.cfg, key, int(float(raw)))
                elif isinstance(cur, float):
                    setattr(self.cfg, key, float(raw))
                else:
                    setattr(self.cfg, key, raw)
            self.cfg.command_blacklist = [x.lower() for x in _split_list(self.box_blacklist.get("1.0", "end"))]
            self.cfg.save()
            save_learned({x.lower() for x in _split_list(self.box_learned.get("1.0", "end"))})
            with open(phrases.user_phrases_path(), "w", encoding="utf-8") as f:
                f.write(self.box_phrases.get("1.0", "end").strip() + "\n")
        except ValueError as e:
            messagebox.showerror("Chyba", f"Neplatná hodnota: {e}")
            return
        if self.runner:
            self.runner.reload_settings()
        log.info("Nastavení uloženo.", extra={"cat": "info"})

    def _check_login(self):
        tok = ensure_valid(self.cfg.client_id, self.store)
        self.q.put(("identity", tok.login if tok else ""))

    def _login_clicked(self):
        if self.login:
            if messagebox.askyesno("Odhlásit", f"Odhlásit účet {self.login}?"):
                self.store.clear()
                self._set_identity("")
                self._restart_if_running()
            return
        if not self.cfg.client_id:
            messagebox.showwarning("Chybí Client ID",
                                   "Vyplň Twitch Client ID v záložce Nastavení (viz README) a ulož.")
            self.tabs.set("Nastavení")
            return
        self._login_cancel = False
        self.btn_login.configure(state="disabled", text="Přihlašuji…")
        threading.Thread(target=self._login_thread, daemon=True).start()

    def _login_thread(self):
        try:
            dev = start_device_flow(self.cfg.client_id)
            self.q.put(("device", dev))
            tok = poll_device_token(self.cfg.client_id, dev, cancelled=lambda: self._login_cancel)
            self.store.save(tok)
            self.q.put(("login_ok", tok.login))
        except Exception as e:
            self.q.put(("login_fail", str(e)))

    def _dry_toggled(self):
        self.cfg.dry_run = bool(self.var_dry.get())
        self.cfg.save()
        if self.cfg.dry_run:
            log.info("Dry-run ZAPNUT – appka nic nepíše.", extra={"cat": "warn"})
        else:
            log.info("Dry-run VYPNUT – appka bude psát do chatu!", extra={"cat": "warn"})
            if not self.login:
                log.warning("Nejsi přihlášený – psát nepůjde.", extra={"cat": "warn"})

    def _start_stop(self):
        if self.runner:
            self.runner.stop()
            self.btn_start.configure(state="disabled", text="Zastavuji…")
            return
        self.cfg.channel = self.ent_channel.get().strip().lstrip("#").lower() or "sfgameofficial"
        self.cfg.dry_run = bool(self.var_dry.get())
        self.cfg.save()
        self.runner = BotRunner(self.cfg, self.store, self.q)
        self.runner.start()
        self.btn_start.configure(text="⏹ Stop", fg_color="#a83232", hover_color="#8a2828")
        self.lbl_state.configure(text="🔌 Připojuji…")

    def _restart_if_running(self):
        if self.runner:
            self._restart_pending = True
            self.runner.stop()

    def _simulate(self, what):
        if not self.runner:
            messagebox.showinfo("Simulace", "Nejdřív dej Start.")
            return
        if not self.var_dry.get():
            messagebox.showwarning("Simulace", "Simulace jde jen se zapnutým dry-runem.")
            return
        self.runner.simulate(what)

    def _open_logs(self):
        path = logs_dir()
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])

    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _on_close(self):
        self._login_cancel = True
        if self.runner:
            self.runner.stop()
            self.runner.join(3)
        self.destroy()

    # ------------------------------------------------------------ queue
    def _poll(self):
        try:
            for _ in range(500):
                item = self.q.get_nowait()
                self._handle(item)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle(self, item):
        kind = item[0]
        if kind == "log":
            self._append_log(item[1], item[2])
        elif kind == "chat":
            self._append_chat(item[1])
        elif kind == "status":
            self._show_status(item[1])
        elif kind == "identity":
            self._set_identity(item[1])
        elif kind == "stopped":
            self.runner = None
            self.btn_start.configure(state="normal", text="▶ Start", fg_color="#2d8a4e", hover_color="#256f3f")
            self.lbl_state.configure(text="⏹ Zastaveno", text_color=("gray10", "gray90"))
            if getattr(self, "_restart_pending", False):
                self._restart_pending = False
                self._start_stop()
        elif kind == "device":
            self.device_dialog = DeviceDialog(self, item[1], on_cancel=self._cancel_login)
        elif kind == "login_ok":
            self._close_device_dialog()
            self._set_identity(item[1])
            log.info(f"Přihlášen jako {item[1]}.", extra={"cat": "info"})
            self._restart_if_running()
        elif kind == "login_fail":
            self._close_device_dialog()
            self._set_identity(self.login)
            if "zrušeno" not in item[1]:
                messagebox.showerror("Přihlášení", item[1])

    def _cancel_login(self):
        self._login_cancel = True

    def _close_device_dialog(self):
        if self.device_dialog is not None:
            try:
                self.device_dialog.destroy()
            except Exception:
                pass
            self.device_dialog = None

    def _set_identity(self, login: str):
        self.login = login or ""
        if self.login:
            self.lbl_user.configure(text=f"✅ Přihlášen jako {self.login}")
            self.btn_login.configure(state="normal", text="Odhlásit")
        else:
            self.lbl_user.configure(text="❌ Nepřihlášen (jen čtení chatu)")
            self.btn_login.configure(state="normal", text="Přihlásit přes Twitch")

    def _append_log(self, cat: str, text: str):
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", cat)
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.txt.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _show_status(self, st: dict):
        state = st["state"]
        word = " + !".join(st.get("words") or []) or st["word"]
        if state == ENTERING:
            text, color = f"🎯 GA !{word} – zapojuji ({st['remaining']:.0f} s)", "#f0a030"
        elif state == ARMED:
            text, color = f"✅ Zapojeno do !{word} – čekám na losování", "#7aa7ff"
        elif state == WON:
            text, color = "🏆 VÝHRA! Odpovídám v chatu", "#ffd700"
        else:
            text, color = "⏳ Čekám na giveaway", ("gray10", "gray90")
        if st.get("banned"):
            text += "   ⛔ BAN"
        elif st.get("paused"):
            text += f"   ⛔ timeout {st['paused']:.0f}s"
        if self.cfg.dry_run:
            text += "   [DRY-RUN]"
        self.lbl_state.configure(text=text, text_color=color)
        self._render_stats(st.get("persist") or {"totals": st["stats"], "today": st["stats"]}, st["slow"])
        if state == WON and self.prev_state != WON:
            self._alert_win()
        self.prev_state = state

    def _render_stats(self, p: dict, slow: int | None = None):
        t, d = p["totals"], p["today"]
        text = (f"Celkem – GA: {t['detected']}  zapojeno: {t['entered']}  výhry: {t['wins']}  zpráv: {t['sent']}"
                f"     |     Dnes – GA: {d['detected']}  zapojeno: {d['entered']}  výhry: {d['wins']}")
        if slow is not None:
            text += f"     |     Slow mode: {slow}s"
        self.lbl_stats.configure(text=text)

    def _alert_win(self):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            self.bell()
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(3000, lambda: self.attributes("-topmost", False))
        except Exception:
            pass


def run_gui(cfg: Config) -> int:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = App(cfg)
    app.mainloop()
    return 0
