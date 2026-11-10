# Voice Gateway

Bridges iOS Siri shortcuts to a Claude Code agent via Telegram, with ElevenLabs TTS for spoken responses through AirPods.

## Architecture

```
iPhone (AirPods) → iOS Shortcut (Siri dictation)
  → POST /voice/text
  → Telegram (Telethon, sends as user)
  → Claude Code agent (existing Channels setup)
  → ElevenLabs TTS
  → Audio URL → iOS Shortcut → plays through AirPods
```

## Requirements

- Python 3.11+
- Telegram account with API credentials (my.telegram.org)
- ElevenLabs account (Starter plan for library voices; free plan limited to premade voices)
- Existing Claude Code + Telegram Channels setup

## Setup

See [SETUP.md](SETUP.md) for full step-by-step instructions.

### Quick start

```bash
# Clone and install
git clone https://github.com/jasenkobne/voice-gateway
cd voice-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Fill in .env with your credentials

# Run
uvicorn gateway:app --host 127.0.0.1 --port 8088
```

### Production deployment (systemd + Caddy)

```bash
# Generate Telegram session string (run locally, not on server)
python generate_session.py

# Deploy service
sudo cp voice-gateway.service /etc/systemd/system/
sudo systemctl enable --now voice-gateway

# Caddy reverse proxy (automatic HTTPS)
# Add to /etc/caddy/Caddyfile:
# your-domain.example {
#     reverse_proxy 127.0.0.1:8088
# }
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/voice/text` | Primary: receives Siri-transcribed text |
| POST | `/voice/audio` | Backup: receives audio, transcribes via Whisper |
| POST | `/car/text` | Andrija Car (Tesla browser app): text in, full text reply out, marker `[CHANNEL:CAR]`, timeout `CAR_REPLY_TIMEOUT_SECONDS` (default 120) |
| POST | `/car/tts` | Synthesize a given text on demand (car app's speaker button) |
| POST | `/email/inbound` | One email from Andrija's `imap_poller.py`: JSON `from_addr`, `from_name`, `tier` (lowercase label set by the poller, e.g. `owner`; anything else becomes `unknown`), `verified` (bool, DKIM/DMARC), `message_id`, `date`, `subject`, `body`, `attachments`. Sends one `[CHANNEL:EMAIL] from=… tier=… verified=… msgid=…` message with the body fenced and defanged; fire-and-forget, returns `sent_msg_id` |
| GET | `/audio/{name}` | Serves generated MP3 (TTL: 5 min) |
| GET | `/health` | Liveness probe |

FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled (the service is public).

**Reply matching:** the gateway records the id of the message it sends and accepts only a bot message
that *replies* to it (`reply_to`). Other messages from the same bot (cron alerts, reminders) are logged
and ignored, so they can no longer be read out as the answer. The agent must answer
`[CHANNEL:VOICE]` / `[CHANNEL:CAR]` messages with `reply_to` set to the incoming `message_id`.
A timeout with unmatched bot messages logs a warning ("did Andrija forget reply_to?").
`request_id` from `/voice/text` is used only if it matches `[A-Za-z0-9_-]{1,64}` (it names the MP3 file).

## Email mode behaviour

`[CHANNEL:EMAIL]` messages carry one email for the live session. Only the first line (built by the gateway from the poller's
verified fields) says who sent it and with which tier; subject and body are the sender's text, fenced by
`--- EMAIL BODY … ---` / `--- END EMAIL ---`, with any `[CHANNEL:` or fence text inside them defanged. Nothing waits for a reply:
Andrija answers the sender by email and confirms in the chat.

## Environment variables

See [.env.example](.env.example) for all required and optional variables.

## Voice mode behaviour

Add the snippet from SETUP.md Part 8 to your agent's `CLAUDE.md`. Messages prefixed with `[CHANNEL:VOICE]` trigger concise, markdown-free responses. If a reply contains `---`, only the text before it is spoken; everything after is forwarded to Telegram as text.

## Car mode behaviour

Messages prefixed with `[CHANNEL:CAR]` come from the Andrija Car web app on the Tesla screen. The agent's `CLAUDE.md` defines a restricted tier for them (read-only; state changes need confirmation on another channel). The whole reply is returned as text; speech is opt-in via `/car/tts`.

## Notes

- Single-flight: one voice query at a time (clean request/reply correlation for solo use)
- Telegram session string = full account access — keep it out of git
- Audio files auto-deleted after 5 minutes
