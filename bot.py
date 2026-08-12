import telebot
import google.generativeai as genai
import os

BOT_TOKEN = os.environ.get("API_TOKEN")
GENAI_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GENAI_KEY)

bot = telebot.TeleBot(BOT_TOKEN)
model = genai.GenerativeModel('gemini-1.5-flash')

@bot.message_handler(commands=['start', 'help'])
def greetings(message):
    bot.reply_to(message, "Namaste! Main hoon Kiva AI. Aap mujhse kisi bhi tarah ka sawal pooch sakte hain.")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        response = model.generate_content(message.text)
        bot.reply_to(message, response.text)
    except Exception as e:
        bot.reply_to(message, "Maaf kijiye, kuch error aa gaya hai. Kripya dobara koshish karein.")

bot.infinity_polling()
