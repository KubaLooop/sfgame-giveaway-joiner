"""Entrypoint.

  SfgGiveaway.exe                      -> GUI
  python run.py --cli [--live]         -> bez GUI, v konzoli (default dry-run)
  python run.py --replay raw.log --nick mujnick
"""
from __future__ import annotations

import argparse
import logging
import sys
import time


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="SfgGiveaway")
    p.add_argument("--cli", action="store_true", help="běh bez GUI")
    p.add_argument("--live", action="store_true", help="v --cli režimu opravdu psát do chatu")
    p.add_argument("--channel", help="přepsat kanál")
    p.add_argument("--replay", metavar="RAW_LOG", help="přehrát raw log a vypsat, co by appka udělala")
    p.add_argument("--nick", default="", help="nick pro detekci výhry v replay režimu")
    args = p.parse_args(argv)

    from . import logsetup
    from .config import Config

    cfg = Config.load()
    if args.channel:
        cfg.channel = args.channel

    if args.replay:
        logsetup.setup(console=True)
        from .replay import replay
        replay(args.replay, cfg, args.nick)
        return 0

    if args.cli:
        logsetup.setup(console=True)
        from .auth import TokenStore
        from .bot import BotRunner
        cfg.dry_run = not args.live
        logging.getLogger("sfg").info(f"Režim: {'NAOSTRO' if args.live else 'dry-run'}", extra={"cat": "info"})
        bot = BotRunner(cfg, TokenStore())
        bot.start()
        try:
            while bot.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            bot.stop()
            bot.join(5)
        return 0

    from .gui import run_gui
    return run_gui(cfg)


if __name__ == "__main__":
    sys.exit(main())
