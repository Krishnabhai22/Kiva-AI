import os
import re
import html
import asyncio
import logging
import threading
import base64
from datetime import datetime, timezone

from flask import Flask, jsonify
from google import genai

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

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("API_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

PRIMARY_MODEL = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-2.5-flash-lite"
MODEL_CANDIDATES = [PRIMARY_MODEL, FALLBACK_MODEL]
WEB_MODEL_CANDIDATES = [PRIMARY_MODEL, FALLBACK_MODEL]

OWNER_NAME = os.getenv("OWNER_NAME", "Krishna Singh")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "qrishna")
OWNER_ID = os.getenv("OWNER_ID", "1332494807")
OWNER_URL = os.getenv("OWNER_URL", "https://t.me/qrishna")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN (or API_TOKEN) environment variable is missing.")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is missing.")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("KIVA-AI")

client = genai.Client(api_key=GEMINI_API_KEY)

conversation_memory = {}
user_locks = {}

def get_display_name(user):
    full = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if full:
        return full
    if user.username:
        return f"@{user.username}"
    return "there"

def user_language(user):
    code = (getattr(user, "language_code", None) or "").lower()
    if code.startswith("hi"):
        return "hi"
    if code.startswith("mr"):
        return "mr"
    return "en"

def get_user_lock(user_id):
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]

def split_message(text, limit=3900):
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

def normalize_bullets(text):
    text = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", text)
    text = re.sub(r"(?m)^\s*[▪◦●○■□]\s+", "• ", text)
    return text

def format_telegram_html(text):
    if not text:
        return "I couldn't generate a response."
    text = normalize_bullets(text.strip())
    code_blocks = []

    def stash_code(match):
        code = html.escape(match.group(1).strip(), quote=False)
        token = f"___KIVA_CODE_{len(code_blocks)}___"
        code_blocks.append(f"<pre>{code}</pre>")
        return token

    text = re.sub(r"```(?:[A-Za-z0-9_+#.-]+)?\s*\n?(.*?)```", stash_code, text, flags=re.S)
    text = html.escape(text, quote=False)
    text = re.sub(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()

    for i, block in enumerate(code_blocks):
        text = text.replace(f"___KIVA_CODE_{i}___", block)
    return text or "I couldn't generate a response."

SYSTEM_PROMPT = '''
You are Kiva AI, a premium, smart, and friendly general-purpose AI assistant inside Telegram.

IDENTITY & TONE
- Your name is Kiva AI.
- Speak in a completely natural, neutral, friendly, and human-like tone matching the user's vibe (e.g., if the user talks casually like "kya haal hai", reply naturally like "haan bhai mast hoon, tu bata").
- Do not assume or project any human gender. Be purely neutral and conversational.
- Never use garbage or glitchy special symbols (*!:£/() etc.) unnecessarily. Keep sentences clean.
- Highlight crucial key points and main terms using **bold** formatting naturally.

ACCURACY & TRUTH
- Never invent facts, historical dates, names, statistics, quotes, or current events. Give genuine, real, and factually verified answers.
- When answering historical, current, or factual queries, rely strictly on web search data if available and never guess.

RESPONSE STYLE
- Keep simple questions simple, direct, and conversational.
- Use logical steps for complex problems, math, science, or coding questions.
- Use solid dot bullets "•" for lists. Avoid decorative ASCII separators.
'''

def needs_web_search(text):
    t = (text or "").lower()
    strong = (
        "latest", "today", "current", "recent", "news", "live",
        "right now", "abhi", "aaj", "kal", "this week", "this month",
        "this year", "2026", "2027", "2028",
        "price", "rate", "stock", "weather", "result", "score",
        "release date", "version", "availability", "government",
        "election", "minister", "president", "prime minister",
        "who is", "who won", "kab hua", "kab huwa", "kab hua tha",
        "kisne kiya", "kiske dwara", "source", "link do", "link",
        "official", "verified", "fact check", "real hai", "operation sindoor",
    )
    return any(x in t for x in strong)

def needs_thinking_process(text):
    t = (text or "").lower()
    casual_phrases = ["hi", "hello", "hey", "gm", "gn", "kaise ho", "kya chal raha", "kya kar raha", "bhai", "sup", "ok", "thanks", "bye"]
    if len(t.split()) <= 3 and any(p in t for p in casual_phrases):
        return False
    complex_triggers = [
        "solve", "calculate", "deriv", "proof", "code", "programming", "python", 
        "algorithm", "explain in detail", "analysis", "why did", "how does", 
        "quantum", "physics", "math", "integral", "matrix", "architecture",
        "comparison", "difference between", "history of", "essay"
    ]
    if len(t.split()) > 12 or any(trig in t for trig in complex_triggers):
        return True
    return False

def looks_like_astrology(text):
    t = (text or "").lower()
    return any(
        x in t
        for x in (
            "astrology", "astrologer", "horoscope", "kundli",
            "janam kundli", "birth chart", "zodiac", "rashi",
            "rashifal", "nakshatra", "mera future", "meri kundli",
        )
    )

def extract_citations(interaction):
    citations = []
    seen = set()
    for step in getattr(interaction, "steps", []) or []:
        if getattr(step, "type", None) != "model_output":
            continue
        for block in getattr(step, "content", []) or []:
            if getattr(block, "type", None) != "text":
                continue
            for annotation in getattr(block, "annotations", []) or []:
                if getattr(annotation, "type", None) != "url_citation":
                    continue
                url = getattr(annotation, "url", None)
                title = getattr(annotation, "title", None) or "Source"
                if url and url not in seen:
                    seen.add(url)
                    citations.append((title, url))
    return citations

async def create_interaction(model, input_payload, previous_id, web_required, thinking_level="low"):
    kwargs = {
        "model": model,
        "input": input_payload,
        "system_instruction": SYSTEM_PROMPT,
        "generation_config": {
            "max_output_tokens": 1800,
            "thinking_level": thinking_level,
        },
    }
    if previous_id:
        kwargs["previous_interaction_id"] = previous_id
    if web_required:
        kwargs["tools"] = [{"type": "google_search"}]
    return await asyncio.to_thread(lambda: client.interactions.create(**kwargs))

async def generate_text(user_id, prompt, display_name):
    previous_id = conversation_memory.get(user_id)
    web_required = needs_web_search(prompt)
    is_complex = needs_thinking_process(prompt)
    thinking_level = "low" if is_complex else "off"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    context_text = (
        f"Telegram display name: {display_name}\n"
        f"Current UTC time: {now}\n\n"
        f"User message:\n{prompt}"
    )

    if looks_like_astrology(prompt):
        context_text += "\n\nASTROLOGY REQUEST: Give a traditional/interpretive reading."

    last_error = None
    models_to_try = WEB_MODEL_CANDIDATES if web_required else MODEL_CANDIDATES

    for model in models_to_try:
        try:
            interaction = await create_interaction(
                model=model,
                input_payload=context_text,
                previous_id=previous_id,
                web_required=web_required,
                thinking_level=thinking_level,
            )
            answer = getattr(interaction, "output_text", None) or "Done."
            citations = extract_citations(interaction)
            conversation_memory[user_id] = interaction.id
            return answer, citations, is_complex
        except Exception as exc:
            last_error = exc
            logger.exception("Model failed model=%s", model)

    raise RuntimeError(f"All text models failed: {last_error}")

async def generate_image_answer(user_id, prompt, display_name, image_bytes, mime_type):
    previous_id = conversation_memory.get(user_id)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    user_prompt = prompt.strip() if prompt.strip() else "Analyze this image."

    input_payload = [
        {"type": "image", "mime_type": mime_type, "data": image_b64},
        {"type": "text", "text": f"Telegram display name: {display_name}\n\nUser's request:\n{user_prompt}"},
    ]

    for model in MODEL_CANDIDATES:
        try:
            interaction = await create_interaction(
                model=model, input_payload=input_payload, previous_id=previous_id, web_required=False, thinking_level="off"
            )
            answer = getattr(interaction, "output_text", None) or "I couldn't analyze the image."
            conversation_memory[user_id] = interaction.id
            return answer, []
        except Exception as exc:
            logger.exception("Image analysis failed")

    raise RuntimeError("All image models failed.")

async def typing_loop(bot, chat_id, stop_event):
    try:
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = html.escape(get_display_name(user))
    await update.message.reply_text(f"<b>Kiva AI</b>\n\nNamaste {name}! Main Kiva AI hoon. Batao kya haal hai?", parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Apna sawal seedha bhejo. Hard questions ke liye thinking process aayegi.", parse_mode="HTML")

async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_copy = f"Kiva AI\nFounder & Developer\n{OWNER_NAME}\nTelegram: @{OWNER_USERNAME}"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Copy Details", copy_text=CopyTextButton(text=owner_copy)),
        InlineKeyboardButton("Contact", url=OWNER_URL),
    ]])
    await update.message.reply_text(f"<b>Founder:</b> {html.escape(OWNER_NAME)} (@{html.escape(OWNER_USERNAME)})", parse_mode="HTML", reply_markup=keyboard)

async def send_answer(update, answer, citations=None):
    citations = citations or []
    formatted = format_telegram_html(answer)
    if citations:
        source_lines = ["", "<b>Sources</b>"]
        for title, url in citations[:6]:
            source_lines.append(f'• <a href="{html.escape(url, quote=True)}">{html.escape(title or "Source", quote=True)}</a>')
        formatted = formatted + "\n" + "\n".join(source_lines)

    for chunk in split_message(formatted):
        try:
            await update.message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(re.sub(r"<[^>]+>", "", chunk))

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    prompt = update.message.text.strip()
    if not prompt:
        return

    lock = get_user_lock(user.id)
    async with lock:
        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(typing_loop(context.bot, update.effective_chat.id, stop_event))
        thinking_msg = None
        try:
            is_complex = needs_thinking_process(prompt)
            if is_complex:
                thinking_msg = await update.message.reply_text("Thinking Process\n─────────────────")

            answer, citations, _ = await generate_text(user.id, prompt, get_display_name(user))

            if thinking_msg:
                try: await thinking_msg.delete()
                except: pass

            await send_answer(update, answer, citations)
        except Exception as exc:
            if thinking_msg:
                try: await thinking_msg.delete()
                except: pass
            logger.exception("Message processing failed")
            await update.message.reply_text("Arre yaar, abhi thoda technical issue aa gaya hai. Dobara try karo!")
        finally:
            stop_event.set()
            typing_task.cancel()

async def image_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    lock = get_user_lock(user.id)
    async with lock:
        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(typing_loop(context.bot, update.effective_chat.id, stop_event))
        try:
            image_bytes, mime_type = None, "image/jpeg"
            if update.message.photo:
                tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
                image_bytes = bytes(await tg_file.download_as_bytearray())
            elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
                tg_file = await context.bot.get_file(update.message.document.file_id)
                image_bytes = bytes(await tg_file.download_as_bytearray())
                mime_type = update.message.document.mime_type
            else:
                return

            prompt = update.message.caption or ""
            answer, citations = await generate_image_answer(user.id, prompt, get_display_name(user), image_bytes, mime_type)
            await send_answer(update, answer, citations)
        except Exception:
            logger.exception("Image processing failed")
            await update.message.reply_text("Image analyze karne me problem aayi, dobara bhejo.")
        finally:
            stop_event.set()
            typing_task.cancel()

web_app = Flask(__name__)

@web_app.get("/")
def home():
    return jsonify({"name": "Kiva AI", "status": "online"})

@web_app.get("/health")
def health():
    return jsonify({"status": "healthy"})

def run_web_server():
    web_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def main():
    logger.info("Starting Kiva AI...")
    threading.Thread(target=run_web_server, daemon=True).sub = True # just to be safe
    # start server thread properly
    threading.Thread(target=run_web_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("owner", owner_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_message))

    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
    
