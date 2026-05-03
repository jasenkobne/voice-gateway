# Voice Gateway — Setup Guide

End-to-end setup for the voice gateway that bridges Siri (or a push-to-talk
app) to your existing Claude Code agent via Telegram Channels.

## Architecture Recap

```
iPhone (AirPods)  "Hey Siri, ask Claude what's on my calendar"
   │
   ▼  iOS Shortcut: dictate → POST {text, request_id}
Linux VPS gateway (this code)
   │
   ▼  Telethon, sends to your bot AS YOU
Your Telegram Claude Code bot (existing)
   │
   ▼  Channels plugin forwards to your Claude Code session
Claude Code (your existing agent, on your machine)
   │
   ▼  Reply text via Telegram bot
Gateway listens, parses `---` split if present
   │
   ▼  ElevenLabs streaming TTS
Gateway returns audio URL
   │
   ▼  iOS Shortcut downloads MP3
Plays through AirPods
```

## Part 1: Pre-Reqs

Before starting, you need:

- A Linux VPS (any 1GB+ droplet works: Hetzner CX22, DigitalOcean basic, etc.)
- A domain or subdomain pointing to your VPS, with HTTPS (iOS requires it)
- Your existing Claude Code + Telegram Channels setup already working
- An ElevenLabs account (Starter plan, ~$5/mo)
- Python 3.11+

## Part 2: Get Telegram API Credentials

1. Go to https://my.telegram.org/apps with the phone number you use for Telegram.
2. Create a new application. Any name and short name work. Platform: Other.
3. Copy `App api_id` and `App api_hash`.
4. Save these. You won't be able to see the hash again easily.

## Part 3: Generate a Telethon Session String

Do this on your **local machine**, not the VPS, because it's interactive.

```bash
git clone <your-fork-or-files-here>  # or copy the files manually
cd voice-gateway
python -m venv .venv
source .venv/bin/activate
pip install telethon python-dotenv
python generate_session.py
```

Enter your `api_id` and `api_hash`. Telethon will prompt for your phone
number and the verification code Telegram sends you. After login, it
prints a long session string. Copy it.

This session string is essentially your Telegram login. **Treat it like a
password.** Anyone with it can read and send messages from your account.

## Part 4: Deploy to VPS

SSH to your VPS as a user with sudo.

```bash
# Create dedicated user
sudo useradd -r -s /bin/bash -m -d /opt/voice-gateway voice-gateway
sudo mkdir -p /opt/voice-gateway
sudo chown voice-gateway:voice-gateway /opt/voice-gateway

# Copy files (scp from local, or git clone)
sudo -u voice-gateway -H bash -c '
  cd /opt/voice-gateway
  python3 -m venv .venv
  ./.venv/bin/pip install -r requirements.txt
'

# Create .env from template
sudo cp .env.example /opt/voice-gateway/.env
sudo chown voice-gateway:voice-gateway /opt/voice-gateway/.env
sudo chmod 600 /opt/voice-gateway/.env
sudo nano /opt/voice-gateway/.env  # fill in all values

# Install systemd service
sudo cp voice-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-gateway

# Check it's running
sudo systemctl status voice-gateway
sudo journalctl -u voice-gateway -f
```

The service binds to `127.0.0.1:8088`. We expose it via Caddy next.

## Part 5: HTTPS Reverse Proxy with Caddy

iOS will refuse plain HTTP. Caddy gets you free Let's Encrypt automatically.

```bash
sudo apt install -y caddy
```

Edit `/etc/caddy/Caddyfile`:

```
gateway.your-domain.example {
    reverse_proxy 127.0.0.1:8088
    request_body {
        max_size 25MB
    }
}
```

```bash
sudo systemctl reload caddy
```

Verify: `curl https://gateway.your-domain.example/health` should return JSON.

## Part 6: Configure ElevenLabs

1. Sign up at https://elevenlabs.io. Starter plan is fine.
2. Go to Voice Library, pick a voice that supports the **Multilingual v2**
   model. Sarah, Charlie, and several pre-made voices work for both
   English and Croatian. Test a Croatian phrase in their preview before
   committing.
3. Click the voice, copy the Voice ID.
4. Generate an API key in your profile.
5. Paste both into `.env`.

## Part 7: Build the iOS Shortcut

On your iPhone, open the **Shortcuts** app and create a new Shortcut named
something like "Ask Claude". Add these actions in order:

1. **Dictate Text**
   - Language: pick the language you'll mostly speak, or set to multilingual.
   - Stop Listening: After Long Pause.

2. **Get Contents of URL**
   - URL: `https://gateway.your-domain.example/voice/text`
   - Method: POST
   - Headers:
     - `Authorization` = `Bearer YOUR_GATEWAY_AUTH_TOKEN`
     - `Content-Type` = `application/json`
   - Request Body: JSON
     - `text` = (Magic Variable: Dictated Text)

3. **Get Dictionary Value**
   - Get: Value
   - Key: `audio_url`
   - From: (Magic Variable: Contents of URL)

4. **Get Contents of URL**
   - URL: (Magic Variable: Dictionary Value, the audio_url)
   - Method: GET

5. **Play Sound** (or **Encode → Base64 Decode → Play**, depending on iOS version)
   - Sound: (Magic Variable: Contents of URL from step 4)

Save. Then:

- **Add to Siri**: Open the Shortcut info, set a phrase like "ask Claude".
- **Add to Action Button** (iPhone 15 Pro+): Settings → Action Button →
  Shortcut → Ask Claude.
- **Apple Watch**: appears automatically in the Shortcuts complication.

Test by saying "Hey Siri, ask Claude what time is it in Zagreb". You
should hear ElevenLabs read Claude's reply through your AirPods.

## Part 8: Append Voice Snippet to Your Agent

Add this to the bottom of your existing `CLAUDE.md` (or wherever your
agent's system prompt lives). Don't edit anything above it.

```markdown
## Voice Channel Behaviour (additive)

Some incoming messages begin with the marker `[CHANNEL:VOICE]`. When that
marker is present, the user is listening through AirPods and sees nothing.

In voice mode, override default formatting:
- No markdown, no code fences, no bullets, no URLs read aloud.
- 1-3 sentences unless asked to elaborate.
- Numbers and times spoken naturally: "ten a.m.", "twenty euros".
- If the answer genuinely needs code, a diff, a long table, or a URL,
  respond with exactly:

      Sending it to Telegram, check there.
      ---
      [full visual answer here]

  The gateway speaks line one and forwards everything after `---` to text.

When the marker is absent, behave normally (full markdown, full length,
existing personality unchanged).
```

Restart your Claude Code session for the change to take effect.

## Part 9: Test Checklist

- [ ] `curl https://gateway.your-domain.example/health` → returns
      `{"ok": true, "telegram_connected": true}`
- [ ] Type a message manually in your Telegram bot chat. Claude replies as
      usual. Confirms Channels still works.
- [ ] Run the iOS Shortcut from the Shortcuts app directly. Audio plays.
- [ ] "Hey Siri, ask Claude what's the time" via AirPods. Audio plays.
- [ ] Ask a coding question via voice. Claude should respond with the
      "Sending it to Telegram, check there" line, and the full code
      should appear in your Telegram chat.
- [ ] Switch to Croatian: "Hej Siri, pitaj Claudea koliko je sati".
      Reply should come back in Croatian.

## Part 10: Optional - Push-to-Talk Backup App

For when Siri isn't available (no signal, AirPods dead, etc.), the
`/voice/audio` endpoint accepts raw audio. Any HTTP client that can
record and POST a file works. Easiest options:

- **Shortcut variant**: same as above but uses "Record Audio" action
  instead of Dictate Text, POSTs the audio file to `/voice/audio`.
- **Browser PWA**: tiny HTML page with a record button and fetch() POST.

Set `OPENAI_API_KEY` in `.env` and restart the gateway to enable this path.

## Troubleshooting

**"Bot did not reply within timeout" (504)**
- Your Claude Code session isn't running, or it crashed. Check the
  terminal where you launched `claude --channels plugin:telegram@...`.
- The Telegram plugin lost connection. Restart Claude Code.

**ElevenLabs 401 / 403**
- API key wrong or quota exhausted. Check ElevenLabs dashboard.

**iOS Shortcut hangs forever**
- The audio URL might be returning 404. Check gateway logs:
  `sudo journalctl -u voice-gateway -f`. Most common cause: Audio file
  was cleaned up before iOS fetched it. Increase `AUDIO_TTL_SECONDS` if
  you have slow connections.

**Claude doesn't honour the voice marker**
- Reload your agent. Verify the snippet is in CLAUDE.md and that the
  marker `[CHANNEL:VOICE]` matches exactly (square brackets, capitalised).

**Telethon "PhoneNumberInvalid" or 2FA prompts**
- Re-run `generate_session.py` on your local machine. Don't try to do
  Telethon login on the headless VPS.

## Operational Notes

- **Costs**: ElevenLabs Starter $5/mo (30k chars), VPS $5-10/mo,
  ElevenLabs overage if you exceed your character budget. Whisper is
  ~$0.006/min if you enable the audio backup path.
- **Privacy**: Siri does STT on-device. Your dictated text goes to your
  own VPS. ElevenLabs sees only Claude's text reply, not your queries.
  Anthropic sees both via your normal Pro flow.
- **Security**: The session string in `.env` is sensitive. The systemd
  unit restricts file permissions. Don't commit `.env` to git.
- **Single-flight**: The gateway processes one voice query at a time
  (cleanest correlation between requests and replies). Solo use, this is
  fine. If you ever need concurrency, add a request-tag echo convention
  in the system prompt.
