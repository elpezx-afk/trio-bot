"""Trio relay: one Flask app that runs three Telegram bots (Grok, Gemini, ChatGPT seat).

* Each bot has its own webhook: /webhook/<seat>, checked against WEBHOOK_SECRET
  (Telegram's X-Telegram-Bot-Api-Secret-Token header).
* Telegram gets a 200 right away; the work happens in a background thread.
* Only the owner (OWNER_ID) is served, in DMs and in one group (ALLOWED_CHAT_ID).
  Everyone else is ignored silently.
* Gemini -> Google AI Studio, ChatGPT seat -> Groq (openai/gpt-oss-120b),
  Grok -> queued for Grok Bot to pick up via GET /grok/pending, answered via POST /reply.

No secrets live in this file. Everything comes from environment variables (see README).
"""

import hmac
import json
import logging
import os
import re
import threading
import time
from collections import deque

import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trio")

app = Flask(__name__)


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEATS = ("grok", "gemini", "chatgpt")
SEAT_LABEL = {"grok": "Grok", "gemini": "Gemini", "chatgpt": "ChatGPT"}
TOKEN_ENV = {"grok": "TG_TOKEN_GROK", "gemini": "TG_TOKEN_GEMINI", "chatgpt": "TG_TOKEN_CHATGPT"}
# "name:" prefixes that send a group message to one seat only
PREFIXES = {"grok": "grok", "gemini": "gemini", "gpt": "chatgpt", "chatgpt": "chatgpt"}

WEBHOOK_SECRET = env("WEBHOOK_SECRET")
TRIO_SHARED_SECRET = env("TRIO_SHARED_SECRET")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODEL = "gemini-flash-latest"
GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-120b")
# OpenAI-compatible endpoint for the ChatGPT seat (Groq by default; e.g. OpenRouter: https://openrouter.ai/api/v1/chat/completions with model openai/gpt-oss-120b:free)
CHATGPT_API_URL = env("CHATGPT_API_URL", "https://api.groq.com/openai/v1/chat/completions")
GROK_FORWARD_URL = env("GROK_FORWARD_URL")
PUBLIC_URL = (env("PUBLIC_URL") or env("RENDER_EXTERNAL_URL")).rstrip("/")
DATA_DIR = env("DATA_DIR", "/tmp/trio")
OWNER_NAME = env("OWNER_NAME", "Michael")

TRANSCRIPT_LEN = 20
DEDUPE_TTL = 15 * 60  # seconds
GROK_QUEUE_MAX = 200
HTTP_TIMEOUT = 60


def token(seat):
    return env(TOKEN_ENV[seat])


def bot_id(seat):
    """A bot's numeric user id is the part of its token before the colon."""
    t = token(seat)
    head = t.split(":", 1)[0]
    return int(head) if head.isdigit() else None


# ---------------------------------------------------------------------------
# Small persistent state (owner + group). Render's free disk is wiped on every
# deploy/restart, so the file only bridges the gap until the ids are copied
# into OWNER_ID / ALLOWED_CHAT_ID.
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
STATE_FILE = os.path.join(DATA_DIR, "state.json")


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("could not save state: %s", e)


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_state = _load_state()
OWNER_ID = _int_or_none(env("OWNER_ID")) or _int_or_none(_state.get("owner_id"))
ALLOWED_CHAT_ID = _int_or_none(env("ALLOWED_CHAT_ID")) or _int_or_none(_state.get("group_id"))


def _remember(key, value):
    _state[key] = value
    _save_state(_state)


# ---------------------------------------------------------------------------
# Dedupe (Telegram retries when a sleeping free instance answers late)
# ---------------------------------------------------------------------------

class TTLSet:
    def __init__(self, ttl):
        self.ttl = ttl
        self._items = {}
        self._lock = threading.Lock()

    def add(self, key):
        """Return True if key is new, False if it was seen within the TTL."""
        now = time.time()
        with self._lock:
            if len(self._items) > 5000:
                self._items = {k: t for k, t in self._items.items() if t > now}
            exp = self._items.get(key)
            if exp and exp > now:
                return False
            self._items[key] = now + self.ttl
            return True


seen_updates = TTLSet(DEDUPE_TTL)   # (seat, update_id)
seen_messages = TTLSet(DEDUPE_TTL)  # (chat_id, message_id): all 3 bots see the same group message

# ---------------------------------------------------------------------------
# Rolling transcript per chat, shared by all seats
# ---------------------------------------------------------------------------

_transcripts = {}
_tx_lock = threading.Lock()


def add_transcript(chat_id, who, text):
    with _tx_lock:
        _transcripts.setdefault(chat_id, deque(maxlen=TRANSCRIPT_LEN)).append({"who": who, "text": text})


def get_transcript(chat_id):
    with _tx_lock:
        return list(_transcripts.get(chat_id, ()))


def transcript_text(chat_id):
    return "\n".join(f"{m['who']}: {m['text']}" for m in get_transcript(chat_id))


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

_usernames = {}


def tg(seat, method, payload=None):
    t = token(seat)
    if not t:
        return {"ok": False, "description": f"{TOKEN_ENV[seat]} not set"}
    try:
        r = requests.post(f"https://api.telegram.org/bot{t}/{method}", json=payload or {}, timeout=HTTP_TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("telegram %s %s failed: %s", seat, method, type(e).__name__)
        return {"ok": False, "description": type(e).__name__}
    if not data.get("ok"):
        log.warning("telegram %s %s error: %s", seat, method, data.get("description"))
    elif method == "sendMessage":
        log.info("telegram %s sendMessage ok (msg %s)", seat, (data.get("result") or {}).get("message_id"))
    return data


def username(seat):
    """Bot @username (lower case), looked up once with getMe."""
    if seat not in _usernames and token(seat):
        res = tg(seat, "getMe")
        if res.get("ok"):
            _usernames[seat] = (res["result"].get("username") or "").lower()
    return _usernames.get(seat, "")


def send(seat, chat_id, text, reply_to=None):
    text = text or "(empty reply)"
    for i in range(0, len(text), 4000):  # Telegram limit is 4096 chars
        payload = {"chat_id": chat_id, "text": text[i:i + 4000]}
        if reply_to and i == 0:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        tg(seat, "sendMessage", payload)


# ---------------------------------------------------------------------------
# AI seats
# ---------------------------------------------------------------------------

class RateLimited(Exception):
    pass


def system_prompt(seat, chat_type):
    others = ", ".join(SEAT_LABEL[s] for s in SEATS if s != seat)
    where = "a private Telegram group" if chat_type != "private" else "a private Telegram chat"
    return (
        f"You are {SEAT_LABEL[seat]}, one of three AI assistants (the Trio: Grok, Gemini, ChatGPT) "
        f"working together for {OWNER_NAME} in {where}. The other assistants are {others}. "
        "You can see the recent conversation, including the other assistants' replies; build on them, "
        "point out disagreements, and avoid repeating what was already said. "
        "Reply in plain text (no Markdown), concise and useful."
    )


def user_prompt(chat_id, text):
    history = transcript_text(chat_id)
    return f"Recent conversation (oldest first):\n{history}\n\nReply to {OWNER_NAME}'s latest message: {text}"


def ask_gemini(chat_id, chat_type, text):
    if not GEMINI_API_KEY:
        return "Gemini seat isn't set up yet (GEMINI_API_KEY missing)."
    body = {
        "system_instruction": {"parts": [{"text": system_prompt("gemini", chat_type)}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt(chat_id, text)}]}],
    }
    for model in (GEMINI_MODEL, GEMINI_FALLBACK_MODEL):
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json=body,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 429:
            raise RateLimited()
        if r.status_code == 404 and model != GEMINI_FALLBACK_MODEL:
            log.warning("gemini model %s not found, trying %s", model, GEMINI_FALLBACK_MODEL)
            continue
        r.raise_for_status()
        data = r.json()
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        out = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        return out or "(Gemini returned no text)"
    return "(Gemini model not available)"


def ask_groq(chat_id, chat_type, text):
    if not GROQ_API_KEY:
        return "ChatGPT seat isn't set up yet (GROQ_API_KEY missing)."
    r = requests.post(
        CHATGPT_API_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt("chatgpt", chat_type)},
                {"role": "user", "content": user_prompt(chat_id, text)},
            ],
        },
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code == 429:
        raise RateLimited()
    r.raise_for_status()
    out = (r.json()["choices"][0]["message"].get("content") or "").strip()
    return out or "(ChatGPT seat returned no text)"


ASK = {"gemini": ask_gemini, "chatgpt": ask_groq}


def answer_with(seat, msg, text):
    chat_id = msg["chat"]["id"]
    log.info("asking %s", seat)
    try:
        reply = ASK[seat](chat_id, msg["chat"].get("type"), text)
    except RateLimited:
        reply = f"{SEAT_LABEL[seat]} is rate-limited on the free plan right now. Try again in a minute."
    except Exception as e:  # network errors, 5xx, bad JSON
        log.warning("%s failed: %s", seat, type(e).__name__)
        reply = f"{SEAT_LABEL[seat]} hit an error ({type(e).__name__}). Try again shortly."
    log.info("%s answered (%d chars), sending", seat, len(reply or ""))
    send(seat, chat_id, reply, reply_to=msg.get("message_id"))
    add_transcript(chat_id, SEAT_LABEL[seat], reply)


# ---------------------------------------------------------------------------
# Grok seat: queue for Grok Bot (no API)
# ---------------------------------------------------------------------------

grok_queue = deque(maxlen=GROK_QUEUE_MAX)
_gq_lock = threading.Lock()
_gq_counter = [0]


def queue_for_grok(msg, text):
    chat_id = msg["chat"]["id"]
    with _gq_lock:
        _gq_counter[0] += 1
        item = {
            "id": _gq_counter[0],
            "ts": int(time.time()),
            "chat_id": chat_id,
            "chat_type": msg["chat"].get("type"),
            "message_id": msg.get("message_id"),
            "text": text,
            "transcript": get_transcript(chat_id),
        }
        grok_queue.append(item)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "grok_queue.jsonl"), "a") as f:
            f.write(json.dumps(item) + "\n")
    except OSError:
        pass
    if GROK_FORWARD_URL:
        try:
            requests.post(GROK_FORWARD_URL, json=item, headers={"X-Trio-Secret": TRIO_SHARED_SECRET}, timeout=10)
        except requests.RequestException as e:
            log.warning("grok forward failed: %s", type(e).__name__)
    send("grok", chat_id, "Grok got it, will answer shortly.", reply_to=msg.get("message_id"))


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def pick_seats(msg, text):
    """Return (seats, cleaned_text) for a group message."""
    m = re.match(r"^\s*(grok|gemini|gpt|chatgpt)\s*[:,]\s*", text, re.I)
    if m:
        return [PREFIXES[m.group(1).lower()]], text[m.end():].strip()
    named = []
    lowered = text.lower()
    for seat in SEATS:
        u = username(seat)
        if u and f"@{u}" in lowered:
            named.append(seat)
            text = re.sub(rf"@{re.escape(u)}\b", "", text, flags=re.I)
    if named:
        return named, text.strip()
    # A reply to one bot's message goes to that bot
    replied = (msg.get("reply_to_message") or {}).get("from") or {}
    for seat in SEATS:
        if replied.get("id") and replied.get("id") == bot_id(seat):
            return [seat], text
    return list(SEATS), text


def is_bot_sender(sender):
    return bool(sender.get("is_bot")) or sender.get("id") in {bot_id(s) for s in SEATS}


def handle_update(seat, update):
    global OWNER_ID, ALLOWED_CHAT_ID
    msg = update.get("message")
    if not msg:
        return
    sender = msg.get("from") or {}
    chat = msg.get("chat") or {}
    chat_id, chat_type = chat.get("id"), chat.get("type")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not sender or is_bot_sender(sender) or chat_id is None:
        return
    uid = sender.get("id")
    log.info("update seat=%s chat_type=%s owner_match=%s has_text=%s", seat, chat_type, uid == OWNER_ID, bool(text))

    # First-run lock: the first private message claims the owner seat.
    if OWNER_ID is None:
        if chat_type != "private":
            return
        with _state_lock:
            if OWNER_ID is None:
                OWNER_ID = uid
                _remember("owner_id", uid)
                log.warning("OWNER LOCKED: set OWNER_ID=%s in Render env vars", uid)
                send(seat, chat_id, f"Locked to you. Your Telegram user id is {uid}. "
                                    "Put it in Render as OWNER_ID so the lock survives restarts.")
                return

    if uid != OWNER_ID:
        return  # silently ignore everyone else

    if chat_type == "private":
        if not seen_messages.add((chat_id, msg.get("message_id"), seat)):
            return
        if text.startswith("/start") or text.startswith("/help"):
            send(seat, chat_id, f"{SEAT_LABEL[seat]} seat ready. Message me here, or talk to all three in your group.")
            return
        if not text:
            return
        add_transcript(chat_id, OWNER_NAME, text)
        if seat == "grok":
            queue_for_grok(msg, text)
        else:
            answer_with(seat, msg, text)
        return

    if chat_type not in ("group", "supergroup"):
        return

    # Group: lock to the first group the owner talks in if none is configured.
    if ALLOWED_CHAT_ID is None:
        with _state_lock:
            if ALLOWED_CHAT_ID is None:
                ALLOWED_CHAT_ID = chat_id
                _remember("group_id", chat_id)
                log.warning("GROUP LOCKED: set ALLOWED_CHAT_ID=%s in Render env vars", chat_id)
                send(seat, chat_id, f"Group locked. Group id is {chat_id}. "
                                    "Put it in Render as ALLOWED_CHAT_ID so the lock survives restarts.")
                seen_messages.add((chat_id, msg.get("message_id")))
                return
    if chat_id != ALLOWED_CHAT_ID:
        return
    # Every bot in the group gets the same message; handle it once.
    if not seen_messages.add((chat_id, msg.get("message_id"))):
        return
    if not text or text.startswith("/"):
        return
    seats, clean = pick_seats(msg, text)
    add_transcript(chat_id, OWNER_NAME, text)
    # Grok first (instant ack), then Gemini, then ChatGPT so each sees the previous reply.
    for s in ("grok", "gemini", "chatgpt"):
        if s not in seats:
            continue
        if s == "grok":
            queue_for_grok(msg, clean)
        else:
            answer_with(s, msg, clean)


def _safe_handle(seat, update):
    try:
        handle_update(seat, update)
    except Exception:
        log.exception("update handling failed")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _eq(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def trio_authorized():
    return _eq(request.headers.get("X-Trio-Secret", ""), TRIO_SHARED_SECRET)


@app.get("/")
def health():
    return jsonify(ok=True, service="trio-relay",
                   seats={s: bool(token(s)) for s in SEATS},
                   owner_locked=OWNER_ID is not None, group_locked=ALLOWED_CHAT_ID is not None)


@app.post("/webhook/<seat>")
def webhook(seat):
    if seat not in SEATS or not token(seat):
        return "unknown bot", 404
    if not _eq(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET):
        return "forbidden", 403
    update = request.get_json(silent=True) or {}
    uid = update.get("update_id")
    if uid is not None and not seen_updates.add((seat, uid)):
        return "ok", 200
    log.info("webhook %s update %s queued", seat, uid)
    threading.Thread(target=_safe_handle, args=(seat, update), daemon=True).start()
    return "ok", 200


@app.get("/grok/pending")
def grok_pending():
    """Messages waiting for Grok. Drains the queue unless ?peek=1."""
    if not trio_authorized():
        return "forbidden", 403
    with _gq_lock:
        items = list(grok_queue)
        if request.args.get("peek") != "1":
            grok_queue.clear()
    return jsonify(pending=items)


@app.post("/reply")
def grok_reply():
    """Post as the Grok bot: {"chat_id": ..., "text": ..., "reply_to_message_id": optional}."""
    if not trio_authorized():
        return "forbidden", 403
    body = request.get_json(silent=True) or {}
    chat_id = _int_or_none(body.get("chat_id"))
    text = (body.get("text") or "").strip()
    if chat_id is None or not text:
        return jsonify(ok=False, error="chat_id and text required"), 400
    if chat_id not in {OWNER_ID, ALLOWED_CHAT_ID} - {None}:
        return jsonify(ok=False, error="chat not allowed"), 403
    send("grok", chat_id, text, reply_to=_int_or_none(body.get("reply_to_message_id")))
    add_transcript(chat_id, "Grok", text)
    return jsonify(ok=True)


def register_webhooks():
    results = {}
    for seat in SEATS:
        if not token(seat):
            results[seat] = {"ok": False, "error": f"{TOKEN_ENV[seat]} not set"}
            continue
        res = tg(seat, "setWebhook", {
            "url": f"{PUBLIC_URL}/webhook/{seat}",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message"],
        })
        info = tg(seat, "getWebhookInfo").get("result", {})
        results[seat] = {"setWebhook": res.get("ok"), "description": res.get("description"),
                         "username": username(seat), "webhook_info": info}
    return results


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if not trio_authorized():
        return "forbidden", 403
    if not PUBLIC_URL or not WEBHOOK_SECRET:
        return jsonify(ok=False, error="PUBLIC_URL (or RENDER_EXTERNAL_URL) and WEBHOOK_SECRET must be set"), 400
    return jsonify(ok=True, public_url=PUBLIC_URL, bots=register_webhooks())


def _auto_setup():
    if env("AUTO_SETUP", "1") == "1" and PUBLIC_URL and WEBHOOK_SECRET:
        try:
            for seat, r in register_webhooks().items():
                log.info("webhook %s: %s", seat, r.get("setWebhook"))
        except Exception:
            log.exception("auto setup failed")


if os.environ.get("TRIO_TESTING") != "1":
    threading.Thread(target=_auto_setup, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "10000")))
