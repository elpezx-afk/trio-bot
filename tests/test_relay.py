"""Offline tests: fake tokens/keys, all outbound HTTP mocked. Run: python -m unittest -v"""
import importlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAKE_ENV = {
    "TRIO_TESTING": "1",
    "TG_TOKEN_GROK": "111:fake",
    "TG_TOKEN_GEMINI": "222:fake",
    "TG_TOKEN_CHATGPT": "333:fake",
    "WEBHOOK_SECRET": "hook-secret",
    "TRIO_SHARED_SECRET": "trio-secret",
    "GEMINI_API_KEY": "g-fake",
    "GROQ_API_KEY": "q-fake",
    "PUBLIC_URL": "https://example.test",
}
USERNAMES = {"111": "trio_grok_bot", "222": "trio_gemini_bot", "333": "trio_gpt_bot"}
OWNER, STRANGER, GROUP = 5001, 6002, -100777


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = dict(FAKE_ENV, DATA_DIR=self.tmp)
        self.env = mock.patch.dict(os.environ, env, clear=False)
        self.env.start()
        for k in ("OWNER_ID", "ALLOWED_CHAT_ID", "GROK_FORWARD_URL"):
            os.environ.pop(k, None)
        import bot
        self.bot = importlib.reload(bot)
        self.calls = []
        self.gemini_status = 200
        self.http = mock.patch.object(self.bot.requests, "post", side_effect=self.fake_post)
        self.http.start()
        # run background work inline so tests are deterministic
        self.thr = mock.patch.object(self.bot.threading, "Thread", side_effect=self.inline_thread)
        self.thr.start()
        self.client = self.bot.app.test_client()
        self.uid = 0

    def tearDown(self):
        self.thr.stop(); self.http.stop(); self.env.stop()

    @staticmethod
    def inline_thread(target, args=(), daemon=None):
        class T:
            def start(self_inner):
                target(*args)
        return T()

    def fake_post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        if "api.telegram.org" in url:
            tok = url.split("/bot", 1)[1].split("/", 1)[0]
            method = url.rsplit("/", 1)[1]
            if method == "getMe":
                return Resp(data={"ok": True, "result": {"username": USERNAMES[tok.split(":")[0]]}})
            if method == "getWebhookInfo":
                return Resp(data={"ok": True, "result": {"url": "https://example.test/webhook/x"}})
            return Resp(data={"ok": True, "result": {}})
        if "generativelanguage" in url:
            if self.gemini_status != 200:
                return Resp(self.gemini_status)
            return Resp(data={"candidates": [{"content": {"parts": [{"text": "gemini says hi"}]}}]})
        if "api.groq.com" in url:
            return Resp(data={"choices": [{"message": {"content": "gpt says hi"}}]})
        return Resp()

    # helpers
    def post_update(self, seat, frm, chat_id, chat_type, text, secret="hook-secret", update_id=None, mid=None, **extra):
        self.uid += 1
        upd = {"update_id": update_id or self.uid, "message": {
            "message_id": mid or self.uid, "from": {"id": frm, "is_bot": False},
            "chat": {"id": chat_id, "type": chat_type}, "text": text, **extra}}
        return self.client.post(f"/webhook/{seat}", json=upd,
                                headers={"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {})

    def sends(self):
        out = []
        for url, body, _ in self.calls:
            if url.endswith("/sendMessage"):
                out.append((url.split("/bot")[1].split(":")[0], body["chat_id"], body["text"]))
        return out

    def ai_calls(self):
        return [u for u, _, _ in self.calls if "groq" in u or "generativelanguage" in u]

    def lock_owner_and_group(self):
        self.post_update("gemini", OWNER, OWNER, "private", "hi")
        self.post_update("gemini", OWNER, GROUP, "supergroup", "hello group")
        self.calls.clear()

    # tests
    def test_health(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["owner_locked"])

    def test_webhook_secret_required(self):
        self.assertEqual(self.post_update("gemini", OWNER, OWNER, "private", "hi", secret=None).status_code, 403)
        self.assertEqual(self.post_update("gemini", OWNER, OWNER, "private", "hi", secret="wrong").status_code, 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.post("/webhook/nope", json={}).status_code, 404)

    def test_first_dm_locks_owner_and_persists(self):
        r = self.post_update("gemini", OWNER, OWNER, "private", "hi")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.bot.OWNER_ID, OWNER)
        self.assertIn("Locked to you", self.sends()[0][2])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "state.json")))
        # reload picks the owner back up from the state file
        bot = importlib.reload(self.bot)
        self.assertEqual(bot.OWNER_ID, OWNER)

    def test_group_before_owner_is_ignored(self):
        self.post_update("gemini", OWNER, GROUP, "group", "hi")
        self.assertIsNone(self.bot.OWNER_ID)
        self.assertEqual(self.sends(), [])

    def test_strangers_ignored_silently(self):
        self.lock_owner_and_group()
        self.post_update("gemini", STRANGER, STRANGER, "private", "hi")
        self.post_update("gemini", STRANGER, GROUP, "supergroup", "hi all")
        self.post_update("gemini", OWNER, -100999, "supergroup", "other group")
        self.assertEqual(self.calls, [])

    def test_bot_senders_ignored(self):
        self.lock_owner_and_group()
        upd = {"update_id": 999, "message": {"message_id": 999, "from": {"id": 222, "is_bot": True},
               "chat": {"id": GROUP, "type": "supergroup"}, "text": "my own reply"}}
        self.client.post("/webhook/grok", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})
        self.assertEqual(self.calls, [])

    def test_dedupe_update_id(self):
        self.lock_owner_and_group()
        self.post_update("gemini", OWNER, OWNER, "private", "q", update_id=42)
        self.post_update("gemini", OWNER, OWNER, "private", "q", update_id=42)
        self.assertEqual(len(self.ai_calls()), 1)

    def test_group_message_seen_by_three_bots_handled_once(self):
        self.lock_owner_and_group()
        for seat in ("grok", "gemini", "chatgpt"):
            self.post_update(seat, OWNER, GROUP, "supergroup", "plan the week", mid=77)
        senders = [s[0] for s in self.sends()]
        self.assertEqual(senders, ["111", "222", "333"])  # grok ack, gemini, chatgpt; once each
        self.assertEqual(len(self.bot.grok_queue), 1)

    def test_group_broadcast_and_transcript(self):
        self.lock_owner_and_group()
        self.post_update("gemini", OWNER, GROUP, "supergroup", "ideas?")
        s = self.sends()
        self.assertEqual(s[0], ("111", GROUP, "Grok got it, will answer shortly."))
        self.assertEqual(s[1], ("222", GROUP, "gemini says hi"))
        self.assertEqual(s[2], ("333", GROUP, "gpt says hi"))
        # ChatGPT's prompt includes Gemini's reply (shared transcript)
        groq_body = [b for u, b, _ in self.calls if "groq" in u][0]
        self.assertIn("Gemini: gemini says hi", groq_body["messages"][1]["content"])
        self.assertNotIn("q-fake", str(groq_body))

    def test_group_prefix_routing(self):
        self.lock_owner_and_group()
        self.post_update("grok", OWNER, GROUP, "supergroup", "gpt: just you")
        self.assertEqual([s[0] for s in self.sends()], ["333"])
        self.calls.clear()
        self.post_update("grok", OWNER, GROUP, "supergroup", "Gemini, your take")
        self.assertEqual([s[0] for s in self.sends()], ["222"])
        self.calls.clear()
        self.post_update("grok", OWNER, GROUP, "supergroup", "grok: you")
        self.assertEqual([s[0] for s in self.sends()], ["111"])
        self.assertEqual(self.bot.grok_queue[-1]["text"], "you")

    def test_group_mention_routing(self):
        self.lock_owner_and_group()
        self.post_update("gemini", OWNER, GROUP, "supergroup", "@trio_gemini_bot @trio_gpt_bot compare")
        self.assertEqual([s[0] for s in self.sends()], ["222", "333"])

    def test_reply_to_bot_routes_to_that_bot(self):
        self.lock_owner_and_group()
        self.post_update("gemini", OWNER, GROUP, "supergroup", "more please",
                         reply_to_message={"message_id": 1, "from": {"id": 333, "is_bot": True}})
        self.assertEqual([s[0] for s in self.sends()], ["333"])

    def test_dm_goes_to_that_bot_only(self):
        self.lock_owner_and_group()
        self.post_update("chatgpt", OWNER, OWNER, "private", "hello")
        self.assertEqual([s[0] for s in self.sends()], ["333"])
        self.calls.clear()
        self.post_update("grok", OWNER, OWNER, "private", "hello grok")
        self.assertEqual(self.sends(), [("111", OWNER, "Grok got it, will answer shortly.")])
        self.assertEqual(self.ai_calls(), [])

    def test_rate_limit_message(self):
        self.lock_owner_and_group()
        self.gemini_status = 429
        self.post_update("gemini", OWNER, OWNER, "private", "q")
        self.assertIn("rate-limited", self.sends()[0][2])

    def test_grok_pending_and_reply(self):
        self.lock_owner_and_group()
        self.post_update("grok", OWNER, OWNER, "private", "for grok")
        self.assertEqual(self.client.get("/grok/pending").status_code, 403)
        self.assertEqual(self.client.get("/grok/pending", headers={"X-Trio-Secret": "bad"}).status_code, 403)
        h = {"X-Trio-Secret": "trio-secret"}
        peek = self.client.get("/grok/pending?peek=1", headers=h).get_json()["pending"]
        self.assertEqual(peek[0]["text"], "for grok")
        items = self.client.get("/grok/pending", headers=h).get_json()["pending"]
        self.assertEqual(len(items), 1)
        self.assertEqual(self.client.get("/grok/pending", headers=h).get_json()["pending"], [])
        self.calls.clear()
        self.assertEqual(self.client.post("/reply", json={"chat_id": OWNER, "text": "x"}).status_code, 403)
        self.assertEqual(self.client.post("/reply", json={"chat_id": 123, "text": "x"}, headers=h).status_code, 403)
        r = self.client.post("/reply", json={"chat_id": GROUP, "text": "Grok here"}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sends(), [("111", GROUP, "Grok here")])
        self.assertEqual(self.bot.get_transcript(GROUP)[-1], {"who": "Grok", "text": "Grok here"})

    def test_grok_forward_url(self):
        os.environ["GROK_FORWARD_URL"] = "https://forward.test/hook"
        self.bot.GROK_FORWARD_URL = "https://forward.test/hook"
        self.lock_owner_and_group()
        self.post_update("grok", OWNER, OWNER, "private", "fwd me")
        fwd = [c for c in self.calls if c[0] == "https://forward.test/hook"]
        self.assertEqual(fwd[0][1]["text"], "fwd me")

    def test_setup_route(self):
        self.assertEqual(self.client.get("/setup").status_code, 403)
        r = self.client.post("/setup", headers={"X-Trio-Secret": "trio-secret"})
        self.assertEqual(r.status_code, 200)
        hooks = [b for u, b, _ in self.calls if u.endswith("/setWebhook")]
        self.assertEqual([h["url"] for h in hooks], [f"https://example.test/webhook/{s}" for s in ("grok", "gemini", "chatgpt")])
        self.assertTrue(all(h["secret_token"] == "hook-secret" for h in hooks))
        self.assertEqual(r.get_json()["bots"]["gemini"]["username"], "trio_gemini_bot")

    def test_env_owner_id(self):
        os.environ["OWNER_ID"] = str(OWNER)
        bot = importlib.reload(self.bot)
        self.assertEqual(bot.OWNER_ID, OWNER)


if __name__ == "__main__":
    unittest.main()
