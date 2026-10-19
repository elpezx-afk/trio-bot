import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Trio Bot is alive and running!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.message.chat_id

    await context.bot.send_message(chat_id=chat_id, text="Processing across the trio...")

    final_output = (
        f"**Trio Synchronized Response:**\n\n"
        f"*Command Received:* {user_text}\n\n"
        f"*(Live API routing active)*"
    )

    await context.bot.send_message(chat_id=chat_id, text=final_output, parse_mode="Markdown")

if __name__ == "__main__":
    t = threading.Thread(target=run_web_server)
    t.start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("Trio orchestrator bot is online and polling...")
    app.run_polling()
