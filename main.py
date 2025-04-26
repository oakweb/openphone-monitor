import os
import re
import json
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
from sqlalchemy import text, func
import openai
import traceback

from extensions import db
from models import Contact, Message, Property
from webhook_route import webhook_bp

# 🔍 Debug: show which DATABASE_URL we’re actually using
print("!!!!!!!!!!!!!!!!! MAIN.PY RELOADED AT LATEST TIMESTAMP !!!!!!!!!!!!!!!!")
print("👉 DATABASE_URL:", os.environ.get("DATABASE_URL"))

# --- Load environment variables ----------
load_dotenv()

# --- Initialize Flask app ---
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

# Ensure SQLite instance folder exists if using sqlite
if (
    app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:")
    and "instance" in app.config["SQLALCHEMY_DATABASE_URI"]
):
    instance_path = os.path.join(app.root_path, "instance")
    os.makedirs(instance_path, exist_ok=True)
    print("✅ Instance folder verified/created.")

# Initialize database
db.init_app(app)

# 🔍 Debug: count properties at startup
with app.app_context():
    try:
        count = Property.query.count()
        print(f"👉 Properties in DB at startup: {count}")
    except Exception as e:
        print("❌ Could not count properties at startup:", e)

# Register webhook blueprint
app.register_blueprint(webhook_bp)

# Configure OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# Create tables on startup
print("Attempting to create database tables...")
try:
    with app.app_context():
        db.create_all()
    print("✅ Database tables created/verified.")
    # 🔍 Debug: verify the row-count again after create_all
    with app.app_context():
        try:
            count2 = Property.query.count()
            print(f"👉 Properties in DB after create_all: {count2}")
        except Exception:
            pass
except Exception as init_e:
    print(f"❌ Error creating tables: {init_e}")


# --- Index Route ---
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

@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    prop = Property.query.get_or_404(property_id)

    msgs = (
        Message.query
        .filter(Message.property_id == property_id)
        .filter(Message.local_media_paths.isnot(None))
        .all()
    )

    for m in msgs:
        print(f"[GALLERY DEBUG] msg.id={m.id}  media={m.local_media_paths}")

    ...


# --- Messages Route ---
@app.route("/messages")
def messages_view():
    error = None
    current_year = datetime.utcnow().year
    messages_query = []
    properties = []
    known_contact_phones = set()

    try:
        # Eager-load assigned property
        messages_query = (
            Message.query.options(db.joinedload(Message.property))
            .order_by(Message.timestamp.desc())
            .limit(50)
            .all()
        )

        # JSON API
        if request.args.get("format") == "json":
            msgs = []
            for m in messages_query:
                msgs.append(
                    {
                        "id": m.id,
                        "timestamp": m.timestamp.isoformat() + "Z",
                        "phone_number": m.phone_number,
                        "contact_name": m.contact_name,
                        "direction": m.direction,
                        "message": m.message,
                        "media_urls": m.media_urls,
                        "is_read": True,
                    }
                )
            now = datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week = start_today - timedelta(days=now.weekday())
            cnt_today = (
                db.session.query(func.count(Message.id))
                .filter(Message.timestamp >= start_today)
                .scalar()
            )
            cnt_week = (
                db.session.query(func.count(Message.id))
                .filter(Message.timestamp >= start_week)
                .scalar()
            )
            summary = f"{cnt_today} message(s) received today."
            stats = {
                "messages_today": cnt_today,
                "messages_week": cnt_week,
                "unread_messages": 0,
                "response_rate": 93,
                "summary_today": summary,
            }
            return jsonify({"messages": msgs, "stats": stats})

        # HTML page
        phones = db.session.query(Contact.phone_number).all()
        known_contact_phones = {p for (p,) in phones}
        print(
            f"--- DEBUG: Passing {len(known_contact_phones)} known numbers to messages template ---"
        )

        try:
            properties = Property.query.order_by(Property.name).all()
            print(f"--- DEBUG: Fetched {len(properties)} properties for dropdown ---")
        except Exception as pe:
            print(f"❌ Error fetching properties: {pe}")
            properties = []
            error = error or str(pe)

        now = datetime.utcnow()
        start_today = datetime.combine(now.date(), datetime.min.time())
        start_week = start_today - timedelta(days=now.weekday())
        cnt_today = (
            db.session.query(func.count(Message.id))
            .filter(Message.timestamp >= start_today)
            .scalar()
        )
        cnt_week = (
            db.session.query(func.count(Message.id))
            .filter(Message.timestamp >= start_week)
            .scalar()
        )
        summary_for_html = f"{cnt_today} message(s) today."

        return render_template(
            "messages.html",
            messages=messages_query,
            error=error,
            messages_today=cnt_today,
            messages_week=cnt_week,
            summary_today=summary_for_html,
            known_contact_phones=known_contact_phones,
            properties=properties,
            current_year=current_year,
        )

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error in /messages: {e}")
        traceback.print_exc()
        if request.args.get("format") == "json":
            return jsonify({"error": str(e)}), 500
        return render_template(
            "messages.html",
            messages=[],
            error=str(e),
            messages_today=0,
            messages_week=0,
            summary_today="Error loading messages.",
            known_contact_phones=set(),
            properties=[],
            current_year=current_year,
        )


# --- Contacts Route ---
@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
    error = None
    current_year = datetime.utcnow().year
    known_contacts = []
    recent_calls = []

    # Handle POST
    if request.method == "POST":
        action = request.form.get("action")
        phone = request.form.get("phone", "").strip()
        if action == "add":
            name = request.form.get("name", "").strip()
            if not phone or not name:
                error = "Phone and name are required."
            else:
                key = "".join(filter(str.isdigit, phone))[-10:]
                if len(key) < 10:
                    key = phone
                if not Contact.query.get(key):
                    cnt_msgs = Message.query.filter_by(phone_number=key).all()
                    c = Contact(phone_number=key, contact_name=name)
                    db.session.add(c)
                    try:
                        db.session.commit()
                        print(f"✅ Added contact {name} ({key})")
                        # update existing messages
                        for m in cnt_msgs:
                            m.contact_name = name
                        db.session.commit()
                    except Exception as ex:
                        db.session.rollback()
                        print(f"❌ Error adding contact: {ex}")
                        error = str(ex)
                else:
                    error = f"Contact {key} already exists."
        elif action == "delete":
            if phone:
                key = "".join(filter(str.isdigit, phone))[-10:]
                c = Contact.query.get(key)
                if c:
                    db.session.delete(c)
                    db.session.commit()
                else:
                    error = f"No contact {key} to delete."
        return redirect(url_for("contacts_view"))

    # Handle GET
    try:
        known_contacts = Contact.query.order_by(Contact.contact_name).all()
        ks = {c.phone_number for c in known_contacts}
        print(f"--- DEBUG: Fetched {len(known_contacts)} known contacts ---")

        cutoff = datetime.utcnow() - timedelta(days=30)
        subq = (
            db.session.query(
                Message.phone_number,
                func.max(Message.timestamp).label("max_ts"),
            )
            .filter(
                Message.direction == "incoming",
                Message.timestamp >= cutoff,
                ~Message.phone_number.in_(ks),
            )
            .group_by(Message.phone_number)
            .subquery()
        )
        unknown = (
            db.session.query(Message)
            .join(
                subq,
                (Message.phone_number == subq.c.phone_number)
                & (Message.timestamp == subq.c.max_ts),
            )
            .order_by(subq.c.max_ts.desc())
            .limit(20)
            .all()
        )
        if unknown:
            for m in unknown:
                recent_calls.append(
                    {
                        "phone_number": m.phone_number,
                        "message": m.message,
                        "timestamp": m.timestamp,
                    }
                )
        else:
            print("--- DEBUG: No recent messages from unknown numbers found ---")
    except Exception as e:
        db.session.rollback()
        print(f"❌ Error in /contacts GET: {e}")
        traceback.print_exc()

    return render_template(
        "contacts.html",
        known_contacts=known_contacts,
        recent_calls=recent_calls,
        error=error,
        current_year=current_year,
    )


# --- Assign Property Route ---
@app.route("/assign_property", methods=["POST"])
def assign_property():
    msg_id = request.form.get("message_id")
    prop_id = request.form.get("property_id", "")
    redirect_url = url_for("messages_view")

    if not msg_id:
        return redirect(redirect_url)

    m = Message.query.get(msg_id)
    if not m:
        return redirect(redirect_url)

    if prop_id:
        try:
            pid = int(prop_id)
            p = Property.query.get(pid)
            if p and m.property_id != pid:
                m.property_id = pid
                db.session.commit()
        except Exception:
            db.session.rollback()
    else:
        m.property_id = None
        db.session.commit()

    return redirect(f"{redirect_url}#msg-{msg_id}")


# --- Ask Route ---
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


# --- Gallery Route ---
@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    # look up the property
    prop = Property.query.get_or_404(property_id)

    # find all messages with that property and local_media_paths set
    msgs = (
        Message.query
        .filter(Message.property_id == property_id)
        .filter(Message.local_media_paths.isnot(None))
        .all()
    )

    # pull out all the relative paths
    image_files = []
    for m in msgs:
        for p in m.local_media_paths.split(","):
            image_files.append(p)

    # sort by timestamp descending if you like
    # image_files = sorted(image_files, reverse=True)

    return render_template(
        "gallery.html",
        image_files=image_files,
        property=prop,
        current_year=datetime.utcnow().year,
    )


@app.route("/gallery")
def gallery_view():
    error = None
    image_files = []
    upload_folder_name = "uploads"
    current_year = datetime.utcnow().year
    upload_folder_path = os.path.join(app.static_folder, upload_folder_name)

    print(f"--- DEBUG: Looking for images in: {upload_folder_path} ---")

    try:
        if os.path.isdir(upload_folder_path):
            print(f"--- DEBUG: Scanning folder: {upload_folder_path} ---")
            all_items_in_dir = os.listdir(upload_folder_path)
            print(f"--- DEBUG: Found {len(all_items_in_dir)} total items. ---")

            valid_image_files = []
            for filename in all_items_in_dir:
                full_path = os.path.join(upload_folder_path, filename)
                if os.path.isfile(full_path):
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                        relative_path = os.path.join(
                            upload_folder_name, filename
                        ).replace(os.path.sep, "/")
                        valid_image_files.append(
                            {
                                "path": relative_path,
                                "mtime": os.path.getmtime(full_path),
                            }
                        )

            print(
                f"--- DEBUG: Found {len(valid_image_files)} valid image files matching pattern/extensions. ---"
            )

            valid_image_files.sort(key=lambda x: x["mtime"], reverse=True)
            image_files = [f["path"] for f in valid_image_files]

            if valid_image_files:
                print("--- DEBUG: Sorted image files by modification time. ---")
        else:
            print(
                f"--- DEBUG: Upload folder not found or is not a directory: {upload_folder_path} ---"
            )
            error = "Upload folder not found. Ensure 'static/uploads' exists."
    except Exception as e:
        print(f"❌ Error accessing gallery images: {e}")
        traceback.print_exc()
        error = "Error loading gallery images."

    return render_template(
        "gallery.html",
        image_files=image_files,
        error=error,
        current_year=current_year,
    )


# --- Ping Route for health checks ---
@app.route("/ping")
def ping_route():
    print("--- /ping route accessed ---")
    return "Pong!", 200


# --- Debug URL map on startup ---
try:
    with app.app_context():
        print("\n--- Registered URL Endpoints ---")
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.endpoint):
            methods = ",".join(
                sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
            )
            print(f"Endpoint: {rule.endpoint:<30} Methods: {methods:<20} Rule: {rule}")
        print("--- End Registered URL Endpoints ---\n")
except Exception as e:
    print(f"--- Error inspecting URL map: {e} ---")


# --- Main execution block ---
if __name__ == "__main__":
    # For local testing:
    # app.run(debug=True, host="0.0.0.0", port=8080)
    pass
