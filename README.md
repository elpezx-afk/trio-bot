# Trio relay

One free Flask app that lets Michael talk to three AI assistants in Telegram, alone or all together:

| Seat    | Telegram bot     | Brain                                                      |
|---------|------------------|------------------------------------------------------------|
| Grok    | `TG_TOKEN_GROK`  | Grok Bot (no API): messages are queued and answered through `/reply` |
| Gemini  | `TG_TOKEN_GEMINI`| Google AI Studio, free tier (`gemini-3.8-flash` by default) |
| ChatGPT | `TG_TOKEN_CHATGPT`| Groq free tier, OpenAI's open model `openai/gpt-oss-120b`  |

In the private group, a message from Michael goes to **all three** seats. To pick one, start with
`grok:`, `gemini:` or `gpt:`, mention a bot by @username, or reply to that bot's message.
Gemini answers first, then ChatGPT, and each sees the last 20 messages (including the other
seats' replies), so they build on each other. In a DM, only that bot answers.

Only Michael and his one group are served. Everyone else is ignored without a reply. Messages
from the bots themselves are ignored too.

**No tokens or keys go in this repo.** They live only in Render's environment variables.

## Environment variables

| Name | Required | What it is |
|------|----------|------------|
| `TG_TOKEN_GROK` | yes | BotFather token for the Grok bot |
| `TG_TOKEN_GEMINI` | yes | BotFather token for the Gemini bot |
| `TG_TOKEN_CHATGPT` | yes | BotFather token for the ChatGPT bot |
| `WEBHOOK_SECRET` | yes | Random string (letters, digits, `_`, `-`; up to 256 chars). Telegram sends it back on every webhook call |
| `TRIO_SHARED_SECRET` | yes | Random string for `/setup`, `/grok/pending` and `/reply` (header `X-Trio-Secret`) |
| `GEMINI_API_KEY` | yes | Free key from https://aistudio.google.com/apikey |
| `GROQ_API_KEY` | yes | Free key from https://console.groq.com/keys |
| `OWNER_ID` | after first message | Michael's Telegram user id (the bot tells you it) |
| `ALLOWED_CHAT_ID` | after first group message | The private group's id (the bot tells you it; starts with `-100`) |
| `PUBLIC_URL` | no | Public base URL. Defaults to Render's `RENDER_EXTERNAL_URL`, so it's normally not needed |
| `GROK_FORWARD_URL` | no | If set, each Grok-bound message is also POSTed here as JSON (with `X-Trio-Secret`) |
| `GEMINI_MODEL` | no | Override the Gemini model (default `gemini-3.8-flash`; falls back to `gemini-flash-latest` if it's not found) |
| `GROQ_MODEL` | no | Override the Groq model (default `openai/gpt-oss-120b`) |
| `OWNER_NAME` | no | Name the AIs use for the owner (default `Michael`) |
| `AUTO_SETUP` | no | `1` (default) registers the three webhooks on every start; `0` turns that off |
| `DATA_DIR` | no | Where the small state file and Grok queue log go (default `/tmp/trio`) |

### How the lock works

If `OWNER_ID` is empty, the **first private message** to any of the three bots claims the owner
seat. The bot answers `Locked to you.` with the user id, logs `OWNER LOCKED: set OWNER_ID=...`, and
saves it to `DATA_DIR/state.json`. The first group Michael writes in is locked the same way
(`Group locked.`, `ALLOWED_CHAT_ID`). Render's free disk is wiped on every restart or deploy, so
**copy both ids into the env vars right away**. After that the file isn't needed, and nobody else
can claim the bots, even after a restart.

## 1. BotFather (in Telegram, chat with @BotFather)

1. Send `/newbot` three times. Name them, for example, Trio Grok, Trio Gemini and Trio ChatGPT.
   Usernames must end in `bot`. Each time, BotFather gives a token. Keep it private and paste it
   only into Render.
2. For each bot, send `/setprivacy`, pick the bot, choose **Disable**. Otherwise bots in a group only
   see messages that @mention them. Do this **before** adding them to the group. A bot already in
   the group has to be removed and re-added for the change to take effect.
3. In Telegram, make a new group with just you and the three bots.
4. After all three are in the group, send `/setjoingroups` for each bot and choose **Disable**, so
   nobody can add them anywhere else.

## 2. Render (free web service)

1. https://dashboard.render.com → **New** → **Web Service** → connect GitHub repo `elpezx-afk/trio-bot`, branch `main`.
2. Runtime **Python**, Instance type **Free**.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn bot:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT`
   (exactly **one** worker: the dedupe set, transcripts and Grok queue live in memory.)
5. **Environment** → add the variables from the table above (leave `OWNER_ID`/`ALLOWED_CHAT_ID` empty for now).
6. Deploy. On start the app registers all three webhooks itself, using Render's URL.
   (`render.yaml` in this repo describes the same setup if you prefer a Blueprint.)

## 3. First run

1. Open the Grok, Gemini or ChatGPT bot in Telegram and send `hi`. It answers `Locked to you.` and your id.
   Put that number in Render as `OWNER_ID`.
2. Write anything in the group. A bot answers `Group locked.` and the group id. Put it in Render as `ALLOWED_CHAT_ID`.
3. Save the env vars (Render redeploys). From now on, only you, in DMs and that group, get answers.

The free instance sleeps after 15 minutes idle. The first message after that can take about a
minute while it wakes up. Telegram retries in the meantime, and duplicates are dropped.

## Grok seat API

Both endpoints need the header `X-Trio-Secret: <TRIO_SHARED_SECRET>`.

* `GET /grok/pending` returns `{"pending": [...]}` and clears the queue (`?peek=1` to look without clearing).
  Each item has `id, ts, chat_id, chat_type, message_id, text, transcript`.
* `POST /reply` with JSON `{"chat_id": ..., "text": "...", "reply_to_message_id": optional}` posts
  as the Grok bot. It only allows the owner's DM or the locked group.

The queue is in memory, so a restart or sleep empties it. Set `GROK_FORWARD_URL` if messages must never be missed.

## Admin

* `GET /` is a health check that shows which seats have tokens and whether the owner and group are locked.
  It never shows secrets.
* `POST /setup` (header `X-Trio-Secret`) re-runs `setWebhook` for all three bots with `WEBHOOK_SECRET`
  and returns each bot's `getWebhookInfo`. Example:
  `curl -X POST -H "X-Trio-Secret: $TRIO_SHARED_SECRET" https://<your-service>.onrender.com/setup`

## Testing

Offline tests use fake tokens and mock all outbound HTTP. They check the secret checks, the owner
and group lock, the stranger and bot filters, dedupe, routing, the transcript, rate-limit handling,
the Grok queue, `/reply` and `/setup`:

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Live check after deploy: open `https://<service>.onrender.com/` (all seats should be `true`), then
DM each bot and write once in the group.

## Free-tier notes

* Gemini free tier: per-minute and daily quotas per project. Google may use free-tier prompts to improve its products.
* Groq free tier: about 1,000 requests a day for gpt-oss-120b, no card needed.
* When a seat gets rate-limited, it says so in the chat instead of failing silently.
