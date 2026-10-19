import os
import requests
from flask import Flask, request as flask_request

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

@app.route('/')
def health_check():
    return "Trio Bot is alive and running!", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = flask_request.get_json()
    if update and "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_text = msg.get("text", "")

        # Send acknowledgment
        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={
            "chat_id": chat_id,
            "text": "Processing across the trio..."
        })

        # Send final formatted response
        final_output = (
            f"**Trio Synchronized Response:**\n\n"
            f"*Command Received:* {user_text}\n\n"
            f"*(Live API routing active)*"
        )
        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={
            "chat_id": chat_id,
            "text": final_output,
            "parse_mode": "Markdown"
        })

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
