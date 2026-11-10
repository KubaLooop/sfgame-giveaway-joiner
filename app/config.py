"""Konfigurace aplikace + cesty k datovým souborům."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields

APP_NAME = "SfgGiveaway"
APP_VERSION = "1.1.0"
CONFIG_VERSION = 2

# Client ID z https://dev.twitch.tv/console/apps (Client type: Public).
# Vlož sem svoje, ať ho kámoši nemusí nastavovat ručně.
DEFAULT_CLIENT_ID = "3has597va8iaqol2dknk5xh77m452y"


def app_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def logs_dir() -> str:
    path = os.path.join(app_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path


def resource_path(rel: str) -> str:
    """Cesta k přibaleným souborům (funguje i uvnitř PyInstaller exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel)


@dataclass
class Config:
    config_version: int = CONFIG_VERSION
    client_id: str = DEFAULT_CLIENT_ID
    channel: str = "sfgameofficial"
    dry_run: bool = True
    # Nick, který se hlídá jako výherce, když nejsi přihlášený (jen pro testy v dry-runu).
    watch_nick: str = ""

    # --- detekce startu GA ---
    min_users: int = 6                 # unikátních lidí se stejným !slovem v okně
    window_s: float = 10.0             # délka klouzavého okna
    authority_min_followers: int = 2   # rychlý trigger: mod/streamer napíše !slovo + N lidí zopakuje
    learned_override_users: int = 15   # naučený command se i tak spustí, když ho spamuje tolik lidí
    word_cooldown_s: float = 600.0     # stejné slovo znovu nespustí GA dřív než za X s

    # --- zapojení ---
    entry_duration_s: float = 60.0     # jak dlouho min. psát !slovo (od prvního výskytu)
    entry_max_s: float = 120.0         # tvrdý strop
    max_entry_messages: int = 7        # na jedno slovo, rovnoměrně rozprostřeno do minuty
    dedupe_suffix: bool = True         # střídat "!slovo" a "!slovo <neviditelný znak>" (Twitch zahazuje duplikáty)
    armed_duration_s: float = 7200.0   # jen zobrazení stavu; na výhru se reaguje VŽDY

    # --- výhra ---
    win_delay_min: float = 1.0
    win_delay_max: float = 3.0
    win_messages: int = 2

    # --- seznamy / regexy ---
    command_blacklist: list = field(default_factory=lambda: [
        "discord", "rules", "help", "commands", "giveaway", "giveaways", "socials", "social",
        "prime", "sub", "subs", "follow", "followage", "uptime", "game", "server", "servers",
        "code", "codes", "promo", "youtube", "yt", "twitter", "instagram", "tiktok", "web",
        "website", "shop", "lurk", "unlurk", "points", "watchtime", "time", "so", "shoutout",
        "drops", "sfgame", "sf", "links", "link", "info", "join", "enter",
    ])
    command_bots: list = field(default_factory=lambda: [
        "nightbot", "streamelements", "fossabot", "moobot", "streamlabs", "wizebot",
        "sery_bot", "botrixoficial", "kofistreambot", "soundalerts",
    ])
    giveaway_bots: list = field(default_factory=lambda: ["spidderofficial", "nightbot"])
    winner_regex: str = r"giveaway|won|win|winner|vyhr|výhr|gratul|congrat|gewinn|gagn|ganad|wygra"
    closed_regex: str = r"giveaway\s*(\d+)\s*/\s*(\d+)\s*closed"

    raw_log: bool = True

    # ------------------------------------------------------------------
    @classmethod
    def path(cls) -> str:
        return os.path.join(app_dir(), "config.json")

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            with open(cls.path(), encoding="utf-8") as f:
                data = json.load(f)
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
            cfg.config_version = int(data.get("config_version", 1))   # starý config verzi neměl
        except FileNotFoundError:
            pass
        except Exception as e:  # poškozený config -> defaulty
            print(f"config load failed: {e}", file=sys.stderr)
        cfg._migrate()
        if not cfg.client_id:
            cfg.client_id = DEFAULT_CLIENT_ID
        return cfg

    def _migrate(self) -> None:
        """Doplní nové výchozí hodnoty do configu uloženého starší verzí."""
        if self.config_version < 2:
            if self.max_entry_messages >= 10:
                self.max_entry_messages = 7
            if "nightbot" not in self.giveaway_bots:
                self.giveaway_bots.append("nightbot")
            self.armed_duration_s = max(self.armed_duration_s, 7200.0)
        self.config_version = CONFIG_VERSION

    def save(self) -> None:
        with open(self.path(), "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------- learned commands
def learned_path() -> str:
    return os.path.join(app_dir(), "learned_commands.json")


def load_learned() -> set[str]:
    try:
        with open(learned_path(), encoding="utf-8") as f:
            return {str(x).lower().lstrip("!") for x in json.load(f)}
    except Exception:
        return set()


def save_learned(words) -> None:
    with open(learned_path(), "w", encoding="utf-8") as f:
        json.dump(sorted(words), f, indent=2, ensure_ascii=False)
