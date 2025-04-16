print("🟢 Final OpenPhone Monitor — HTML + SendGrid + media + both directions")

import os
import json
import traceback
import requests
from flask import Flask, request, abort
from datetime import datetime

app = Flask(__name__)

# --- Config ---
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
FROM_EMAIL = "phil@sincityrentals.com"
TO_EMAIL = "oakweb@gmail.com"
MY_NAME = "Me"
conversation_history = {}
MAX_HISTORY = 5

# --- Format HTML Message Bubbles ---
def format_bubble_html(messages):
    html = '<div style="font-family: Arial, sans-serif; background: #f4f4f4; padding: 16px;">'
    for msg in messages:
        is_outgoing = msg.get("direction") == "outgoing"
        sender = MY_NAME if is_outgoing else msg.get("display_name", msg.get("from"))
        time = msg.get("timestamp")
        body = msg.get("body", "").replace("\n", "<br>")
        media = msg.get("media", [])
        align = "right" if is_outgoing else "left"
        bubble_color = "#DCF8C6" if is_outgoing else "#F1F0F0"

        html += f'''
        <div style="text-align:{align}; margin-bottom:12px;">
            <div style="font-size:0.75em; color:#555;">{sender} — {time}</div>
            <div style="display:inline-block; background:{bubble_color}; padding:10px 14px; border-radius:12px; max-width:75%; word-wrap:break-word;">
                {body}
        '''

        for m in media:
            if m.get("type", "").startswith("image/"):
                html += f'<br><img src="{m.get("url")}" alt="image" style="max-width:100%; border-radius:8px; margin-top:8px;">'
            else:
                html += f'<br><a href="{m.get("url")}" target="_blank" style="color:#007bff;">{m.get("type") or "Download File"}</a>'

        html += '</div></div>'
    html += '</div>'
    return html

# --- Send Email via SendGrid ---
def send_email(subject, html_body, plain_body):
    try:
        print("📧 Sending via SendGrid...")
        payload = {
            "personalizations": [{"to": [{"email": TO_EMAIL}], "subject": subject}],
            "from": {"email": FROM_EMAIL},
            "content": [
                {"type": "text/plain", "value": plain_body},
                {"type": "text/html", "value": html_body}
            ]
        }
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload
        )
        if response.status_code == 202:
            print("✅ Email accepted by SendGrid.")
        else:
            print(f"❌ SendGrid error {response.status_code}: {response.text}")
    except Exception as e:
        print("❌ SendGrid error:")
        traceback.print_exc()

# --- Webhook Endpoint ---
@app.route("/webhook", methods=["POST"])
def webhook():
    print("\n📬 Webhook hit.")
    try:
        event = json.loads(request.data)
        event_type = event.get("type")
        obj = event.get("data", {}).get("object", {})
        direction = obj.get("direction", "unknown")

        print(f"📦 Event: {event_type}, Direction: {direction}")

        if event_type in ["message.received", "message.delivered"] and obj.get("object") == "message":
            convo_id = obj.get("conversationId")
            msg = {
                "id": obj.get("id"),
                "timestamp": obj.get("createdAt"),
                "direction": direction,
                "from": obj.get("from"),
                "to": obj.get("to"),
                "body": obj.get("body", ""),
                "media": obj.get("media", []),
                "display_name": obj.get("from")
            }

            if convo_id not in conversation_history:
                conversation_history[convo_id] = []
            conversation_history[convo_id].append(msg)
            conversation_history[convo_id] = conversation_history[convo_id][-MAX_HISTORY:]

            messages = conversation_history[convo_id]
            html = format_bubble_html(messages)
            plain = "\n\n".join([f"{m['direction']} — {m['body']}" for m in messages])
            subject = f"New OpenPhone Message ({direction}) — {msg.get('from')}"

            send_email(subject.strip(), html.strip(), plain.strip())
        else:
            print("⚠️ Skipped non-message event.")
    except Exception as e:
        print("🚨 Webhook error:")
        traceback.print_exc()
        abort(400)

    return "OK", 200

@app.route("/")
def index():
    return "🟢 OpenPhone HTML Monitor is live."

if __name__ == "__main__":
    print("🚀 Starting OpenPhone Monitor App")
    app.run(host="0.0.0.0", port=8080)
