import os
import re
import io
import base64
import asyncio
import logging
import threading
from typing import Optional

from flask import Flask, jsonify
from google import genai
from google.genai import types
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("API_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

# Current Gemini models
TEXT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
FALLBACK_MODELS = [
    TEXT_MODEL,
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
]

IMAGE_MODEL = os.getenv(
    "GEMINI_IMAGE_MODEL",
    "gemini-3.1-flash-image"
)

# Public branding shown to Telegram users. Keep provider/model IDs private.
PUBLIC_TEXT_MODEL = "Kiva AI-3.6-flash"
PUBLIC_IMAGE_MODEL = "Kiva AI-3.1-flash-image"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is missing.")


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

client = genai.Client(api_key=GEMINI_API_KEY)


# =========================================================
# CONVERSATION MEMORY
# =========================================================

# Telegram user_id -> last Gemini interaction ID
conversation_memory = {}

# Prevent two requests from same user fighting with each other
user_locks = {}


def get_user_lock(user_id: int):
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()

    return user_locks[user_id]


# =========================================================
# PREMIUM SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = """
You are KIVA AI, a premium, modern, highly capable AI assistant inside Telegram.

PERSONALITY:
- Professional, intelligent, warm and natural.
- Speak like a high-quality modern AI assistant.
- Never sound robotic, repetitive or cheap.
- Understand Hinglish, Hindi, English and mixed-language messages naturally.
- Reply in the same language/style the user prefers.
- If the user speaks Hinglish, respond naturally in Hinglish.
- If the user speaks Hindi, respond naturally in Hindi.
- If the user speaks English, respond naturally in English.

STYLE:
- Clean and professional formatting.
- Use short paragraphs.
- Use headings when useful.
- Use bullet points when useful.
- Use emojis sparingly and professionally.
- Do not overuse emojis.
- Do not begin every answer with "Sure!".
- Do not repeat the user's question unnecessarily.
- Be direct but helpful.
- For technical/code questions, provide production-quality answers.
- For difficult questions, reason carefully before answering.
- Never pretend that a feature was performed if it was not actually performed.

IDENTITY:
- Your name is KIVA AI.
- You are a premium AI assistant.
- The user's Telegram display name may be supplied in the prompt.
- Use their display name naturally when appropriate.
- Do NOT claim to know a person's legal/real-world identity.
- Only use information actually available to you.

CAPABILITIES:
- General conversation
- Coding and debugging
- Mathematics
- Analysis
- Current information when web search is available
- Image understanding
- Document understanding
- Creative writing
- Technical explanations
- Translation
- Planning
- Brainstorming
- Other normal AI-assistant tasks

IMPORTANT:
When current/recent information is needed and Google Search is enabled, use it.
When code execution is available and useful for calculations/data analysis, use it.
"""


# =========================================================
# FLASK HEALTH SERVER FOR RENDER
# =========================================================

web_app = Flask(__name__)


@web_app.get("/")
def home():
    return jsonify({
        "name": "KIVA AI",
        "status": "online",
        "service": "Telegram AI Bot",
        "engine": PUBLIC_TEXT_MODEL,
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
# HELPERS
# =========================================================

def get_display_name(user) -> str:
    """
    Telegram gives us the user's display name.
    We must not claim it is their legal/real name.
    """
    first = (user.first_name or "").strip()
    last = (user.last_name or "").strip()

    full_name = f"{first} {last}".strip()

    if full_name:
        return full_name

    if user.username:
        return f"@{user.username}"

    return "there"


def split_message(text: str, limit: int = 3900):
    """
    Telegram messages have a practical length limit.
    Split long AI answers safely.
    """
    if not text:
        return ["I couldn't generate a response."]

    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text

    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)

        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)

        if cut < limit // 2:
            cut = limit

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


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

    return any(keyword in text_lower for keyword in keywords)


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

    return any(keyword in text_lower for keyword in keywords)


def contains_url(text: str) -> bool:
    return bool(re.search(r"https?://\S+", text))


def is_image_request(text: str) -> bool:
    text_lower = text.lower().strip()

    prefixes = [
        "/image",
        "/generate",
        "/imagine",
    ]

    if any(text_lower.startswith(p) for p in prefixes):
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
    ]

    return any(p in text_lower for p in phrases)


def clean_image_prompt(text: str) -> str:
    text = text.strip()

    for prefix in ["/image", "/generate", "/imagine"]:
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()

    return text


# =========================================================
# TYPING INDICATOR
# =========================================================

async def typing_loop(bot, chat_id: int, stop_event: asyncio.Event):
    """
    Telegram's typing status lasts only a few seconds,
    so refresh it while Gemini is processing.
    """
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
# GEMINI TEXT GENERATION
# =========================================================

async def generate_text(
    user_id: int,
    prompt: str,
    display_name: str,
    extra_input=None,
):
    previous_id = conversation_memory.get(user_id)

    tools = []

    if needs_web_search(prompt):
        tools.append({"type": "google_search"})

    if needs_code_execution(prompt):
        tools.append({"type": "code_execution"})

    if contains_url(prompt):
        tools.append({"type": "url_context"})

    user_context = f"""
Telegram user's display name: {display_name}

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
                kwargs["previous_interaction_id"] = previous_id

            interaction = await asyncio.to_thread(
                lambda: client.interactions.create(**kwargs)
            )

            answer = interaction.output_text

            if not answer:
                answer = "I completed the request, but there was no text response."

            # Save state only after successful response
            conversation_memory[user_id] = interaction.id

            return answer

        except Exception as exc:
            last_error = exc

            logger.exception(
                "Gemini model failed: %s | %s",
                model,
                exc,
            )

            # If previous conversation state caused the problem,
            # retry the request without it.
            if previous_id:
                try:
                    kwargs.pop("previous_interaction_id", None)

                    interaction = await asyncio.to_thread(
                        lambda: client.interactions.create(**kwargs)
                    )

                    answer = interaction.output_text

                    if answer:
                        conversation_memory[user_id] = interaction.id
                        return answer

                except Exception as retry_exc:
                    last_error = retry_exc

    raise RuntimeError(
        f"All Gemini models failed. Last error: {last_error}"
    )


# =========================================================
# GEMINI IMAGE GENERATION
# =========================================================

async def generate_image(prompt: str):
    """Generate an image directly with the Gemini image model.

    IMPORTANT: google-genai 2.13.0 rejects the newer response_format
    field inside GenerateContentConfig. Keep the image request minimal
    and request IMAGE-only output. The model defaults to 1K for speed.
    """
    prompt = prompt.strip()

    if not prompt:
        raise ValueError("Image prompt is empty.")

    def call_image_api():
        return client.models.generate_content(
            model=IMAGE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

    response = await asyncio.to_thread(call_image_api)

    # Gemini returns generated image data as an inline_data part.
    for part in response.parts:
        inline_data = getattr(part, "inline_data", None)
        if inline_data is not None:
            data = getattr(inline_data, "data", None)
            if data:
                if isinstance(data, str):
                    return base64.b64decode(data)
                return bytes(data)

    raise RuntimeError("Image model returned no image data.")


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    name = get_display_name(user)

    keyboard = [
        [
            InlineKeyboardButton(
                "💬 Start Chat",
                callback_data="chat",
            ),
            InlineKeyboardButton(
                "🎨 Create Image",
                callback_data="image",
            ),
        ],
        [
            InlineKeyboardButton(
                "🧠 New Conversation",
                callback_data="clear",
            ),
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help",
            ),
        ],
    ]

    message = f"""
✨ <b>Welcome to KIVA AI</b>, {name}!

━━━━━━━━━━━━━━━━━━

Your personal <b>next-generation AI assistant</b> is ready.

🧠 Intelligent conversations
⚡ Fast responses
🌐 Live web information
💻 Coding & problem solving
🖼️ Image understanding
🎨 AI image generation
📄 Document & PDF analysis
🎙️ Audio understanding
🌍 Hindi • Hinglish • English

━━━━━━━━━━━━━━━━━━

<b>KIVA AI is ready whenever you are.</b>

Just type your message below and let's get started. 🚀
"""

    await update.message.reply_text(
        message,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# HELP
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
✨ KIVA AI — Help

<b>Chat</b>
Simply type anything and KIVA AI will respond.

<b>Image generation</b>
Use:
<code>/image a futuristic city at night</code>

You can also simply say:
"Ek futuristic city ki image banao."

<b>Commands</b>
/start — Welcome screen
/help — Help
/image — Generate an image
/clear — Start a fresh conversation
/status — Bot status

<b>Tip:</b>
You can speak in Hindi, Hinglish or English naturally.
"""

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# CLEAR MEMORY
# =========================================================

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    conversation_memory.pop(user_id, None)

    await update.message.reply_text(
        "🧠 <b>Fresh conversation ready.</b>\n\n"
        "Purani chat context clear kar di gayi hai. "
        "Ab hum bilkul fresh start kar sakte hain. ✨",
        parse_mode="HTML",
    )


# =========================================================
# STATUS
# =========================================================

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"""
🟢 <b>KIVA AI is operational</b>

━━━━━━━━━━━━━━━━━━

⚡ Engine: <code>{PUBLIC_TEXT_MODEL}</code>
🎨 Image: <code>{PUBLIC_IMAGE_MODEL}</code>
🧠 Memory: Active
🌐 Web Search: Available
💻 Code Execution: Available
🖼️ Vision: Available
📄 Documents: Available

━━━━━━━━━━━━━━━━━━

KIVA AI is ready. 🚀
""",
        parse_mode="HTML",
    )


# =========================================================
# IMAGE COMMAND
# =========================================================

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()

    if not prompt:
        await update.message.reply_text(
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
        await update.message.reply_text(
            "🎨 Creating your image...\n\n"
            "Please wait a moment. ✨"
        )

        image_bytes = await generate_image(prompt)

        await update.message.reply_photo(
            photo=io.BytesIO(image_bytes),
            caption="✨ Generated by KIVA AI",
        )

    except Exception as exc:
        logger.exception("Image generation failed")

        await update.message.reply_text(
            "⚠️ Image generation is temporarily unavailable.\n\n"
            "Please try again in a few seconds."
        )

    finally:
        stop_event.set()
        typing_task.cancel()


# =========================================================
# NORMAL TEXT MESSAGE
# =========================================================

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id

    prompt = update.message.text.strip()

    if not prompt:
        return

    # Image request
    if is_image_request(prompt):
        image_prompt = clean_image_prompt(prompt)

        if not image_prompt:
            await update.message.reply_text(
                "🎨 Bataiye image mein kya create karna hai?"
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
            await update.message.reply_text(
                "🎨 <b>Creating your image...</b>\n\n"
                "Aapke prompt ko image mein convert kar raha hoon. ✨",
                parse_mode="HTML",
            )

            image_bytes = await generate_image(image_prompt)

            await update.message.reply_photo(
                photo=io.BytesIO(image_bytes),
                caption="✨ Created by KIVA AI",
            )

        except Exception:
            logger.exception("Image request failed")

            await update.message.reply_text(
                "⚠️ Image generation temporarily unavailable.\n"
                "Please try again."
            )

        finally:
            stop_event.set()
            typing_task.cancel()

        return

    display_name = get_display_name(user)

    lock = get_user_lock(user_id)

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

            for chunk in split_message(answer):
                await update.message.reply_text(chunk)

        except Exception as exc:
            logger.exception(
                "Message processing failed: %s",
                exc,
            )

            await update.message.reply_text(
                "⚠️ KIVA AI temporarily unavailable.\n\n"
                "Please try again in a few seconds."
            )

        finally:
            stop_event.set()
            typing_task.cancel()


# =========================================================
# PHOTO / IMAGE UNDERSTANDING
# =========================================================

async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id

    caption = (
        update.message.caption
        or "Analyze this image carefully and explain what you see."
    )

    photo = update.message.photo[-1]

    tg_file = await context.bot.get_file(photo.file_id)

    image_data = await tg_file.download_as_bytearray()

    mime_type = "image/jpeg"

    extra_input = {
        "type": "image",
        "data": base64.b64encode(bytes(image_data)).decode("utf-8"),
        "mime_type": mime_type,
    }

    display_name = get_display_name(user)

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
            display_name=display_name,
            extra_input=extra_input,
        )

        for chunk in split_message(answer):
            await update.message.reply_text(chunk)

    except Exception:
        logger.exception("Image understanding failed")

        await update.message.reply_text(
            "⚠️ I couldn't analyze that image right now.\n"
            "Please try again."
        )

    finally:
        stop_event.set()
        typing_task.cancel()


# =========================================================
# PDF / DOCUMENT UNDERSTANDING
# =========================================================

async def document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document

    if not document:
        return

    mime_type = document.mime_type or ""

    # Currently focus on PDFs for native document understanding
    if mime_type != "application/pdf":
        await update.message.reply_text(
            "📄 Abhi document understanding ke liye PDF support enabled hai.\n\n"
            "Please PDF upload karke uske saath apna question bhejiye."
        )
        return

    if document.file_size and document.file_size > 50 * 1024 * 1024:
        await update.message.reply_text(
            "⚠️ Ye PDF 50 MB se badi hai.\n"
            "Please smaller PDF upload karein."
        )
        return

    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id

    question = (
        update.message.caption
        or "Summarize this PDF and explain its important points."
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
        tg_file = await context.bot.get_file(document.file_id)
        pdf_data = await tg_file.download_as_bytearray()

        extra_input = {
            "type": "document",
            "data": base64.b64encode(bytes(pdf_data)).decode("utf-8"),
            "mime_type": "application/pdf",
        }

        answer = await generate_text(
            user_id=user_id,
            prompt=question,
            display_name=get_display_name(user),
            extra_input=extra_input,
        )

        for chunk in split_message(answer):
            await update.message.reply_text(chunk)

    except Exception:
        logger.exception("PDF processing failed")

        await update.message.reply_text(
            "⚠️ PDF process nahi ho paayi.\n"
            "Please try again with a smaller PDF."
        )

    finally:
        stop_event.set()
        typing_task.cancel()


# =========================================================
# VOICE / AUDIO UNDERSTANDING
# =========================================================

async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice

    if not voice:
        return

    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id

    stop_event = asyncio.Event()

    typing_task = asyncio.create_task(
        typing_loop(
            context.bot,
            chat_id,
            stop_event,
        )
    )

    try:
        tg_file = await context.bot.get_file(voice.file_id)

        audio_data = await tg_file.download_as_bytearray()

        extra_input = {
            "type": "audio",
            "data": base64.b64encode(bytes(audio_data)).decode("utf-8"),
            "mime_type": "audio/ogg",
        }

        answer = await generate_text(
            user_id=user_id,
            prompt=(
                "Listen to this voice message and respond naturally. "
                "If it contains a question, answer it."
            ),
            display_name=get_display_name(user),
            extra_input=extra_input,
        )

        for chunk in split_message(answer):
            await update.message.reply_text(chunk)

    except Exception:
        logger.exception("Audio processing failed")

        await update.message.reply_text(
            "⚠️ Voice message process nahi ho paaya.\n"
            "Please try again."
        )

    finally:
        stop_event.set()
        typing_task.cancel()


# =========================================================
# BUTTONS
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if query.data == "clear":

        conversation_memory.pop(user_id, None)

        await query.message.reply_text(
            "🧠 <b>Fresh conversation started.</b>\n\n"
            "Ab KIVA AI bilkul fresh context ke saath ready hai. ✨",
            parse_mode="HTML",
        )

    elif query.data == "image":

        await query.message.reply_text(
            "🎨 <b>Image Generator</b>\n\n"
            "Simply type:\n\n"
            "<code>/image a futuristic city at night</code>\n\n"
            "Ya normal language mein bolo:\n"
            "<i>Ek futuristic city ki image banao.</i>",
            parse_mode="HTML",
        )

    elif query.data == "help":

        await query.message.reply_text(
            "✨ <b>KIVA AI</b>\n\n"
            "Bas message type karo — KIVA AI automatically samajhne ki "
            "koshish karega ki aapko kya chahiye.\n\n"
            "🎨 Image: /image\n"
            "🧠 New chat: /clear\n"
            "📊 Status: /status\n"
            "ℹ️ Help: /help",
            parse_mode="HTML",
        )

    elif query.data == "chat":

        await query.message.reply_text(
            "💬 <b>Chat mode activated.</b>\n\n"
            "Bas apna message bhejiye. 🚀",
            parse_mode="HTML",
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error,
    )


# =========================================================
# POST INIT
# =========================================================

async def post_init(application: Application):

    await application.bot.set_my_commands([
        ("start", "Open KIVA AI"),
        ("help", "How to use KIVA AI"),
        ("image", "Generate an AI image"),
        ("clear", "Start a new conversation"),
        ("status", "Show bot status"),
    ])

    try:
        await application.bot.set_my_short_description(
            "KIVA AI — your premium intelligent AI assistant."
        )

        await application.bot.set_my_description(
            "KIVA AI is a professional AI assistant for chat, coding, "
            "analysis, image generation, documents and more."
        )
    except Exception:
        logger.exception("Could not update bot profile")


# =========================================================
# MAIN
# =========================================================

def main():

    logger.info("Starting KIVA AI...")
    logger.info("Text model: %s", TEXT_MODEL)
    logger.info("Image model: %s", IMAGE_MODEL)

    # Start Render health server
    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("image", image_command)
    )

    application.add_handler(
        CommandHandler("clear", clear_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    # Buttons
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # Images
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_message,
        )
    )

    # PDFs / documents
    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_message,
        )
    )

    # Voice
    application.add_handler(
        MessageHandler(
            filters.VOICE,
            voice_message,
        )
    )

    # Normal text
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("KIVA AI is starting polling...")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
    
