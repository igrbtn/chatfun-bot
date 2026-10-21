"""Local UI preview without Telegram: fake bot, demo data, auto-signed initData.

    venv/bin/python scripts/dev_server.py [--user 10]   -> http://127.0.0.1:8095/
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BOT_TOKEN"] = "123456:DEV"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="chatfun-dev-")
# --global: the dev user (id 10) becomes a global admin; must be set before core reads the config.
if "--global" in sys.argv:
    os.environ["ADMIN_IDS"] = "10"

from aiohttp import web

import core
import webapp
from fakes import FakeBot, sign_init_data

CHATS = {-1001: "Office chat", -1002: "Family", -1003: "Football friends"}
PEOPLE = {10: "Igor", 20: "Anna", 30: "Bob", 40: "Dilnoza", 50: "Sardor"}


def seed() -> None:
    s = core.store
    for cid, title in CHATS.items():
        s.upsert_chat(cid, title)
        for uid, name in PEOPLE.items():
            s.touch(cid, uid, name)
    for i, uid in enumerate([20, 30, 20, 40, 20, 30, 50]):
        s.save_pick(-1001, f"2026-09-{i + 1:02d}", uid)
    today = core.today().isoformat()
    for uid, n in ((20, 42), (10, 17), (30, 9), (40, 3)):
        for _ in range(n):
            s.count_message(-1001, uid, today)
    for _ in range(60):
        s.count_message(-1001, 50, "2026-09-01")
    for i, (a, b) in enumerate([(20, 30), (40, 50), (20, 30)]):
        s.save_pair(-1001, f"2026-09-{i + 1:02d}", a, b)
    for challenger, opponent, winner in ((10, 20, 10), (20, 30, 30), (30, 10, 30)):
        d = s.create_duel(-1001, challenger, opponent, 0.0)
        s.finish_duel(d, "done", opponent=opponent, winner=winner)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, default=10)
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--global", dest="global_admin", action="store_true")
    args = ap.parse_args()
    seed()
    roles = {(cid, uid): "member" for cid in CHATS for uid in PEOPLE if cid != -1003}
    roles[(-1001, 10)] = "creator"
    init = sign_init_data(core.cfg.bot_token, {"id": args.user, "first_name": PEOPLE.get(args.user, "Dev")})
    page = (ROOT / "webapp" / "index.html").read_text(encoding="utf-8")
    page = page.replace("<head>", f"<head><script>window.DEV_INIT_DATA = {init!r};</script>", 1)
    app = webapp.build_app(FakeBot(roles), index_html=page)
    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
