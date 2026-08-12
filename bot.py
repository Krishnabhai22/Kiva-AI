import os
import re
import sqlite3
import threading
import html
import time
import uuid
from datetime import datetime, timedelta

import telebot
from telebot import types
from flask import Flask
import google.generativeai as genai

# ============================================================
# KIVA AI • ADVANCED ENTERPRISE INTELLIGENCE BOT
# ============================================================

TOKEN = os.environ.get("API_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

BOT_NAME = "KIVA AI"
BOT_VERSION = "3.0 ULTRA PROFESSIONAL"

OWNER_IDS = {
    1332494807
}

DB_FILE = "kiva_ai.db"

app = Flask(__name__)
db_lock = threading.Lock()
start_time = time.time()

# ============================================================
# TOKEN VALIDATION & INITIALIZATION
# ============================================================

if not TOKEN:
    raise RuntimeError("API_TOKEN environment variable is missing.")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is missing.")

bot = telebot.TeleBot(TOKEN, parse_mode=None)
genai.configure(api_key=GEMINI_API_KEY)

# System instruction to make it behave like a professional standalone AI (Kiva AI)
SYSTEM_INSTRUCTION = (
    "You are Kiva AI, an advanced, highly intelligent, and professional AI assistant "
    "created by Krishna. You provide clean, detailed, and accurate answers. "
    "You seamlessly detect and match the user's language—whether it is Hinglish, Hindi, "
    "English, or any other global language—and reply in the exact same language and tone. "
    "Never mention Google or Gemini; you are entirely Kiva AI."
)

generation_config = {
    "temperature": 0.7,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

ai_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    generation_config=generation_config,
    system_instruction=SYSTEM_INSTRUCTION
)

# Store chat sessions per user for conversational memory
user_sessions = {}

# ============================================================
# FLASK KEEP-ALIVE SERVER (FOR RENDER FREE WEB SERVICE)
# ============================================================

@app.route("/")
def home():
    return "KIVA-AI ADVANCED ENGINE • ONLINE"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ============================================================
# DATABASE MANAGEMENT
# ============================================================

def get_connection():
    return sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)

def init_database():
    with db_lock:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT
            )
        """)
        connection.commit()
        connection.close()

def register_user(user_id, first_name, username):
    with db_lock:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO bot_users (user_id, first_name, username)
            VALUES (?, ?, ?)
        """, (user_id, first_name, username))
        connection.commit()
        connection.close()

def get_total_users():
    with db_lock:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM bot_users")
        count = cursor.fetchone()[0]
        connection.close()
        return count

# ============================================================
# TELEGRAM HANDLERS
# ============================================================

@bot.message_handler(commands=["start", "help"])
def start_command(message):
    user = message.from_user
    if not user:
        return
        
    register_user(user.id, user.first_name, user.username)
    
    name = html.escape(user.first_name or "User")
    welcome_text = (
        f"<b>✨ Hii {name}! Welcome to {BOT_NAME}</b>\n"
        "────────────────────────\n"
        "I am your advanced, high-performance professional AI assistant. "
        "You can chat with me about anything, ask questions, write code, solve problems, "
        "or converse in any language (Hinglish, Hindi, English, etc.)!\n\n"
        "● <b>Status:</b> ONLINE & ACTIVE\n"
        f"● <b>Engine:</b> {BOT_VERSION}\n\n"
        "<i>What would you like to discuss today? Just type your prompt below!</i>"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="HTML")

@bot.message_handler(func=lambda message: message.from_user and not message.from_user.is_bot, content_types=["text"])
def handle_ai_messages(message):
    user = message.from_user
    user_id = user.id
    user_prompt = message.text.strip()
    
    if not user_prompt:
        return

    register_user(user_id, user.first_name, user.username)

    try:
        bot.send_chat_action(message.chat.id, 'typing')
    except Exception:
        pass

    try:
        # Maintain separate conversational chat history for each user (ChatGPT style memory)
        if user_id not in user_sessions:
            user_sessions[user_id] = ai_model.start_chat(history=[])
        
        chat_session = user_sessions[user_id]
        response = chat_session.send_message(user_prompt)
        ai_reply = response.text if response and response.text else "I am processing your request. Could you please rephrase?"
    except Exception as e:
        # If chat session errors out, reset session and try a direct generation fallback
        try:
            if user_id in user_sessions:
                del user_sessions[user_id]
            fallback_response = ai_model.generate_content(user_prompt)
            ai_reply = fallback_response.text if fallback_response and fallback_response.text else "An error occurred."
        except Exception as err:
            ai_reply = f"System Error: Unable to process response right now. Please try again later."

    # Handle Telegram max message length limit (4096 characters)
    if len(ai_reply) > 4000:
        ai_reply = ai_reply[:4000] + "\n\n<i>[Response truncated due to length limits]</i>"

    try:
        bot.reply_to(message, ai_reply, parse_mode="Markdown")
    except Exception:
        # Fallback without markdown formatting if symbols clash
        try:
            bot.reply_to(message, ai_reply)
        except Exception as final_err:
            print(f"Failed to send reply: {final_err}")

# ============================================================
# BOT BOOTSTRAP
# ============================================================

if __name__ == "__main__":
    print("========================================")
    print("      KIVA-AI PROFESSIONAL ENGINE")
    print("========================================")

    init_database()

    threading.Thread(target=run_flask, daemon=True).start()

    print("KIVA-AI ENGINE is ONLINE.")

    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        pass

    while True:
        try:
            bot.polling(non_stop=True, interval=1, timeout=30, skip_pending=True)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(3)
            
