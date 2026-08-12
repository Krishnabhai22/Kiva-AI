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
# KIVA AI • TELEGRAM INTELLIGENCE BOT
# ============================================================

TOKEN = os.environ.get("API_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

BOT_NAME = "KIVA AI"
BOT_VERSION = "2.0 ULTRA SECURE"

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
gemini_model = genai.GenerativeModel("gemini-pro")

# ============================================================
# FLASK KEEP-ALIVE SERVER (FOR RENDER FREE WEB SERVICE)
# ============================================================

@app.route("/")
def home():
    return "KIVA-AI BOT ENGINE • ONLINE"

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
                first_name TEXT
            )
        """)
        connection.commit()
        connection.close()

def register_user(user_id, first_name):
    with db_lock:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO bot_users (user_id, first_name)
            VALUES (?, ?)
        """, (user_id, first_name))
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
    if message.from_user:
        register_user(message.from_user.id, message.from_user.first_name)
    
    welcome_text = (
        f"<b>🤖 {BOT_NAME} • COMMAND CENTER</b>\n"
        "────────────────────────\n"
        "Welcome! I am your advanced AI assistant powered by Gemini.\n\n"
        "● <b>Status:</b> ONLINE\n"
        f"● <b>Version:</b> {BOT_VERSION}\n\n"
        "<i>Send me any question or prompt, and I will generate a response for you!</i>"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="HTML")

@bot.message_handler(func=lambda message: message.from_user and not message.from_user.is_bot, content_types=["text"])
def handle_ai_messages(message):
    user_prompt = message.text.strip()
    if not user_prompt:
        return

    # Typing action show karein
    try:
        bot.send_chat_action(message.chat.id, 'typing')
    except Exception:
        pass

    try:
        response = gemini_model.generate_content(user_prompt)
        ai_reply = response.text if response and response.text else "Sorry, I couldn't generate a response."
    except Exception as e:
        ai_reply = f"An error occurred while communicating with AI: {e}"

    # Telegram message limit handle karne ke liye (max 4096 chars)
    if len(ai_reply) > 4000:
        ai_reply = ai_reply[:4000] + "\n\n<i>[Response truncated due to length]</i>"

    try:
        bot.reply_to(message, ai_reply, parse_mode="Markdown")
    except Exception:
        # Fallback agar markdown fail ho jaye
        try:
            bot.reply_to(message, ai_reply)
        except Exception as err:
            print(f"Failed to send reply: {err}")

# ============================================================
# BOT BOOTSTRAP
# ============================================================

if __name__ == "__main__":
    print("========================================")
    print("        KIVA-AI INTELLIGENCE BOT")
    print("========================================")

    init_database()

    # Flask server ko background thread me chalana (Render Free Tier ke liye zaroori hai)[span_3](start_span)[span_3](end_span)
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
            
