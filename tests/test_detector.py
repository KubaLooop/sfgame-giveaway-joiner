import random

from app.config import Config
from app.detector import Closed, CommandLearned, Detector, GiveawayStart, Winner
from app.engine import ARMED, ENTERING, IDLE, WON, Engine
from app.irc import DEDUPE_SUFFIX, ChatEvent, parse_line, to_event
from app.phrases import PhrasePicker

ME = "mujnick"


def msg(user, text, ts, badges=()):
    return ChatEvent("privmsg", login=user, display=user, badges=frozenset(badges), text=text, ts=ts)


def ann(user, text, ts):
    return ChatEvent("announcement", login=user, display=user, badges=frozenset({"moderator"}),
                     text=text, ts=ts, msg_id="announcement")


def make(**over):
    cfg = Config()
    for k, v in over.items():
        setattr(cfg, k, v)
    det = Detector(cfg, ME, "MujNick")
    eng = Engine(cfg, det, PhrasePicker(["NICE!!!", "YESSS!"], random.Random(1)), random.Random(1))
    return cfg, det, eng


def feed_all(det, events):
    out = []
    for e in events:
        out += det.feed(e)
    return out


# ------------------------------------------------------------------ detector
def test_spam_triggers_start():
    _, det, _ = make()
    evs = [msg(f"user{i}", "!Kolobezka", 100 + i * 0.5) for i in range(10)]
    sigs = [s for s in feed_all(det, evs) if isinstance(s, GiveawayStart)]
    assert len(sigs) == 1
    assert sigs[0].word == "Kolobezka" and sigs[0].first_seen == 100


def test_same_user_spamming_does_not_trigger():
    _, det, _ = make()
    evs = [msg("spammer", "!cokoliv", 100 + i) for i in range(10)]
    assert not [s for s in feed_all(det, evs) if isinstance(s, GiveawayStart)]


def test_blacklisted_command_never_triggers():
    _, det, _ = make()
    evs = [msg(f"u{i}", "!discord", 100 + i * 0.2) for i in range(30)]
    assert not [s for s in feed_all(det, evs) if isinstance(s, GiveawayStart)]


def test_invisible_suffix_is_ignored():
    _, det, _ = make()
    evs = [msg(f"u{i}", "!slovo" + (DEDUPE_SUFFIX if i % 2 else ""), 100 + i * 0.3) for i in range(8)]
    assert [s for s in feed_all(det, evs) if isinstance(s, GiveawayStart)]


def test_command_bot_reply_learns_command():
    _, det, _ = make()
    sigs = feed_all(det, [msg("viewer", "!mojekomanda", 100), msg("nightbot", "Tady je odpoved", 101)])
    assert any(isinstance(s, CommandLearned) and s.key == "mojekomanda" for s in sigs)
    evs = [msg(f"u{i}", "!mojekomanda", 200 + i * 0.3) for i in range(8)]
    assert not [s for s in feed_all(det, evs) if isinstance(s, GiveawayStart)]
    # ...ale masivní spam (>= learned_override_users) GA i tak spustí
    evs = [msg(f"x{i}", "!mojekomanda", 300 + i * 0.2) for i in range(20)]
    assert [s for s in feed_all(det, evs) if isinstance(s, GiveawayStart)]


def test_bot_reply_during_spam_does_not_learn_ga_word():
    _, det, _ = make()
    evs = [msg(f"u{i}", "!ga", 100 + i * 0.3) for i in range(8)] + [msg("nightbot", "timer zprava", 102.5)]
    assert not [s for s in feed_all(det, evs) if isinstance(s, CommandLearned)]


def test_authority_fast_trigger():
    _, det, _ = make()
    evs = [msg("host", "!rychle", 100, badges={"vip"}), msg("a", "!rychle", 101), msg("b", "!rychle", 102)]
    sigs = [s for s in feed_all(det, evs) if isinstance(s, GiveawayStart)]
    assert sigs and "host" in sigs[0].reason


def test_cooldown_prevents_retrigger():
    _, det, _ = make()
    evs = [msg(f"u{i}", "!slovo", 100 + i * 0.5) for i in range(20)]
    assert len([s for s in feed_all(det, evs) if isinstance(s, GiveawayStart)]) == 1


def test_winner_announcement():
    _, det, _ = make()
    sigs = det.feed(ann("spidderofficial", "Giveaway 2/15 won by MujNick!", 100))
    assert any(isinstance(s, Winner) for s in sigs)


def test_viewer_mentioning_me_is_not_a_win():
    _, det, _ = make()
    assert not [s for s in det.feed(msg("random", "giveaway @mujnick won lol", 100)) if isinstance(s, Winner)]


def test_nick_substring_is_not_a_win():
    _, det, _ = make()
    assert not [s for s in det.feed(ann("mod", "Giveaway 1/5 won by mujnick2", 100)) if isinstance(s, Winner)]


def test_closed():
    _, det, _ = make()
    sigs = det.feed(ann("spidderofficial", "Giveaway 1/3 Closed", 100))
    assert sigs and isinstance(sigs[-1], Closed) and sigs[-1].index == 1 and sigs[-1].total == 3


# ------------------------------------------------------------------ engine
def run(eng, events, until, step=0.1):
    """Přehraje eventy a ticky; vrátí [(ts, text, kind)]."""
    sent = []
    events = sorted(events, key=lambda e: e.ts)
    t = events[0].ts if events else 0
    i = 0
    while t <= until:
        while i < len(events) and events[i].ts <= t:
            eng.handle_event(events[i])
            i += 1
        for text, kind in eng.tick(t):
            sent.append((t, text, kind))
        t = round(t + step, 3)
    return sent


def test_engine_full_flow():
    cfg, det, eng = make()
    eng.slow = 5
    evs = [msg(f"u{i % 80}", "!Hura", 100 + i * 0.3) for i in range(200)]  # 60 s spamu
    evs.append(ann("spidderofficial", "Giveaway 1/3 Closed", 160))       # konec přihlášek po minutě
    evs.append(ann("nightbot", "Congratulations, someoneelse! You won the giveaway!", 180))
    evs.append(ann("nightbot", "Congratulations, MujNick! You won the giveaway!", 200))
    sent = run(eng, evs, 270)
    entries = [s for s in sent if s[2] == "entry"]
    wins = [s for s in sent if s[2] == "win"]
    assert 6 <= len(entries) <= cfg.max_entry_messages
    assert entries[0][1] == "!Hura" and entries[1][1] == "!Hura" + DEDUPE_SUFFIX
    gaps = [b[0] - a[0] for a, b in zip(entries, entries[1:])]
    assert min(gaps) >= 5.4
    assert all(e[0] <= 100 + cfg.entry_max_s for e in entries)
    assert len(wins) == 2 and 201 <= wins[0][0] <= 203.1 and wins[1][0] - 200 < 30
    assert all(e[0] <= 160 for e in entries)          # po "Closed" už nic nepíše
    assert eng.state == ARMED                         # čeká na další losování
    assert eng.stats.wins == 1 and eng.stats.entered == 1 and eng.stats.closed == 1


def test_engine_stops_when_spam_fades():
    _, det, eng = make()
    eng.slow = 5
    evs = [msg(f"u{i}", "!kratke", 100 + i * 0.5) for i in range(10)]
    run(eng, evs, 140)
    assert eng.state == ARMED and eng.stats.sent < 6


def test_engine_respects_timeout():
    _, det, eng = make()
    evs = [msg(f"u{i}", "!slovo", 100 + i * 0.2) for i in range(10)]
    evs.append(ChatEvent("notice", text="You are timed out for 600 more seconds.", msg_id="msg_timedout", ts=100.1))
    sent = run(eng, evs, 130)
    assert sent == []


def test_parse_real_irc_lines():
    line = ("@badge-info=;badges=moderator/1,partner/1;color=#FF0000;display-name=SpidderOfficial;"
            "login=spidderofficial;msg-id=announcement;msg-param-color=PRIMARY;room-id=1;tmi-sent-ts=1 "
            ":tmi.twitch.tv USERNOTICE #sfgameofficial :Giveaway 1/3 Closed")
    ev = to_event(parse_line(line), 5.0)
    assert ev.kind == "announcement" and ev.login == "spidderofficial" and ev.text == "Giveaway 1/3 Closed"
    assert "moderator" in ev.badges
    line = "@badges=;display-name=Foo\\sBar :foo!foo@foo.tmi.twitch.tv PRIVMSG #sfgameofficial :!slovo"
    ev = to_event(parse_line(line), 1.0)
    assert ev.kind == "privmsg" and ev.login == "foo" and ev.display == "Foo Bar" and ev.text == "!slovo"
    ev = to_event(parse_line("@slow=5 :tmi.twitch.tv ROOMSTATE #sfgameofficial"), 1.0)
    assert ev.kind == "roomstate" and ev.tags["slow"] == "5"


def test_nightbot_winner_announcement():
    _, det, _ = make()
    sigs = det.feed(ann("nightbot", "Congratulations, mujnick! You won the giveaway!", 100))
    assert any(isinstance(s, Winner) for s in sigs)


def test_nightbot_winner_for_someone_else_is_ignored_and_not_learned():
    _, det, _ = make()
    feed_all(det, [msg("viewer", "!nekomu", 99)])
    sigs = det.feed(ann("nightbot", "Congratulations, tenhladas! You won the giveaway!", 100))
    assert not [s for s in sigs if isinstance(s, (Winner, CommandLearned))]


def test_win_requires_authority():
    _, det, _ = make()
    sigs = det.feed(msg("nightbot", "Congratulations, mujnick! You won the giveaway!", 100, badges={"moderator"}))
    assert any(isinstance(s, Winner) for s in sigs)
    assert not [s for s in det.feed(msg("viewer", "Congratulations, mujnick! You won the giveaway!", 101))
                if isinstance(s, Winner)]


def test_closed_stops_entry_immediately():
    _, det, eng = make()
    eng.slow = 3
    evs = [msg(f"u{i % 50}", "!slovo", 100 + i * 0.2) for i in range(400)]  # spam 80 s
    evs.append(ann("spidderofficial", "Giveaway 2/3 Closed", 130))
    sent = run(eng, evs, 180)
    assert sent and max(t for t, _, _ in sent) <= 130
    assert eng.state == ARMED


def test_win_is_answered_anytime_even_without_entry():
    """Žádná GA detekována, appka v IDLE, výhra přijde o 3 hodiny později -> stejně odpoví."""
    _, det, eng = make()
    eng.slow = 5
    evs = [ann("nightbot", "Congratulations, MujNick! You won the giveaway!", 10_800)]
    sent = run(eng, evs, 10_830)
    wins = [s for s in sent if s[2] == "win"]
    assert len(wins) == 2 and wins[0][0] - 10_800 <= 3.1 and wins[-1][0] - 10_800 < 30


def test_win_long_after_entry_is_answered():
    _, det, eng = make()
    eng.slow = 5
    evs = [msg(f"u{i % 50}", "!slovo", 100 + i * 0.3) for i in range(200)]
    evs.append(ann("spidderofficial", "Giveaway 1/3 Closed", 160))
    evs.append(ann("nightbot", "Congratulations, mujnick! You won the giveaway!", 100 + 3 * 3600))
    sent = run(eng, evs, 100 + 3 * 3600 + 30, step=0.5)
    assert [s for s in sent if s[2] == "win"]


def test_nightbot_plain_message_without_badge_counts():
    _, det, _ = make()
    sigs = det.feed(msg("nightbot", "Congratulations, MujNick! You won the giveaway!", 100))
    assert any(isinstance(s, Winner) for s in sigs)


def test_second_win_announcement_later_is_answered_again():
    """Reroll / opakované oznámení po >45 s -> odpoví znovu."""
    _, det, eng = make()
    eng.slow = 5
    evs = [ann("nightbot", "Congratulations, MujNick! You won the giveaway!", 100),
           ann("nightbot", "Congratulations, MujNick! You won the giveaway!", 200)]
    sent = run(eng, evs, 230)
    assert len([s for s in sent if s[2] == "win"]) == 4


# ------------------------------------------------------------------ podvrh vs. skutečné slovo
def spam(word, start, end, users, prefix):
    """`users` unikátních lidí rovnoměrně spamuje `word` v intervalu <start, end)."""
    n = max(1, int((end - start) / 0.25))
    return [msg(f"{prefix}{i % users}", "!" + word, start + i * (end - start) / n) for i in range(n)]


def test_decoy_then_real_word_switches_to_real():
    """Reálný průběh z 9. 11.: chat spamuje !HolyGrail (podvrh), po ~25 s se objeví !hahahah (107 lidí)."""
    _, det, eng = make()
    eng.slow = 3
    evs = (spam("HolyGrail", 100, 125, 24, "a") + spam("HolyGrail", 125, 135, 4, "a")
           + spam("hahahah", 127, 200, 100, "b"))
    evs.append(ann("spidderofficial", "Giveaway 2/2 Closed", 209))
    sent = run(eng, evs, 215)
    real = [t for t, txt, _ in sent if txt.startswith("!hahahah")]
    decoy_after = [t for t, txt, _ in sent if txt.startswith("!HolyGrail") and t > 131]
    assert real and real[0] < 131                     # na skutečné slovo zareaguje do pár sekund
    assert 5 <= len(real) <= 7
    assert not decoy_after                            # podvrh opustí
    assert eng.state == ARMED and eng.stats.detected == 2


def test_real_word_keeps_going_when_trolls_spam_another():
    """Opačný případ: skutečné slovo běží, trollové začnou spamovat jiné – appka posílá obě."""
    _, det, eng = make()
    eng.slow = 3
    evs = spam("real", 100, 170, 80, "a") + spam("troll", 120, 150, 8, "b")
    sent = run(eng, evs, 175)
    real_after = [t for t, txt, _ in sent if txt.startswith("!real") and t > 125]
    troll = [t for t, txt, _ in sent if txt.startswith("!troll")]
    assert real_after and troll


def test_new_word_after_entry_finished_is_entered():
    _, det, eng = make()
    eng.slow = 3
    evs = spam("prvni", 100, 160, 30, "a") + spam("druhe", 200, 240, 30, "b")
    sent = run(eng, evs, 250)
    assert any(txt.startswith("!druhe") for _, txt, _ in sent)


def test_entry_messages_are_spread_over_minute():
    cfg, det, eng = make()
    eng.slow = 3
    sent = run(eng, spam("slovo", 100, 165, 60, "a"), 170)
    times = [t for t, _, kind in sent if kind == "entry"]
    assert len(times) == cfg.max_entry_messages == 7
    assert times[-1] - times[0] >= 40                 # ne 7 zpráv v prvních 25 s


# ------------------------------------------------------------------ nick: velikost písmen
def test_nick_matching_is_case_insensitive():
    cfg = Config()
    det = Detector(cfg, "coolviewer")                  # login z OAuth je vždy malými písmeny
    for text in ("Congratulations, CoolViewer! You won the giveaway!",
                 "Congratulations, coolviewer! You won the giveaway!",
                 "Congratulations, COOLVIEWER! You won the giveaway!",
                 "Congratulations, @CoolViewer! You won the giveaway!"):
        assert any(isinstance(s, Winner) for s in det.feed(ann("nightbot", text, 100))), text
        det = Detector(cfg, "coolviewer")
    assert not [s for s in det.feed(ann("nightbot", "Congratulations, CoolViewer2! You won the giveaway!", 100))
                if isinstance(s, Winner)]


# ------------------------------------------------------------------ perzistence
def test_stats_persist_between_runs(tmp_path):
    from app.stats import StatsStore
    path = str(tmp_path / "stats.json")
    s1 = StatsStore(path)
    s1.add("detected"); s1.add("entered"); s1.add("wins"); s1.log_win("Congratulations, x!")
    s2 = StatsStore(path)
    assert s2.totals["detected"] == 1 and s2.totals["wins"] == 1
    assert s2.snapshot()["today"]["entered"] == 1 and len(s2.wins_log) == 1


def test_old_config_is_migrated(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "SfgGiveaway").mkdir()
    (tmp_path / "SfgGiveaway" / "config.json").write_text(json.dumps(
        {"max_entry_messages": 12, "giveaway_bots": ["spidderofficial"], "armed_duration_s": 900.0,
         "dry_run": False}), encoding="utf-8")
    cfg = Config.load()
    assert cfg.max_entry_messages == 7 and "nightbot" in cfg.giveaway_bots
    assert cfg.armed_duration_s >= 7200 and cfg.dry_run is False and cfg.config_version == 2


def test_old_logs_are_cleaned_up(tmp_path):
    from app.logsetup import cleanup_old_logs
    names = ["2026-11-06.log", "raw-2026-11-06.log", "2026-11-07.log", "raw-2026-11-08.log",
             "2026-11-09.log", "raw-2026-11-10.log", "2026-11-10.log", "notes.txt"]
    for n in names:
        (tmp_path / n).write_text("x")
    removed = cleanup_old_logs(3, str(tmp_path), today="2026-11-10")
    assert sorted(removed) == ["2026-11-06.log", "2026-11-07.log", "raw-2026-11-06.log"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-11-09.log", "2026-11-10.log", "notes.txt", "raw-2026-11-08.log", "raw-2026-11-10.log"]
    assert cleanup_old_logs(0, str(tmp_path), today="2030-01-01") == []      # 0 = nikdy nemazat
