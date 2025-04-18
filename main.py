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
from flask import Flask, request, render_template, redirect, url_for, Response, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect
from openai import OpenAI
from dotenv import load_dotenv
from sqlalchemy.exc import OperationalError

# ─── Load environment variables ───────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
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

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "secret")
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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

# ─── Initialize OpenAI client ────────────────────────────────────────────
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ─── Email Sending ───────────────────────────────────────────────────────
def send_email(subject, html_body, plain_body, attachments=None):
    if attachments is None:
        attachments = []
    final_html = wrap_email_html(html_body)
    if EMAIL_PROVIDER == 'SMTP':
        _send_via_smtp(subject, final_html, plain_body, attachments)
    elif EMAIL_PROVIDER == 'SENDGRID':
        _send_via_sendgrid(subject, final_html, plain_body, attachments)
    else:
        print("⚠️ Invalid EMAIL_PROVIDER", flush=True)

def wrap_email_html(inner: str) -> str:
    year = datetime.utcnow().year
    return f"""<html><body><div style='font-family:sans-serif;padding:20px;'>
        <h2>OpenPhone Message Notification</h2>
        {inner}
        <hr><small>© {year}</small></div></body></html>"""

# ─── SMTP and SendGrid Providers ─────────────────────────────────────────
def _send_via_smtp(subject, html_body, plain_body, attachments):
    try:
        server = smtplib.SMTP(SMTP_HOST, int(SMTP_PORT), timeout=15)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        msg = MIMEMultipart()
        msg['From'] = FROM_EMAIL
        msg['To'] = TO_EMAIL
        msg['Subject'] = subject
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        alt.attach(MIMEText(plain_body, 'plain'))
        alt.attach(MIMEText(html_body, 'html'))
        for att in attachments:
            data = base64.b64decode(att['content'])
            part = MIMEBase(*att['type'].split('/', 1))
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment', filename=att['filename'])
            msg.attach(part)
        server.sendmail(FROM_EMAIL, TO_EMAIL, msg.as_string())
        server.quit()
        print("✅ Email sent via SMTP", flush=True)
    except Exception as e:
        print(f"❌ SMTP Error: {e}", flush=True)


def _send_via_sendgrid(subject, html_body, plain_body, attachments):
    try:
        if not SENDGRID_API_KEY:
            print("⚠️ No SendGrid API key configured.", flush=True)
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
                {
                    "content": att['content'],
                    "type": att['type'],
                    "filename": att['filename'],
                    "disposition": "attachment"
                } for att in attachments
            ]
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload
        )
        print(f"SendGrid response: {r.status_code}", flush=True)
    except Exception as e:
        print(f"❌ SendGrid Error: {e}", flush=True)

# ─── Routes ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/messages')
def messages_view():
    messages = Message.query.order_by(Message.timestamp.desc()).limit(20).all()
    return render_template('messages.html', messages=messages)

@app.route('/contacts', methods=['GET', 'POST'])
def contacts_view():
    if request.method == 'POST':
        phone = request.form['phone']
        name = request.form['name']
        db.session.merge(Contact(phone_number=phone, contact_name=name))
        db.session.commit()
        flash(f"✅ Contact saved: {name} ({phone})")
        return redirect(url_for('contacts_view'))

    known = [row[0] for row in db.session.query(Contact.phone_number).all()]
    unknown = Message.query.filter(~Message.phone_number.in_(known)).order_by(Message.timestamp.desc()).limit(5).all()
    return render_template('contacts.html', recent_calls=unknown)

@app.route('/ask', methods=['GET', 'POST'])
def ask_view():
    response_text, error_message = None, None
    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        if not OPENAI_API_KEY:
            error_message = "❌ OpenAI API key is not configured."
        else:
            try:
                completion = openai_client.chat.completions.create(
                    model="gpt-4",
                    messages=[{"role": "user", "content": query}]
                )
                response_text = completion.choices[0].message.content
            except Exception as e:
                error_message = f"Error from OpenAI: {e}"
    return render_template('ask.html', response=response_text, error=error_message)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        obj = data.get('data', {}).get('object', {})
        direction = 'incoming' if data.get('type', '').endswith('received') else 'outgoing'
        phone = obj.get('from') or obj.get('to')
        text = obj.get('body', '')
        media_items = obj.get('media', [])
        key = phone[-10:] if phone else ''
        contact = Contact.query.get(key)
        name = contact.contact_name if contact else phone
        urls = [m.get('url') for m in media_items if m.get('url')]
        msg = Message(phone_number=phone, contact_name=name, direction=direction, message=text, media_urls=','.join(urls))
        db.session.add(msg)
        db.session.commit()

        attachments = []
        for url in urls:
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                b64 = base64.b64encode(r.content).decode()
                ctype = r.headers.get('Content-Type', 'application/octet-stream')
                attachments.append({
                    'content': b64,
                    'type': ctype,
                    'filename': url.split('/')[-1]
                })
            except Exception as e:
                print(f"⚠️ Media fetch error: {e}", flush=True)

        subject = f"New message {'from' if direction == 'incoming' else 'to'} {name}"
        send_email(subject, text, text, attachments)
    except Exception as e:
        print(f"❌ Webhook error: {e}", flush=True)
        traceback.print_exc()
    return Response(status=200)

# ─── Run App on Replit ──────────────────────────────────────────────────
if __name__ == '__main__':
    try:
        port = int(os.getenv('PORT', 8080))
    except ValueError:
        print("⚠️ Invalid PORT. Defaulting to 8080")
        port = 8080
    is_debug = os.getenv('FLASK_DEBUG', '0') in ['1', 'true', 'True']
    print(f"🚀 Starting Flask server on host 0.0.0.0 port {port} (Debug: {is_debug})")
    app.run(host='0.0.0.0', port=port, debug=is_debug)
