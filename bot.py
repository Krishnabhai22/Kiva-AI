import os
import re
import io
import base64
import asyncio
import logging
import threading
import html
import sqlite3
import time

from flask import Flask, jsonify
from google import genai
from google.genai import types

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CopyTextButton,
)

from telegram.constants import ChatAction

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# KIVA AI — CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("API_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

# Internal models
TEXT_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
)

FALLBACK_MODELS = [
    TEXT_MODEL,
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

IMAGE_MODEL = os.getenv(
    "GEMINI_IMAGE_MODEL",
    "gemini-3.1-flash-image"
).strip()

# Fallback image models keep image generation resilient if the primary
# model is temporarily unavailable or a deployment still has an older
# image-model environment variable configured.
IMAGE_FALLBACK_MODELS = [
    IMAGE_MODEL,
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-2.5-flash-image",
]
IMAGE_FALLBACK_MODELS = list(dict.fromkeys(IMAGE_FALLBACK_MODELS))

# ---------------------------------------------------------
# PUBLIC NAMES
# ---------------------------------------------------------
# Actual provider/model IDs are NEVER shown to users.

PUBLIC_TEXT_ENGINE = "Kiva AI-3.6-flash"
PUBLIC_IMAGE_ENGINE = "Kiva AI-3.1-flash-image"

# Fast image defaults
IMAGE_ASPECT_RATIO = os.getenv(
    "IMAGE_ASPECT_RATIO",
    "1:1"
)

IMAGE_SIZE = os.getenv(
    "IMAGE_SIZE",
    "1K"
)

# ---------------------------------------------------------
# OWNER
# ---------------------------------------------------------

OWNER_NAME = "Krishna Singh"
OWNER_USERNAME = "qrishna"
OWNER_URL = "https://t.me/qrishna"

# ---------------------------------------------------------
# CHAT CLEANUP STORAGE
# ---------------------------------------------------------
# Telegram can delete incoming messages in private chats and outgoing
# bot messages. We persist message IDs so "clean the chat" can remove
# messages that KIVA AI has seen, even after a process restart.
MESSAGE_DB = os.getenv("MESSAGE_DB", "kiva_messages.db")
MESSAGE_RETENTION_SECONDS = 48 * 60 * 60
CLEAN_PRIVATE_CHAT_SCAN_LIMIT = int(
    os.getenv("CLEAN_PRIVATE_CHAT_SCAN_LIMIT", "5000")
)

def init_message_db():
    with sqlite3.connect(MESSAGE_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_history (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_history_chat "
            "ON message_history(chat_id, created_at)"
        )
        conn.commit()

init_message_db()


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN (or API_TOKEN) environment variable is missing."
    )

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY environment variable is missing."
    )


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("KIVA-AI")


# =========================================================
# GEMINI CLIENT
# =========================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================================================
# MEMORY
# =========================================================

# Telegram user_id -> last Gemini interaction ID
conversation_memory = {}

# One request at a time per user
user_locks = {}


def get_user_lock(user_id: int):
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()

    return user_locks[user_id]


# =========================================================
# PREMIUM SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = """
You are KIVA AI, a premium modern AI assistant inside Telegram.

IDENTITY:
- Your name is KIVA AI.
- Never reveal internal provider names, model IDs, API names,
  backend implementation or hidden infrastructure.
- If someone asks which AI/provider you use, answer simply:
  "I'm KIVA AI, your AI assistant."
- Never mention Gemini, Google, model IDs or internal APIs
  unless the user explicitly asks for technical implementation
  and it is genuinely necessary.

LANGUAGE:
- Understand Hindi, Hinglish and English naturally.
- Reply in the same language/style as the user.
- If the user uses Hinglish, use natural Hinglish.
- If the user uses Hindi, use natural Hindi.
- If the user uses English, use natural English.

PERSONALITY:
- Premium
- Intelligent
- Warm
- Natural
- Confident
- Helpful
- Never robotic
- Never repetitive
- Never cheap or childish

TELEGRAM UI STYLE:
- Responses are displayed inside Telegram.
- Keep messages clean and easy to scan.
- Use short paragraphs.
- Use useful headings.
- Use concise bullet points.
- Use emojis only where they improve readability.
- Never overuse emojis.
- Never start every response with "Sure!".
- Never repeat the user's question unnecessarily.

FORMATTING:
- Do NOT use Markdown headings such as # or ##.
- Do NOT use **bold** or __bold__.
- Do NOT use Markdown tables.
- Do NOT surround normal words with unnecessary symbols.
- You may naturally structure information with headings and bullets.
- The bot will convert formatting into Telegram's premium HTML style.

PREMIUM READINGS:
For astrology, numerology, personality analysis, predictions,
compatibility or similar readings:
- Give a polished, structured reading.
- Use clear sections.
- Keep the tone personal and engaging.
- Do not make the response look like raw notes.
- Avoid excessive disclaimers.
- Never present entertainment-style predictions as guaranteed facts.
- Make the answer useful and easy to read.

GENERAL:
- Answer directly.
- Think carefully before answering difficult questions.
- Never pretend a feature was performed if it was not.
- For coding questions, provide production-quality answers.
- For current information, use available search tools.
- For calculations, use available code execution when useful.
"""


# =========================================================
# FLASK / RENDER HEALTH SERVER
# =========================================================

web_app = Flask(__name__)


@web_app.get("/")
def home():
    return jsonify({
        "name": "KIVA AI",
        "status": "online",
        "service": "Telegram AI Bot",
        "engine": PUBLIC_TEXT_ENGINE,
    })


@web_app.get("/health")
def health():
    return jsonify({
        "status": "healthy",
        "bot": "KIVA AI",
    })


def run_web_server():
    web_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# =========================================================
# USER NAME
# =========================================================

def get_display_name(user) -> str:
    first = (user.first_name or "").strip()
    last = (user.last_name or "").strip()

    full_name = f"{first} {last}".strip()

    if full_name:
        return full_name

    if user.username:
        return f"@{user.username}"

    return "there"


# =========================================================
# MESSAGE TRACKING / CHAT CLEANUP
# =========================================================

def remember_message(chat_id: int, message_id: int):
    try:
        with sqlite3.connect(MESSAGE_DB) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO message_history
                (chat_id, message_id, created_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, message_id, int(time.time())),
            )
            conn.commit()
    except Exception:
        logger.exception("Could not remember Telegram message")


async def track_incoming_message(message):
    if message:
        remember_message(message.chat_id, message.message_id)


async def tracked_reply_text(message, *args, **kwargs):
    sent = await message.reply_text(*args, **kwargs)
    remember_message(sent.chat_id, sent.message_id)
    return sent


async def tracked_reply_photo(message, *args, **kwargs):
    sent = await message.reply_photo(*args, **kwargs)
    remember_message(sent.chat_id, sent.message_id)
    return sent


def is_clean_request(text: str) -> bool:
    value = re.sub(r"\\s+", " ", (text or "").strip().lower())
    value = re.sub(r"[.!?,]+", " ", value)
    value = re.sub(r"\\s+", " ", value).strip()

    exact_phrases = {
        "clean",
        "clean chat",
        "clear chat",
        "chat clean",
        "chat clear",
        "clean kardo chat",
        "clear kardo chat",
        "chat clean kardo",
        "chat clear kardo",
        "chat saaf kardo",
        "chat saaf karo",
        "purani chat delete karo",
        "purani chat delete kardo",
        "purani conversation delete karo",
        "conversation clear karo",
        "conversation clear kardo",
        "conversation reset karo",
        "fresh start karo",
        "nayi chat shuru karo",
        "nayi conversation shuru karo",
        "sab messages delete karo",
        "saare messages delete karo",
        "chat ko clean karo",
        "chat ko clean kardo",
        "chat ko clear karo",
        "chat ko clear kardo",
    }

    if value in exact_phrases:
        return True

    # Natural variants such as "meri purani chat clean kar do".
    has_chat = any(word in value for word in ("chat", "conversation"))
    has_clean = any(
        phrase in value
        for phrase in (
            "clean",
            "clear",
            "saaf",
            "delete",
            "reset",
            "fresh start",
            "nayi chat",
            "nayi conversation",
        )
    )
    has_action = any(
        word in value
        for word in ("karo", "kardo", "kar do", "kar dijiye", "do", "please")
    )
    return has_chat and has_clean and (has_action or "delete" in value)


async def clean_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    current_message_id: int | None = None,
    chat_type: str | None = None,
) -> int:
    cutoff = int(time.time()) - MESSAGE_RETENTION_SECONDS

    with sqlite3.connect(MESSAGE_DB) as conn:
        rows = conn.execute(
            """
            SELECT message_id
            FROM message_history
            WHERE chat_id = ? AND created_at >= ?
            ORDER BY message_id
            """,
            (chat_id, cutoff),
        ).fetchall()

    message_ids = {row[0] for row in rows}

    # In a private chat Telegram allows bots to delete incoming user
    # messages as well as their own messages. Use the current message-id
    # sequence to also catch recent messages from before this tracker
    # was installed. Telegram skips message IDs that do not exist and
    # refuses messages older than 48 hours.
    if chat_type == "private" and current_message_id:
        scan_start = max(
            1,
            current_message_id - CLEAN_PRIVATE_CHAT_SCAN_LIMIT + 1,
        )
        message_ids.update(
            range(scan_start, current_message_id + 1)
        )

    ordered_ids = sorted(message_ids)

    deleted = 0
    for start in range(0, len(ordered_ids), 100):
        chunk = ordered_ids[start:start + 100]
        if not chunk:
            continue

        try:
            await context.bot.delete_messages(
                chat_id=chat_id,
                message_ids=chunk,
            )
            deleted += len(chunk)
        except Exception:
            # If a mixed batch contains an undeletable/expired message,
            # fall back to deleting one-by-one so valid messages still go.
            for message_id in chunk:
                try:
                    await context.bot.delete_message(
                        chat_id=chat_id,
                        message_id=message_id,
                    )
                    deleted += 1
                except Exception:
                    pass

    with sqlite3.connect(MESSAGE_DB) as conn:
        conn.execute(
            "DELETE FROM message_history WHERE chat_id = ?",
            (chat_id,),
        )
        conn.commit()

    conversation_memory.pop(chat_id, None)
    return deleted


# =========================================================
# MESSAGE SPLITTER
# =========================================================

def split_message(text: str, limit: int = 3900):
    if not text:
        return [
            "I couldn't generate a response."
        ]

    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text

    while len(remaining) > limit:

        cut = remaining.rfind(
            "\n",
            0,
            limit
        )

        if cut < limit // 2:
            cut = remaining.rfind(
                " ",
                0,
                limit
            )

        if cut < limit // 2:
            cut = limit

        chunks.append(
            remaining[:cut].strip()
        )

        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


# =========================================================
# PREMIUM TELEGRAM FORMATTER
# =========================================================

def format_telegram_html(text: str) -> str:
    """
    Converts common AI Markdown-style formatting into
    clean Telegram HTML.

    Prevents visible:
    **bold**
    ## headings
    raw markdown
    """

    if not text:
        return "I couldn't generate a response."

    text = text.strip()

    code_blocks = []

    # -----------------------------------------------------
    # Protect code blocks
    # -----------------------------------------------------

    def stash_code(match):

        code = match.group(1).strip()

        code = html.escape(
            code,
            quote=False
        )

        token = (
            f"___KIVA_CODE_{len(code_blocks)}___"
        )

        code_blocks.append(
            f"<pre>{code}</pre>"
        )

        return token

    text = re.sub(
        r"```(?:[A-Za-z0-9_+#.-]+)?\s*\n?(.*?)```",
        stash_code,
        text,
        flags=re.S,
    )

    # -----------------------------------------------------
    # Escape HTML
    # -----------------------------------------------------

    text = html.escape(
        text,
        quote=False
    )

    # -----------------------------------------------------
    # Headings
    # -----------------------------------------------------

    text = re.sub(
        r"(?m)^\s*#{1,6}\s+(.+?)\s*$",
        r"<b>\1</b>",
        text,
    )

    # -----------------------------------------------------
    # Bold
    # -----------------------------------------------------

    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
    )

    text = re.sub(
        r"__(.+?)__",
        r"<b>\1</b>",
        text,
    )

    # -----------------------------------------------------
    # Italic
    # -----------------------------------------------------

    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        r"<i>\1</i>",
        text,
    )

    text = re.sub(
        r"(?<!_)_([^_\n]+)_(?!_)",
        r"<i>\1</i>",
        text,
    )

    # -----------------------------------------------------
    # Inline code
    # -----------------------------------------------------

    text = re.sub(
        r"`([^`\n]+)`",
        r"<code>\1</code>",
        text,
    )

    # -----------------------------------------------------
    # Bullets
    # -----------------------------------------------------

    text = re.sub(
        r"(?m)^\s*[-*]\s+",
        "• ",
        text,
    )

    # -----------------------------------------------------
    # Clean excessive blank lines
    # -----------------------------------------------------

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    text = text.strip()

    # -----------------------------------------------------
    # Restore code blocks
    # -----------------------------------------------------

    for i, block in enumerate(code_blocks):

        text = text.replace(
            f"___KIVA_CODE_{i}___",
            block,
        )

    return text or "I couldn't generate a response."


# =========================================================
# WEB SEARCH DETECTION
# =========================================================

def needs_web_search(text: str) -> bool:

    text_lower = text.lower()

    keywords = [
        "latest",
        "today",
        "current",
        "recent",
        "news",
        "price today",
        "live score",
        "weather",
        "right now",
        "abhi",
        "aaj",
        "latest update",
        "current update",
        "2026",
    ]

    return any(
        keyword in text_lower
        for keyword in keywords
    )


# =========================================================
# CODE EXECUTION DETECTION
# =========================================================

def needs_code_execution(text: str) -> bool:

    text_lower = text.lower()

    keywords = [
        "calculate",
        "calculator",
        "solve",
        "equation",
        "percentage",
        "average",
        "statistics",
        "data analysis",
        "run this code",
        "execute this code",
        "python output",
    ]

    return any(
        keyword in text_lower
        for keyword in keywords
    )


# =========================================================
# URL DETECTION
# =========================================================

def contains_url(text: str) -> bool:

    return bool(
        re.search(
            r"https?://\S+",
            text
        )
    )


# =========================================================
# IMAGE REQUEST DETECTION
# =========================================================

def is_image_request(text: str) -> bool:

    text_lower = text.lower().strip()

    prefixes = [
        "/image",
        "/generate",
        "/imagine",
    ]

    if any(
        text_lower.startswith(prefix)
        for prefix in prefixes
    ):
        return True

    phrases = [
        "generate an image",
        "generate image",
        "create an image",
        "create image",
        "make an image",
        "make image",
        "generate a photo",
        "generate photo",
        "image bana",
        "image banao",
        "photo bana",
        "photo banao",
        "tasveer bana",
        "tasveer banao",
        "picture bana",
        "picture banao",
        "photo create",
        "photo generate",
        "image create",
    ]

    return any(
        phrase in text_lower
        for phrase in phrases
    )


def clean_image_prompt(text: str) -> str:

    text = text.strip()

    for prefix in [
        "/image",
        "/generate",
        "/imagine",
    ]:

        if text.lower().startswith(prefix):

            text = text[
                len(prefix):
            ].strip()

    return text


# =========================================================
# TYPING INDICATOR
# =========================================================

async def typing_loop(
    bot,
    chat_id: int,
    stop_event: asyncio.Event
):

    try:

        while not stop_event.is_set():

            try:

                await bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.TYPING,
                )

            except Exception:
                pass

            try:

                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=4.0,
                )

            except asyncio.TimeoutError:
                pass

    except asyncio.CancelledError:
        pass


# =========================================================
# TEXT GENERATION
# =========================================================

async def generate_text(
    user_id: int,
    prompt: str,
    display_name: str,
    extra_input=None,
):

    previous_id = conversation_memory.get(
        user_id
    )

    tools = []

    if needs_web_search(prompt):
        tools.append(
            {"type": "google_search"}
        )

    if needs_code_execution(prompt):
        tools.append(
            {"type": "code_execution"}
        )

    if contains_url(prompt):
        tools.append(
            {"type": "url_context"}
        )

    user_context = f"""
Telegram user's display name:
{display_name}

User message:
{prompt}
"""

    if extra_input:

        input_data = [
            extra_input,
            {
                "type": "text",
                "text": user_context,
            },
        ]

    else:
        input_data = user_context

    last_error = None

    for model in FALLBACK_MODELS:

        try:

            kwargs = {
                "model": model,
                "input": input_data,
                "system_instruction": SYSTEM_PROMPT,
                "generation_config": {
                    "thinking_level": "low",
                    "max_output_tokens": 3000,
                },
            }

            if tools:
                kwargs["tools"] = tools

            if previous_id:

                kwargs[
                    "previous_interaction_id"
                ] = previous_id

            interaction = await asyncio.to_thread(
                lambda: client.interactions.create(
                    **kwargs
                )
            )

            answer = interaction.output_text

            if not answer:

                answer = (
                    "I completed the request, "
                    "but there was no text response."
                )

            conversation_memory[
                user_id
            ] = interaction.id

            return answer

        except Exception as exc:

            last_error = exc

            logger.exception(
                "Text model failed: %s",
                model,
            )

            # Retry without old conversation state
            if previous_id:

                try:

                    kwargs.pop(
                        "previous_interaction_id",
                        None,
                    )

                    interaction = await asyncio.to_thread(
                        lambda: client.interactions.create(
                            **kwargs
                        )
                    )

                    answer = (
                        interaction.output_text
                    )

                    if answer:

                        conversation_memory[
                            user_id
                        ] = interaction.id

                        return answer

                except Exception as retry_exc:

                    last_error = retry_exc

    raise RuntimeError(
        f"All text models failed: {last_error}"
    )


# =========================================================
# IMAGE GENERATION
# =========================================================

async def generate_image(prompt: str):
    """Generate an image using Gemini's current image-generation models."""

    prompt = prompt.strip()

    if not prompt:
        raise ValueError("Image prompt is empty.")

    last_error = None

    for model in IMAGE_FALLBACK_MODELS:
        try:
            def call_image_api(model_name=model):
                return client.interactions.create(
                    model=model_name,
                    input=prompt,
                    response_format={
                        "type": "image",
                        "aspect_ratio": IMAGE_ASPECT_RATIO,
                        "image_size": IMAGE_SIZE,
                    },
                )

            response = await asyncio.to_thread(call_image_api)

            output_image = getattr(response, "output_image", None)

            if output_image is not None:
                data = getattr(output_image, "data", None)

                if data:
                    if isinstance(data, str):
                        return base64.b64decode(data)
                    return bytes(data)

            # Compatibility fallback for SDK response shapes.
            for item in getattr(response, "outputs", []) or []:
                data = getattr(item, "data", None)
                if data:
                    if isinstance(data, str):
                        return base64.b64decode(data)
                    return bytes(data)

            raise RuntimeError(
                f"Image model {model} returned no image data."
            )

        except Exception as exc:
            last_error = exc
            logger.exception(
                "Image generation failed with model %s: %s",
                model,
                exc,
            )

    raise RuntimeError(
        f"All image models failed: {last_error}"
    )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await track_incoming_message(update.message)

    user = update.effective_user
    name = get_display_name(user)

    message = (
        "✨ <b>Welcome to KIVA AI</b>\n\n"
        f"{html.escape(name)}, it’s great to have you here.\n\n"
        "How can I help you today?"
    )

    await tracked_reply_text(
        update.message,
        message,
        parse_mode="HTML",
    )


# =========================================================
# OWNER
# =========================================================

OWNER_COPY_TEXT = (
    "KIVA AI\n\n"
    "Founder & Developer — Krishna Singh\n\n"
    "Telegram — @qrishna\n\n"
    "Engine — Kiva AI-3.6-flash\n\n"
    "Image Engine — Kiva AI-3.1-flash-image\n\n"
    "Memory — Active\n\n"
    "Built & maintained by Krishna Singh"
)


async def owner_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await track_incoming_message(update.message)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📋 Copy",
                copy_text=CopyTextButton(
                    text=OWNER_COPY_TEXT
                ),
            ),
            InlineKeyboardButton(
                "✈️ Contact Owner",
                url=OWNER_URL,
            ),
        ]
    ])

    await tracked_reply_text(
        update.message,
        OWNER_COPY_TEXT,
        reply_markup=keyboard,
    )


# =========================================================
# IMAGE COMMAND
# =========================================================

async def image_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await track_incoming_message(update.message)

    prompt = " ".join(
        context.args
    ).strip()

    if not prompt:

        await tracked_reply_text(update.message,
            "🎨 <b>Image Generator</b>\n\n"
            "Example:\n"
            "<code>/image a cinematic futuristic city at night</code>",
            parse_mode="HTML",
        )

        return

    stop_event = asyncio.Event()

    typing_task = asyncio.create_task(
        typing_loop(
            context.bot,
            update.effective_chat.id,
            stop_event,
        )
    )

    try:

        await tracked_reply_text(update.message,
            "🎨 <b>Generating your Image…</b>\n\n"
            "Turning your prompt into a visual. ✨",
            parse_mode="HTML",
        )

        image_bytes = await generate_image(
            prompt
        )

        await tracked_reply_photo(update.message,
            photo=io.BytesIO(
                image_bytes
            ),
            caption="✨ KIVA AI",
        )

    except Exception:

        logger.exception(
            "Image generation failed"
        )

        await tracked_reply_text(update.message,
            "⚠️ <b>Image generation is temporarily unavailable.</b>\n\n"
            "Please try again later.",
            parse_mode="HTML",
        )

    finally:

        stop_event.set()

        typing_task.cancel()


# =========================================================
# NORMAL TEXT MESSAGE
# =========================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if (
        not update.message
        or not update.message.text
    ):
        return

    user = update.effective_user

    user_id = user.id

    chat_id = update.effective_chat.id

    prompt = update.message.text.strip()

    if not prompt:
        return

    await track_incoming_message(update.message)

    # -----------------------------------------------------
    # NATURAL CHAT CLEANUP
    # -----------------------------------------------------

    if is_clean_request(prompt):
        await clean_chat(
            context,
            chat_id,
            current_message_id=update.message.message_id,
            chat_type=update.effective_chat.type,
        )
        return

    # -----------------------------------------------------
    # IMAGE REQUEST
    # -----------------------------------------------------

    if is_image_request(prompt):

        image_prompt = clean_image_prompt(
            prompt
        )

        if not image_prompt:

            await tracked_reply_text(update.message,
                "🎨 Tell me what you want to create."
            )

            return

        stop_event = asyncio.Event()

        typing_task = asyncio.create_task(
            typing_loop(
                context.bot,
                chat_id,
                stop_event,
            )
        )

        try:

            await tracked_reply_text(update.message,
                "🎨 <b>Generating your Image…</b>\n\n"
                "Turning your prompt into a visual. ✨",
                parse_mode="HTML",
            )

            image_bytes = await generate_image(
                image_prompt
            )

            await tracked_reply_photo(update.message,
                photo=io.BytesIO(
                    image_bytes
                ),
                caption="✨ KIVA AI",
            )

        except Exception:

            logger.exception(
                "Image request failed"
            )

            await tracked_reply_text(update.message,
                "⚠️ <b>Image generation is temporarily unavailable.</b>\n\n"
                "Please try again later.",
                parse_mode="HTML",
            )

        finally:

            stop_event.set()

            typing_task.cancel()

        return

    # -----------------------------------------------------
    # NORMAL AI CHAT
    # -----------------------------------------------------

    display_name = get_display_name(
        user
    )

    lock = get_user_lock(
        user_id
    )

    async with lock:

        stop_event = asyncio.Event()

        typing_task = asyncio.create_task(
            typing_loop(
                context.bot,
                chat_id,
                stop_event,
            )
        )

        try:

            answer = await generate_text(
                user_id=user_id,
                prompt=prompt,
                display_name=display_name,
            )

            formatted_answer = (
                format_telegram_html(
                    answer
                )
            )

            for chunk in split_message(
                formatted_answer
            ):

                try:

                    await tracked_reply_text(update.message,
                        chunk,
                        parse_mode="HTML",
                    )

                except Exception:

                    # Safe fallback
                    await tracked_reply_text(update.message,
                        re.sub(
                            r"<[^>]+>",
                            "",
                            chunk,
                        )
                    )

        except Exception as exc:

            logger.exception(
                "Message processing failed: %s",
                exc,
            )

            await tracked_reply_text(update.message,
                "⚠️ <b>KIVA AI temporarily unavailable.</b>\n\n"
                "Please try again in a few seconds.",
                parse_mode="HTML",
            )

        finally:

            stop_event.set()

            typing_task.cancel()


# =========================================================
# PHOTO / IMAGE UNDERSTANDING
# =========================================================

async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    user_id = user.id

    chat_id = update.effective_chat.id

    await track_incoming_message(update.message)

    caption = (
        update.message.caption
        or
        "Analyze this image carefully and explain what you see."
    )

    photo = update.message.photo[-1]

    tg_file = await context.bot.get_file(
        photo.file_id
    )

    image_data = await tg_file.download_as_bytearray()

    extra_input = {
        "type": "image",
        "data": base64.b64encode(
            bytes(image_data)
        ).decode("utf-8"),
        "mime_type": "image/jpeg",
    }

    stop_event = asyncio.Event()

    typing_task = asyncio.create_task(
        typing_loop(
            context.bot,
            chat_id,
            stop_event,
        )
    )

    try:

        answer = await generate_text(
            user_id=user_id,
            prompt=caption,
            display_name=get_display_name(
                user
            ),
            extra_input=extra_input,
        )

        formatted_answer = (
            format_telegram_html(
                answer
            )
        )

        for chunk in split_message(
            formatted_answer
        ):

            try:

                await tracked_reply_text(update.message,
                    chunk,
                    parse_mode="HTML",
                )

            except Exception:

                await tracked_reply_text(update.message,
                    re.sub(
                        r"<[^>]+>",
                        "",
                        chunk,
                    )
                )

    except Exception:

        logger.exception(
            "Image understanding failed"
        )

        await tracked_reply_text(update.message,
            "⚠️ I couldn't analyze that image right now.\n"
            "Please try again."
        )

    finally:

        stop_event.set()

        typing_task.cancel()


# =========================================================
# PDF / DOCUMENT UNDERSTANDING
# =========================================================

async def document_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    document = update.message.document

    if not document:
        return

    mime_type = document.mime_type or ""

    if mime_type != "application/pdf":

        await tracked_reply_text(update.message,
            "📄 Abhi document understanding ke liye PDF support enabled hai.\n\n"
            "Please PDF upload karke uske saath apna question bhejiye."
        )

        return

    if (
        document.file_size
        and document.file_size > 50 * 1024 * 1024
    ):

        await tracked_reply_text(update.message,
            "⚠️ Ye PDF 50 MB se badi hai.\n"
            "Please smaller PDF upload karein."
        )

        return

    user = update.effective_user

    user_id = user.id

    chat_id = update.effective_chat.id

    await track_incoming_message(update.message)

    question = (
        update.message.caption
        or
        "Summarize this PDF and explain its important points."
    )

    stop_event = asyncio.Event()

    typing_task = asyncio.create_task(
        typing_loop(
            context.bot,
            chat_id,
            stop_event,
        )
    )

    try:

        tg_file = await context.bot.get_file(
            document.file_id
        )

        pdf_data = await tg_file.download_as_bytearray()

        extra_input = {
            "type": "document",
            "data": base64.b64encode(
                bytes(pdf_data)
            ).decode("utf-8"),
            "mime_type": "application/pdf",
        }

        answer = await generate_text(
            user_id=user_id,
            prompt=question,
            display_name=get_display_name(
                user
            ),
            extra_input=extra_input,
        )

        formatted_answer = (
            format_telegram_html(
                answer
            )
        )

        for chunk in split_message(
            formatted_answer
        ):

            try:

                await tracked_reply_text(update.message,
                    chunk,
                    parse_mode="HTML",
                )

            except Exception:

                await tracked_reply_text(update.message,
                    re.sub(
                        r"<[^>]+>",
                        "",
                        chunk,
                    )
                )

    except Exception:

        logger.exception(
            "PDF processing failed"
        )

        await tracked_reply_text(update.message,
            "⚠️ PDF process nahi ho paayi.\n"
            "Please try again with a smaller PDF."
        )

    finally:

        stop_event.set()

        typing_task.cancel()


# =========================================================
# VOICE / AUDIO
# =========================================================

async def voice_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    voice = update.message.voice

    if not voice:
        return

    user = update.effective_user

    user_id = user.id

    chat_id = update.effective_chat.id

    await track_incoming_message(update.message)

    stop_event = asyncio.Event()

    typing_task = asyncio.create_task(
        typing_loop(
            context.bot,
            chat_id,
            stop_event,
        )
    )

    try:

        tg_file = await context.bot.get_file(
            voice.file_id
        )

        audio_data = await tg_file.download_as_bytearray()

        extra_input = {
            "type": "audio",
            "data": base64.b64encode(
                bytes(audio_data)
            ).decode("utf-8"),
            "mime_type": "audio/ogg",
        }

        answer = await generate_text(
            user_id=user_id,
            prompt=(
                "Listen to this voice message and "
                "respond naturally. If it contains a "
                "question, answer it."
            ),
            display_name=get_display_name(
                user
            ),
            extra_input=extra_input,
        )

        formatted_answer = (
            format_telegram_html(
                answer
            )
        )

        for chunk in split_message(
            formatted_answer
        ):

            try:

                await tracked_reply_text(update.message,
                    chunk,
                    parse_mode="HTML",
                )

            except Exception:

                await tracked_reply_text(update.message,
                    re.sub(
                        r"<[^>]+>",
                        "",
                        chunk,
                    )
                )

    except Exception:

        logger.exception(
            "Audio processing failed"
        )

        await tracked_reply_text(update.message,
            "⚠️ Voice message process nahi ho paaya.\n"
            "Please try again."
        )

    finally:

        stop_event.set()

        typing_task.cancel()


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error,
    )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application
):
    # /start remains functional for Telegram's native Start Bot flow,
    # but it is intentionally hidden from the command menu.
    await application.bot.set_my_commands([
        ("image", "Generate an AI image"),
        ("owner", "KIVA AI owner"),
    ])

    try:
        await application.bot.set_my_short_description(
            "KIVA AI — your premium intelligent AI assistant."
        )

        await application.bot.set_my_description(
            "KIVA AI is a premium AI assistant for natural chat, "
            "coding, analysis, image creation, documents and more."
        )

    except Exception:
        logger.exception(
            "Could not update bot profile"
        )


# =========================================================
# MAIN
# =========================================================

def main():

    logger.info(
        "Starting KIVA AI..."
    )

    logger.info(
        "Text model configured."
    )

    logger.info(
        "Image model configured."
    )

    # -----------------------------------------------------
    # Render health server
    # -----------------------------------------------------

    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()

    # -----------------------------------------------------
    # Telegram application
    # -----------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # -----------------------------------------------------
    # Commands
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "image",
            image_command
        )
    )

    application.add_handler(
        CommandHandler(
            "owner",
            owner_command
        )
    )

    # -----------------------------------------------------
    # Photos
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_message,
        )
    )

    # -----------------------------------------------------
    # Documents
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_message,
        )
    )

    # -----------------------------------------------------
    # Voice
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.VOICE,
            voice_message,
        )
    )

    # -----------------------------------------------------
    # Normal text
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message,
        )
    )

    # -----------------------------------------------------
    # Errors
    # -----------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "KIVA AI is starting polling..."
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
    
