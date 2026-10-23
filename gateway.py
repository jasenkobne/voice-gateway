"""
Voice Gateway for Claude Code Channels.

Bridges iOS Shortcut (and a backup push-to-talk audio path) to the user's
Telegram bot, which is connected to Claude Code via the official Channels
plugin. Uses Telethon to send messages AS the user to their own bot.

Endpoints:
  POST /voice/text   - Primary path: receives transcribed text from Siri
  POST /voice/audio  - Backup path: receives audio, transcribes via Whisper
  GET  /audio/{name} - Serves generated MP3 (short-lived)
  GET  /health       - Liveness probe
"""

import asyncio
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from telethon import TelegramClient, events
from telethon.sessions import StringSession

load_dotenv()

# -- Configuration -----------------------------------------------------------

TG_API_ID = int(os.environ["TELEGRAM_API_ID"])
TG_API_HASH = os.environ["TELEGRAM_API_HASH"]
TG_SESSION = os.environ["TELEGRAM_SESSION_STRING"]
BOT_USERNAME = os.environ["TELEGRAM_BOT_USERNAME"]

ELEVENLABS_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

GATEWAY_TOKEN = os.environ["GATEWAY_AUTH_TOKEN"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
REPLY_TIMEOUT = int(os.environ.get("REPLY_TIMEOUT_SECONDS", "60"))
CAR_REPLY_TIMEOUT = int(os.environ.get("CAR_REPLY_TIMEOUT_SECONDS", "120"))

WHISPER_KEY = os.environ.get("OPENAI_API_KEY")  # optional, only for /voice/audio

VOICE_MARKER = "[CHANNEL:VOICE]"
CAR_MARKER = "[CHANNEL:CAR]"  # Andrija Car (Tesla browser app): text on a car screen
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "/tmp/voice-gateway"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_TTL_SECONDS = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice-gateway")

# -- Telegram client ---------------------------------------------------------

tg = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

# Single-flight lock: one voice query in flight at a time. Solo use, keeps
# reply-to-request correlation trivial.
voice_lock = asyncio.Lock()

# Holds the Future awaiting the next bot reply (or None if no query in flight)
pending_reply: dict[str, asyncio.Future] = {}


# -- Lifespan ----------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg.start()
    log.info("Telegram client started")

    bot_entity = await tg.get_entity(BOT_USERNAME)
    log.info(f"Bot resolved: @{BOT_USERNAME} (id={bot_entity.id})")
    app.state.bot_entity = bot_entity

    @tg.on(events.NewMessage(from_users=bot_entity.id))
    async def bot_reply_handler(event):
        text = event.message.message
        log.info(f"Bot reply received: {text[:120]!r}")
        if "current" in pending_reply:
            future = pending_reply.pop("current")
            if not future.done():
                future.set_result(text)

    asyncio.create_task(cleanup_old_audio())

    yield

    await tg.disconnect()
    log.info("Telegram client disconnected")


async def cleanup_old_audio():
    while True:
        try:
            now = time.time()
            for f in AUDIO_DIR.glob("*.mp3"):
                if now - f.stat().st_mtime > AUDIO_TTL_SECONDS:
                    f.unlink(missing_ok=True)
        except Exception as e:
            log.warning(f"cleanup_old_audio error: {e}")
        await asyncio.sleep(60)


app = FastAPI(lifespan=lifespan, title="Voice Gateway")


# -- Helpers -----------------------------------------------------------------

def require_auth(authorization: Optional[str]) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    if not secrets.compare_digest(authorization[7:], GATEWAY_TOKEN):
        raise HTTPException(401, "Invalid token")


def split_voice_reply(reply: str) -> str:
    """If the reply contains a `---` separator on its own line, return only
    the part before it (the spoken portion). Everything after is left for
    the user to read in Telegram.
    """
    lines = reply.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[:i]).strip()
    return reply.strip()


async def send_to_bot_and_wait(text: str, marker: str = VOICE_MARKER,
                               timeout: int = REPLY_TIMEOUT) -> str:
    """Send a marked message to the user's Claude Code bot and wait for the
    reply. Serialised via voice_lock for clean correlation.
    """
    async with voice_lock:
        future = asyncio.get_event_loop().create_future()
        pending_reply["current"] = future

        payload = f"{marker} {text}"
        await tg.send_message(app.state.bot_entity, payload)
        log.info(f"Sent to bot: {payload[:120]!r}")

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            pending_reply.pop("current", None)
            raise HTTPException(504, "Bot did not reply within timeout")


async def synthesize_speech(text: str, request_id: str) -> Path:
    """Stream TTS from ElevenLabs and save to local MP3."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVENLABS_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    body = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    out_path = AUDIO_DIR / f"{request_id}.mp3"

    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode(errors="replace")[:300]
                raise HTTPException(502, f"ElevenLabs error {resp.status_code}: {detail}")
            with out_path.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)

    log.info(f"TTS saved: {out_path} ({out_path.stat().st_size} bytes)")
    return out_path


async def transcribe_audio(audio_bytes: bytes, filename: str) -> str:
    """OpenAI Whisper API. Used by the backup /voice/audio endpoint only."""
    if not WHISPER_KEY:
        raise HTTPException(501, "Whisper not configured (OPENAI_API_KEY missing)")

    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {WHISPER_KEY}"}

    async with httpx.AsyncClient(timeout=60) as client:
        files = {"file": (filename, audio_bytes, "audio/mpeg")}
        data = {"model": "whisper-1"}
        resp = await client.post(url, headers=headers, files=files, data=data)
        if resp.status_code != 200:
            raise HTTPException(502, f"Whisper error {resp.status_code}: {resp.text[:200]}")
        return resp.json()["text"]


# -- Endpoints ---------------------------------------------------------------

@app.post("/voice/text")
async def voice_text(
    payload: dict,
    authorization: Optional[str] = Header(None),
):
    """Primary path. iOS Shortcut sends pre-transcribed text from Siri."""
    require_auth(authorization)

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Empty text")

    request_id = payload.get("request_id") or str(uuid.uuid4())
    log.info(f"[{request_id}] voice/text query: {text[:120]!r}")

    reply = await send_to_bot_and_wait(text)
    speak_text = split_voice_reply(reply)
    if not speak_text:
        speak_text = "Reply sent to Telegram, check there."

    audio_path = await synthesize_speech(speak_text, request_id)
    audio_url = f"{PUBLIC_BASE_URL}/audio/{audio_path.name}"

    return {
        "request_id": request_id,
        "audio_url": audio_url,
        "spoken_text": speak_text,
        "full_reply_in_telegram": True,
    }


@app.post("/voice/audio")
async def voice_audio(
    audio: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Backup path. Push-to-talk app uploads audio; we transcribe via Whisper."""
    require_auth(authorization)

    audio_bytes = await audio.read()
    text = await transcribe_audio(audio_bytes, audio.filename or "audio.m4a")
    request_id = str(uuid.uuid4())
    log.info(f"[{request_id}] voice/audio transcribed: {text[:120]!r}")

    reply = await send_to_bot_and_wait(text)
    speak_text = split_voice_reply(reply)
    if not speak_text:
        speak_text = "Reply sent to Telegram, check there."

    audio_path = await synthesize_speech(speak_text, request_id)
    audio_url = f"{PUBLIC_BASE_URL}/audio/{audio_path.name}"

    return {
        "request_id": request_id,
        "transcribed_query": text,
        "audio_url": audio_url,
        "spoken_text": speak_text,
    }


@app.post("/car/text")
async def car_text(
    payload: dict,
    authorization: Optional[str] = Header(None),
):
    """Andrija Car: text in, text out (no TTS — the car app asks /car/tts
    separately only when the driver taps the speaker button)."""
    require_auth(authorization)

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Empty text")
    log.info(f"car/text query: {text[:120]!r}")

    reply = await send_to_bot_and_wait(text, marker=CAR_MARKER, timeout=CAR_REPLY_TIMEOUT)
    return {"reply": reply.strip()}


@app.post("/car/tts")
async def car_tts(
    payload: dict,
    authorization: Optional[str] = Header(None),
):
    """Speak a car reply on demand (same voice as the Shortcuts)."""
    require_auth(authorization)

    text = (payload.get("text") or "").strip()[:1500]
    if not text:
        raise HTTPException(400, "Empty text")
    request_id = str(uuid.uuid4())
    audio_path = await synthesize_speech(text, request_id)
    return {"audio_url": f"{PUBLIC_BASE_URL}/audio/{audio_path.name}"}


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    if not filename.endswith(".mp3") or "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = AUDIO_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Audio expired or not found")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/health")
async def health():
    return {"ok": True, "telegram_connected": tg.is_connected()}
