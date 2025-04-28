import os
import json
from pathlib import Path
from datetime import datetime, timedelta

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    url_for,
    redirect,
)
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import text, func
import openai
import traceback

from extensions import db
from models import Contact, Message, Property
from webhook_route import webhook_bp

# ──────────────────────────────────────────────────────────────────────────────
#   App configuration
# ──────────────────────────────────────────────────────────────────────────────


app = Flask(__name__)

# ensure our "instance" folder and sqlite file live next to main.py
BASEDIR  = Path(__file__).resolve().parent
INSTANCE = BASEDIR / "instance"
INSTANCE.mkdir(exist_ok=True)
DB_FILE  = INSTANCE / "messages.db"

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{DB_FILE}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
app.secret_key = os.getenv("FLASK_SECRET", "default_secret")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# initialize extensions & webhook blueprint
db.init_app(app)
app.register_blueprint(webhook_bp)

openai.api_key = os.getenv("OPENAI_API_KEY")


# ──────────────────────────────────────────────────────────────────────────────
#   Startup: create tables, ensure `sid`, reset sequence
# ──────────────────────────────────────────────────────────────────────────────
print("🔄 MAIN.PY RELOADED — DATABASE_URL:", app.config["SQLALCHEMY_DATABASE_URI"])
try:
    with app.app_context():
        # 1) Create all tables if missing
        db.create_all()
        print("✅ Tables created/verified.")

        # 2) Ensure the `sid` column exists on messages
        db.session.execute(text(
            "ALTER TABLE messages "
            "ADD COLUMN IF NOT EXISTS sid VARCHAR"
        ))
        print("✅ Ensured messages.sid column exists.")

        # 3) Reset the `id` sequence (PostgreSQL only; no-op on SQLite)
        db.session.execute(text(
            "SELECT setval(pg_get_serial_sequence('messages','id'), "
            "COALESCE((SELECT MAX(id) FROM messages), 1) + 1, false)"
        ))
        print("🔁 messages.id sequence reset.")

        db.session.commit()
        print(f"👉 Properties in DB: {Property.query.count()}")

except Exception as e:
    print("❌ Startup error:", e)
    traceback.print_exc()


# ──────────────────────────────────────────────────────────────────────────────
#   Index
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    current_year = datetime.utcnow().year
    try:
        db.session.execute(text("SELECT 1"))
        now = datetime.utcnow()
        start_today = datetime.combine(now.date(), datetime.min.time())
        start_week  = start_today - timedelta(days=now.weekday())

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
        summary_week  = f"{count_week} message(s) this week."
        db_status     = "Connected"
    except Exception as ex:
        db.session.rollback()
        db_status      = f"Error: {ex}"
        summary_today  = summary_week = "Unavailable"

    return render_template(
        "index.html",
        db_status=db_status,
        summary_today=summary_today,
        summary_week=summary_week,
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Image Delete
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/delete-media/<int:message_id>/<int:file_index>', methods=['POST'])
def delete_media(message_id, file_index):
    message = Message.query.get_or_404(message_id)

    if not message.local_media_paths:
        flash("No media to delete.", "error")
        return redirect(request.referrer or url_for('galleries_overview'))

    media_paths = message.local_media_paths.split(',')

    if 0 <= file_index < len(media_paths):
        file_path = os.path.join(app.static_folder, media_paths[file_index])

        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                flash(f"Successfully deleted {os.path.basename(file_path)}.", "success")
            except Exception as e:
                flash(f"Error deleting file: {e}", "error")
                return redirect(request.referrer or url_for('galleries_overview'))

        media_paths.pop(file_index)
        message.local_media_paths = ','.join(media_paths) if media_paths else None
        db.session.commit()
    else:
        flash("Invalid media index.", "error")

    return redirect(request.referrer or url_for('galleries_overview'))

# ──────────────────────────────────────────────────────────────────────────────
#   Messages & assignment
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/messages")
def messages_view():
    current_year = datetime.utcnow().year
    try:
        msgs = (
            Message.query
            .options(db.joinedload(Message.property))
            .order_by(Message.timestamp.desc())
            .limit(50)
            .all()
        )

        # JSON endpoint support
        if request.args.get("format") == "json":
            now = datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week  = start_today - timedelta(days=now.weekday())

            data = [{
                "id": m.id,
                "timestamp": m.timestamp.isoformat() + "Z",
                "phone_number": m.phone_number,
                "contact_name": m.contact_name,
                "direction": m.direction,
                "message": m.message,
                "media_urls": m.media_urls,
            } for m in msgs]

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
                "summary_today": f"{count_today} message(s) today.",
            }
            return jsonify({"messages": data, "stats": stats})

        phones = db.session.query(Contact.phone_number).all()
        known  = {p for (p,) in phones}
        props  = Property.query.order_by(Property.name).all()

        return render_template(
            "messages.html",
            messages=msgs,
            known_contact_phones=known,
            properties=props,
            current_year=current_year,
        )

    except Exception as ex:
        db.session.rollback()
        traceback.print_exc()
        return render_template(
            "messages.html",
            messages=[],
            error=str(ex),
            current_year=current_year,
        )


@app.route("/assign_property", methods=["POST"])
def assign_property():
    mid = request.form.get("message_id")
    pid = request.form.get("property_id", "")
    if mid:
        m = Message.query.get(mid)
        if m:
            m.property_id = int(pid) if pid else None
            db.session.commit()
    return redirect(url_for("messages_view") + f"#msg-{mid}")


# ──────────────────────────────────────────────────────────────────────────────
#   Contacts
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
    current_year = datetime.utcnow().year
    error = None
    recent = []
    if request.method == "POST":
        # …your add/delete logic…
        return redirect(url_for("contacts_view"))

    try:
        known = Contact.query.order_by(Contact.contact_name).all()
        # …build recent…
    except Exception as ex:
        db.session.rollback()
        traceback.print_exc()
        error  = str(ex)
        known, recent = [], []

    return render_template(
        "contacts.html",
        known_contacts=known,
        recent_calls=recent,
        error=error,
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Ask (OpenAI)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    current_year = datetime.utcnow().year
    response = error = query = None

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        if not query:
            error = "Please enter a question."
        elif not openai.api_key:
            error = "OpenAI key not configured."
        else:
            try:
                completion = openai.ChatCompletion.create(
                    model="gpt-4",
                    messages=[{"role": "user", "content": query}],
                )
                response = completion.choices[0].message["content"]
            except Exception as ex:
                error = f"OpenAI error: {ex}"
                traceback.print_exc()

    return render_template(
        "ask.html",
        response=response,
        error=error,
        current_query=query,
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Static gallery (all uploads)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery_static")
def gallery_static():
    error = None
    images = []
    folder = os.path.join(app.static_folder, "uploads")
    try:
        for fn in os.listdir(folder):
            if os.path.splitext(fn)[1].lower() in {
                ".jpg", ".png", ".gif", ".jpeg", ".webp"
            }:
                images.append(f"uploads/{fn}")
    except Exception as ex:
        error = "Error loading static gallery."
        traceback.print_exc()

    return render_template(
        "gallery.html",
        image_files=images,
        error=error,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Unsorted images (assign from here)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/unsorted")
def unsorted_gallery():
    # 1️⃣ grab every message with media but no property
    msgs = (
        Message.query
        .filter(Message.property_id.is_(None))
        .filter(Message.local_media_paths.isnot(None))
        .order_by(Message.timestamp.desc())
        .all()
    )

    # 2️⃣ flatten to list of {msg, path}
    unsorted = []
    for m in msgs:
        for p in (m.local_media_paths or "").split(","):
            p = p.strip()
            if p and os.path.isfile(os.path.join(app.static_folder, p)):
                unsorted.append({"msg": m, "path": p})

    # 3️⃣ properties list for dropdown
    props = Property.query.order_by(Property.name).all()

    return render_template(
        "unsorted.html",
        unsorted=unsorted,
        properties=props,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Per-property gallery
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    prop = Property.query.get_or_404(property_id)
    msgs = (
        Message.query
        .filter_by(property_id=property_id)
        .filter(Message.local_media_paths.isnot(None))
        .all()
    )
    image_files = [
        p.strip()
        for m in msgs
        for p in (m.local_media_paths or "").split(",")
        if p.strip()
           and os.path.isfile(os.path.join(app.static_folder, p.strip()))
    ]
    return render_template(
        "gallery.html",
        image_files=image_files,
        property=prop,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   “All Galleries” overview
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/galleries")
def galleries_overview():
    gallery_summaries = []
    for prop in Property.query.order_by(Property.name).all():
        paths = []
        for m in Message.query.filter(
            Message.property_id == prop.id,
            Message.local_media_paths.isnot(None)
        ):
            for p in (m.local_media_paths or "").split(","):
                p = p.strip()
                full = os.path.join(app.static_folder, p)
                if p and os.path.isfile(full):
                    paths.append(p)

        gallery_summaries.append({
            "property": prop,
            "count": len(paths),
            "thumb": paths[-1] if paths else None,
        })

    return render_template(
        "galleries_overview.html",
        gallery_summaries=gallery_summaries,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Combined uploads gallery
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery", endpoint="gallery_view")
def gallery_view():
    upload_folder = os.path.join(app.static_folder, "uploads")
    image_files = []
    if os.path.isdir(upload_folder):
        for fn in os.listdir(upload_folder):
            ext = os.path.splitext(fn)[1].lower()
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                image_files.append(f"uploads/{fn}")
    return render_template(
        "gallery.html",
        image_files=image_files,
        property=None,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Health check
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ping")
def ping_route():
    return "Pong!", 200


# ──────────────────────────────────────────────────────────────────────────────
#   Debug URL map
# ──────────────────────────────────────────────────────────────────────────────
with app.app_context():
    print("\n--- URL MAP ---")
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.endpoint):
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        print(f"{rule.endpoint:25} {methods:15} {rule.rule}")
    print("--- END URL MAP ---\n")

@app.route('/delete_media', methods=['POST'])
def delete_media():
    file_path = request.form.get('file_path')
    if file_path:
        full_path = os.path.join(app.static_folder, file_path)
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
                flash('File deleted successfully.', 'success')
            except Exception as e:
                flash(f'Error deleting file: {e}', 'danger')
        else:
            flash('File does not exist.', 'warning')
    else:
        flash('No file specified.', 'warning')

    return redirect(request.referrer or url_for('gallery_view'))