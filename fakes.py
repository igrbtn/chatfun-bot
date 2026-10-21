"""Signed initData and a stand-in Bot for tests and the local UI dev server."""
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode


def sign_init_data(token: str, user: dict, start_param: str | None = None, auth_date: int | None = None) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "user": json.dumps(user, separators=(",", ":"))}
    if start_param:
        fields["start_param"] = start_param
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeBot:
    """Records outgoing calls; chat roles come from `roles[(chat_id, user_id)]`."""

    def __init__(self, roles: dict[tuple[int, int], str] | None = None):
        self.roles = roles or {}
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []
        self.deleted: list[tuple] = []
        self._next_id = 100

    def _msg(self):
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=self.roles.get((chat_id, user_id), "left"))

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("message", chat_id, text))
        return self._msg()

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.edits.append((chat_id, message_id, text))

    async def delete_message(self, chat_id, message_id, **kw):
        self.deleted.append((chat_id, message_id))

    async def send_chat_action(self, chat_id, action, **kw):
        self.sent.append(("action", chat_id, action))

    async def send_poll(self, chat_id, question, options, **kw):
        self.sent.append(("poll", chat_id, question, list(options)))
        self.poll_votes = [0] * len(options)
        return self._msg()

    async def stop_poll(self, chat_id, message_id, **kw):
        return SimpleNamespace(options=[SimpleNamespace(voter_count=v) for v in self.poll_votes])

    async def send_video(self, chat_id, video, **kw):
        self.sent.append(("video", chat_id, kw.get("caption")))
        return self._msg()
