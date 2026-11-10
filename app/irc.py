"""Parser Twitch IRC zpráv (s IRCv3 tagy) -> ChatEvent."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Neviditelné znaky, které lidé (a Chatterino) přidávají kvůli duplicitám.
INVISIBLE_RE = re.compile("[\U000E0000-\U000E007F͏​-‏⁠-⁤﻿­]")

DEDUPE_SUFFIX = " \U000E0000"

_ESCAPES = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}


def _unescape(value: str) -> str:
    if "\\" not in value:
        return value
    out = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            out.append(_ESCAPES.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            if c != "\\":
                out.append(c)
            i += 1
    return "".join(out)


@dataclass
class IrcMessage:
    tags: dict
    prefix: str
    command: str
    params: list

    @property
    def nick(self) -> str:
        return self.prefix.split("!", 1)[0] if self.prefix else ""

    @property
    def trailing(self) -> str:
        return self.params[-1] if self.params else ""


def parse_line(line: str) -> IrcMessage | None:
    line = line.rstrip("\r\n")
    if not line:
        return None
    tags: dict = {}
    prefix = ""
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for kv in raw_tags.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape(v)
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    if " :" in line:
        head, _, trailing = line.partition(" :")
        params = head.split() + [trailing]
    else:
        params = line.split()
    if not params:
        return None
    command = params.pop(0)
    return IrcMessage(tags, prefix, command, params)


def parse_badges(value: str) -> frozenset:
    if not value:
        return frozenset()
    return frozenset(b.split("/", 1)[0] for b in value.split(",") if b)


@dataclass
class ChatEvent:
    kind: str                     # privmsg | announcement | usernotice | notice | roomstate | clearchat | userstate | reconnect
    login: str = ""
    display: str = ""
    badges: frozenset = frozenset()
    text: str = ""
    tags: dict = field(default_factory=dict)
    ts: float = 0.0
    msg_id: str = ""
    raw: str = ""

    @property
    def clean_text(self) -> str:
        return INVISIBLE_RE.sub("", self.text).strip()


def to_event(msg: IrcMessage, ts: float, raw: str = "") -> ChatEvent | None:
    cmd = msg.command
    t = msg.tags
    common = dict(tags=t, ts=ts, raw=raw, msg_id=t.get("msg-id", ""))
    if cmd == "PRIVMSG":
        text = msg.trailing
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            text = text[8:-1]
        login = msg.nick.lower()
        return ChatEvent("privmsg", login=login, display=t.get("display-name") or login,
                         badges=parse_badges(t.get("badges", "")), text=text, **common)
    if cmd == "USERNOTICE":
        msg_id = t.get("msg-id", "")
        login = t.get("login", "").lower()
        text = msg.trailing if len(msg.params) > 1 else ""
        return ChatEvent("announcement" if msg_id == "announcement" else "usernotice",
                         login=login, display=t.get("display-name") or login,
                         badges=parse_badges(t.get("badges", "")), text=text, **common)
    if cmd == "NOTICE":
        return ChatEvent("notice", text=msg.trailing, **common)
    if cmd == "ROOMSTATE":
        return ChatEvent("roomstate", **common)
    if cmd == "CLEARCHAT":
        target = msg.trailing.lower() if len(msg.params) > 1 else ""
        return ChatEvent("clearchat", login=target, **common)
    if cmd == "USERSTATE":
        return ChatEvent("userstate", **common)
    if cmd == "RECONNECT":
        return ChatEvent("reconnect", **common)
    return None
