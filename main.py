```python
import os
import re
import json
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, request, jsonify,
    url_for, redirect
)
from dotenv import load_dotenv
from sqlalchemy import text, func
import openai
import traceback

from extensions import db
from models import Contact, Message, Property
from webhook_route import webhook_bp

# ═════════════════════════════════════════════════════════════════════════════
#  Application Setup & Configuration
# ═════════════════════════════════════════════════════════════════════════════

# Load environment variables from .env
load_dotenv()

# Initialize Flask app and configure
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", "sqlite:///instance/messages.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
app.secret_key = os.getenv("FLASK_SECRET", "default_secret")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Ensure SQLite instance folder exists if used
if (
    app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:")
    and "instance" in app.config["SQLALCHEMY_DATABASE_URI"]
):
    instance_path = os.path.join(app.root_path, "instance")
    os.makedirs(instance_path, exist_ok=True)
    print("✅ Instance folder verified/created.")

# Initialize extensions and blueprints
db.init_app(app)
app.register_blueprint(webhook_bp)

# Configure OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")

# Debug startup info
print("!!!!!!!!!!!!!!!!! MAIN.PY RELOADED AT LATEST TIMESTAMP !!!!!!!!!!!!!!!!")
print("👉 DATABASE_URL:", os.environ.get("DATABASE_URL"))
print("Attempting to create database tables...")
try:
    with app.app_context():
        db.create_all()
    print("✅ Database tables created/verified.")
    with app.app_context():
        print(f"👉 Properties in DB after create_all: {Property.query.count()}")
except Exception as init_e:
    print(f"❌ Error creating tables: {init_e}")

from sqlalchemy import text

# --- Create tables on startup, then ensure the sid column exists and reset the sequence ---
print("Attempting to create database tables...")
try:
    with app.app_context():
        # 1) Create any missing tables (does nothing if tables already exist)
        db.create_all()
        print("✅ Database tables created/verified.")

        # 2) Ensure the new 'sid' column exists in the messages table
        db.session.execute(text(
            "ALTER TABLE messages "
            "ADD COLUMN IF NOT EXISTS sid VARCHAR"
        ))
        print("✅ Ensured messages.sid column exists.")

        # 3) (Optional) Reset the messages.id sequence so new inserts don't collide
        db.session.execute(text(
            "SELECT setval(pg_get_serial_sequence('messages','id'), "
            "COALESCE((SELECT MAX(id) FROM messages), 1) + 1, false)"
        ))
        print("🔄 messages.id sequence reset.")

        db.session.commit()

        # 4) Debug: verify there are still the same number of properties
        count2 = Property.query.count()
        print(f"👉 Properties in DB after create_all: {count2}")

except Exception as init_e:
    print(f"❌ Error creating/verifying tables or columns: {init_e}")


except Exception as init_e:
    print(f"❌ Error creating tables: {init_e}")


# ═════════════════════════════════════════════════════════════════════════════
#  Index Route
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    db_status = "Unknown"
    summary_today = summary_week = "Unavailable"
    current_year = datetime.utcnow().year

    try:
        db.session.execute(text("SELECT 1"))
        db_status = "Connected"

        now = datetime.utcnow()
        start_today = datetime.combine(now.date(), datetime.min.time())
        start_week = start_today - timedelta(days=now.weekday())

        count_today = (
            db.session.query(func.count(Message.id))
            .filter(Message.timestamp >= start_today)
            .scalar()
        )
        count_week = (
            db.session.query(func.count(Message.id))
            .filter(Message.timestamp >= start_week)
            .scalar()
        )

        summary_today = f"{count_today} message(s) today."
        summary_week = f"{count_week} message(s) this week."
    except Exception as e:
        db.session.rollback()
        db_status = f"Error: {e}"

    return render_template(
        "index.html",
        db_status=db_status,
        summary_today=summary_today,
        summary_week=summary_week,
        current_year=current_year,
    )

# ═════════════════════════════════════════════════════════════════════════════
#  Messages and Assignment Routes
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/messages")
def messages_view():
    current_year = datetime.utcnow().year

    try:
        messages_query = (
            Message.query
            .options(db.joinedload(Message.property))
            .order_by(Message.timestamp.desc())
            .limit(50)
            .all()
        )

        # JSON API support
        if request.args.get("format") == "json":
            msgs, now = [], datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week = start_today - timedelta(days=now.weekday())
            for m in messages_query:
                msgs.append({
                    "id": m.id,
                    "timestamp": m.timestamp.isoformat() + "Z",
                    "phone_number": m.phone_number,
                    "contact_name": m.contact_name,
                    "direction": m.direction,
                    "message": m.message,
                    "media_urls": m.media_urls,
                    "is_read": True,
                })
            stats = {
                "messages_today": (
                    db.session.query(func.count(Message.id))
                    .filter(Message.timestamp >= start_today)
                    .scalar()
                ),
                "messages_week": (
                    db.session.query(func.count(Message.id))
                    .filter(Message.timestamp >= start_week)
                    .scalar()
                ),
                "unread_messages": 0,
                "response_rate": 93,
                "summary_today": f"{start_today.date()} stats",
            }
            return jsonify({"messages": msgs, "stats": stats})

        # HTML page logic
        phones = db.session.query(Contact.phone_number).all()
        known_contact_phones = {p for (p,) in phones}
        properties = Property.query.order_by(Property.name).all()

    except Exception as e:
        db.session.rollback()
        traceback.print_exc()
        return render_template(
            "messages.html",
            messages=[],
            error=str(e),
            current_year=current_year,
        )

    return render_template(
        "messages.html",
        messages=messages_query,
        known_contact_phones=known_contact_phones,
        properties=properties,
        current_year=current_year,
    )

@app.route("/assign_property", methods=["POST"])
def assign_property():
    msg_id = request.form.get("message_id")
    prop_id = request.form.get("property_id", "")
    if msg_id:
        m = Message.query.get(msg_id)
        if m:
            m.property_id = int(prop_id) if prop_id else None
            db.session.commit()
    return redirect(url_for("messages_view") + f"#msg-{msg_id}")

# ═════════════════════════════════════════════════════════════════════════════
#  Contacts Route
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
    error = None
    current_year = datetime.utcnow().year
    known_contacts, recent_calls = [], []

    if request.method == "POST":
        # add/delete logic here
        return redirect(url_for("contacts_view"))

    try:
        known_contacts = Contact.query.order_by(Contact.contact_name).all()
        # build recent_calls...
    except Exception as e:
        traceback.print_exc()

    return render_template(
        "contacts.html",
        known_contacts=known_contacts,
        recent_calls=recent_calls,
        error=error,
        current_year=current_year,
    )

# ═════════════════════════════════════════════════════════════════════════════
#  Ask (OpenAI) Route
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    response, error, query = None, None, ""
    current_year = datetime.utcnow().year

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        if not query:
            error = "Please enter a question."
        elif not openai.api_key:
            error = "❌ OpenAI API key not configured."
        else:
            try:
                completion = openai.ChatCompletion.create(
                    model="gpt-4",
                    messages=[{"role": "user", "content": query}],
                )
                response = completion.choices[0].message["content"]
            except Exception as e:
                error = f"❌ OpenAI API Error: {e}"
                traceback.print_exc()

    return render_template(
        "ask.html",
        response=response,
        error=error,
        current_query=query,
        current_year=current_year,
    )

# ═════════════════════════════════════════════════════════════════════════════
#  Gallery Routes
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/gallery_static")
def gallery_static():
    error = None
    image_files = []
    upload_folder = os.path.join(app.static_folder, "uploads")

    try:
        for fn in os.listdir(upload_folder):
            ext = os.path.splitext(fn)[1].lower()
            if ext in {".jpg", ".png", ".gif", ".jpeg", ".webp"}:
                image_files.append(f"uploads/{fn}")
    except Exception as e:
        error = "Error loading static gallery"
        traceback.print_exc()

    return render_template(
        "gallery.html",
        image_files=image_files,
        error=error,
        current_year=datetime.utcnow().year,
    )

@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    prop = Property.query.get_or_404(property_id)
    msgs = (
        Message.query
        .filter(Message.property_id == property_id)
        .filter(Message.local_media_paths.isnot(None))
        .all()
    )
    image_files = []
    for m in msgs:
        for p in m.local_media_paths.split(","):  # comma-separated paths
            image_files.append(p)

    return render_template(
        "gallery.html",
        image_files=image_files,
        property=prop,
        current_year=datetime.utcnow().year,
    )

# ═════════════════════════════════════════════════════════════════════════════
#  Ping Health Check
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/ping")
def ping_route():
    return "Pong!", 200

# ═════════════════════════════════════════════════════════════════════════════
#  Debug URL Map on Startup
# ═════════════════════════════════════════════════════════════════════════════

with app.app_context():
    print("\n--- Registered URL Endpoints ---")
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.endpoint):
        methods = ",".join(
            sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        )
        print(f"Endpoint: {rule.endpoint:<30} Methods: {methods:<20} Rule: {rule}")
    print("--- End Registered URL Endpoints ---\n")

# --- End of main.py ---
```
