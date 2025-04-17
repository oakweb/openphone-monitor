print("\U0001F7E2 Final OpenPhone Monitor — HTML + SendGrid + media + GPT + full history")

import os
import json
import traceback
import requests
from flask import Flask, request, abort, render_template_string
from datetime import datetime
import sqlite3
from openai import OpenAI

app = Flask(__name__)

# --- Database Initialization ---
def init_db():
    conn = sqlite3.connect("messages.db")
    c = conn.cursor()

    # --- messages table ---
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp TEXT,
      phone_number TEXT,
      contact_name TEXT,
      direction TEXT,
      message TEXT,
      media_urls TEXT
    )
    """)

    # --- contacts table ---
    c.execute("""
    CREATE TABLE IF NOT EXISTS contacts (
      phone_number TEXT PRIMARY KEY,
      contact_name TEXT
    )
    """)

    conn.commit()
    conn.close()

# ←––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# RUN init_db() EXACTLY ONCE, ON THE FIRST REQUEST
first_request = True

@app.before_request
def _init_db_once():
    global first_request
    if first_request:
        init_db()
        first_request = False
# ←––––––––––––––––––––––––––––––––––––––––––––––––––––––––––

# --- Config ---
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
FROM_EMAIL = "phil@sincityrentals.com"
TO_EMAIL = "oakweb@gmail.com"
MY_NAME = "Me"
# temporarily hard-code OpenAI API key until env var is fixed# temporarily hard-code OpenAI API key until env var is fixed\urllib.parse
OPENAI_API_KEY = "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"
conversation_history = {}
MAX_HISTORY = 5

# --- Index Route ---
@app.route("/")
def index():
    return "🟢 OpenPhone Monitor is running."

# --- Format Message Bubbles ---
def format_bubble_html(messages):
    html = '<div style="font-family: Arial, sans-serif; background: #f4f4f4; padding: 16px;">'
    for msg in messages:
        is_outgoing = msg.get("direction") == "outgoing"
        sender = MY_NAME if is_outgoing else msg.get("display_name", msg.get("from"))
        time = msg.get("timestamp", "")
        body = msg.get("body", "").replace("\n", "<br>")
        media = msg.get("media", [])
        align = "right" if is_outgoing else "left"
        bubble_color = "#007BFF" if is_outgoing else "#EDEDED"

        html += f'''
        <div style="text-align:{align}; margin-bottom:12px;">
            <div style="font-size:0.75em; color:#555;">{sender} — {time}</div>
            <div style="display:inline-block; background:{bubble_color}; padding:10px 14px; border-radius:12px; max-width:75%; word-wrap:break-word;">
                {body}'''
        for m in media:
            if m.get("type", "").startswith("image/"):
                html += f'<br><img src="{m.get("url")}" alt="image" style="max-width:100%; border-radius:8px; margin-top:8px;">'
            else:
                html += f'<br><a href="{m.get("url")}" target="_blank" style="color:#007bff;">{m.get("type") or "Download File"}</a>'
        html += '</div></div>'
    html += '</div>'
    return html

# --- Send Email ---
def send_email(subject, html_body, plain_body):
    try:
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
        if response.status_code != 202:
            print(f"❌ SendGrid error {response.status_code}: {response.text}")
    except Exception:
        traceback.print_exc()

# --- Save Message to DB ---
def save_message_to_db(phone_number, direction, message, media_urls=[]):
    conn = sqlite3.connect("messages.db")
    c = conn.cursor()
    normalized = ''.join(filter(str.isdigit, phone_number or ""))
    contact_name = ""
    c.execute("SELECT contact_name FROM contacts WHERE phone_number LIKE ?", ('%' + normalized[-10:],))
    row = c.fetchone()
    if row:
        contact_name = row[0]

    c.execute(
        "INSERT INTO messages (timestamp, phone_number, contact_name, direction, message, media_urls) VALUES (?,?,?,?,?,?)",
        (datetime.utcnow().isoformat(), phone_number or "unknown", contact_name, direction, message, ",".join(media_urls))
    )
    conn.commit()
    conn.close()

# --- Webhook Endpoint ---
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True)
        et = payload.get("type") or payload.get("event")
        obj = payload.get("data", {}).get("object", {})
        dirn = 'incoming' if et == 'message.received' else 'outgoing'

        phone = obj.get("from") if dirn == 'incoming' else obj.get("to")
        text = obj.get("body", "")
        media = [m.get("fileUrl") for m in obj.get("media", [])] if obj.get("media") else []

        save_message_to_db(phone, dirn, text, media)

        if obj.get("object") == "message":
            cid = obj.get("conversationId")
            msg = {"id":obj.get("id"),"timestamp":obj.get("createdAt"),"direction":dirn,"from":phone,"to":obj.get("to"),"body":text,"media":obj.get("media",[]),"display_name":phone}
            conversation_history.setdefault(cid, []).append(msg)
            convo = conversation_history[cid][-MAX_HISTORY:]
            html = format_bubble_html(convo)
            plain = "\n\n".join(f"{m['direction']} — {m['body']}" for m in convo)
            cnum = phone[-10:]
            conn = sqlite3.connect("messages.db"); curs = conn.cursor()
            curs.execute("SELECT contact_name FROM contacts WHERE phone_number LIKE ?",('%'+cnum,))
            rn = curs.fetchone(); conn.close()
            name = rn[0] if rn else phone
            subj = f"New Message from {name} — {msg['id'][-6:]}"
            send_email(subj, html, plain)
    except Exception:
        traceback.print_exc()
        return "Webhook error",500
    return "OK",200

# --- Ask Route ---
@app.route("/ask", methods=["GET","POST"])
def ask():
    if request.method=="POST":
        prompt = request.form.get("prompt","")
        logs=""
        conn=sqlite3.connect("messages.db"); cur=conn.cursor()
        cur.execute("SELECT timestamp,contact_name,direction,message FROM messages ORDER BY timestamp DESC LIMIT 100")
        for ts,name,dirn,msg in reversed(cur.fetchall()):
            who=name if dirn=='incoming' else MY_NAME
            logs+=f"{who}@{ts}: {msg}\n"
        conn.close()

        full_prompt = (
            f"Conversation logs:\n{logs}\nSearch: {prompt}\n"
            "Find matching message plus 3 before and 2 after."
        )
        try:
            resp = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
                model="gpt-4", messages=[{"role":"system","content":"You analyze logs."},{"role":"user","content":full_prompt}]
            )
            answer=resp.choices[0].message.content
        except Exception as e:
            answer=f"Error: {e}"
        return render_template_string(
            """
            <form method='POST'><textarea name='prompt' required></textarea><button>Ask</button></form><div>{{answer}}</div>
            """,answer=answer)

    return render_template_string(
        """
        <form method='POST'><textarea name='prompt' required></textarea><button>Ask</button></form>
        """
    )

# --- Contacts Route ---
@app.route("/contacts", methods=["GET","POST"])
def contacts():
    conn=sqlite3.connect("messages.db"); cur=conn.cursor()
    if request.method=="POST":
        p=request.form['phone']; n=request.form['name']
        cur.execute("INSERT OR REPLACE INTO contacts(phone_number,contact_name) VALUES(?,?)",(p,n)); conn.commit()
    cur.execute("SELECT phone_number,contact_name FROM contacts ORDER BY contact_name")
    saved=cur.fetchall()
    cur.execute("SELECT DISTINCT phone_number FROM messages WHERE phone_number NOT IN(SELECT phone_number FROM contacts) ORDER BY timestamp DESC LIMIT 5")
    unknowns=[r[0] for r in cur.fetchall()]
    conn.close()
    return render_template_string(
        """
        <h2>📇 Contacts</h2><ul>{% for ph,nm in saved %}<li>{{nm}}: {{ph}}</li>{% endfor %}</ul>
        <h3>Unknowns</h3><ul>{% for x in unknowns %}<li>{{x}}</li>{% endfor %}</ul>
        """,saved=saved,unknowns=unknowns)

# --- Messages Route ---
@app.route("/messages")
def messages():
    conn=sqlite3.connect("messages.db"); cur=conn.cursor()
    cur.execute("SELECT timestamp,contact_name,direction,message FROM messages ORDER BY timestamp DESC LIMIT 50")
    rows=cur.fetchall(); conn.close(); rows.reverse()
    return render_template_string("<ul>{% for t,n,d,m in rows %}<li>{{n}}({{d}})@{{t}}:{{m}}</li>{% endfor %}</ul>",rows=rows)

# --- Start ---
if __name__=="__main__": app.run(host="0.0.0.0",port=8080)
