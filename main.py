import os
import traceback
import base64
import requests
import uuid
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from datetime import datetime
from flask import Flask, request, Response, render_template_string, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect
from openai import OpenAI
from dotenv import load_dotenv
from sqlalchemy.exc import OperationalError

# ─── Load environment variables ───────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SMTP_HOST        = os.getenv("SMTP_HOST")
SMTP_PORT        = os.getenv("SMTP_PORT")
SMTP_USER        = os.getenv("SMTP_USER")
SMTP_PASS        = os.getenv("SMTP_PASS")
FROM_EMAIL       = os.getenv("FROM_EMAIL", "phil@sincityrentals.com")
TO_EMAIL         = os.getenv("TO_EMAIL", "oakweb@gmail.com")
MY_NAME          = os.getenv("MY_NAME", "Me")
DATABASE_URL     = os.getenv("DATABASE_URL", "sqlite:///messages.db")
EMAIL_PROVIDER   = os.getenv("EMAIL_PROVIDER", "SMTP").strip().upper()

# ─── Flask & Database Setup ─────────────────────────────────────────────
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if DATABASE_URL.startswith('postgresql'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "connect_args": {"sslmode": "require"}
    }
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True}

db = SQLAlchemy(app)

# ─── Models ─────────────────────────────────────────────────────────────
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

# ─── Schema Migration ───────────────────────────────────────────────────
with app.app_context():
    engine = db.engine
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if 'messages' in tables and 'message' not in tables:
        db.session.execute(text("ALTER TABLE messages RENAME TO message"))
    if 'contacts' in tables and 'contact' not in tables:
        db.session.execute(text("ALTER TABLE contacts RENAME TO contact"))
    if 'media' in tables:
        db.session.execute(text("DROP TABLE media"))
    db.session.commit()
    db.create_all()
    cols = [c['name'] for c in inspector.get_columns('message')]
    if 'media_urls' not in cols:
        db.session.execute(text("ALTER TABLE message ADD COLUMN media_urls TEXT"))
        db.session.commit()

# ─── Initialize OpenAI client ────────────────────────────────────────────
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ─── Email HTML Wrapper ──────────────────────────────────────────────────
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
</body></html>
"""  # noqa: W605

# ─── Email Providers ─────────────────────────────────────────────────────
def _send_via_sendgrid(subject, html_body, plain_body, attachments):
    print("DEBUG: _send_via_sendgrid()", flush=True)
    if not SENDGRID_API_KEY:
        print("⚠️ No SendGrid API key configured. Skipping.", flush=True)
        return
    payload = {
        "personalizations": [{"to": [{"email": TO_EMAIL}], "subject": subject}],
        "from": {"email": FROM_EMAIL},
        "content": [
            {"type": "text/plain", "value": plain_body},
            {"type": "text/html",  "value": html_body}
        ]
    }
    if attachments:
        payload["attachments"] = [
            {"content": att['content'], "type": att['type'], "filename": att['filename']} for att in attachments
        ]
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
        json=payload
    )
    print(f"DEBUG: sendgrid response {r.status_code}", flush=True)
    if r.status_code != 202:
        print(f"❌ SendGrid error {r.status_code}: {r.text}", flush=True)
def _send_via_smtp(subject, html_body, plain_body, attachments):
    print("DEBUG: Entered _send_via_smtp", flush=True)
    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS):
        print("⚠️ Missing SMTP config. Aborting.", flush=True)
        return
    try:
        print("DEBUG: Building MIME message...", flush=True)
        msg = MIMEMultipart('related')
        msg['From'] = FROM_EMAIL
        msg['To'] = TO_EMAIL
        msg['Subject'] = subject
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        alt.attach(MIMEText(plain_body, 'plain'))
        alt.attach(MIMEText(html_body, 'html'))
        print("DEBUG: MIME parts attached", flush=True)
        print(f"DEBUG: Connecting SMTP {SMTP_HOST}:{SMTP_PORT}", flush=True)
        server = smtplib.SMTP(SMTP_HOST, int(SMTP_PORT), timeout=15)
        server.set_debuglevel(1)
        print(f"DEBUG: Envelope from={FROM_EMAIL}, to={TO_EMAIL}", flush=True)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(FROM_EMAIL, TO_EMAIL, msg.as_string())
        print("✅ Email sent via SMTP", flush=True)
        server.quit()
    except Exception as e:
        print(f"❌ SMTP Error: {e}", flush=True)
        traceback.print_exc()
def send_email(subject, html_body, plain_body, attachments=None):
    print(f"DEBUG: send_email subject={subject}", flush=True)
    print(f"DEBUG: EMAIL_PROVIDER={EMAIL_PROVIDER}", flush=True)
    if attachments is None:
        attachments = []
    final_html = wrap_email_html(html_body)
    print(f"DEBUG: HTML len={len(final_html)}", flush=True)
    if EMAIL_PROVIDER == 'SMTP':
        _send_via_smtp(subject, final_html, plain_body, attachments)
    elif EMAIL_PROVIDER == 'SENDGRID':
        _send_via_sendgrid(subject, final_html, plain_body, attachments)
    else:
        print(f"⚠️ Invalid EMAIL_PROVIDER {EMAIL_PROVIDER}", flush=True)
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        obj = data.get('data', {}).get('object', {})
        direction = 'incoming' if data.get('type', '').endswith('received') else 'outgoing'
        phone = obj.get('from') or obj.get('to')
        text = obj.get('body') or ''
        media_items = obj.get('media', [])
        key = phone[-10:]
        try:
            contact = Contact.query.get(key)
        except OperationalError:
            db.session.rollback()
            contact = None
        name = contact.contact_name if contact else phone
        urls = [m.get('url') for m in media_items if m.get('url')]
        attachments = []
        for url in urls:
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                b64 = base64.b64encode(r.content).decode()
                ctype = r.headers.get('Content-Type', 'application/octet-stream')
                fname = url.split('/')[-1]
                attachments.append({'content': b64, 'type': ctype, 'filename': fname, 'content_id': uuid.uuid4().hex})
            except Exception as e:
                print(f"⚠️ Fetch error {url}: {e}", flush=True)
        msg = Message(phone_number=phone, contact_name=name, direction=direction, message=text, media_urls=','.join(urls))
        db.session.add(msg)
        db.session.commit()
        convo = Message.query.filter_by(phone_number=phone).order_by(Message.id.desc()).limit(5).all()[::-1]
        plain_parts = [f"{m.contact_name or m.phone_number} — {m.timestamp}: {m.message}" for m in convo]
        html_parts = [f"<p><b>{m.contact_name or m.phone_number}</b>: {m.message}</p>" for m in convo]
        plain = "\n\n".join(plain_parts)
        htmlb = "".join(html_parts)
        subject = f"New message {'to' if direction=='outgoing' else 'from'} {name}"
        send_email(subject, htmlb, plain, attachments)
    except Exception:
        traceback.print_exc()
    return Response(status=200)
@app.route('/')
def index():
    return '🟢 Running'
@app.route('/messages')
def messages_view():
    msgs = Message.query.order_by(Message.id.desc()).limit(100).all()
    html = '<h2>Last 100 Messages</h2><ul>'
    for m in msgs:
        html += f"<li>[{m.timestamp}] {m.contact_name or m.phone_number}: {m.message}</li>"
    html += '</ul>'
    return html
@app.route('/contacts')
def contacts_view():
    nums = {m.phone_number[-10:] for m in Message.query.order_by(Message.id.desc()).limit(20)}
    html = '<h2>Recent Contacts</h2><ul>'
    for num in nums:
        c = Contact.query.get(num)
        name = c.contact_name if c else num
        html += f"<li>{name}: {num}</li>"
    html += '</ul>'
    return html
@app.route('/contacts/add', methods=['GET','POST'])
def add_contact():
    if request.method == 'POST':
        pn = request.form['phone'][-10:]
        name = request.form['name']
        db.session.merge(Contact(phone_number=pn, contact_name=name))
        db.session.commit()
        return redirect(url_for('contacts_view'))
    return render_template_string('''
<h2>Add Contact</h2>
<form method=post>
  <input name="phone" placeholder="Phone#…"><br>
  <input name="name" placeholder="Name…"><br>
  <button>Add</button>
</form>
''')
@app.route('/ask', methods=['GET','POST'])
def ask_view():
    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        recent = Message.query.order_by(Message.id.desc()).limit(20).all()[::-1]
        convo = "\n".join(f"{m.contact_name or m.phone_number}: {m.message}" for m in recent)
        prompt = f"You are..."  # truncated for brevity
        resp = openai_client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}])
        answer = resp.choices[0].message.content
        return render_template_string('<pre>{{answer}}</pre>', answer=answer)
    return render_template_string('''
<h2>Search</h2><form method=post><input name="query"><button>Go</button></form>
''')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
