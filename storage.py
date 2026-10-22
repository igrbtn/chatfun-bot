import json
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS picks (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, day)
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS members_by_user ON members (user_id);
CREATE TABLE IF NOT EXISTS bot_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    delete_at REAL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS tie_polls (
    chat_id INTEGER NOT NULL,
    state_key TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    options TEXT NOT NULL,
    close_at REAL NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, state_key)
);
CREATE TABLE IF NOT EXISTS activity (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS pair_picks (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    user_a INTEGER NOT NULL,
    user_b INTEGER NOT NULL,
    PRIMARY KEY (chat_id, day)
);
CREATE TABLE IF NOT EXISTS hero_bonus (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (chat_id, day, user_id, reason)
);
CREATE TABLE IF NOT EXISTS hero_pushes (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    hero INTEGER NOT NULL,
    victim INTEGER NOT NULL,
    winner INTEGER NOT NULL,
    PRIMARY KEY (chat_id, day, hero)
);
CREATE TABLE IF NOT EXISTS duels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    challenger INTEGER NOT NULL,
    target INTEGER,
    opponent INTEGER,
    message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    winner INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS duels_by_chat ON duels (chat_id, status);
"""

# Telegram lets a bot delete its own messages only for 48 hours.
DELETABLE_FOR = 48 * 3600

# Per-chat switches editable by chat admins in the Mini App.
DEFAULT_SETTINGS = {
    "downloads": True,      # react to video links
    "hero": True,           # person-of-the-day game
    "auto_register": True,  # anyone who writes joins the game
    "drumroll": True,       # suspense messages before the reveal
    "tie_poll": True,       # shared first place -> 15-minute "who is the very best" poll
    "pair": True,           # pair-of-the-day game
    "stats": True,          # message counters and /stats
    "random": True,         # /who /ball /coin /roll
    "duels": True,          # /duel challenges
    "chatter": True,        # answer when someone replies to or mentions the bot
    "weighted": True,       # daily pick odds grow with recent activity
    "push": True,           # today's hero may try once to push the title onto someone (/push)
    "cleanup_commands": False,  # delete people's /commands to the bot shortly after answering (needs admin rights)
}


def manual_id(key: str) -> int:
    """Stable negative id for an admin-added player; real Telegram user ids are positive."""
    return -(zlib.crc32(key.encode()) + 1)


@dataclass(frozen=True)
class Member:
    user_id: int
    name: str
    active: bool = True
    username: str | None = None


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Additive column changes for databases created by older versions."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(members)")}
        if "username" not in columns:
            self.db.execute("ALTER TABLE members ADD COLUMN username TEXT")
        self._widen_push_key()
        self._repair_orphans()

    def _widen_push_key(self) -> None:
        """0.13 allowed one /push per chat per day; now it is one per person per day (bonus holders push too)."""
        sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name = 'hero_pushes'").fetchone()[0]
        if "PRIMARY KEY (chat_id, day, hero)" in sql:
            return
        self.db.execute("ALTER TABLE hero_pushes RENAME TO hero_pushes_old")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO hero_pushes SELECT chat_id, day, hero, victim, winner FROM hero_pushes_old")
        self.db.execute("DROP TABLE hero_pushes_old")

    # --- chats ---------------------------------------------------------

    def upsert_chat(self, chat_id: int, title: str) -> None:
        self.db.execute(
            "INSERT INTO chats (chat_id, title) VALUES (?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET title = excluded.title",
            (chat_id, title),
        )

    def chat_title(self, chat_id: int) -> str | None:
        row = self.db.execute("SELECT title FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else None

    def _raw_settings(self, chat_id: int) -> dict:
        row = self.db.execute("SELECT settings FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _save_raw_settings(self, chat_id: int, raw: dict) -> None:
        self.db.execute(
            "INSERT INTO chats (chat_id, title, settings) VALUES (?, ?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET settings = excluded.settings",
            (chat_id, str(chat_id), json.dumps(raw, ensure_ascii=False)),
        )

    def settings(self, chat_id: int) -> dict:
        stored = self._raw_settings(chat_id)
        return {k: bool(stored.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}

    def update_settings(self, chat_id: int, changes: dict) -> dict:
        raw = self._raw_settings(chat_id)
        raw.update({k: bool(v) for k, v in changes.items() if k in DEFAULT_SETTINGS})
        self._save_raw_settings(chat_id, raw)
        return self.settings(chat_id)

    def hero_title(self, chat_id: int) -> str | None:
        """Custom name of the daily game ("Токсик дня"); None = default text."""
        return self._raw_settings(chat_id).get("hero_title") or None

    def set_hero_title(self, chat_id: int, title: str | None) -> None:
        raw = self._raw_settings(chat_id)
        if title:
            raw["hero_title"] = title
        else:
            raw.pop("hero_title", None)
        self._save_raw_settings(chat_id, raw)

    def all_chats(self) -> list[tuple[int, str]]:
        rows = self.db.execute("SELECT chat_id, title FROM chats ORDER BY title COLLATE NOCASE")
        return [(cid, title) for cid, title in rows]

    def user_chats(self, user_id: int) -> list[tuple[int, str]]:
        """Group chats where the user has been seen, for the Mini App chat picker."""
        rows = self.db.execute(
            "SELECT m.chat_id, COALESCE(c.title, CAST(m.chat_id AS TEXT)) FROM members m "
            "LEFT JOIN chats c ON c.chat_id = m.chat_id WHERE m.user_id = ? ORDER BY 2",
            (user_id,),
        )
        return [(cid, title) for cid, title in rows]

    # --- members -------------------------------------------------------

    def add_manual(self, chat_id: int, usernames: list[str], names: list[str]) -> tuple[list[str], list[str]]:
        """Admin-added players the bot has not seen yet (it cannot resolve @username to an id).

        They get a synthetic negative user_id; a username player is merged into the real account
        on their first message (see touch). Returns (added, skipped) labels.
        """
        added, skipped = [], []
        for username in usernames:
            exists = self.db.execute(
                "SELECT 1 FROM members WHERE chat_id = ? AND lower(username) = lower(?)", (chat_id, username)
            ).fetchone()
            if exists:
                skipped.append(f"@{username}")
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO members (chat_id, user_id, name, active, username) VALUES (?, ?, ?, 1, ?)",
                (chat_id, manual_id("u:" + username.lower()), f"@{username}", username),
            )
            added.append(f"@{username}")
        for name in names:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO members (chat_id, user_id, name, active) VALUES (?, ?, ?, 1)",
                (chat_id, manual_id("n:" + name.lower()), name),
            )
            (added if cur.rowcount == 1 else skipped).append(name)
        return added, skipped

    def remove_member(self, chat_id: int, user_id: int) -> bool:
        """Only manual players can be removed; real ones opt out instead (history stays theirs)."""
        if user_id >= 0:
            return False
        self.db.execute("DELETE FROM picks WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        return self.db.execute(
            "DELETE FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).rowcount == 1

    def _move_user(self, chat_id: int, old: int, new: int) -> None:
        """Re-point every record of `old` to `new` (placeholder -> real account): wins, pairs, duels, bonuses."""
        updates = [
            ("picks", "user_id"), ("pair_picks", "user_a"), ("pair_picks", "user_b"),
            ("hero_pushes", "hero"), ("hero_pushes", "victim"), ("hero_pushes", "winner"),
            ("duels", "challenger"), ("duels", "target"), ("duels", "opponent"), ("duels", "winner"),
        ]
        for table, column in updates:
            self.db.execute(
                f"UPDATE {table} SET {column} = ? WHERE chat_id = ? AND {column} = ?", (new, chat_id, old)
            )
        # Pairs are stored ordered (user_a < user_b); the swap uses the old values on both sides.
        self.db.execute("UPDATE pair_picks SET user_a = user_b, user_b = user_a WHERE chat_id = ? AND user_a > user_b",
                        (chat_id,))
        self.db.execute("UPDATE OR IGNORE hero_bonus SET user_id = ? WHERE chat_id = ? AND user_id = ?",
                        (new, chat_id, old))
        self.db.execute("DELETE FROM hero_bonus WHERE chat_id = ? AND user_id = ?", (chat_id, old))

    def _repair_orphans(self) -> None:
        """Records of merged placeholders that older versions forgot to re-point (pairs, duels): the
        placeholder id is derived from the username, so the real account can still be found."""
        by_placeholder = {}
        for chat_id, user_id, username in self.db.execute(
            "SELECT chat_id, user_id, username FROM members WHERE user_id > 0 AND username IS NOT NULL"
        ):
            by_placeholder[(chat_id, manual_id("u:" + username.lower()))] = user_id
        orphans = self.db.execute(
            "SELECT chat_id, user_a FROM pair_picks WHERE user_a < 0 UNION SELECT chat_id, user_b FROM pair_picks "
            "WHERE user_b < 0 UNION SELECT chat_id, user_id FROM picks WHERE user_id < 0 "
            "UNION SELECT chat_id, challenger FROM duels WHERE challenger < 0"
        ).fetchall()
        for chat_id, old in orphans:
            real = by_placeholder.get((chat_id, old))
            exists = self.db.execute("SELECT 1 FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, old)).fetchone()
            if real and not exists:
                self._move_user(chat_id, old, real)

    def _claim_manual(self, chat_id: int, user_id: int, username: str) -> None:
        """A real user with a pre-added @username shows up: move the placeholder's wins and state."""
        row = self.db.execute(
            "SELECT user_id, active FROM members WHERE chat_id = ? AND user_id < 0 AND lower(username) = lower(?)",
            (chat_id, username),
        ).fetchone()
        if row is None:
            return
        placeholder, active = row
        self._move_user(chat_id, placeholder, user_id)
        has_real = self.db.execute(
            "SELECT 1 FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()
        if has_real:
            self.db.execute("DELETE FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, placeholder))
        else:
            self.db.execute(
                "UPDATE members SET user_id = ?, active = ? WHERE chat_id = ? AND user_id = ?",
                (user_id, active, chat_id, placeholder),
            )

    def touch(
        self, chat_id: int, user_id: int, name: str, register: bool = True, username: str | None = None
    ) -> None:
        """Seen in chat: remember the user (the Mini App lists chats from this), refresh name/username.

        A new user joins the game only when register is set; existing opt-in/opt-out is kept.
        """
        if username:
            self._claim_manual(chat_id, user_id, username)
        self.db.execute(
            "INSERT INTO members (chat_id, user_id, name, active, username) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (chat_id, user_id) DO UPDATE SET name = excluded.name, username = excluded.username",
            (chat_id, user_id, name, int(register), username),
        )

    def set_active(self, chat_id: int, user_id: int, name: str | None, active: bool) -> bool:
        """Returns False if the member was already in that state (or unknown and being deactivated)."""
        row = self.db.execute(
            "SELECT active, name FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()
        current = bool(row[0]) if row else False
        if current == active:
            return False
        if row is None and name is None:
            return False
        self.db.execute(
            "INSERT INTO members (chat_id, user_id, name, active) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (chat_id, user_id) DO UPDATE SET name = excluded.name, active = excluded.active",
            (chat_id, user_id, name or row[1], int(active)),
        )
        return True

    def active_members(self, chat_id: int) -> list[Member]:
        return [m for m in self.all_members(chat_id) if m.active]

    def all_members(self, chat_id: int) -> list[Member]:
        rows = self.db.execute(
            "SELECT user_id, name, active, username FROM members WHERE chat_id = ? ORDER BY name COLLATE NOCASE",
            (chat_id,),
        )
        return [Member(uid, name, bool(active), username) for uid, name, active, username in rows]

    def member(self, chat_id: int, user_id: int) -> Member | None:
        row = self.db.execute(
            "SELECT user_id, name, active, username FROM members WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return Member(row[0], row[1], bool(row[2]), row[3]) if row else None

    # --- picks ---------------------------------------------------------

    def pick_for(self, chat_id: int, day: str) -> int | None:
        row = self.db.execute(
            "SELECT user_id FROM picks WHERE chat_id = ? AND day = ?", (chat_id, day)
        ).fetchone()
        return row[0] if row else None

    def save_pick(self, chat_id: int, day: str, user_id: int) -> bool:
        """False if another roll already took this day."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO picks (chat_id, day, user_id) VALUES (?, ?, ?)", (chat_id, day, user_id)
        )
        return cur.rowcount == 1

    def clear_pick(self, chat_id: int, day: str) -> bool:
        self.db.execute("DELETE FROM hero_pushes WHERE chat_id = ? AND day = ?", (chat_id, day))
        cur = self.db.execute("DELETE FROM picks WHERE chat_id = ? AND day = ?", (chat_id, day))
        return cur.rowcount == 1

    def reset_scores(self, chat_id: int) -> int:
        self.db.execute("DELETE FROM hero_pushes WHERE chat_id = ?", (chat_id,))
        self.db.execute("DELETE FROM hero_bonus WHERE chat_id = ?", (chat_id,))
        return self.db.execute("DELETE FROM picks WHERE chat_id = ?", (chat_id,)).rowcount

    def add_bonus(self, chat_id: int, day: str, user_id: int, reason: str) -> bool:
        """+1 to the score (e.g. won a tie poll). One bonus per person, day and reason."""
        return self.db.execute(
            "INSERT OR IGNORE INTO hero_bonus (chat_id, day, user_id, reason) VALUES (?, ?, ?, ?)",
            (chat_id, day, user_id, reason),
        ).rowcount == 1

    # Every win as (user_id, day): daily picks, the "double" a hero earns by failing to push the title away,
    # and bonus points (tie polls).
    SCORES = (
        "SELECT user_id, day FROM picks WHERE chat_id = :chat "
        "UNION ALL SELECT hero AS user_id, day FROM hero_pushes WHERE chat_id = :chat AND winner = hero "
        "UNION ALL SELECT user_id, day FROM hero_bonus WHERE chat_id = :chat"
    )

    def top(self, chat_id: int, limit: int = 10) -> list[tuple[int, str, int]]:
        rows = self.db.execute(
            f"SELECT s.user_id, COALESCE(m.name, CAST(s.user_id AS TEXT)), COUNT(*) AS n FROM ({self.SCORES}) s "
            "LEFT JOIN members m ON m.chat_id = :chat AND m.user_id = s.user_id "
            "GROUP BY s.user_id ORDER BY n DESC, MAX(s.day) DESC LIMIT :limit",
            {"chat": chat_id, "limit": limit},
        )
        return [(uid, name, n) for uid, name, n in rows]

    def wins(self, chat_id: int, user_id: int) -> int:
        return self.db.execute(
            f"SELECT COUNT(*) FROM ({self.SCORES}) s WHERE s.user_id = :user", {"chat": chat_id, "user": user_id}
        ).fetchone()[0]

    def last_win_before(self, chat_id: int, user_id: int, day: str) -> str | None:
        return self.db.execute(
            "SELECT MAX(day) FROM picks WHERE chat_id = ? AND user_id = ? AND day < ?", (chat_id, user_id, day)
        ).fetchone()[0]

    def transfer_pick(self, chat_id: int, day: str, user_id: int) -> None:
        self.db.execute("UPDATE picks SET user_id = ? WHERE chat_id = ? AND day = ?", (user_id, chat_id, day))

    def push_for(self, chat_id: int, day: str, hero: int) -> dict | None:
        row = self.db.execute(
            "SELECT hero, victim, winner FROM hero_pushes WHERE chat_id = ? AND day = ? AND hero = ?",
            (chat_id, day, hero),
        ).fetchone()
        return {"hero": row[0], "victim": row[1], "winner": row[2]} if row else None

    def has_bonus(self, chat_id: int, day: str, user_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM hero_bonus WHERE chat_id = ? AND day = ? AND user_id = ?", (chat_id, day, user_id)
        ).fetchone() is not None

    def move_bonus(self, chat_id: int, day: str, from_user: int, to_user: int) -> bool:
        """Hand one of today's bonus points over (a successful /push by a bonus holder)."""
        return self.db.execute(
            "UPDATE OR IGNORE hero_bonus SET user_id = ? WHERE rowid = (SELECT rowid FROM hero_bonus "
            "WHERE chat_id = ? AND day = ? AND user_id = ? LIMIT 1)",
            (to_user, chat_id, day, from_user),
        ).rowcount == 1

    def member_by_username(self, chat_id: int, username: str) -> int | None:
        row = self.db.execute(
            "SELECT user_id FROM members WHERE chat_id = ? AND lower(username) = lower(?) ORDER BY user_id DESC",
            (chat_id, username.lstrip("@")),
        ).fetchone()
        return row[0] if row else None

    def save_push(self, chat_id: int, day: str, hero: int, victim: int, winner: int) -> bool:
        """One push attempt per person per day; False if it was already used."""
        return self.db.execute(
            "INSERT OR IGNORE INTO hero_pushes (chat_id, day, hero, victim, winner) VALUES (?, ?, ?, ?, ?)",
            (chat_id, day, hero, victim, winner),
        ).rowcount == 1

    def messages_since(self, chat_id: int, since: str) -> dict[int, int]:
        """user_id -> messages counted from `since` (ISO day, inclusive)."""
        rows = self.db.execute(
            "SELECT user_id, SUM(messages) FROM activity WHERE chat_id = ? AND day >= ? GROUP BY user_id",
            (chat_id, since),
        )
        return {uid: n for uid, n in rows}

    # --- bot messages ----------------------------------------------------

    def remember(self, chat_id: int, message_id: int, kind: str, now: float, delete_at: float | None = None) -> None:
        """Track a message the bot sent so it can be edited or deleted later (even after a restart)."""
        self.db.execute(
            "INSERT OR REPLACE INTO bot_messages (chat_id, message_id, kind, created_at, delete_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, message_id, kind, now, delete_at),
        )

    def remember_new(self, chat_id: int, message_id: int, kind: str, now: float) -> None:
        """Record a sent message unless it is already tracked (keeps an existing kind / scheduled deletion)."""
        self.db.execute(
            "INSERT OR IGNORE INTO bot_messages (chat_id, message_id, kind, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, message_id, kind, now),
        )

    def expire_older_than(self, chat_id: int, older_than: float, now: float) -> int:
        """Mark tracked bot messages sent before `older_than` for deletion now. Returns how many."""
        return self.db.execute(
            "UPDATE bot_messages SET delete_at = ? WHERE chat_id = ? AND created_at < ? AND delete_at IS NULL "
            "AND kind != 'command'",
            (now, chat_id, older_than),
        ).rowcount

    def forget(self, chat_id: int, message_id: int) -> None:
        self.db.execute("DELETE FROM bot_messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))

    def schedule_delete(self, chat_id: int, message_id: int, kind: str, at: float) -> None:
        self.db.execute(
            "UPDATE bot_messages SET kind = ?, delete_at = ? WHERE chat_id = ? AND message_id = ?",
            (kind, at, chat_id, message_id),
        )

    def orphan_statuses(self, now: float) -> int:
        """Progress messages left by a crash: due for deletion right away. Returns how many."""
        return self.db.execute(
            "UPDATE bot_messages SET delete_at = ? WHERE kind = 'status' AND delete_at IS NULL", (now,)
        ).rowcount

    def due_for_delete(self, now: float, limit: int = 50) -> list[tuple[int, int]]:
        rows = self.db.execute(
            "SELECT chat_id, message_id FROM bot_messages WHERE delete_at IS NOT NULL AND delete_at <= ? "
            "ORDER BY delete_at LIMIT ?",
            (now, limit),
        )
        return [(c, m) for c, m in rows]

    def messages(self, chat_id: int, kind: str | None = None) -> list[tuple[int, str]]:
        rows = self.db.execute(
            "SELECT message_id, kind FROM bot_messages WHERE chat_id = ? AND (? IS NULL OR kind = ?) "
            "ORDER BY message_id",
            (chat_id, kind, kind),
        )
        return [(m, k) for m, k in rows]

    def prune_messages(self, now: float) -> int:
        """Past the 48h window the ids are useless: the bot can no longer delete those messages."""
        return self.db.execute(
            "DELETE FROM bot_messages WHERE created_at < ?", (now - DELETABLE_FOR,)
        ).rowcount

    # --- tie polls -------------------------------------------------------

    def leaders(self, chat_id: int) -> tuple[int, list[int]]:
        """(top score, user ids sharing it), ordered by who reached that score first
        (that one is the "previous leader" and wins a drawn vote)."""
        reached: dict[int, tuple[int, str]] = {}  # user -> (wins so far, day of the latest win)
        for user_id, day in self.db.execute(f"SELECT user_id, day FROM ({self.SCORES}) ORDER BY day", {"chat": chat_id}):
            wins = reached.get(user_id, (0, ""))[0] + 1
            reached[user_id] = (wins, day)
        if not reached:
            return 0, []
        best = max(w for w, _ in reached.values())
        tied = sorted((day, uid) for uid, (w, day) in reached.items() if w == best)
        return best, [uid for _, uid in tied]

    def save_tie_poll(self, chat_id: int, state_key: str, message_id: int, options: list[int], close_at: float) -> bool:
        """False if this exact tie already had a poll."""
        return self.db.execute(
            "INSERT OR IGNORE INTO tie_polls (chat_id, state_key, message_id, options, close_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, state_key, message_id, json.dumps(options), close_at),
        ).rowcount == 1

    def tie_polled(self, chat_id: int, state_key: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM tie_polls WHERE chat_id = ? AND state_key = ?", (chat_id, state_key)
        ).fetchone() is not None

    def due_tie_polls(self, now: float) -> list[tuple[int, str, int, list[int]]]:
        rows = self.db.execute(
            "SELECT chat_id, state_key, message_id, options FROM tie_polls WHERE closed = 0 AND close_at <= ?",
            (now,),
        )
        return [(c, k, m, json.loads(o)) for c, k, m, o in rows]

    def close_tie_poll(self, chat_id: int, state_key: str) -> None:
        self.db.execute(
            "UPDATE tie_polls SET closed = 1 WHERE chat_id = ? AND state_key = ?", (chat_id, state_key)
        )

    # --- custom texts ----------------------------------------------------

    def custom_text(self, chat_id: int, key: str) -> str | None:
        """Admin-set per-chat wording (e.g. "pair_title"); None = default text."""
        return self._raw_settings(chat_id).get(key) or None

    def set_custom_text(self, chat_id: int, key: str, value: str | None) -> None:
        raw = self._raw_settings(chat_id)
        if value:
            raw[key] = value
        else:
            raw.pop(key, None)
        self._save_raw_settings(chat_id, raw)

    # --- activity --------------------------------------------------------

    def count_message(self, chat_id: int, user_id: int, day: str) -> None:
        """Only a per-day counter: message text is never stored."""
        self.db.execute(
            "INSERT INTO activity (chat_id, user_id, day, messages) VALUES (?, ?, ?, 1) "
            "ON CONFLICT (chat_id, user_id, day) DO UPDATE SET messages = messages + 1",
            (chat_id, user_id, day),
        )

    def activity_top(self, chat_id: int, since: str | None, limit: int = 10) -> tuple[list[tuple[int, str, int]], int]:
        """([(user_id, name, messages)], total messages) from `since` (ISO day, inclusive) or all time."""
        since = since or ""
        rows = self.db.execute(
            "SELECT a.user_id, COALESCE(m.name, CAST(a.user_id AS TEXT)), SUM(a.messages) AS n FROM activity a "
            "LEFT JOIN members m ON m.chat_id = a.chat_id AND m.user_id = a.user_id "
            "WHERE a.chat_id = ? AND a.day >= ? GROUP BY a.user_id ORDER BY n DESC LIMIT ?",
            (chat_id, since, limit),
        ).fetchall()
        total = self.db.execute(
            "SELECT COALESCE(SUM(messages), 0) FROM activity WHERE chat_id = ? AND day >= ?", (chat_id, since)
        ).fetchone()[0]
        return [(uid, name, n) for uid, name, n in rows], total

    # --- pair of the day -------------------------------------------------

    def pair_for(self, chat_id: int, day: str) -> tuple[int, int] | None:
        row = self.db.execute(
            "SELECT user_a, user_b FROM pair_picks WHERE chat_id = ? AND day = ?", (chat_id, day)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def save_pair(self, chat_id: int, day: str, a: int, b: int) -> bool:
        a, b = sorted((a, b))
        return self.db.execute(
            "INSERT OR IGNORE INTO pair_picks (chat_id, day, user_a, user_b) VALUES (?, ?, ?, ?)",
            (chat_id, day, a, b),
        ).rowcount == 1

    def clear_pair(self, chat_id: int, day: str) -> bool:
        return self.db.execute(
            "DELETE FROM pair_picks WHERE chat_id = ? AND day = ?", (chat_id, day)
        ).rowcount == 1

    def pair_top(self, chat_id: int, limit: int = 10) -> list[tuple[int, int, int]]:
        rows = self.db.execute(
            "SELECT user_a, user_b, COUNT(*) AS n FROM pair_picks WHERE chat_id = ? "
            "GROUP BY user_a, user_b ORDER BY n DESC, MAX(day) DESC LIMIT ?",
            (chat_id, limit),
        )
        return [(a, b, n) for a, b, n in rows]

    # --- duels -----------------------------------------------------------

    def create_duel(self, chat_id: int, challenger: int, target: int | None, now: float) -> int:
        return self.db.execute(
            "INSERT INTO duels (chat_id, challenger, target, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, challenger, target, now),
        ).lastrowid

    def set_duel_message(self, duel_id: int, message_id: int) -> None:
        self.db.execute("UPDATE duels SET message_id = ? WHERE id = ?", (message_id, duel_id))

    def duel(self, duel_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT id, chat_id, challenger, target, opponent, message_id, status, winner FROM duels WHERE id = ?",
            (duel_id,),
        ).fetchone()
        keys = ("id", "chat_id", "challenger", "target", "opponent", "message_id", "status", "winner")
        return dict(zip(keys, row, strict=True)) if row else None

    def pending_duel(self, chat_id: int, challenger: int) -> int | None:
        row = self.db.execute(
            "SELECT id FROM duels WHERE chat_id = ? AND challenger = ? AND status = 'pending'", (chat_id, challenger)
        ).fetchone()
        return row[0] if row else None

    def finish_duel(self, duel_id: int, status: str, opponent: int | None = None, winner: int | None = None) -> bool:
        """Moves a pending duel to its final state; False if someone else already did (double tap)."""
        return self.db.execute(
            "UPDATE duels SET status = ?, opponent = ?, winner = ? WHERE id = ? AND status = 'pending'",
            (status, opponent, winner, duel_id),
        ).rowcount == 1

    def stale_duels(self, older_than: float) -> list[tuple[int, int, int | None]]:
        rows = self.db.execute(
            "SELECT id, chat_id, message_id FROM duels WHERE status = 'pending' AND created_at < ?", (older_than,)
        )
        return [(i, c, m) for i, c, m in rows]

    def duel_stats(self, chat_id: int, limit: int = 10) -> list[tuple[int, str, int, int]]:
        """[(user_id, name, wins, losses)] over finished duels, most wins first."""
        rows = self.db.execute(
            "SELECT p.uid, COALESCE(m.name, CAST(p.uid AS TEXT)), "
            "SUM(p.uid = d.winner) AS wins, SUM(p.uid != d.winner) AS losses "
            "FROM duels d JOIN (SELECT id, challenger AS uid FROM duels UNION ALL "
            "SELECT id, opponent AS uid FROM duels) p ON p.id = d.id "
            "LEFT JOIN members m ON m.chat_id = d.chat_id AND m.user_id = p.uid "
            "WHERE d.chat_id = ? AND d.status = 'done' AND p.uid IS NOT NULL "
            "GROUP BY p.uid ORDER BY wins DESC, losses ASC LIMIT ?",
            (chat_id, limit),
        )
        return [(uid, name, int(w), int(lo)) for uid, name, w, lo in rows]
