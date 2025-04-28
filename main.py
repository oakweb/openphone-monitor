# -*- coding: utf-8 -*-
import os
import json
import logging # Import logging
from pathlib import Path
from datetime import datetime, timedelta
import traceback

# Import necessary Flask components, including 'flash' and 'current_app'
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    url_for,
    redirect,
    flash,
    current_app # Import current_app
)
from dotenv import load_dotenv
load_dotenv()
# Import SQLAlchemy components needed for queries/operations
from sqlalchemy import text, func, exc as sqlalchemy_exc, select, over
from sqlalchemy.orm import attributes, joinedload # Import attributes and joinedload

import openai


from extensions import db
from models import Contact, Message, Property # Import Contact model
from webhook_route import webhook_bp # Assuming webhook_bp is correctly defined elsewhere

# ──────────────────────────────────────────────────────────────────────────────
#  App configuration
# ──────────────────────────────────────────────────────────────────────────────


app = Flask(__name__)

# --- Logging Setup ---
# Use Flask's built-in logger
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(name)s : %(message)s')
app.logger.setLevel(logging.DEBUG) # Set level for app's logger
# logging.getLogger('werkzeug').setLevel(logging.INFO) # Optional: Adjust Werkzeug level
# logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO) # Optional: Log SQL statements


# ensure our "instance" folder and sqlite file live next to main.py
BASEDIR  = Path(__file__).resolve().parent
INSTANCE = BASEDIR / "instance"
INSTANCE.mkdir(exist_ok=True)
DB_FILE  = INSTANCE / "messages.db"

# Default to SQLite if DATABASE_URL is not set
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{DB_FILE}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300, # Standard setting for recycling connections
}
# Make sure FLASK_SECRET is set in your environment variables for production
app.config["FLASK_SECRET"] = os.getenv("FLASK_SECRET", "a_very_strong_default_secret_key_for_dev_only") # Use a better default for dev
app.secret_key = app.config["FLASK_SECRET"] # Use the same secret key

app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Define the static folder explicitly (usually 'static' by default)
app.static_folder = os.path.join(app.root_path, 'static') # More robust way to define static folder
# Ensure the upload directory exists within the static folder
UPLOAD_FOLDER = os.path.join(app.static_folder, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True) # Create the directory on app startup
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.logger.info(f"ℹ️ Configured UPLOAD_FOLDER: {app.config['UPLOAD_FOLDER']}")


# initialize extensions & webhook blueprint
db.init_app(app)
app.register_blueprint(webhook_bp) # Assumes webhook_bp uses url_prefix="/webhook"

# Configure OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")


# ──────────────────────────────────────────────────────────────────────────────
#  Startup: create tables, ensure `sid`, reset sequence
# ──────────────────────────────────────────────────────────────────────────────
def initialize_database(app_context):
    """Initializes the database: creates tables, checks columns, resets sequences."""
    with app_context:
        app.logger.info("🔄 Initializing Database...")
        try:
            # 1) Create all tables if missing
            db.create_all()
            app.logger.info("✅ Tables created/verified.")

            # 2) Ensure the `sid` column exists on messages
            try:
                db.session.execute(text("ALTER TABLE messages ADD COLUMN sid VARCHAR"))
                db.session.commit()
                app.logger.info("✅ Ensured messages.sid column exists (added if missing).")
            except Exception as alter_err:
                db.session.rollback()
                err_str = str(alter_err).lower()
                if "already exists" in err_str or "duplicate column name" in err_str:
                    app.logger.info("✅ messages.sid column already exists.")
                else:
                     app.logger.warning(f"⚠️ Could not add 'sid' column (may already exist or other issue): {alter_err}")

            # 3) Reset the `id` sequence (PostgreSQL only)
            if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
                try:
                    sequence_name_query = text("SELECT pg_get_serial_sequence('messages', 'id');")
                    result = db.session.execute(sequence_name_query).scalar()

                    if result:
                        sequence_name = result
                        max_id_query = text("SELECT COALESCE(MAX(id), 0) FROM messages")
                        max_id = db.session.execute(max_id_query).scalar()
                        next_val = max_id + 1
                        reset_seq_query = text(f"SELECT setval('{sequence_name}', :next_val, false)")
                        db.session.execute(reset_seq_query, {'next_val': next_val})
                        db.session.commit()
                        app.logger.info(f"🔁 messages.id sequence ('{sequence_name}') reset to {next_val}.")
                    else:
                        app.logger.warning("⚠️ Could not determine sequence name for messages.id.")

                except Exception as seq_err:
                    db.session.rollback()
                    app.logger.error(f"❌ Error resetting PostgreSQL sequence: {seq_err}")
                    if "permission denied" in str(seq_err).lower():
                        app.logger.info("ℹ️ Hint: The database user might lack permissions to alter sequences.")
            else:
                app.logger.info("ℹ️ Skipping sequence reset (not PostgreSQL).")

            prop_count = Property.query.count()
            app.logger.info(f"👉 Properties in DB: {prop_count}")
            app.logger.info("✅ Database initialization complete.")

        except Exception as e:
            db.session.rollback()
            app.logger.critical(f"❌ FATAL STARTUP ERROR during database initialization: {e}")
            traceback.print_exc()

# Call initialization within app context when the app starts
initialize_database(app.app_context())


# ──────────────────────────────────────────────────────────────────────────────
#  Index / Dashboard
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    current_year = datetime.utcnow().year
    db_status = "Unknown"
    summary_today = "Unavailable"
    summary_week = "Unavailable"
    count_today = 0
    count_week = 0

    try:
        db.session.execute(text("SELECT 1"))
        db_status = "Connected"

        now_utc = datetime.utcnow()
        start_today_utc = datetime.combine(now_utc.date(), datetime.min.time())
        start_week_utc  = start_today_utc - timedelta(days=now_utc.weekday())

        count_today = (db.session.query(func.count(Message.id))
                       .filter(Message.timestamp >= start_today_utc).scalar() or 0)
        count_week = (db.session.query(func.count(Message.id))
                      .filter(Message.timestamp >= start_week_utc).scalar() or 0)

        summary_today = f"{count_today} message(s) today."
        summary_week  = f"{count_week} message(s) this week."

    except Exception as ex:
        db.session.rollback()
        db_status     = f"Error connecting or querying DB: {ex}"
        app.logger.error(f"❌ DB Error on index page: {ex}")

    return render_template(
        "index.html",
        db_status=db_status,
        summary_today=summary_today,
        summary_week=summary_week,
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Media Deletion (Associated with a Message)
# ──────────────────────────────────────────────────────────────────────────────
@app.route('/delete-media/<int:message_id>/<int:file_index>', methods=['POST'])
def delete_media_for_message(message_id, file_index):
    message = Message.query.get_or_404(message_id)
    redirect_url = request.referrer or url_for('galleries_overview')
    if message.property_id:
        redirect_url = url_for('gallery_for_property', property_id=message.property_id)

    if not message.local_media_paths:
        flash("No media associated with this message to delete.", "warning")
        return redirect(redirect_url)

    media_paths = [p.strip() for p in message.local_media_paths.split(',') if p.strip()]

    if 0 <= file_index < len(media_paths):
        relative_path_to_delete = media_paths[file_index]
        # Use app.config['UPLOAD_FOLDER'] which should be absolute
        full_file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], os.path.basename(relative_path_to_delete))

        # More robust security check using common path and normpath
        upload_folder_norm = os.path.normpath(current_app.config['UPLOAD_FOLDER'])
        file_path_norm = os.path.normpath(full_file_path)
        if os.path.commonpath([upload_folder_norm, file_path_norm]) != upload_folder_norm:
             app.logger.error(f"Attempt to delete file outside UPLOAD_FOLDER: {full_file_path}")
             flash("Invalid file path specified.", "danger")
             return redirect(redirect_url)

        app.logger.info(f"ℹ️ [delete-media] Checking for file: {full_file_path}")
        if os.path.exists(full_file_path):
            try:
                os.remove(full_file_path)
                app.logger.info(f"✅ [delete-media] Deleted file: {full_file_path}")
                flash(f"Successfully deleted media file: {os.path.basename(relative_path_to_delete)}.", "success")

                media_paths.pop(file_index)
                message.local_media_paths = ','.join(media_paths) if media_paths else None
                db.session.commit()
                app.logger.info(f"✅ [delete-media] Updated DB for message {message_id}")

            except OSError as e:
                app.logger.error(f"❌ [delete-media] OS error deleting file {full_file_path}: {e}")
                flash(f"Error deleting file from disk: {e}", "danger")
                db.session.rollback()
            except Exception as e:
                 app.logger.error(f"❌ [delete-media] Unexpected error deleting file {full_file_path}: {e}")
                 flash(f"An unexpected error occurred during deletion: {e}", "danger")
                 db.session.rollback()
        else:
            app.logger.warning(f"⚠️ [delete-media] File not found on disk: {full_file_path}. Removing from DB.")
            flash(f"Media file not found on disk: {os.path.basename(relative_path_to_delete)}. Removing from database record.", "warning")
            media_paths.pop(file_index)
            message.local_media_paths = ','.join(media_paths) if media_paths else None
            db.session.commit()
            app.logger.info(f"✅ [delete-media] Updated DB for message {message_id} (file was missing).")
    else:
        flash("Invalid media index provided.", "error")

    return redirect(redirect_url)


# ──────────────────────────────────────────────────────────────────────────────
#  Messages & Assignment (Handles BOTH Overview and Detail View)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/messages")
def messages_view():
    """Displays message overview or detail for a specific number."""
    current_year = datetime.utcnow().year
    target_phone_number = request.args.get("phone_number")
    app.logger.debug(f"Accessing messages_view. Target phone: {target_phone_number}")

    try:
        # --- CASE 1: DETAIL VIEW ---
        if target_phone_number:
            app.logger.debug(f"--- Loading DETAIL view for {target_phone_number} ---")
            msgs_for_number = (
                Message.query
                .filter_by(phone_number=target_phone_number)
                .order_by(Message.timestamp.desc())
                .all()
            )
            # --- Log query result count ---
            app.logger.debug(f"Query for {target_phone_number} returned {len(msgs_for_number)} message objects.")

            contact = Contact.query.get(target_phone_number)
            is_known = contact is not None
            contact_name = contact.contact_name if is_known else None
            app.logger.debug(f"Contact lookup for {target_phone_number}: is_known={is_known}, name='{contact_name}'")

            return render_template(
                "messages_detail.html", # Expects this template to exist
                phone_number=target_phone_number,
                messages=msgs_for_number,
                is_known=is_known,
                contact_name=contact_name,
                current_year=current_year,
            )

        # --- CASE 2: OVERVIEW VIEW ---
        else:
            app.logger.debug("--- Loading OVERVIEW view ---")
            msgs_overview = (
                Message.query
                .options(joinedload(Message.property)) # Use joinedload from imports
                .order_by(Message.timestamp.desc())
                .limit(100)
                .all()
            )
            # --- Log query result count ---
            app.logger.debug(f"Query for overview returned {len(msgs_overview)} message objects.")

            known_phones_query = db.session.query(Contact.phone_number).distinct().all()
            known_contact_phones_set = {p for (p,) in known_phones_query}
            app.logger.debug(f"Known contact phone keys for overview: {known_contact_phones_set}")

            properties_list = Property.query.order_by(Property.name).all()

            return render_template(
                "messages_overview.html", # Expects this template to exist
                messages=msgs_overview,
                known_contact_phones=known_contact_phones_set,
                properties=properties_list,
                current_year=current_year,
            )

    except Exception as ex:
        db.session.rollback()
        traceback.print_exc()
        error_msg = f"Error loading messages page: {ex}"
        app.logger.error(f"❌ {error_msg}")
        flash(error_msg, "danger")
        # Fallback rendering (adjust as needed)
        template_name = "messages_detail.html" if target_phone_number else "messages_overview.html"
        return render_template(
            template_name,
            messages=[],
            properties=[],
            known_contact_phones=set(),
            phone_number=target_phone_number,
            is_known=False,
            contact_name=None,
            error=error_msg,
            current_year=current_year,
        )


@app.route("/assign_property", methods=["POST"])
def assign_property():
    """Assigns or unassigns a property to a message."""
    message_id_str = request.form.get("message_id")
    property_id_str = request.form.get("property_id", "")
    redirect_url = request.referrer or url_for("messages_view")

    if not message_id_str:
         flash("No message ID provided.", "error")
         return redirect(redirect_url)

    try:
        message_id = int(message_id_str)
        message = Message.query.get(message_id)

        if not message:
            flash(f"Message ID {message_id} not found.", "error")
            return redirect(redirect_url)

        if property_id_str and property_id_str.lower() not in ["none", ""]:
            property_id = int(property_id_str)
            prop = Property.query.get(property_id)
            if not prop:
                 flash(f"Property ID {property_id} not found.", "error")
                 return redirect(redirect_url)
            message.property_id = property_id
            flash(f"Message assigned to property: {prop.name}", "success")
        else:
            message.property_id = None
            flash(f"Message unassigned from property.", "info")

        db.session.commit()

    except ValueError:
        flash("Invalid Message or Property ID.", "error")
        db.session.rollback()
    except Exception as e:
        flash(f"Error assigning property: {e}", "danger")
        db.session.rollback()
        app.logger.error(f"Error in assign_property: {e}")
        traceback.print_exc()

    if "#" not in redirect_url and 'message_id' in locals() and isinstance(message_id, int):
         redirect_url += f"#msg-{message_id}"
    return redirect(redirect_url)


# ──────────────────────────────────────────────────────────────────────────────
#  Contacts Management (SAFER DELETE + EXPLICIT FLUSH)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
    """Manages contacts: add, delete, list known, list recent unknown."""
    current_year = datetime.utcnow().year
    error_message = None
    dummy_phone_key = '0000000000'

    # --- POST Request Handling ---
    if request.method == "POST":
        action = request.form.get("action")
        current_app.logger.debug(f"Contacts POST action: {action}")
        try:
            if action == "add":
                # --- Add Contact Logic (Unchanged) ---
                name = request.form.get("name", "").strip()
                phone_key = request.form.get("phone", "").strip()
                if name and phone_key:
                    if len(phone_key) != 10 or not phone_key.isdigit():
                        flash("Invalid phone key format (10 digits).", "error")
                    else:
                        existing = Contact.query.get(phone_key)
                        if existing:
                            flash(f"Contact key {phone_key} already exists.", "warning")
                        else:
                            new_contact = Contact(phone_number=phone_key, contact_name=name)
                            db.session.add(new_contact)
                            current_app.logger.info(f"Contact '{name}' added to session.")
                else:
                    flash("Name and Phone Key are required.", "error")
                # --- End Add ---

            elif action == "delete":
                # --- Delete Contact Logic (REVISED with FLUSH) ---
                contact_key_to_delete = request.form.get("contact_id")
                if not contact_key_to_delete:
                    flash("No Contact ID provided for deletion.", "error")
                else:
                    contact_to_delete = Contact.query.get(contact_key_to_delete)
                    if not contact_to_delete:
                        flash("Contact not found.", "error")
                    elif contact_key_to_delete == dummy_phone_key:
                         flash("Cannot delete the default reference contact.", "warning")
                    else:
                        # 1. Ensure dummy contact exists
                        dummy_contact = db.session.get(Contact, dummy_phone_key)
                        if not dummy_contact:
                             current_app.logger.error(f"CRITICAL: Dummy contact '{dummy_phone_key}' missing.")
                             flash("Internal error: Default reference missing. Delete aborted.", "danger")
                             contact_to_delete = None # Prevent delete op below
                        else:
                            # 2. Find and update associated messages
                            current_app.logger.info(f"Finding messages for key '{contact_key_to_delete}' to reassign...")
                            messages_to_update = Message.query.filter_by(phone_number=contact_key_to_delete).all()
                            if messages_to_update:
                                current_app.logger.info(f"   Found {len(messages_to_update)} messages. Updating phone_number to '{dummy_phone_key}'...")
                                for msg in messages_to_update:
                                    current_app.logger.debug(f"   Updating Message ID {msg.id}: Setting phone_number FROM {msg.phone_number} TO {dummy_phone_key}")
                                    msg.phone_number = dummy_phone_key
                                    db.session.add(msg) # Ensure it's tracked

                                # *** EXPLICITLY FLUSH MESSAGE UPDATES ***
                                try:
                                    current_app.logger.info("   Attempting explicit flush for message updates ONLY...")
                                    db.session.flush(objects=messages_to_update) # Try to write UPDATEs to DB now
                                    current_app.logger.info("   Explicit flush for message updates successful.")
                                except Exception as flush_err:
                                    # If flush fails here, the problem IS the update itself (e.g., type mismatch)
                                    current_app.logger.error(f"   ❌ Error during explicit flush of message updates: {flush_err}", exc_info=True)
                                    db.session.rollback() # Rollback immediately
                                    flash(f"DB error updating message refs: {flush_err}", "danger")
                                    # Stop further processing for this request
                                    return redirect(url_for('contacts_view'))
                                # *** END FLUSH ***
                            else:
                                current_app.logger.info("   No associated messages found.")

                            # 3. Mark the contact for deletion (only if message update/flush succeeded)
                            current_app.logger.info(f"Marking Contact '{contact_to_delete.contact_name}' for deletion.")
                            db.session.delete(contact_to_delete)
                # --- End Delete ---

            # --- Attempt Final Commit ---
            current_app.logger.info("Attempting final db.session.commit()...")
            db.session.commit() # Should now only commit contact add/delete
            current_app.logger.info("✅ Final db.session.commit() successful.")

            # Flash success messages AFTER commit worked
            if action == "add" and 'new_contact' in locals() and db.session.query(Contact).get(new_contact.phone_number):
                 flash(f"Contact '{new_contact.contact_name}' added.", "success")
            elif action == "delete" and 'contact_to_delete' in locals() and contact_to_delete:
                 flash(f"Contact '{contact_to_delete.contact_name}' deleted.", "success")

            return redirect(url_for("contacts_view")) # Redirect on success

        # --- Exception Handling (Unchanged) ---
        except sqlalchemy_exc.IntegrityError as ie:
             db.session.rollback()
             app.logger.error(f"❌ IntegrityError during contact POST commit: {ie}", exc_info=True)
             app.logger.error(f"   SQL: {getattr(ie, 'statement', 'N/A')}")
             app.logger.error(f"   Params: {getattr(ie, 'params', 'N/A')}")
             flash(f"Database integrity error: {ie}", "error")
        except ValueError:
            db.session.rollback()
            flash("Invalid ID format.", "error")
            app.logger.warning("ValueError during contact POST", exc_info=True)
        except Exception as ex:
             db.session.rollback()
             app.logger.error(f"❌ Unexpected error during contact POST: {ex}", exc_info=True)
             flash(f"An error occurred: {ex}", "danger")

        return redirect(url_for("contacts_view")) # Redirect on failure

    # --- GET Request Handling (Unchanged - Assumed OK) ---
    # (Keep the existing GET logic)
    known_contacts_list = []
    unknown_recent_numbers = []
    try:
        known_contacts_list = Contact.query.order_by(Contact.contact_name).all()
        known_numbers_set = {c.phone_number for c in known_contacts_list}
        # ... (rest of GET logic with window function) ...
        from sqlalchemy import select, func, over
        row_num_subq = select(Message.phone_number, Message.timestamp, func.row_number().over(partition_by=Message.phone_number, order_by=Message.timestamp.desc()).label('rn')).subquery('ranked_messages')
        latest_distinct_numbers_query = select(row_num_subq.c.phone_number).where(row_num_subq.c.rn == 1).order_by(row_num_subq.c.timestamp.desc()).limit(50)
        recent_distinct_numbers_result = db.session.execute(latest_distinct_numbers_query).all()
        recent_distinct_numbers = [num for (num,) in recent_distinct_numbers_result]
        count = 0
        seen_unknown = set()
        for number in recent_distinct_numbers:
            if number and number not in known_numbers_set and number not in seen_unknown:
                unknown_recent_numbers.append(number)
                seen_unknown.add(number)
                count += 1
                if count >= 10: break
    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error loading contacts page data: {ex}", exc_info=True)
        error_message = f"Error loading contacts page data: {ex}"
        flash(error_message, "danger")
    return render_template(
        "contacts.html",
        known_contacts=known_contacts_list,
        unknown_recent_numbers=unknown_recent_numbers,
        error=error_message,
        current_year=current_year,
    )


# ... (rest of main.py, including clear_contacts_debug with session expiration) ...

# ──────────────────────────────────────────────────────────────────────────────
#  Ask (OpenAI Integration) - Assuming OK, leaving as is
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    current_year = datetime.utcnow().year
    openai_response = None
    error_message = None
    current_query = None

    if request.method == "POST":
        current_query = request.form.get("query", "").strip()
        if not current_query:
            flash("Please enter a question or prompt.", "warning")
        elif not openai.api_key:
            error_message = "OpenAI API key is not configured."
            app.logger.error(f"ERROR: {error_message}")
            flash(error_message, "danger")
        else:
            try:
                app.logger.info(f"Sending query to OpenAI: '{current_query}'")
                completion = openai.ChatCompletion.create(
                    model=os.getenv("OPENAI_MODEL", "gpt-3.5-turbo"),
                    messages=[
                        {"role": "system", "content": os.getenv("OPENAI_SYSTEM_PROMPT", "You are a helpful assistant.")},
                        {"role": "user", "content": current_query}
                    ],
                    temperature=float(os.getenv("OPENAI_TEMPERATURE", 0.7)),
                    max_tokens=int(os.getenv("OPENAI_MAX_TOKENS", 150)),
                )
                openai_response = completion.choices[0].message["content"].strip()
                app.logger.info("Received response from OpenAI.")
            except openai.error.AuthenticationError as auth_err:
                 error_message = f"OpenAI Auth Error: {auth_err}."
                 app.logger.error(f"ERROR: {error_message}")
                 flash(error_message, "danger")
            except openai.error.RateLimitError as rate_err:
                 error_message = f"OpenAI Rate Limit Exceeded: {rate_err}."
                 app.logger.warning(f"ERROR: {error_message}")
                 flash(error_message, "warning")
            except openai.error.OpenAIError as api_err:
                 error_message = f"OpenAI API Error: {api_err}"
                 app.logger.error(f"ERROR: {error_message}")
                 flash(error_message, "danger")
            except Exception as ex:
                error_message = f"Unexpected OpenAI error: {ex}"
                app.logger.error(f"ERROR: {error_message}")
                flash(error_message, "danger")
                traceback.print_exc()

    return render_template(
        "ask.html",
        response=openai_response,
        error=error_message,
        current_query=current_query,
        current_year=current_year,
    )

# ──────────────────────────────────────────────────────────────────────────────
#  Gallery Routes - Assuming OK, leaving as is, using logger
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery_static")
def gallery_static():
    error_message = None
    image_paths = []
    static_upload_folder = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, "uploads"))

    if not os.path.isdir(static_upload_folder):
         app.logger.warning(f"Static upload folder not found: {static_upload_folder}")
         flash(f"Static upload folder not found.", "warning")
         # Render empty gallery page
         return render_template("gallery.html", image_items=[], gallery_title="Static Uploads (Folder Not Found)", error=f"Folder not found", current_year=datetime.utcnow().year)

    try:
        allowed_extensions = {".jpg", ".png", ".gif", ".jpeg", ".webp", ".heic", ".avif", ".svg"}
        upload_dir_name = os.path.basename(static_upload_folder)
        for filename in os.listdir(static_upload_folder):
            if os.path.splitext(filename)[1].lower() in allowed_extensions:
                full_path = os.path.join(static_upload_folder, filename)
                if os.path.isfile(full_path):
                    relative_path = os.path.join(upload_dir_name, filename)
                    image_paths.append({"path": relative_path, "message_id": None, "index": None})
    except Exception as ex:
        error_message = f"Error loading static gallery: {ex}"
        app.logger.error(f"❌ {error_message}")
        flash(error_message, "danger")
        traceback.print_exc()

    return render_template("gallery.html", image_items=image_paths, gallery_title="Static Uploads Gallery", error=error_message, current_year=datetime.utcnow().year)

@app.route("/unsorted")
def unsorted_gallery():
    unsorted_items_list = []
    properties_list = []
    error_message = None
    try:
        msgs_with_unsorted_media = (
            Message.query
            .filter(Message.property_id.is_(None), Message.local_media_paths.isnot(None), Message.local_media_paths != '')
            .order_by(Message.timestamp.desc()).all() )

        for msg in msgs_with_unsorted_media:
            paths = [p.strip() for p in (msg.local_media_paths or "").split(",") if p.strip()]
            for idx, relative_path in enumerate(paths):
                 unsorted_items_list.append({"message": msg, "path": relative_path, "index": idx})

        properties_list = Property.query.order_by(Property.name).all()
    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error loading unsorted gallery: {ex}")
        traceback.print_exc()
        error_message = f"Error loading unsorted media gallery: {ex}"
        flash(error_message, "danger")

    return render_template("unsorted.html", unsorted_items=unsorted_items_list, properties=properties_list, error=error_message, current_year=datetime.utcnow().year)

@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    prop = Property.query.get_or_404(property_id)
    image_items_list = []
    error_message = None
    app.logger.debug(f"--- Loading Gallery for Property ID: {property_id} ({prop.name}) ---")
    try:
        msgs_for_property = (
            Message.query
            .filter_by(property_id=property_id)
            .filter(Message.local_media_paths.isnot(None), Message.local_media_paths != '')
            .order_by(Message.timestamp.desc()).all() )
        app.logger.debug(f"Found {len(msgs_for_property)} messages for property.")

        for msg in msgs_for_property:
            paths = [p.strip() for p in (msg.local_media_paths or "").split(",") if p.strip()]
            for idx, relative_path in enumerate(paths):
                 image_items_list.append({"path": relative_path, "message_id": msg.id, "index": idx})
        app.logger.debug(f"Processed {len(image_items_list)} image items for property gallery.")

    except Exception as ex:
         app.logger.error(f"❌ Error loading gallery for property {property_id}: {ex}")
         traceback.print_exc()
         error_message = f"Error loading gallery: {ex}"
         flash(error_message, "danger")

    return render_template("gallery.html", image_items=image_items_list, property=prop, gallery_title=f"Gallery for {prop.name}", error=error_message, current_year=datetime.utcnow().year)

@app.route("/galleries")
def galleries_overview():
    gallery_summaries_list = []
    error_message = None
    unsorted_image_count = 0
    try:
        properties = Property.query.order_by(Property.name).all()
        for prop in properties:
            media_message_count = db.session.query(func.count(Message.id)).filter(
                 Message.property_id == prop.id, Message.local_media_paths.isnot(None), Message.local_media_paths != ''
              ).scalar() or 0

            thumbnail_relative_path = None
            if media_message_count > 0:
                latest_message_with_media = Message.query.filter(
                     Message.property_id == prop.id, Message.local_media_paths.isnot(None), Message.local_media_paths != ''
                 ).order_by(Message.timestamp.desc()).first()
                if latest_message_with_media and latest_message_with_media.local_media_paths:
                    potential_thumbs = [p.strip() for p in latest_message_with_media.local_media_paths.split(',') if p.strip()]
                    if potential_thumbs:
                         thumbnail_relative_path = potential_thumbs[0] # Just take the first path

            gallery_summaries_list.append({"property": prop, "count": media_message_count, "thumb": thumbnail_relative_path})

        # Count unsorted separately
        unsorted_msgs_query = Message.query.filter(
            Message.property_id.is_(None), Message.local_media_paths.isnot(None), Message.local_media_paths != ''
        ).all()
        for msg in unsorted_msgs_query:
            paths = [p.strip() for p in (msg.local_media_paths or "").split(",") if p.strip()]
            unsorted_image_count += len(paths)

    except Exception as ex:
         app.logger.error(f"❌ Error loading galleries overview: {ex}")
         traceback.print_exc()
         error_message = f"Error loading galleries overview: {ex}"
         flash(error_message, "danger")

    return render_template("galleries_overview.html", gallery_summaries=gallery_summaries_list, unsorted_count=unsorted_image_count, error=error_message, current_year=datetime.utcnow().year)

@app.route("/gallery", endpoint="gallery_view")
def gallery_view():
    all_image_items_list = []
    error_message = None
    upload_dir = current_app.config.get('UPLOAD_FOLDER')
    app.logger.debug(f"--- Loading Combined Gallery View ---")
    if upload_dir:
        try:
            app.logger.debug(f"Listing upload directory: {upload_dir}")
            current_files = os.listdir(upload_dir)
            app.logger.debug(f"Files in upload dir ({len(current_files)}): {current_files[:10]}...") # Log first few
        except Exception as list_err:
            app.logger.warning(f"Could not list upload directory: {list_err}")
    else:
         app.logger.warning("UPLOAD_FOLDER not configured, cannot list files.")

    try:
        all_msgs_with_media = (
            Message.query
            .options(joinedload(Message.property))
            .filter(Message.local_media_paths.isnot(None), Message.local_media_paths != '')
            .order_by(Message.timestamp.desc()).all() )
        app.logger.debug(f"Found {len(all_msgs_with_media)} total messages with media paths.")

        for msg in all_msgs_with_media:
            paths = [p.strip() for p in (msg.local_media_paths or "").split(",") if p.strip()]
            for idx, relative_path in enumerate(paths):
                 all_image_items_list.append({
                    "path": relative_path, "message_id": msg.id, "index": idx,
                    "property_name": msg.property.name if msg.property else "Unsorted",
                    "timestamp": msg.timestamp })
        app.logger.debug(f"Processed {len(all_image_items_list)} total image items.")

    except Exception as ex:
         app.logger.error(f"❌ Error loading combined gallery: {ex}")
         traceback.print_exc()
         error_message = f"Error loading combined media gallery: {ex}"
         flash(error_message, "danger")

    return render_template("gallery.html", image_items=all_image_items_list, gallery_title="All Media Gallery", property=None, error=error_message, current_year=datetime.utcnow().year)


# ──────────────────────────────────────────────────────────────────────────────
#  Health check endpoint
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ping")
def ping_route():
    """Basic health check, includes DB connectivity."""
    try:
        db.session.execute(text("SELECT 1"))
        return "Pong! DB OK.", 200
    except Exception as e:
        app.logger.error(f"DB Health Check Failed: {e}")
        return f"Pong! DB Error: {e}", 503


# ──────────────────────────────────────────────────────────────────────────────
#  DEBUG: List Uploaded Files Endpoint
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/list-uploads")
def list_uploads():
    """Debug route to list files currently present in the upload directory via JSON."""
    upload_dir = current_app.config.get('UPLOAD_FOLDER')
    if not upload_dir:
         return jsonify({"error": "UPLOAD_FOLDER not configured.", "path": None}), 500

    app.logger.info(f"ℹ️ [list-uploads] Checking directory: {upload_dir}")
    try:
        if not os.path.isdir(upload_dir):
             app.logger.warning(f"⚠️ [list-uploads] Upload directory not found.")
             return jsonify({"error": "Upload directory not found.", "path": upload_dir}), 404

        files = os.listdir(upload_dir)
        app.logger.info(f"✅ [list-uploads] Found {len(files)} raw items.")
        file_details = []
        for f in files:
            try:
                full_path = os.path.join(upload_dir, f)
                if os.path.isfile(full_path):
                     stat_result = os.stat(full_path)
                     file_details.append({
                        "name": f, "size_bytes": stat_result.st_size,
                        "modified_utc": datetime.utcfromtimestamp(stat_result.st_mtime).isoformat() + "Z" })
            except Exception as stat_err:
                 app.logger.warning(f"⚠️ [list-uploads] Error stating file {f}: {stat_err}")
                 file_details.append({"name": f, "error": str(stat_err)})

        return jsonify({"directory": upload_dir, "file_count": len(file_details), "files": file_details})
    except Exception as e:
        app.logger.error(f"❌ [list-uploads] Error listing directory: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Failed to list uploads: {e}", "path": upload_dir}), 500

# ──────────────────────────────────────────────────────────────────────────────
#  DEBUG: Clear Contacts Route (SAFER VERSION - Use with caution!)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/clear-contacts-debug", methods=['POST'])
def clear_contacts_debug():
    """Temporary route to clear all contacts and update message references."""
    current_app.logger.warning("--- [clear-contacts-debug] Route accessed ---")
    dummy_phone_key = '0000000000'
    dummy_contact_name = 'Deleted Reference'
    update_count = 0
    num_deleted = 0

    if not dummy_phone_key or len(dummy_phone_key) != 10 or not dummy_phone_key.isdigit():
         app.logger.critical(f"   ❌ Invalid dummy_phone_key defined: '{dummy_phone_key}'. Aborting.")
         flash("Internal config error: Invalid dummy phone key.", "danger")
         return redirect(url_for('contacts_view'))

    try:
        # 1: Ensure Dummy Contact Exists
        dummy_contact = db.session.get(Contact, dummy_phone_key)
        if not dummy_contact:
            app.logger.info(f"   Creating dummy contact: {dummy_phone_key}")
            dummy_contact = Contact(phone_number=dummy_phone_key, contact_name=dummy_contact_name)
            db.session.add(dummy_contact)
            try:
                db.session.commit()
                app.logger.info("   Dummy contact created/committed.")
            except Exception as e_dummy:
                db.session.rollback()
                app.logger.error(f"   ❌ Error creating dummy contact, cannot proceed: {e_dummy}", exc_info=True)
                flash(f"Error creating dummy contact: {e_dummy}. Aborting.", "danger")
                return redirect(url_for('contacts_view'))
        else:
             app.logger.info("   Dummy contact already exists.")

        # 2: Identify Real Contacts
        contacts_to_delete = Contact.query.filter(Contact.phone_number != dummy_phone_key).all()
        contact_keys = [c.phone_number for c in contacts_to_delete]
        app.logger.info(f"ℹ️ Found {len(contact_keys)} real contact keys to process: {contact_keys}")

        # 3: Update Associated Messages
        if contact_keys:
            app.logger.info(f"   Attempting bulk update on Messages for keys {contact_keys} -> '{dummy_phone_key}'...")
            try:
                update_count = Message.query.filter(
                    Message.phone_number.in_(contact_keys)
                ).update(
                    {Message.phone_number: dummy_phone_key}, synchronize_session=False # Use 'fetch' if issues persist, but 'False' is often fine
                )
                app.logger.info(f"   ✅ Bulk update successful for {update_count} message records.")

                # CRITICAL: Expire updated messages from session
                # This clears SQLAlchemy's cache for objects that might have been affected by the bulk update.
                app.logger.info("   Expiring ALL objects from session identity map to prevent stale state issues...")
                db.session.expire_all()
                app.logger.info("   Session objects expired.")

            except Exception as update_err:
                 app.logger.error(f"   ❌ Error during bulk update of Messages: {update_err}", exc_info=True)
                 flash(f"Error updating message records: {update_err}", "danger")
                 db.session.rollback()
                 return redirect(url_for('contacts_view'))
        else:
            app.logger.info("   No real contacts found, skipping message update step.")

        # 4: Delete Real Contacts
        if contacts_to_delete:
            app.logger.info(f"   Attempting to delete {len(contacts_to_delete)} real contact records...")
            try:
                # Delete using the objects fetched earlier
                for contact_obj in contacts_to_delete:
                     db.session.delete(contact_obj)
                num_deleted = len(contacts_to_delete)
                app.logger.info(f"   ✅ Marked {num_deleted} real contacts for deletion in session.")
            except Exception as delete_err:
                 app.logger.error(f"   ❌ Error during deletion of Contacts: {delete_err}", exc_info=True)
                 flash(f"Error deleting contact records: {delete_err}", "danger")
                 db.session.rollback()
                 return redirect(url_for('contacts_view'))
        else:
            num_deleted = 0
            app.logger.info("   No real contact records to delete.")

        # 5: Commit All Changes
        app.logger.info("   Attempting final commit for updates and deletions...")
        db.session.commit()
        message = f"Cleared {num_deleted} real contacts. Updated {update_count} msg refs (if any)."
        app.logger.info(f"✅ [clear-contacts-debug] {message}")
        flash(message, "success")

    except Exception as e:
        db.session.rollback()
        message = f"Error during clear contacts operation: {e}"
        app.logger.error(f"❌ [clear-contacts-debug] {message}", exc_info=True)
        flash(message, "danger")

    # Redirect back to messages overview after clearing
    redirect_target_url = url_for('messages_view')
    app.logger.info(f"--- [clear-contacts-debug] Redirecting to {redirect_target_url} ---")
    return redirect(redirect_target_url)


# ──────────────────────────────────────────────────────────────────────────────
#  Debug URL map (Prints registered routes at startup)
# ──────────────────────────────────────────────────────────────────────────────
def print_url_map(app_instance):
     """Logs all registered URL routes."""
     with app_instance.app_context():
        app.logger.info("\n--- URL MAP ---")
        rules = sorted(app_instance.url_map.iter_rules(), key=lambda r: r.endpoint)
        for rule in rules:
            methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
            app.logger.info(f"{rule.endpoint:30} {methods:<15} {rule.rule}")
        app.logger.info("--- END URL MAP ---\n")

print_url_map(app)


# ──────────────────────────────────────────────────────────────────────────────
#  Entry Point for Development Server (if script is run directly)
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.logger.info("🚀 Starting Flask development server...")
    host = os.environ.get("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_RUN_PORT", 8080))
    debug_mode = os.environ.get("FLASK_DEBUG", "True").lower() in ['true', '1', 't']
    app.logger.info(f"Running on http://{host}:{port} with debug mode: {debug_mode}")
    app.run(host=host, port=port, debug=debug_mode)