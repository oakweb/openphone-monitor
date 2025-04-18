import os
import traceback
import base64
import requests
import mimetypes
from datetime import datetime
from flask import Flask, request, Response, render_template_string, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect
from openai import OpenAI
from dotenv import load_dotenv

# ─── Load environment variables ────────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL       = os.getenv("FROM_EMAIL", "phil@sincityrentals.com")
TO_EMAIL         = os.getenv("TO_EMAIL", "oakweb@gmail.com")
MY_NAME          = os.getenv("MY_NAME", "Me")
DATABASE_URL     = os.getenv("DATABASE_URL", "sqlite:///messages.db")

# ─── Flask & Database Setup ────────────────────────────────────────────────
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ─── Models ────────────────────────────────────────────────────────────────
class Contact(db.Model):
    __tablename__ = 'contact'
    phone_number = db.Column(db.String, primary_key=True)
    contact_name = db.Column(db.String, nullable=False)

class Message(db.Model):
    __tablename__ = 'message'
    id           = db.Column(db.Integer, primary_key=True)
    timestamp    = db.Column(db.DateTime, default=datetime.utcnow)
    phone_number = db.Column(db.String, nullable=False)
    contact_name = db.Column(db.String, nullable=True)
    direction    = db.Column(db.String, nullable=False)
    message      = db.Column(db.Text)
    media_urls   = db.Column(db.Text)

# ─── Schema Migration at Startup ───────────────────────────────────────────
with app.app_context():
    engine = db.engine
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    # Rename old plural tables if needed
    if 'messages' in tables and 'message' not in tables:
        db.session.execute(text("ALTER TABLE messages RENAME TO message"))
    if 'contacts' in tables and 'contact' not in tables:
        db.session.execute(text("ALTER TABLE contacts RENAME TO contact"))
    # Drop stray media table
    if 'media' in tables:
        db.session.execute(text("DROP TABLE media"))
    db.session.commit()
    # Create missing tables
    db.create_all()
    # Ensure media_urls column exists
    cols = [c['name'] for c in inspector.get_columns('message')] if 'message' in inspector.get_table_names() else []
    if 'media_urls' not in cols:
        db.session.execute(text("ALTER TABLE message ADD COLUMN media_urls TEXT"))
        db.session.commit()

# ─── Initialize OpenAI client ──────────────────────────────────────────────
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ─── Email Helpers ────────────────────────────────────────────────────────
def wrap_email_html(inner: str) -> str:
    year = datetime.utcnow().year
    return f"""
<!DOCTYPE html>
<html lang=\"en\"><head><meta charset=\"utf-8\">
  <style>
    body {{ margin:0; padding:20px; font-family:Arial,sans-serif; background:#f4f4f4; }}
    .container {{ max-width:600px; margin:auto; background:#fff; padding:20px; border-radius:8px; }}
    img {{ max-width:100%; border-radius:8px; margin-top:8px; }}
    .footer {{ font-size:0.75em; color:#888; text-align:center; margin-top:2em; }}
    a {{ color:#6B30FF; text-decoration:none; }}
  </style>
</head><body>
  <div class=\"container\">
    <h1>OpenPhone Notification</h1>
    {inner}
    <div class=\"footer\">© {year}</div>
  </div>
</body></html>"""

def send_email(subject: str, html_body: str, plain_body: str, attachments=None):
    print(f"DEBUG: send_email called subject={subject}", flush=True)
    if attachments is None:
        attachments = []
    if not SENDGRID_API_KEY:
        print("⚠️ No SendGrid API key, skipping email.", flush=True)
        return
    payload = {
        "personalizations": [{"to": [{"email": TO_EMAIL}], "subject": subject}],
        "from": {"email": FROM_EMAIL},
        "content": [
            {"type": "text/plain", "value": plain_body},
            {"type": "text/html",  "value": wrap_email_html(html_body)}
        ],
        "attachments": attachments
    }
    print(f"DEBUG: payload={payload}", flush=True)
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload
        )
        print(f"DEBUG: sendgrid response {resp.status_code}", flush=True)
        if resp.status_code == 202:
            print("✅ Email sent with attachments.", flush=True)
        else:
            print(f"❌ SendGrid error {resp.status_code}: {resp.text}", flush=True)
    except Exception:
        traceback.print_exc()

# ─── Webhook Route ─────────────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    print(f"DEBUG: webhook payload={data}", flush=True)
    obj = data.get('data', {}).get('object', {})
    direction = 'incoming' if data.get('type') == 'message.received' else 'outgoing'
    phone = obj.get('from') if direction == 'incoming' else obj.get('to')
    text = obj.get('body', '')
    media_items = obj.get('media', []) or []
    print(f"DEBUG: media_items={media_items}", flush=True)
    urls = [m.get('url') or m.get('fileUrl') for m in media_items if m.get('url') or m.get('fileUrl')]
    print(f"DEBUG: resolved urls={urls}", flush=True)

    key = phone[-10:]
    contact = Contact.query.get(key)
    name = contact.contact_name if contact else phone

    msg = Message(
        phone_number=phone,
        contact_name=name,
        direction=direction,
        message=text,
        media_urls=','.join(urls)
    )
    db.session.add(msg)
    db.session.commit()

    # Build email content and inline attachments
    plain_parts = [f"{name} — {datetime.utcnow()}: {text}"]
    html_parts = [f"<p><b>{name}</b>: {text}</p>"]
    attachments = []
    cid_index = 0
    for url in urls:
        try:
            r = requests.get(url); r.raise_for_status()
            data_b64 = base64.b64encode(r.content).decode('utf-8')
            mime_type = mimetypes.guess_type(url)[0] or 'application/octet-stream'
            filename = os.path.basename(url)
            cid = f"image{cid_index}"; cid_index += 1
            attachments.append({
                "content": data_b64,
                "filename": filename,
                "type": mime_type,
                "disposition": "inline",
                "content_id": cid
            })
            html_parts.append(f"<img src='cid:{cid}' style='max-width:300px;margin-top:5px;'><br>")
            plain_parts.append(f"[Image CID: {cid}]")
        except Exception as e:
            print(f"⚠️ Failed to fetch {url}: {e}", flush=True)

    subject = f"New message {('from' if direction=='incoming' else 'to')} {name}"
    plain = "\n\n".join(plain_parts)
    htmlb = "".join(html_parts)
    send_email(subject, htmlb, plain, attachments)
    return Response(status=200)

# ─── HTTP Views ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string("""
🟢 OpenPhone Monitor is running.<br>
<a href="/messages">Messages</a> | <a href="/contacts">Contacts</a> | <a href="/contacts/add">Add Contact</a> | <a href="/ask">Ask GPT</a>
"""
)

@app.route('/messages')
def messages_view():
    msgs = Message.query.order_by(Message.id.desc()).limit(100).all()
    return render_template_string("""
<h2>Last 100 Messages</h2>
<ul>
{% for m in msgs %}
  <li>[{{m.timestamp}}] {{m.contact_name or m.phone_number}} ({{ '📤' if m.direction=='outgoing' else '📥' }}): {{m.message}}</li>
{% endfor %}
</ul>
""", msgs=msgs)

@app.route('/contacts')
def contacts_view():
    contacts = Contact.query.all()
    known = {c.phone_number for c in contacts}
    all_nums = {pn for (pn,) in db.session.query(Message.phone_number.distinct())}
    unknown = sorted(all_nums - known)
    return render_template_string("""
<h2>Contacts</h2>
<h3>Known</h3>
<ul>
{% for c in contacts %}
  <li>{{c.contact_name}}: {{c.phone_number}}</li>
{% endfor %}
</ul>
{% if unknown %}
<h3>Unknown</h3>
<ul>
{% for num in unknown %}
  <li>{{num}} (<a href=\"/contacts/add?phone={{num}}\">Add</a>)</li>
{% endfor %}
</ul>
{% endif %}
<p><a href=\"/contacts/add\">Add new contact</a></p>
""", contacts=contacts, unknown=unknown)

@app.route('/contacts/add', methods=['GET','POST'])
def contacts_add():
    prefill = request.args.get('phone', '')
    if request.method == 'POST':
        num  = request.form.get('phone', '').strip()
        name = request.form.get('name', '').strip()
        if num and name:
            key = num[-10:]
            db.session.merge(Contact(phone_number=key, contact_name=name))
            db.session.commit()
            return redirect(url_for('contacts_view'))
        return 'Phone and name required', 400
    return render_template_string("""
<h2>Add Contact</h2>
<form method=\"post\">
  <input name=\"name\" placeholder=\"Contact Name\" required/><br>
  <input name=\"phone\" placeholder=\"Phone Number\" value=\"{{prefill}}\" required/><br>
  <button type=\"submit\">Add</button>
</form>
""", prefill=prefill)

@app.route('/ask', methods=['GET','POST'])
def ask_view():
    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        if not query or not openai_client:
            return "Provide a search term and ensure OpenAI is configured.", 400
        recent = Message.query.order_by(Message.id.desc()).limit(20).all()[::-1]
        convo = "\n".join(f"{m.contact_name or m.phone_number} ({'out' if m.direction=='outgoing' else 'in'}): {m.message}" for m in recent)
        prompt = f"You are a message-search assistant.\nRecent conversation:\n{convo}\n\nFind the first message containing '{query}', plus two before and one after."
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}]
        )
        answer = resp.choices[0].message.content
        return render_template_string("""
<h2>Results</h2>
<pre>{{answer}}</pre>
<p><a href=\"/ask\">Back</a></p>
""", answer=answer)
    return render_template_string("""
<h2>Search Messages</h2>
<form method=\"post\">
  <input name=\"query\" placeholder=\"Keyword…\"/><button type=\"submit\">Search</button>
</form>
"""
)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
