# -*- coding: utf-8 -*-
import os
import json  # Ensure json is imported for JSONDecodeError
import logging  # Import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone  # Added timezone for now()
import traceback
import requests  # For OpenPhone API calls
import mimetypes  # Added for notifications uploads
import werkzeug  # Keep if abort uses it
import csv  # For reading CSV file
import re   # For cleaning phone numbers using regular expressions
import click # For creating the command line command argument
from werkzeug.utils import secure_filename
from flask_migrate import Migrate


# Import necessary Flask components
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    url_for,
    redirect,
    flash,
    send_from_directory,
    current_app,
    abort,
)
from dotenv import load_dotenv

load_dotenv()
# Import SQLAlchemy components
from sqlalchemy import (
    text,
    func,
    exc as sqlalchemy_exc,
    select,
    over,
    String,
    cast,
    or_,
    not_,
    and_,
)
from sqlalchemy.orm import attributes, joinedload,  aliased # <-- ADDED aliased
from sqlalchemy.sql.expression import null  # For setting NULL explicitly if needed

import openai

# --- Create Extension Objects ---
migrate = Migrate() # <-- ADD THIS LINE if it's missing!


# Import local modules
from extensions import db

# Import ALL your models needed here
from models import Contact, Message, Property, Tenant, NotificationHistory
from webhook_route import (
    webhook_bp,
)  # Assuming webhook_bp is correctly defined elsewhere

# Assuming send_email and wrap_email_html are in email_utils.py:
from email_utils import send_email, wrap_email_html

# ──────────────────────────────────────────────────────────────────────────────
#  App configuration
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
STATIC_ROOT_FOR_WHITENOISE = os.path.join(app.root_path, "static") # <-- MAKE SURE THIS LINE EXISTS!


# --- Logging Setup ---
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s : %(message)s"
)
app.logger.setLevel(logging.DEBUG)
# Log WhiteNoise config after logger is set up
# app.logger.info(f"ℹ️ WhiteNoise configured with root: {STATIC_ROOT_FOR_WHITENOISE} and prefix /static/"



# --- Flask App Configuration ---
INSTANCE = Path(app.instance_path)
INSTANCE.mkdir(exist_ok=True)
DB_FILE = INSTANCE / "messages.db"

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", f"sqlite:///{DB_FILE}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
app.config["FLASK_SECRET"] = os.getenv(
    "FLASK_SECRET", "a_very_strong_default_secret_key_for_dev_only"
)
app.secret_key = app.config["FLASK_SECRET"]
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# --- Initialize Extensions with App ---
db.init_app(app)
migrate.init_app(app, db)

# --- Static and Upload Folder Setup ---
app.static_folder = os.path.join(app.root_path, "static")  # Still needed for url_for
UPLOAD_FOLDER = os.path.join(app.static_folder, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.logger.info(f"ℹ️ Configured UPLOAD_FOLDER: {app.config['UPLOAD_FOLDER']}")

# --- Configure OpenAI ---
openai.api_key = os.getenv("OPENAI_API_KEY")
# --- End App Setup ---


# --- Helper Function for Sending SMS via OpenPhone API ---
def send_openphone_sms(recipient_phone, message_body):
    api_token = os.getenv("OPENPHONE_API_TOKEN")
    sending_number = os.getenv("OPENPHONE_FROM") or os.getenv(
        "OPENPHONE_SENDING_NUMBER"
    )
    log_func = getattr(current_app, "logger", logging.getLogger(__name__))
    if not api_token or not sending_number:
        log_func.error("OpenPhone API Token or Sending Number not configured.")
        return False  # Indented block
    api_url = "https://api.openphone.com/v1/messages"
    headers = {
        "Authorization": api_token,
        "Content-Type": "application/json",
    }
    payload = {
        "from": sending_number,
        "to": [recipient_phone],
        "content": message_body,
    }
    try:
        log_func.debug(
            f"Sending OpenPhone SMS To: {payload['to']}, From: {payload['from']}"
        )
        log_func.debug(f"Authorization Header Type Sent: Direct Token")
        response = requests.post(api_url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        response_data = response.json()
        message_id = response_data.get("id", "N/A")
        status = response_data.get("status", "N/A")
        log_func.info(
            f"Successfully sent SMS via OpenPhone to {recipient_phone}. Response ID: {message_id}, Status: {status}"
        )
        return True
    except requests.exceptions.HTTPError as http_err:
        log_func.error(
            f"HTTP Error sending OpenPhone SMS to {recipient_phone}: {http_err}"
        )
        try:
            error_details = http_err.response.json()
            log_func.error(
                f"OpenPhone API Error Response: Status={http_err.response.status_code}, Details={error_details}"
            )
        except json.JSONDecodeError:
            try:
                log_func.error(
                    f"OpenPhone API Error Response: Status={http_err.response.status_code}, Body={http_err.response.text}"
                )
            except Exception:
                pass  # Correct indentation
        return False
    except requests.exceptions.RequestException as req_err:
        log_func.error(
            f"Request Exception sending OpenPhone SMS to {recipient_phone}: {req_err}"
        )
        return False  # Indented block
    except Exception as e:
        log_func.error(
            f"Unexpected error in send_openphone_sms to {recipient_phone}: {e}",
            exc_info=True,
        )
        return False  # Indented block


# --- Database Initialization Helper ---
def initialize_database(app_context):
    with app_context:
        app.logger.info("🔄 Initializing Database...")
        try:
            db.create_all()
            app.logger.info("✅ Tables created/verified.")
            try:
                db.session.execute(text("ALTER TABLE messages ADD COLUMN sid VARCHAR"))
                db.session.commit()
                app.logger.info("✅ Ensured messages.sid column exists.")
            except Exception as alter_err:
                db.session.rollback()
                err_str = str(alter_err).lower()
                if "already exists" in err_str or "duplicate column name" in err_str:
                    app.logger.info("✅ messages.sid column already exists.")
                else:
                    app.logger.warning(f"⚠️ Could not add 'sid' column: {alter_err}")
            if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
                try:
                    sequence_name_query = text(
                        "SELECT pg_get_serial_sequence('messages', 'id');"
                    )
                    result = db.session.execute(sequence_name_query).scalar()
                    if result:
                        sequence_name = result
                        max_id_query = text("SELECT COALESCE(MAX(id), 0) FROM messages")
                        max_id = db.session.execute(max_id_query).scalar()
                        next_val = max_id + 1
                        reset_seq_query = text(
                            f"SELECT setval('{sequence_name}', :next_val, false)"
                        )
                        db.session.execute(reset_seq_query, {"next_val": next_val})
                        db.session.commit()
                        app.logger.info(
                            f"🔁 messages.id sequence ('{sequence_name}') reset to {next_val}."
                        )
                    else:
                        app.logger.warning(
                            "⚠️ Could not determine sequence name for messages.id."
                        )
                except Exception as seq_err:
                    db.session.rollback()
                    app.logger.error(
                        f"❌ Error resetting PostgreSQL sequence: {seq_err}",
                        exc_info=True,
                    )
            else:
                app.logger.info("ℹ️ Skipping sequence reset (not PostgreSQL).")
            app.logger.info("✅ Database initialization complete.")
        except Exception as e:
            db.session.rollback()
            app.logger.critical(
                f"❌ FATAL STARTUP ERROR during database initialization: {e}",
                exc_info=True,
            )


# --- URL Map Helper ---
def print_url_map(app_instance):
    with app_instance.app_context():
        app.logger.info("\n--- URL MAP ---")
        rules = sorted(app_instance.url_map.iter_rules(), key=lambda r: r.endpoint)
        for rule in rules:
            methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
            app.logger.info(f"{rule.endpoint:30} {methods:<15} {rule.rule}")
        app.logger.info("--- END URL MAP ---\n")


# ──────────────────────────────────────────────────────────────────────────────
#  INITIALIZE DATABASE & PRINT URL MAP
# ──────────────────────────────────────────────────────────────────────────────
with app.app_context():
    initialize_database(app.app_context())

# --- ADD THIS LINE TO ALWAYS PRINT THE MAP ON STARTUP ---
print_url_map(app)
# --- END ADDED LINE ---

# This conditional block below is now optional, can be kept or removed
if os.environ.get("FLASK_DEBUG", "false").lower() in ["true", "1", "t"]:
    # print_url_map(app) # Already called unconditionally above
    pass # Or just leave the conditional block out completely

# ──────────────────────────────────────────────────────────────────────────────
#  Jinja Context Processors & Custom Filters (Optional)
# ──────────────────────────────────────────────────────────────────────────────
@app.context_processor
def inject_now():
    return {"current_year": datetime.now(timezone.utc).year}


# ──────────────────────────────────────────────────────────────────────────────
#  ROUTES
# ──────────────────────────────────────────────────────────────────────────────

# --- MESSAGES, CONTACTS, ASSIGNMENT ROUTES ---
@app.route("/messages")
def messages_view():
     """Displays message overview or detail for a specific number."""
     target_phone_number=request.args.get("phone_number")
     app.logger.debug(f"Accessing messages_view. Target phone: {target_phone_number}")
     try:
        if target_phone_number:
            app.logger.debug(f"--- Loading DETAIL view for {target_phone_number} ---")
            # Eager load property and contact data related to the messages
            msgs_for_number = Message.query.options(
                joinedload(Message.property),
                joinedload(Message.contact)
            ).filter(
                Message.phone_number==target_phone_number
            ).order_by(Message.timestamp.desc()).all()
            app.logger.debug(f"Query for {target_phone_number} returned {len(msgs_for_number)} message objects.")

            # Determine contact info
            is_known = False
            contact_name = None
            if msgs_for_number and msgs_for_number[0].contact:
                contact = msgs_for_number[0].contact
                is_known = True
                contact_name = contact.contact_name if contact.contact_name else contact.phone_number
            # Corrected fallback logic
            if not is_known:
                 contact = db.session.get(Contact, target_phone_number)
                 if contact:
                      is_known = True
                      contact_name = contact.contact_name if contact.contact_name else contact.phone_number

            app.logger.debug(f"Contact lookup {target_phone_number}: known={is_known}, name='{contact_name}'")
            # Render detail view
            return render_template("messages_detail.html", phone_number=target_phone_number, messages=msgs_for_number, is_known=is_known, contact_name=contact_name)

        else:
            app.logger.debug("--- Loading OVERVIEW view ---")
            msgs_overview = (
                Message.query.options(
                    joinedload(Message.property), joinedload(Message.contact)
                )
                .order_by(Message.timestamp.desc())
                .limit(100)
                .all()
            )
            app.logger.debug(f"Query overview returned {len(msgs_overview)} messages.")
            properties_list = Property.query.order_by(Property.name).all()
            return render_template(
                "messages_overview.html",
                messages=msgs_overview,
                properties=properties_list,
            )
     except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error messages_view: {ex}", exc_info=True)
        error_msg = f"Error: {ex}"
        flash(error_msg, "danger")
        template_name = (
            "messages_detail.html" if target_phone_number else "messages_overview.html"
        )
        try:
            # Render template even on error, passing the error message
            return render_template(template_name, messages=[], properties=[], phone_number=target_phone_number, is_known=False, contact_name=None, error=error_msg)
        except:
            # Ultimate fallback if rendering error template fails
            return redirect(url_for('index'))


# --- INDEX ROUTE ---
@app.route("/")
def index():
    """Displays the main dashboard including AI summaries for last 5 contacts."""
    db_status = "?"; summary_today = "?"; summary_week = "?"
    ai_summaries = [] # List to hold AI summary results
    error_message = None # For general page errors

    try:
        # --- Basic Stats (Existing Logic) ---
        db.session.execute(text("SELECT 1"))
        db_status = "Connected"
        now_utc = datetime.now(timezone.utc)
        start_today_utc = datetime.combine(now_utc.date(), datetime.min.time(), tzinfo=timezone.utc)
        start_week_utc  = start_today_utc - timedelta(days=start_today_utc.weekday())
        count_today = (db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_today_utc).scalar() or 0)
        count_week = (db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_week_utc).scalar() or 0)
        summary_today = f"{count_today} messages today."
        summary_week  = f"{count_week} messages this week."

        # --- AI Summaries Logic ---
        current_app.logger.info("Starting AI summary generation for dashboard...")

        message_alias = aliased(Message); contact_alias = aliased(Contact) # Uses aliased
        subq = (select(message_alias.phone_number, message_alias.timestamp, contact_alias.contact_name, func.row_number().over(partition_by=message_alias.phone_number, order_by=message_alias.timestamp.desc()).label('rn')).outerjoin(contact_alias, message_alias.phone_number == contact_alias.phone_number).where(message_alias.direction == 'incoming').subquery('ranked_messages'))
        latest_contacts_query = (select(subq.c.phone_number, subq.c.contact_name).where(subq.c.rn == 1).order_by(subq.c.timestamp.desc()).limit(5))
        latest_contacts_result = db.session.execute(latest_contacts_query).all()
        current_app.logger.info(f"Found {len(latest_contacts_result)} recent contacts for AI summary.")

        if not openai.api_key:
             current_app.logger.error("OpenAI API key not configured. Skipping AI summaries.")
             error_message = "AI Summaries disabled: OpenAI API key not set."
        elif latest_contacts_result:
            for phone_number, contact_name in latest_contacts_result:
                summary_info = {'phone': phone_number, 'name': contact_name or phone_number, 'summary': 'Could not generate summary.', 'error': None}
                try:
                    recent_messages = (Message.query.filter(Message.phone_number == phone_number).order_by(Message.timestamp.desc()).limit(3).all())
                    recent_messages.reverse()
# --- START DEBUGGING BLOCK ---
                    if recent_messages: # Check if list is not empty
                        m_debug = recent_messages[0] # Get one message object to inspect
                        current_app.logger.debug(f"--- Inspecting Message Object (ID: {getattr(m_debug, 'id', 'N/A')}) ---")
                        current_app.logger.debug(f"Type of m_debug: {type(m_debug)}")
                        try:
                            # Use vars() or dir() to see attributes
                            current_app.logger.debug(f"vars(m_debug): {vars(m_debug)}")
                        except TypeError: # Handle potential errors if vars() doesn't work
                             try:
                                 current_app.logger.debug(f"dir(m_debug): {dir(m_debug)}")
                             except Exception as inspect_err:
                                  current_app.logger.error(f"Could not inspect attributes: {inspect_err}")
                        current_app.logger.debug(f"--- End Inspecting Message Object ---")
                    # --- END DEBUGGING BLOCK ---
                    # *** Use m.message instead of m.text or m.body ***
                    message_texts = "\n".join([f"- {m.message}" for m in recent_messages if m.message]) # CORRECTED LINE

                    if not message_texts:
                        summary_info['summary'] = "No recent message text found."
                        ai_summaries.append(summary_info)
                        continue # Skip to next contact if no text found

                    display_name = contact_name if contact_name and not contact_name.isdigit() else phone_number
                    prompt = (f"You are an assistant for Sin City Rentals property management. Summarize the main point, request, or topic from these recent SMS messages from '{display_name}'. Be very concise (ideally one short sentence like 'Requested info about XYZ property' or 'Sent quote for $ABC'). Focus on the *latest* message if applicable. Mention specific properties or dollar amounts if present.\n\nRecent Messages (oldest first):\n{message_texts}")
                    current_app.logger.debug(f"Calling OpenAI for {display_name}...")
                    response = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role": "system", "content": "Provide a concise one-sentence summary."}, {"role": "user", "content": prompt}], temperature=0.2, max_tokens=60)
                    ai_summary = response.choices[0].message.content.strip(); summary_info['summary'] = ai_summary; current_app.logger.debug(f"OpenAI summary for {display_name}: {ai_summary}")
                except AttributeError as attr_err:
                    # Catch the specific error if it somehow still occurs
                    current_app.logger.error(f"AttributeError during AI processing for {phone_number}: {attr_err}", exc_info=True)
                    summary_info['error'] = f"Data error: {attr_err}"
                    summary_info['summary'] = "Data processing error."
                except Exception as ai_err:
                    current_app.logger.error(f"OpenAI API call failed for {phone_number}: {ai_err}", exc_info=True); summary_info['error'] = f"AI analysis failed: {ai_err}"; summary_info['summary'] = "AI analysis failed."
                ai_summaries.append(summary_info)
        else:
             current_app.logger.info("No recent incoming messages found to summarize.")
    except NameError as ne:
         # Catch the specific NameError for aliased if import was missed
         db.session.rollback(); app.logger.error(f"❌ Error loading index page (Likely missing import): {ne}", exc_info=True); error_message = f"Error initializing AI Summaries: {ne}";
         if db_status != "Connected": db_status = "Error"
    except Exception as ex:
        db.session.rollback(); app.logger.error(f"❌ Error loading index page: {ex}", exc_info=True); error_message = f"Error loading page data: {ex}"
        if db_status != "Connected": db_status = f"Error: {ex}"

    return render_template(
        "index.html",
        db_status=db_status,
        summary_today=summary_today,
        summary_week=summary_week,
        ai_summaries=ai_summaries,
        error=error_message
    )

# --- PROPERTY ROUTES ---

@app.route('/properties')
def properties_list_view():
    """Displays a list of all properties."""
    # Fetch all properties from the database, ordered by name perhaps
    properties = Property.query.order_by(Property.name).all()
    return render_template('properties_list.html', properties=properties)

@app.route('/property/<int:property_id>')
def property_detail_view(property_id):
    """Displays detailed information for a specific property."""
    try:
        property_obj = db.session.get(Property, property_id)
        if not property_obj:
            flash(f"Property with ID {property_id} not found.", "warning")
            return redirect(url_for("properties_list_view"))
        
        # Get recent messages for this property
        recent_messages = (
            Message.query.options(joinedload(Message.contact))
            .filter(Message.property_id == property_id)
            .order_by(Message.timestamp.desc())
            .limit(10)
            .all()
        )
        
        return render_template(
            'property_detail.html', 
            property=property_obj,
            recent_messages=recent_messages
        )
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error loading property {property_id}: {e}", exc_info=True)
        flash(f"Error loading property details: {e}", "danger")
        return redirect(url_for("properties_list_view"))

@app.route("/assign_property", methods=["POST"])
def assign_property():
    message_id_str = request.form.get("message_id")
    property_id_str = request.form.get("property_id")
    redirect_url = request.referrer or url_for("index")
    current_app.logger.info(
        f"Received assignment request via POST: message_id='{message_id_str}', property_id='{property_id_str}', referrer='{request.referrer}'"
    )
    message_id = None
    if message_id_str and message_id_str.isdigit():
        message_id = int(message_id_str)
    else:
        flash("Invalid or missing Message ID provided.", "danger")
        current_app.logger.warning(
            "Assign property failed: Invalid or missing message_id"
        )
        return redirect(redirect_url)
    message = db.session.get(Message, message_id)
    if not message:
        flash(f"Message with ID {message_id} not found.", "warning")
        current_app.logger.warning(
            f"Assign property failed: Message ID {message_id} not found."
        )
        return redirect(redirect_url)
    target_property_id = None
    prop_name_for_flash = "Unassigned"
    if property_id_str and property_id_str.isdigit() and int(property_id_str) > 0:
        target_property_id = int(property_id_str)
        property_obj = db.session.get(Property, target_property_id)
        if not property_obj:
            flash(f"Selected Property (ID: {target_property_id}) not found.", "warning")
            current_app.logger.warning(
                f"Assign property failed: Target Property ID {target_property_id} not found."
            )
            return redirect(redirect_url)
        prop_name_for_flash = property_obj.name
    elif (
        property_id_str is None
        or property_id_str == ""
        or property_id_str.lower() == "none"
    ):
        target_property_id = None
        current_app.logger.info(
            f"Request to unassign property from message {message_id}"
        )
    else:
        flash(
            f"Invalid Property ID format: {property_id_str}. Please select a valid property or leave blank to unassign.",
            "warning",
        )
        current_app.logger.warning(
            f"Assign property failed: Invalid property_id format '{property_id_str}' for message {message_id}."
        )
        return redirect(redirect_url)
    try:
        message.property_id = target_property_id
        db.session.commit()
        flash(
            f'Message #{message_id} assigned to property "{prop_name_for_flash}" successfully.',
            "success",
        )
        current_app.logger.info(
            f"Successfully updated Message ID {message_id} property_id to {target_property_id} ('{prop_name_for_flash}')"
        )
    except Exception as e:
        db.session.rollback()
        flash(f"Database error assigning property: {str(e)}", "danger")
        current_app.logger.error(
            f"Error committing property assignment for message {message_id} to property {target_property_id}: {e}",
            exc_info=True,
        )
    # Add fragment identifier
    if "#" not in redirect_url and "msg-" in redirect_url:
        redirect_url += f"#msg-{message_id}"
        current_app.logger.debug(f"Appending fragment, redirecting to: {redirect_url}")
    return redirect(redirect_url)


@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
    error_message = None
    dummy_phone_key = "0000000000"
    if request.method == "POST":
        action = request.form.get("action")
        current_app.logger.debug(f"Contacts POST action: {action}")
        try:
            if action == "add":
                name = request.form.get("name", "").strip()
                phone_key = request.form.get("phone", "").strip()
                if name and phone_key:
                    if len(phone_key) != 10 or not phone_key.isdigit():
                        flash("Invalid phone key format (10 digits).", "error")
                    else:
                        existing = db.session.get(Contact, phone_key)
                        if existing:
                            is_default_name = (
                                existing.contact_name.startswith("+")
                                and len(existing.contact_name) > 1
                                and existing.contact_name[1:].isdigit()
                            ) or (
                                len(existing.contact_name) == 10
                                and existing.contact_name.isdigit()
                                and existing.contact_name == existing.phone_number
                            )
                            if is_default_name:
                                existing.contact_name = name
                                current_app.logger.info(
                                    f"Contact '{phone_key}' updated with name '{name}'."
                                )
                                flash(
                                    f"Auto-named contact {phone_key} updated to '{name}'.",
                                    "success",
                                )
                            else:
                                flash(
                                    f"Contact key {phone_key} ('{existing.contact_name}') already exists.",
                                    "warning",
                                )
                        else:
                            new_contact = Contact(
                                phone_number=phone_key, contact_name=name
                            )
                            db.session.add(new_contact)
                            current_app.logger.info(
                                f"Contact '{name}' added to session."
                            )
                            flash(
                                f"Contact '{new_contact.contact_name}' added.",
                                "success",
                            )
                else:
                    flash("Name and Phone Key are required.", "error")
            elif action == "delete":
                contact_key_to_delete = request.form.get("contact_id")
                if not contact_key_to_delete:
                    flash("No Contact ID provided.", "error")
                else:
                    contact_to_delete = db.session.get(Contact, contact_key_to_delete)
                    if not contact_to_delete:
                        flash("Contact not found.", "error")
                    elif contact_key_to_delete == dummy_phone_key:
                        flash("Cannot delete default reference.", "warning")
                    else:
                        dummy_contact = db.session.get(Contact, dummy_phone_key)
                        if not dummy_contact:
                            current_app.logger.error(
                                f"CRITICAL: Dummy contact '{dummy_phone_key}' missing."
                            )
                            flash(
                                "Internal error: Default reference missing.", "danger"
                            )
                            contact_to_delete = None
                        else:
                            update_stmt = (
                                Message.__table__.update()
                                .where(Message.phone_number == contact_key_to_delete)
                                .values(phone_number=dummy_phone_key)
                            )
                            result = db.session.execute(update_stmt)
                            updated_count = result.rowcount
                            current_app.logger.info(
                                f"Re-assigned {updated_count} messages from contact {contact_key_to_delete} to dummy contact."
                            )
                            current_app.logger.info(
                                f"Marking Contact '{contact_to_delete.contact_name}' ({contact_key_to_delete}) for deletion."
                            )
                            db.session.delete(contact_to_delete)
                            flash(
                                f"Contact '{contact_to_delete.contact_name}' deleted (Messages reassigned).",
                                "success",
                            )
            current_app.logger.info(
                "Attempting final db.session.commit() for contacts POST..."
            )
            db.session.commit()
            current_app.logger.info("✅ Final db.session.commit() successful.")
        except Exception as ex:
            db.session.rollback()
            app.logger.error(f"❌ Error contacts POST: {ex}", exc_info=True)
            flash(f"Error processing contact: {ex}", "danger")
        redirect_target = request.referrer or url_for("contacts_view")
        return redirect(redirect_target)

    # --- GET Request Handling ---
    properly_known_contacts = []
    recent_auto_named_contacts = []
    try:
        all_contacts_list = Contact.query.all()
        all_contacts_dict = {c.phone_number: c for c in all_contacts_list}
        auto_named_contacts_dict = {}
        for contact in all_contacts_list:
            is_default_name = False
            if contact.contact_name:
                looks_like_plus_e164 = (
                    contact.contact_name.startswith("+")
                    and len(contact.contact_name) > 1
                    and contact.contact_name[1:].isdigit()
                )
                looks_like_key = (
                    len(contact.contact_name) == 10
                    and contact.contact_name.isdigit()
                    and contact.contact_name == contact.phone_number
                )
                is_default_name = looks_like_plus_e164 or looks_like_key
            if not is_default_name and contact.phone_number != dummy_phone_key:
                properly_known_contacts.append(contact)
            elif is_default_name:
                auto_named_contacts_dict[contact.phone_number] = contact
        properly_known_contacts.sort(
            key=lambda x: x.contact_name.lower() if x.contact_name else ""
        )
        message_alias = Message.__table__.alias("m")
        row_num_col = (
            func.row_number()
            .over(
                partition_by=message_alias.c.phone_number,
                order_by=message_alias.c.timestamp.desc(),
            )
            .label("rn")
        )
        ranked_messages_subq = (
            select(message_alias.c.phone_number, message_alias.c.timestamp, row_num_col)
            .select_from(message_alias)
            .subquery("ranked_messages")
        )
        latest_distinct_numbers_query = (
            select(ranked_messages_subq.c.phone_number)
            .where(ranked_messages_subq.c.rn == 1)
            .order_by(ranked_messages_subq.c.timestamp.desc())
            .limit(50)
        )
        recent_distinct_numbers_result = db.session.execute(
            latest_distinct_numbers_query
        ).all()
        recent_distinct_numbers = [num for (num,) in recent_distinct_numbers_result]
        count = 0
        processed_keys = set()
        for number in recent_distinct_numbers:
            if number in auto_named_contacts_dict and number not in processed_keys:
                recent_auto_named_contacts.append(auto_named_contacts_dict[number])
                processed_keys.add(number)
                count += 1
                if count >= 10:
                    break
        app.logger.debug(
            f"Contacts GET: Known={len(properly_known_contacts)}, Auto={len(recent_auto_named_contacts)}"
        )
    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error loading contacts GET: {ex}", exc_info=True)
        error_message = f"Error: {ex}"
        flash(error_message, "danger")
    return render_template(
        "contacts.html",
        properly_known_contacts=properly_known_contacts,
        recent_auto_named_contacts=recent_auto_named_contacts,
        error=error_message,
    )

@app.route("/update_contact_name", methods=["POST"])
def update_contact_name():
    phone_key = request.form.get("phone_key")
    new_name = request.form.get("new_name", "").strip()
    if not phone_key or not new_name:
        flash("Missing key or name.", "error")
        return redirect(url_for("contacts_view"))  # Indented
    try:
        contact_to_update = db.session.get(Contact, phone_key)
        if contact_to_update:
            is_reverting = (
                new_name.startswith("+")
                and len(new_name) > 1
                and new_name[1:].isdigit()
            ) or (len(new_name) == 10 and new_name.isdigit())
            if is_reverting:
                flash("Cannot set name to just a phone number.", "warning")  # Indented
            else:
                old_name = contact_to_update.contact_name
                contact_to_update.contact_name = new_name
                db.session.commit()
                app.logger.info(
                    f"✅ Updated contact name {phone_key}: '{old_name}' -> '{new_name}'."
                )
                flash(f"Contact {phone_key} updated to '{new_name}'.", "success")
        else:
            flash(f"Contact key '{phone_key}' not found.", "error")  # Indented
    except Exception as e:
        db.session.rollback()
        app.logger.error(
            f"❌ Error update_contact_name {phone_key}: {e}", exc_info=True
        )
        flash(f"Error: {e}", "danger")
    return redirect(url_for("contacts_view"))


# ──────────────────────────────────────────────────────────────────────────────
#  Tenant Notifications Page
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/notifications", methods=["GET", "POST"])
def notifications_view():
    """Displays notification form and history, handles sending."""
    properties = []; history = []; error_message = None
    if request.method == "POST":
        # --- Get form data ---
        property_ids = request.form.getlist("property_ids")
        subject = request.form.get("subject", "")
        message_body = request.form.get("message_body", "")
        channels = request.form.getlist("channels")
        uploaded_files = request.files.getlist("attachments")
        attachments_data = []

        # --- Handle File Uploads with extra checks ---
        current_app.logger.debug(f"Processing {len(uploaded_files)} potential file uploads.")
        for file in uploaded_files:
            # *** ADDED CHECKS for file and filename ***
            if file and file.filename: # Check if file object exists AND has a non-empty filename
                try:
                    filename = secure_filename(file.filename)
                    # Double check secure_filename didn't return empty
                    if not filename:
                         current_app.logger.warning(f"Skipping file: secure_filename resulted in empty string for original name '{file.filename}'")
                         continue # Skip this file

                    current_app.logger.debug(f"Processing valid file: '{filename}' (Original: '{file.filename}')")
                    file_content_bytes = file.read()
                    content_type = (
                        file.content_type
                        or mimetypes.guess_type(filename)[0]
                        or "application/octet-stream"
                    )
                    attachments_data.append(
                        {
                            "content_bytes": file_content_bytes,
                            "filename": filename, # Use the secured filename
                            "type": content_type,
                        }
                    )
                except Exception as e_file:
                     # Log error during processing THIS specific file
                     current_app.logger.error(f"Error processing file '{getattr(file, 'filename', 'N/A')}': {e_file}", exc_info=True)
                     # Optionally flash a message? For now, just log and continue.
                     # flash(f"Could not process attachment: {getattr(file, 'filename', 'N/A')}", "warning")
                     continue # Skip this file if error occurs
            elif file:
                 # Log if a file object was sent but had no filename (e.g., empty input field)
                 current_app.logger.debug(f"Skipping file upload object with no filename.")
            # No need for else, non-file objects shouldn't be in request.files.getlist

        current_app.logger.debug(f"Prepared {len(attachments_data)} attachments for email.")

        # --- Input Validation (kept as before) ---
        if not property_ids:
            flash("Please select at least one property.", "error")
            return redirect(url_for("notifications_view"))
        if not message_body:
            flash("Message body cannot be empty.", "error")
            return redirect(url_for("notifications_view"))
        if not channels:
            flash("Please select at least one channel (Email or SMS).", "error")
            return redirect(url_for("notifications_view"))

        # --- Send Logic (kept mostly as before) ---
        try:
            target_property_ids = [int(pid) for pid in property_ids if pid.isdigit()]
            if not target_property_ids:
                 flash("Invalid property selection.", "error")
                 return redirect(url_for('notifications_view'))
            target_properties = Property.query.filter(Property.id.in_(target_property_ids)).all()
            properties_targeted_str = ", ".join(sorted([f"'{p.name}' (ID:{p.id})" for p in target_properties]))
            current_app.logger.info(f"Fetching current tenants for properties: {target_property_ids}")
            target_tenants = Tenant.query.filter(Tenant.property_id.in_(target_property_ids), Tenant.status == 'current').all()
            current_app.logger.info(f"Found {len(target_tenants)} potential tenant recipients.")
            for t in target_tenants: current_app.logger.debug(f"   Tenant Found: ID={t.id}, Name='{t.name}', Status='{t.status}', PropID={t.property_id}, Email='{t.email}', Phone='{t.phone}'")
            emails_to_send = {t.email for t in target_tenants if t.email}; phones_to_send = {t.phone for t in target_tenants if t.phone}
            current_app.logger.debug(f"Unique Emails prepared: {emails_to_send}"); current_app.logger.debug(f"Unique Phones prepared: {phones_to_send}")

            if not emails_to_send and not phones_to_send:
                flash(f"No current tenants with email or phone found for the selected properties.", "warning")
                history_log = NotificationHistory(subject=subject or None, body=message_body, channels=", ".join(channels), status="No Recipients Found", properties_targeted=properties_targeted_str, recipients_summary="Email: 0/0. SMS: 0/0.", error_info=None)
                db.session.add(history_log); db.session.commit()
                current_app.logger.warning(f"Notification attempt logged (ID: {history_log.id}), but no recipients found.")
                return redirect(url_for('notifications_view'))

            email_success_count = 0; sms_success_count = 0; email_errors = []; sms_errors = []; channels_attempted = []; final_status = "Sent"
            if 'email' in channels and emails_to_send:
                channels_attempted.append("Email"); current_app.logger.info(f"Attempting email to {len(emails_to_send)} addresses...")
                email_subject = subject if subject else message_body[:50] + ("..." if len(message_body) > 50 else ""); html_body = f"<p>{message_body.replace(os.linesep, '<br>')}</p>"
                for email in emails_to_send:
                    try:
                        email_sent_successfully = send_email(to_emails=[email], subject=email_subject, html_content=wrap_email_html(html_body), attachments=attachments_data)
                        if email_sent_successfully: email_success_count += 1
                        else: email_errors.append(f"{email}: Failed")
                    except Exception as e: current_app.logger.error(f"Email Exception for {email}: {e}", exc_info=True); email_errors.append(f"{email}: Exception")
            elif 'email' in channels: current_app.logger.info("Email channel selected, but no valid tenant emails found.")

            if 'sms' in channels and phones_to_send:
                channels_attempted.append("SMS"); current_app.logger.info(f"Attempting SMS to {len(phones_to_send)} numbers...")
                for phone in phones_to_send:
                    try:
                        sms_sent = send_openphone_sms(recipient_phone=phone, message_body=message_body)
                        if sms_sent: sms_success_count += 1
                        else: sms_errors.append(f"{phone}: Failed")
                    except Exception as e: current_app.logger.error(f"SMS Exception for {phone}: {e}", exc_info=True); sms_errors.append(f"{phone}: Exception")
            elif 'sms' in channels: current_app.logger.info("SMS channel selected, but no valid tenant phone numbers found.")

            # Status calculation and logging (kept as before)
            total_email_attempts = len(emails_to_send) if 'Email' in channels_attempted else 0; total_sms_attempts = len(phones_to_send) if 'SMS' in channels_attempted else 0
            total_successes = email_success_count + sms_success_count; total_attempts = total_email_attempts + total_sms_attempts
            recipients_summary = f"Email: {email_success_count}/{total_email_attempts}. SMS: {sms_success_count}/{total_sms_attempts}."
            error_details = [];
            if email_errors: error_details.append(f"{len(email_errors)} Email failure(s)")
            if sms_errors: error_details.append(f"{len(sms_errors)} SMS failure(s)")
            error_info_str = "; ".join(error_details) + " (See logs)" if error_details else None
            if total_attempts == 0: final_status = "No Recipients Found"
            elif email_errors or sms_errors:
                if total_successes > 0: final_status = "Partial Failure"; flash(f"Notifications sent with some failures. ({recipients_summary})", "warning")
                else: final_status = "Failed"; flash(f"All notifications failed to send. Check logs.", "danger")
            else: final_status = "Sent"; flash(f"Notifications sent successfully! ({recipients_summary})", "success")
            current_app.logger.info(f"Attempting to log history with calculated status: {final_status}")
            history_log = NotificationHistory(subject=subject if 'Email' in channels_attempted else None, body=message_body, channels=", ".join(channels_attempted), status=final_status, properties_targeted=properties_targeted_str, recipients_summary=recipients_summary, error_info=error_info_str )
            db.session.add(history_log); db.session.commit(); current_app.logger.info(f"Notification history logged (ID: {history_log.id}, Status: {final_status}).")

        except Exception as ex:
            db.session.rollback()
            current_app.logger.error(f"❌ Unexpected error during notification POST: {ex}", exc_info=True)
            flash(f"An unexpected error occurred: {ex}", "danger")
        return redirect(url_for('notifications_view'))

    # GET Logic (kept as before)
    try:
        properties = Property.query.order_by(Property.name).all()
        history = NotificationHistory.query.order_by(NotificationHistory.timestamp.desc()).limit(20).all()
    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error loading notifications GET: {ex}", exc_info=True)
        error_message = f"Error loading page data: {ex}"
        flash(error_message, "danger")
    return render_template("notifications.html", properties=properties, history=history, error=error_message)



# ──────────────────────────────────────────────────────────────────────────────
#  GALLERY ROUTES
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/galleries")
def galleries_overview():
    properties_with_galleries = []
    unsorted_count = 0
    error_message = None
    try:
        property_ids_with_media = (
            db.session.query(Message.property_id)
            .filter(
                Message.property_id.isnot(None), Message.local_media_paths.isnot(None)
            )
            .distinct()
            .all()
        )
        prop_ids = [pid for (pid,) in property_ids_with_media]
        app.logger.debug(f"Found {len(prop_ids)} property IDs with media: {prop_ids}")
        if prop_ids:
            properties_with_galleries = (
                Property.query.filter(Property.id.in_(prop_ids))
                .order_by(Property.name)
                .all()
            )
            app.logger.debug(
                f"Fetched {len(properties_with_galleries)} property objects for gallery overview."
            )  # Indented
        unsorted_message_count_query = db.session.query(func.count(Message.id)).filter(
            Message.property_id.is_(None), Message.local_media_paths.isnot(None)
        )
        unsorted_count = unsorted_message_count_query.scalar() or 0
        app.logger.debug(f"Found {unsorted_count} unsorted messages with media.")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"❌ Error loading galleries overview: {e}", exc_info=True)
        error_message = f"Error loading galleries overview: {e}"
        flash(error_message, "danger")
    return render_template(
        "galleries_overview.html",
        gallery_summaries=properties_with_galleries,
        unsorted_count=unsorted_count,
        error=error_message,
    )


@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    prop = db.session.get(Property, property_id)
    if not prop:
        abort(404, description="Property not found")  # Indented
    gallery_title = f"Gallery for {prop.name}"
    image_items = []
    error_message = None
    image_extensions = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic")
    try:
        messages_with_media = (
            Message.query.options(joinedload(Message.contact))
            .filter(
                Message.property_id == property_id,
                Message.local_media_paths.isnot(None),
            )
            .order_by(Message.timestamp.desc())
            .all()
        )
        current_app.logger.debug(
            f"Found {len(messages_with_media)} messages with media for property {property_id}"
        )
        for msg in messages_with_media:
            if msg.local_media_paths and isinstance(msg.local_media_paths, str):
                paths = [
                    p.strip() for p in msg.local_media_paths.split(",") if p.strip()
                ]
                current_app.logger.debug(
                    f"  Paths split from string: {paths} (Msg ID: {msg.id})"
                )
                for idx, path in enumerate(paths):
                    trimmed_path = path
                    current_app.logger.debug(
                        f"  Checking path for gallery: '{trimmed_path}' (Msg ID: {msg.id})"
                    )
                    is_image = (
                        trimmed_path.lower().endswith(image_extensions)
                        if trimmed_path
                        else False
                    )
                    current_app.logger.debug(
                        f"  Path ends with valid extension? {is_image}"
                    )
                    if trimmed_path and is_image:
                        current_app.logger.debug(
                            f"  >>> Adding image item: {trimmed_path}"
                        )
                        sender_name = msg.phone_number
                        if msg.contact and msg.contact.contact_name:
                            sender_name = msg.contact.contact_name  # Indented
                        image_items.append(
                            {
                                "path": trimmed_path,
                                "message_id": msg.id,
                                "index": idx,
                                "timestamp": msg.timestamp,
                                "sender_info": sender_name,
                            }
                        )
                    else:
                        current_app.logger.debug(
                            f"  --- Skipping non-image or empty path from split: '{trimmed_path}'"
                        )  # Indented
            elif msg.local_media_paths and isinstance(msg.local_media_paths, list):
                current_app.logger.debug(
                    f"  local_media_paths is already a list: {msg.local_media_paths} (Msg ID: {msg.id})"
                )
                for idx, path in enumerate(msg.local_media_paths):
                    trimmed_path = path.strip()
                    current_app.logger.debug(
                        f"  Checking list path for gallery: '{trimmed_path}' (Msg ID: {msg.id})"
                    )
                    is_image = (
                        trimmed_path.lower().endswith(image_extensions)
                        if trimmed_path
                        else False
                    )
                    current_app.logger.debug(
                        f"  List Path ends with valid extension? {is_image}"
                    )
                    if trimmed_path and is_image:
                        current_app.logger.debug(
                            f"  >>> Adding image item from list: {trimmed_path}"
                        )
                        sender_name = msg.phone_number
                        if msg.contact and msg.contact.contact_name:
                            sender_name = msg.contact.contact_name  # Indented
                        image_items.append(
                            {
                                "path": trimmed_path,
                                "message_id": msg.id,
                                "index": idx,
                                "timestamp": msg.timestamp,
                                "sender_info": sender_name,
                            }
                        )
                    else:
                        current_app.logger.debug(
                            f"  --- Skipping list item non-image or empty path: '{trimmed_path}'"
                        )  # Indented
            elif msg.local_media_paths:
                current_app.logger.warning(
                    f"  local_media_paths for Msg ID {msg.id} is neither string nor list (Type: {type(msg.local_media_paths)}), Value: {msg.local_media_paths}"
                )  # Indented
        current_app.logger.debug(
            f"Prepared {len(image_items)} image items for gallery template"
        )
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"❌ Error loading gallery for property {property_id}: {e}", exc_info=True
        )
        error_message = f"Error loading gallery: {e}"
        flash(error_message, "danger")
    return render_template(
        "gallery.html",
        image_items=image_items,
        property=prop,
        gallery_title=gallery_title,
        error=error_message,
    )


@app.route("/unsorted")
def unsorted_gallery():
    current_app.logger.info("Accessing /unsorted route")
    error_message = None
    unsorted_items_for_template = []
    properties_list = []
    image_extensions = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic")
    try:
        unsorted_messages = (
            Message.query.options(joinedload(Message.contact))
            .filter(
                Message.property_id.is_(None), Message.local_media_paths.isnot(None)
            )
            .order_by(Message.timestamp.desc())
            .all()
        )
        current_app.logger.info(
            f"Found {len(unsorted_messages)} messages with unsorted media."
        )
        for message in unsorted_messages:
            if message.local_media_paths and isinstance(message.local_media_paths, str):
                paths = [
                    p.strip() for p in message.local_media_paths.split(",") if p.strip()
                ]
                current_app.logger.debug(
                    f"  Paths split from string: {paths} (Msg ID: {message.id})"
                )
                for path in paths:
                    trimmed_path = path
                    current_app.logger.debug(
                        f"  Checking path for unsorted: '{trimmed_path}' (Msg ID: {message.id})"
                    )
                    is_image = (
                        trimmed_path.lower().endswith(image_extensions)
                        if trimmed_path
                        else False
                    )
                    current_app.logger.debug(
                        f"  Path ends with valid extension? {is_image}"
                    )
                    if trimmed_path and is_image:
                        current_app.logger.debug(
                            f"  >>> Adding unsorted item: {trimmed_path}"
                        )
                        item_data = {"path": trimmed_path, "msg": message}
                        unsorted_items_for_template.append(item_data)
                    else:
                        current_app.logger.debug(
                            f"  --- Skipping non-image or empty path from split: '{trimmed_path}'"
                        )  # Indented
            elif message.local_media_paths and isinstance(
                message.local_media_paths, list
            ):
                current_app.logger.debug(
                    f"  local_media_paths is already a list: {message.local_media_paths} (Msg ID: {message.id})"
                )
                for path in message.local_media_paths:
                    trimmed_path = path.strip()
                    current_app.logger.debug(
                        f"  Checking list path for unsorted: '{trimmed_path}' (Msg ID: {message.id})"
                    )
                    is_image = (
                        trimmed_path.lower().endswith(image_extensions)
                        if trimmed_path
                        else False
                    )
                    current_app.logger.debug(
                        f"  List Path ends with valid extension? {is_image}"
                    )
                    if trimmed_path and is_image:
                        current_app.logger.debug(
                            f"  >>> Adding unsorted item from list: {trimmed_path}"
                        )
                        item_data = {"path": trimmed_path, "msg": message}
                        unsorted_items_for_template.append(item_data)
                    else:
                        current_app.logger.debug(
                            f"  --- Skipping list item non-image or empty path: '{trimmed_path}'"
                        )  # Indented
            elif message.local_media_paths:
                current_app.logger.warning(
                    f"  local_media_paths for Msg ID {message.id} is neither string nor list (Type: {type(message.local_media_paths)}), Value: {message.local_media_paths}"
                )  # Indented
        current_app.logger.info(
            f"Prepared {len(unsorted_items_for_template)} image items for the template."
        )
        properties_list = Property.query.order_by(Property.name).all()
        current_app.logger.info(
            f"Found {len(properties_list)} properties for the dropdown."
        )
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in unsorted_gallery: {e}", exc_info=True)
        error_message = f"An error occurred while loading unsorted media: {e}"
        flash(error_message, "danger")
    return render_template(
        "unsorted.html",
        unsorted=unsorted_items_for_template,
        properties=properties_list,
        error=error_message,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  OTHER / LEGACY / DEBUG ROUTES
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    return render_template("ask.html")  # Indented


@app.route("/gallery_static")
def gallery_static():
    return render_template(
        "gallery.html", image_items=[], gallery_title="Static Uploads Gallery"
    )  # Indented


@app.route("/ping")
def ping_route():
    try:
        db.session.execute(text("SELECT 1"))
        return "Pong! DB OK.", 200  # Indented
    except Exception as e:
        app.logger.error(f"DB Ping Fail: {e}")
        return f"Pong! DB Error: {e}", 503  # Indented


@app.route("/list-uploads")
def list_uploads():
    upload_path = app.config["UPLOAD_FOLDER"]
    try:
        files = [
            f
            for f in os.listdir(upload_path)
            if os.path.isfile(os.path.join(upload_path, f))
        ]
        return jsonify({"uploads": files})  # Indented
    except Exception as e:
        app.logger.error(f"Error listing uploads in {upload_path}: {e}", exc_info=True)
        return (
            jsonify({"error": "Failed to list uploads", "details": str(e)}),
            500,
        )  # Indented


@app.route("/clear-contacts-debug", methods=["POST"])
def clear_contacts_debug():
    current_app.logger.warning("--- [clear-contacts-debug] Route accessed ---")
    dummy_phone_key = "0000000000"
    try:
        dummy_contact = db.session.get(Contact, dummy_phone_key)
        if not dummy_contact:
            current_app.logger.info(f"Creating dummy contact: {dummy_phone_key}")
            dummy_contact = Contact(
                phone_number=dummy_phone_key, contact_name="Deleted Contact Ref"
            )
            db.session.add(dummy_contact)
            db.session.flush()  # Indented
        update_stmt = (
            Message.__table__.update()
            .where(Message.phone_number != dummy_phone_key)
            .values(phone_number=dummy_phone_key)
        )
        result = db.session.execute(update_stmt)
        updated_count = result.rowcount
        current_app.logger.info(
            f"Re-assigned {updated_count} messages to dummy contact ({dummy_phone_key})."
        )
        delete_stmt = Contact.__table__.delete().where(
            Contact.phone_number != dummy_phone_key
        )
        result = db.session.execute(delete_stmt)
        deleted_count = result.rowcount
        current_app.logger.info(f"Deleted {deleted_count} non-dummy contacts.")
        db.session.commit()
        flash(
            f"Debug: Contacts cleared ({deleted_count} deleted, {updated_count} messages reassigned).",
            "info",
        )
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"❌ Error clear_contacts_debug: {e}", exc_info=True)
        flash(f"Error clearing contacts: {e}", "danger")  # Indented
    return redirect(url_for("contacts_view"))


# ──────────────────────────────────────────────────────────────────────────────
#  Custom Flask CLI Commands
# ──────────────────────────────────────────────────────────────────────────────

def clean_phone_number(raw_phone_str):
    """
    Cleans a raw phone number string (e.g., from CSV) into a 10-digit format.
    Handles formats like (XXX) XXX-XXXX, +1XXXXXXXXXX, XXXXXXXXXX etc.
    Returns 10-digit string or None if cleaning fails.
    """
    if not raw_phone_str or not isinstance(raw_phone_str, str):
        return None

    # Remove all non-digit characters
    digits = re.sub(r'\D', '', raw_phone_str)

    # Check if it starts with '1' (common country code) and is 11 digits
    if len(digits) == 11 and digits.startswith('1'):
        return digits[1:] # Return the last 10 digits
    # Check if it's exactly 10 digits
    elif len(digits) == 10:
        return digits
    else:
        # Failed to clean to 10 digits
        # Log the failure for diagnosis if needed:
        # current_app.logger.warning(f"Could not parse phone '{raw_phone_str}' into 10 digits. Got: '{digits}'")
        return None

def clean_contact_name_revised(raw_name_str):
    """ Cleans the contact name by simply stripping whitespace. """
    if not raw_name_str or not isinstance(raw_name_str, str):
        return "Unnamed Contact" # Return a default if empty

    name = raw_name_str.strip()
    # If cleaning results in an empty string, use a default
    return name if name else "Unnamed Contact"


@app.cli.command("import-contacts")
@click.argument("filename")
def import_contacts_command(filename):
    """Imports contacts from a specified CSV file (expects 'Name' and 'Phone' columns)."""
    processed_count = 0
    added_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0

    # Default name check function (checks if name is just digits or looks like +E164)
    def is_default_name(name_str, phone_str):
        if not name_str: return True
        # Check if name is just the phone number (10 digits or +1 version)
        name_digits = re.sub(r'\D', '', name_str)
        phone_digits_10 = phone_str
        phone_digits_11 = '+1' + phone_str
        if name_str == phone_digits_10 or name_str == phone_digits_11:
             return True
        # Check if name ONLY contains digits (flexible length check)
        if name_digits == name_str and len(name_digits)>0:
             return True

        return False

    click.echo(f"Starting contact import from: {filename}")

    try:
        with open(filename, mode='r', encoding='utf-8-sig') as csvfile: # Use utf-8-sig to handle potential BOM
            reader = csv.DictReader(csvfile)
            # *** Check for NEW column names ***
            if "Name" not in reader.fieldnames or "Phone" not in reader.fieldnames:
                 click.echo("Error: CSV file must contain 'Name' and 'Phone' columns.")
                 return

            for row in reader:
                processed_count += 1
                # *** Use NEW column names ***
                raw_name = row.get("Name")
                raw_phone = row.get("Phone")
                # We are ignoring "Notes", "Tags", etc. for now

                cleaned_phone = clean_phone_number(raw_phone)
                if not cleaned_phone:
                    # Make warning clearer
                    click.echo(f"WARN: Skipping row {processed_count+1}. Failed to extract 10-digit phone from: '{raw_phone}'")
                    skipped_count += 1
                    continue

                # Use the revised (simpler) name cleaning
                cleaned_name = clean_contact_name_revised(raw_name)

                # Skip if cleaned name is empty after stripping (should use default now)
                # if not cleaned_name:
                #    click.echo(f"WARN: Skipping row {processed_count+1}. Cleaned name is empty for phone: '{cleaned_phone}'")
                #    skipped_count += 1
                #    continue

                try:
                    existing_contact = db.session.get(Contact, cleaned_phone)

                    if existing_contact:
                        # Contact exists - update name ONLY if existing name is a default/placeholder
                        # and the new name is not a default/placeholder
                        if is_default_name(existing_contact.contact_name, cleaned_phone) and \
                           not is_default_name(cleaned_name, cleaned_phone) and \
                           cleaned_name != "Unnamed Contact": # Don't update if new name is also bad
                            click.echo(f"INFO: Updating contact {cleaned_phone}: '{existing_contact.contact_name}' -> '{cleaned_name}'")
                            existing_contact.contact_name = cleaned_name
                            db.session.add(existing_contact)
                            updated_count += 1
                        else:
                             # Skip update if existing name is already good or new name is bad
                             skipped_count += 1
                    else:
                        # Contact does not exist - add new one
                        # Only add if the cleaned name isn't empty (handled by clean_contact_name_revised now)
                        click.echo(f"INFO: Adding new contact {cleaned_phone}: '{cleaned_name}'")
                        new_contact = Contact(phone_number=cleaned_phone, contact_name=cleaned_name)
                        db.session.add(new_contact)
                        added_count += 1

                except Exception as db_err:
                    click.echo(f"ERROR: Database error processing row {processed_count+1} (Phone: {cleaned_phone}): {db_err}")
                    error_count += 1
                    db.session.rollback()

        # Commit all changes after processing the file
        if added_count > 0 or updated_count > 0:
             click.echo("Committing changes to database...")
             try:
                 db.session.commit()
                 click.echo("Commit successful.")
             except Exception as commit_err:
                 click.echo(f"ERROR: Failed to commit changes to database: {commit_err}")
                 db.session.rollback()
                 error_count += 1 # Count commit error
        else:
             click.echo("No new contacts added or updated.")

    except FileNotFoundError:
        click.echo(f"Error: File not found at {filename}")
        error_count += 1
    except Exception as e:
        click.echo(f"An unexpected error occurred: {e}")
        error_count += 1
        db.session.rollback()

    click.echo("\n--- Import Summary ---")
    click.echo(f"Rows Processed: {processed_count}")
    click.echo(f"Contacts Added: {added_count}")
    click.echo(f"Contacts Updated: {updated_count}")
    click.echo(f"Rows Skipped/Not Updated: {skipped_count}")
    click.echo(f"Errors: {error_count}")
    click.echo("----------------------")

# --- End Custom Flask CLI Commands ---

app.register_blueprint(webhook_bp)

# --- Keep __main__ block ---
if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8080))
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() in ["true", "1", "t"]
    app.run(host=host, port=port, debug=debug_mode)"Contact '{name}' added to session."
                            )
                            flash(
                                f"Contact '{new_contact.contact_name}' added.",
                                "success",
                            )
                else:
                    flash("Name and Phone Key are required.", "error")  # Indented
            elif action == "delete":
                contact_key_to_delete = request.form.get("contact_id")
                if not contact_key_to_delete:
                    flash("No Contact ID provided.", "error")  # Indented
                else:
                    contact_to_delete = db.session.get(Contact, contact_key_to_delete)
                    if not contact_to_delete:
                        flash("Contact not found.", "error")  # Indented
                    elif contact_key_to_delete == dummy_phone_key:
                        flash("Cannot delete default reference.", "warning")  # Indented
                    else:
                        dummy_contact = db.session.get(Contact, dummy_phone_key)
                        if not dummy_contact:
                            current_app.logger.error(
                                f"CRITICAL: Dummy contact '{dummy_phone_key}' missing."
                            )
                            flash(
                                "Internal error: Default reference missing.", "danger"
                            )
                            contact_to_delete = None  # Prevent deletion
                        else:
                            update_stmt = (
                                Message.__table__.update()
                                .where(Message.phone_number == contact_key_to_delete)
                                .values(phone_number=dummy_phone_key)
                            )
                            result = db.session.execute(update_stmt)
                            updated_count = result.rowcount
                            current_app.logger.info(
                                f