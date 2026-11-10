"""Twitch OAuth – Device Code Grant Flow (public client, bez client secretu)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from .config import app_dir

SCOPES = "chat:read chat:edit"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
USER_AGENT = "SfgGiveaway/1.0"


class AuthError(Exception):
    pass


def _request(url: str, data: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload


@dataclass
class Token:
    access_token: str = ""
    refresh_token: str = ""
    login: str = ""
    user_id: str = ""
    expires_at: float = 0.0


class TokenStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(app_dir(), "token.json")

    def load(self) -> Token | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                t = Token(**json.load(f))
            return t if t.access_token else None
        except Exception:
            return None

    def save(self, t: Token) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(asdict(t), f, indent=2)

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- device flow
def start_device_flow(client_id: str) -> dict:
    """Vrací dict s user_code, verification_uri, device_code, interval, expires_in."""
    status, data = _request(DEVICE_URL, {"client_id": client_id, "scopes": SCOPES})
    if status != 200:
        raise AuthError(f"Device flow selhal ({status}): {data.get('message', data)}")
    return data


def poll_device_token(client_id: str, device: dict, cancelled=lambda: False) -> Token:
    interval = max(1, int(device.get("interval", 5)))
    deadline = time.time() + int(device.get("expires_in", 1800))
    while time.time() < deadline:
        if cancelled():
            raise AuthError("Přihlášení zrušeno")
        time.sleep(interval)
        status, data = _request(TOKEN_URL, {
            "client_id": client_id,
            "scopes": SCOPES,
            "device_code": device["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if status == 200:
            tok = Token(data["access_token"], data.get("refresh_token", ""),
                        expires_at=time.time() + int(data.get("expires_in", 0)))
            info = validate(tok.access_token)
            if not info:
                raise AuthError("Token nejde ověřit")
            tok.login, tok.user_id = info["login"], info["user_id"]
            return tok
        msg = str(data.get("message", ""))
        if "authorization_pending" in msg:
            continue
        if "slow_down" in msg:
            interval += 2
            continue
        raise AuthError(f"Přihlášení selhalo: {msg or status}")
    raise AuthError("Kód vypršel, zkus to znovu")


def validate(access_token: str) -> dict | None:
    status, data = _request(VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"})
    return data if status == 200 else None


def refresh(client_id: str, tok: Token) -> Token | None:
    if not tok.refresh_token:
        return None
    status, data = _request(TOKEN_URL, {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": tok.refresh_token,
    })
    if status != 200:
        return None
    return Token(data["access_token"], data.get("refresh_token", tok.refresh_token), tok.login, tok.user_id,
                 time.time() + int(data.get("expires_in", 0)))


def ensure_valid(client_id: str, store: TokenStore) -> Token | None:
    """Ověří uložený token, případně ho obnoví. None = nepřihlášen / token neplatný."""
    tok = store.load()
    if not tok:
        return None
    info = validate(tok.access_token)
    if info and int(info.get("expires_in", 0)) > 300:
        tok.login, tok.user_id = info["login"], info["user_id"]
        return tok
    new = refresh(client_id, tok) if client_id else None
    if new:
        info = validate(new.access_token)
        if info:
            new.login, new.user_id = info["login"], info["user_id"]
        store.save(new)
        return new
    if info:  # brzy vyprší, ale zatím platí
        return tok
    return None
