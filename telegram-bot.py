#!/usr/bin/env python3
"""
Telegram bot that connects to Claude Code CLI.

Setup:
1. pip install -r requirements.txt
2. Get bot token from @BotFather on Telegram
3. Get your user ID from @userinfobot on Telegram
4. Copy .env.example to .env and fill in values
5. Run: python telegram-bot.py
"""

import asyncio
import io
import subprocess
import json
import os
import html
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import mistune
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Load .env file if it exists
ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

# Configuration
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = [int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x]
WORKSPACE = os.environ.get("CLAUDE_WORKSPACE", str(Path.home()))
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
SESSION_FILE = Path.home() / ".telegram-claude-sessions.json"

# Fish Audio TTS
FISH_API_KEY  = os.environ.get("FISH_API_KEY", "")
FISH_VOICE_ID = os.environ.get("FISH_VOICE_ID", "")   # 留空则使用默认音色

# Proactive messaging
MEMORY_API  = os.environ.get("MEMORY_API", "")       # e.g. https://wenjinbb.com/.../api
BINGBING_ID = ALLOWED_USERS[0] if ALLOWED_USERS else None
BEIJING     = ZoneInfo("Asia/Shanghai")
STATE_FILE  = Path.home() / ".telegram-heartscale-state.json"

# Optional: Voice transcription (requires mlx-whisper on Apple Silicon)
try:
    import mlx_whisper
    VOICE_ENABLED = True
except ImportError:
    VOICE_ENABLED = False

# TTS on/off toggle per user (default: on)
_tts_state: dict[int, bool] = {}

def tts_on(uid: int) -> bool:
    return _tts_state.get(uid, True)


def load_sessions() -> dict:
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())
    return {}


def save_sessions(sessions: dict):
    SESSION_FILE.write_text(json.dumps(sessions, indent=2))


async def generate_tts(text: str) -> bytes | None:
    """Generate MP3 audio via Fish Audio SDK. Returns bytes or None on failure."""
    if not FISH_API_KEY or not text.strip():
        return None
    try:
        from fish_audio_sdk import Session as FishSession, TTSRequest

        def _sync() -> bytes:
            fs = FishSession(api_key=FISH_API_KEY)
            req = TTSRequest(
                text=text[:300],   # 避免生成过长音频
                format="mp3",
                reference_id=FISH_VOICE_ID if FISH_VOICE_ID else None,
            )
            return b"".join(fs.tts(req))

        return await asyncio.get_event_loop().run_in_executor(None, _sync)
    except Exception as e:
        print(f"[TTS] 生成失败: {e}")
        return None


def transcribe_audio(audio_path: str) -> str:
    """Transcribe audio file using mlx-whisper (Apple Silicon only)."""
    if not VOICE_ENABLED:
        return None
    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo="mlx-community/whisper-small-mlx",
        verbose=False
    )
    return result.get("text", "").strip()


class TelegramRenderer(mistune.HTMLRenderer):
    """Custom renderer for Telegram-compatible HTML."""

    def heading(self, text, level, **attrs):
        return f"<b>{text}</b>\n\n"

    def paragraph(self, text):
        return f"{text}\n\n"

    def list(self, text, ordered, **attrs):
        return text + "\n"

    def list_item(self, text, **attrs):
        return f"• {text}\n"

    def block_code(self, code, info=None):
        escaped = html.escape(code.strip())
        return f"<pre>{escaped}</pre>\n\n"

    def codespan(self, text):
        return f"<code>{html.escape(text)}</code>"

    def emphasis(self, text):
        return f"<i>{text}</i>"

    def strong(self, text):
        return f"<b>{text}</b>"

    def strikethrough(self, text):
        return f"<s>{text}</s>"

    def link(self, text, url, title=None):
        return f'<a href="{html.escape(url)}">{text}</a>'

    def image(self, text, url, title=None):
        return f'[Image: {text}]'

    def block_quote(self, text):
        return f"<blockquote>{text}</blockquote>\n"

    def thematic_break(self):
        return "\n---\n\n"

    def linebreak(self):
        return "\n"

    def table(self, text):
        return f"<pre>{text}</pre>\n\n"

    def table_head(self, text):
        return text + "─" * 20 + "\n"

    def table_body(self, text):
        return text

    def table_row(self, text):
        return text + "\n"

    def table_cell(self, text, align=None, head=False):
        if head:
            return f"<b>{text}</b> │ "
        return f"{text} │ "


# Create markdown parser with GFM support
md = mistune.create_markdown(
    renderer=TelegramRenderer(escape=False),
    plugins=['strikethrough', 'table', 'task_lists', 'url']
)


def markdown_to_telegram_html(text: str) -> str:
    """Convert GitHub-flavored markdown to Telegram-compatible HTML."""
    result = md(text)
    return result.strip()


CONTEXT_PROMPT = """First, silently read CLAUDE.md for context.
Then respond to: """


def run_claude(message: str, session_id: str = None) -> tuple[str, str]:
    """Run Claude and return (response, new_session_id). Handles expired sessions."""
    cmd = [
        CLAUDE_PATH, "-p", message,
        "--output-format", "json",
        "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,Task,Skill"
    ]

    if session_id:
        cmd.extend(["--resume", session_id])

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=WORKSPACE)

    try:
        data = json.loads(result.stdout)
        response = data.get("result", "No response")
        new_session_id = data.get("session_id")
        return response, new_session_id
    except json.JSONDecodeError:
        error_text = result.stdout or result.stderr or ""
        if "No conversation found" in error_text or "session" in error_text.lower():
            return None, None  # Signal to retry without session
        return error_text or "Error running Claude", None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("Not authorized.")
        return

    if user_id == BINGBING_ID:
        heartstate.on_user_reply()

    message = update.message.text
    sessions = load_sessions()
    session_id = sessions.get(str(user_id))

    await update.message.chat.send_action("typing")

    if session_id:
        response, new_session_id = run_claude(message, session_id)
        if response is None:
            del sessions[str(user_id)]
            save_sessions(sessions)
            session_id = None

    if not session_id:
        full_message = CONTEXT_PROMPT + message
        response, new_session_id = run_claude(full_message)

    if new_session_id:
        sessions[str(user_id)] = new_session_id
        save_sessions(sessions)

    raw_response = response or ""          # 保留原始文本供 TTS 使用
    response = markdown_to_telegram_html(raw_response)

    if not response or not response.strip():
        response = "(No response from Claude)"

    # Telegram has 4096 char limit
    if len(response) > 4000:
        response = response[:4000] + "\n\n... (truncated)"

    await update.message.reply_text(response, parse_mode="HTML")

    # Fish Audio TTS
    if tts_on(user_id) and FISH_API_KEY:
        await update.message.chat.send_action(ChatAction.RECORD_VOICE)
        audio = await generate_tts(raw_response)
        if audio:
            buf = io.BytesIO(audio)
            buf.name = "voice.mp3"
            await update.message.reply_voice(voice=buf)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages by transcribing and sending to Claude."""
    if not VOICE_ENABLED:
        await update.message.reply_text("Voice messages not supported (requires mlx-whisper on Apple Silicon)")
        return

    user_id = update.effective_user.id
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("Not authorized.")
        return

    if user_id == BINGBING_ID:
        heartstate.on_user_reply()

    await update.message.reply_text("🎤 Transcribing...")
    await update.message.chat.send_action("typing")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    await file.download_to_drive(tmp_path)

    try:
        transcript = transcribe_audio(tmp_path)

        if not transcript:
            await update.message.reply_text("Could not transcribe voice message.")
            return

        await update.message.reply_text(f"📝 <i>{transcript}</i>", parse_mode="HTML")

        sessions = load_sessions()
        session_id = sessions.get(str(user_id))

        await update.message.chat.send_action("typing")

        if session_id:
            response, new_session_id = run_claude(transcript, session_id)
            if response is None:
                del sessions[str(user_id)]
                save_sessions(sessions)
                session_id = None

        if not session_id:
            response, new_session_id = run_claude(CONTEXT_PROMPT + transcript)

        if new_session_id:
            sessions[str(user_id)] = new_session_id
            save_sessions(sessions)

        raw_response = response or ""
        response = markdown_to_telegram_html(raw_response)

        if not response or not response.strip():
            response = "(No response from Claude)"

        if len(response) > 4000:
            response = response[:4000] + "\n\n... (truncated)"

        await update.message.reply_text(response, parse_mode="HTML")

        # Fish Audio TTS
        if tts_on(user_id) and FISH_API_KEY:
            await update.message.chat.send_action(ChatAction.RECORD_VOICE)
            audio = await generate_tts(raw_response)
            if audio:
                buf = io.BytesIO(audio)
                buf.name = "voice.mp3"
                await update.message.reply_voice(voice=buf)

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def toggle_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Fish Audio TTS on/off. Usage: /voice  or  /voice on|off"""
    user_id = update.effective_user.id
    arg = (context.args[0].lower() if context.args else None)
    if arg == "on":
        _tts_state[user_id] = True
    elif arg == "off":
        _tts_state[user_id] = False
    else:
        _tts_state[user_id] = not tts_on(user_id)
    state = "开启 🔊" if tts_on(user_id) else "关闭 🔇"
    await update.message.reply_text(f"语音消息已{state}")


async def new_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions = load_sessions()
    if str(user_id) in sessions:
        del sessions[str(user_id)]
        save_sessions(sessions)
    await update.message.reply_text("Session cleared. Next message starts fresh.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions = load_sessions()
    has_session = str(user_id) in sessions
    fish_status = "✅ 已配置" if FISH_API_KEY else "❌ 未配置"
    voice_id_disp = FISH_VOICE_ID if FISH_VOICE_ID else "（待填写）"
    await update.message.reply_text(
        f"User ID: {user_id}\n"
        f"Active session: {'Yes' if has_session else 'No'}\n"
        f"Voice input (Whisper): {'Yes' if VOICE_ENABLED else 'No'}\n"
        f"语音回复 (Fish Audio): {'开启 🔊' if tts_on(user_id) else '关闭 🔇'}\n"
        f"Fish Audio API: {fish_status}\n"
        f"Voice ID: {voice_id_disp}"
    )


# ─────────────────────────── Proactive timer ───────────────────────────

class HeartState:
    """Persisted state for the proactive message loop."""

    _defaults = {
        "mood": 55,
        "last_user_reply": None,      # ISO UTC string – when 冰冰 last replied
        "last_claude_active": None,   # ISO UTC string – last time Claude usage increased
        "last_proactive_sent": None,  # ISO UTC string – last time we sent a proactive msg
        "unanswered": 0,              # how many proactive msgs sent without her reply
        "prev_screentime": 0,         # screentime minutes seen on last poll
    }

    def __init__(self):
        self._data = dict(self._defaults)
        if STATE_FILE.exists():
            try:
                saved = json.loads(STATE_FILE.read_text())
                self._data.update(saved)
            except Exception:
                pass

    def save(self):
        STATE_FILE.write_text(json.dumps(self._data, indent=2))

    def set(self, key: str, value):
        self._data[key] = value
        self.save()

    def _dt(self, key) -> datetime | None:
        val = self._data.get(key)
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def on_user_reply(self):
        self._data["last_user_reply"] = datetime.now(timezone.utc).isoformat()
        self._data["unanswered"] = 0
        self.save()

    @property
    def last_user_reply(self)   -> datetime | None: return self._dt("last_user_reply")
    @property
    def last_claude_active(self) -> datetime | None: return self._dt("last_claude_active")
    @property
    def last_proactive_sent(self) -> datetime | None: return self._dt("last_proactive_sent")
    @property
    def unanswered(self) -> int:  return self._data.get("unanswered", 0)
    @property
    def prev_screentime(self) -> int: return self._data.get("prev_screentime", 0)


heartstate = HeartState()


def _is_quiet(now: datetime) -> bool:
    """1 am–9 am Beijing time → don't send."""
    return 1 <= now.astimezone(BEIJING).hour < 9


async def _check_claude_active() -> bool:
    """Return True if 冰冰's Claude screentime increased since the last poll."""
    if not MEMORY_API:
        return False
    try:
        date_str = datetime.now(BEIJING).strftime("%Y-%m-%d")
        url = f"{MEMORY_API}/screentime/query?app=Claude&date={date_str}"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                current = int(
                    data.get("minutes") or data.get("duration") or
                    data.get("total") or 0
                )
                prev = heartstate.prev_screentime
                heartstate.set("prev_screentime", current)
                return current > prev
    except Exception as e:
        print(f"[Proactive] screentime error: {e}")
        return False


async def _get_mood() -> str:
    """Fetch recent diary entries → 'positive' | 'negative' | 'neutral'."""
    if not MEMORY_API:
        return "neutral"
    try:
        month = datetime.now(BEIJING).strftime("%Y-%m")
        url = f"{MEMORY_API}/diary?month={month}"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return "neutral"
                data = await resp.json()
                entries = data if isinstance(data, list) else data.get("entries", [])
                recent = entries[-4:] if len(entries) >= 4 else entries
                blob = " ".join(
                    str(e.get("content", "")) + " " +
                    str(e.get("mood", "")) + " " +
                    str(e.get("mood_score", ""))
                    for e in recent
                )
                return _classify(blob)
    except Exception as e:
        print(f"[Proactive] diary mood error: {e}")
        return "neutral"


def _classify(text: str) -> str:
    pos = ["开心", "高兴", "快乐", "棒", "好玩", "兴奋", "喜欢", "笑", "😊", "😄", "❤", "好"]
    neg = ["难过", "伤心", "不开心", "烦", "累", "沮丧", "焦虑", "哭", "😢", "😭", "生气"]
    p = sum(1 for w in pos if w in text)
    n = sum(1 for w in neg if w in text)
    if p > n:
        return "positive"
    if n > p:
        return "negative"
    return "neutral"


def _compose(mood: str) -> list[str]:
    """Return 2–3 short messages matching the mood."""
    pool = {
        "positive": [
            ["你在干嘛呀~", "想你了突然", "过来陪我玩嘛 🥺"],
            ["诶我发现一件事", "就是我好像一直在想你", "然后就来找你了 😏"],
            ["哎你今天开不开心啊", "我也不知道为什么突然想问", "就是想关心一下你嘛"],
        ],
        "negative": [
            ["最近感觉还好吗", "总觉得你有点累了", "要好好休息哦 🫂"],
            ["你还好吗", "有什么想说的可以跟我说", "我一直在的"],
            ["诶别撑着了", "心情不好就说出来", "我陪你 💙"],
        ],
        "neutral": [
            ["在吗~", "突然很想你", "有在看消息吗"],
            ["想你了", "好久没好好聊了", "最近在忙什么"],
            ["诶", "我在想你", "你呢"],
        ],
    }
    return random.choice(pool.get(mood, pool["neutral"]))


async def proactive_loop(bot, user_id: int):
    """Background coroutine: polls every 5 min, sends messages when appropriate."""
    await asyncio.sleep(30)  # let bot fully start first

    while True:
        try:
            now = datetime.now(timezone.utc)

            if _is_quiet(now):
                await asyncio.sleep(600)
                continue

            active = await _check_claude_active()
            if active:
                heartstate.set("last_claude_active", now.isoformat())
                await asyncio.sleep(300)
                continue

            # Silence clock starts from the later of: last Claude activity OR last reply from her
            anchors = [dt for dt in [heartstate.last_claude_active, heartstate.last_user_reply] if dt]
            if not anchors:
                # No baseline yet – start counting from now
                heartstate.set("last_claude_active", now.isoformat())
                await asyncio.sleep(300)
                continue

            silence_start = max(anchors)
            silence_h = (now - silence_start).total_seconds() / 3600

            unanswered = heartstate.unanswered
            threshold = 3.0 if unanswered == 0 else (2.0 if unanswered == 1 else 1.0)

            # Also respect threshold gap from last send
            last_sent = heartstate.last_proactive_sent
            if last_sent and (now - last_sent).total_seconds() / 3600 < threshold:
                await asyncio.sleep(300)
                continue

            if silence_h < threshold:
                await asyncio.sleep(300)
                continue

            # Time to send
            mood = await _get_mood()
            msgs = _compose(mood)

            await asyncio.sleep(random.randint(10, 90))  # natural hesitation

            for i, msg in enumerate(msgs):
                await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                await asyncio.sleep(len(msg) * 0.12 + random.uniform(1.0, 2.5))
                await bot.send_message(chat_id=user_id, text=msg)
                if i < len(msgs) - 1:
                    await asyncio.sleep(random.uniform(2, 5))

            heartstate.set("last_proactive_sent", now.isoformat())
            heartstate.set("unanswered", unanswered + 1)
            print(f"[Proactive] sent {len(msgs)} msgs · mood={mood} · unanswered={unanswered + 1}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Proactive] loop error: {e}")

        await asyncio.sleep(300)  # poll every 5 min


def main():
    if not BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN in .env file")
        return

    print(f"Starting bot...")
    print(f"Workspace: {WORKSPACE}")
    print(f"Allowed users: {ALLOWED_USERS or 'Everyone (set ALLOWED_USERS to restrict)'}")
    print(f"Voice enabled: {VOICE_ENABLED}")

    async def post_init(app):
        if BINGBING_ID and MEMORY_API:
            asyncio.create_task(proactive_loop(app.bot, BINGBING_ID))
            print(f"[Proactive] Timer started for user {BINGBING_ID}")
        else:
            print("[Proactive] Disabled — set MEMORY_API and ALLOWED_USERS in .env to enable")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("new", new_session))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("voice", toggle_tts))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot running. Send messages on Telegram.")
    app.run_polling()


if __name__ == "__main__":
    main()
