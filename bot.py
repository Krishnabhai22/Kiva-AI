import os
import re
import sqlite3
import threading
import html
import time

import telebot
from flask import Flask
import google.generativeai as genai

# ============================================================
# KIVA AI • ADVANCED ENTERPRISE INTELLIGENCE BOT
# ============================================================

TOKEN = os.environ.get("API_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

BOT_NAME = "KIVA AI"
BOT_VERSION = "3.2 ULTRA PROFESSIONAL"

DB_FILE = "kiva_ai.db"

app = Flask(__name__)
db_lock = threading.Lock()

if not TOKEN:
    raise RuntimeError("API_TOKEN environment variable is missing.")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is missing.")

bot = telebot.TeleBot(TOKEN, parse_mode=None)
genai.configure(api_key=GEMINI_API_KEY)

ai_model = genai.GenerativeModel("gemini-1.5-flash")

@app.route("/")
def home():
    return "KIVA-AI ADVANCED ENGINE • ONLINE"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

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
        "You can chat with me about anything, write code, solve problems, "
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
        # Professional persona prompt bundled directly with user message to ensure zero errors
        full_prompt = (
            "You are Kiva AI, an advanced professional AI assistant. "
            "Never mention Google, Gemini, or any underlying model provider. "
            "Match and reply in the user's exact language and tone (Hinglish, Hindi, English, etc.).\n\n"
            f"User Prompt: {user_prompt}"
        )
        
        response = ai_model.generate_content(full_prompt)
        ai_reply = response.text if response and response.text else "I am processing your request. Could you please rephrase?"
    except Exception as e:
        ai_reply = f"Kiva AI Engine Error: {e}"

    if len(ai_reply) > 4000:
        ai_reply = ai_reply[:4000] + "\n\n<i>[Response truncated due to length limits]</i>"

    try:
        bot.reply_to(message, ai_reply, parse_mode="Markdown")
    except Exception:
        try:
            bot.reply_to(message, ai_reply)
        except Exception as final_err:
            print(f"Failed to send reply: {final_err}")

if __name__ == "__main__":
    init_database()
    threading.Thread(target=run_flask, daemon=True).start()
    
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
            
