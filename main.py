# -*- coding: utf-8 -*-
import os
import json
import logging # Import logging
from pathlib import Path
from datetime import datetime, timedelta
import traceback
import requests # For OpenPhone API calls

# Import necessary Flask components
from flask import (
    Flask, render_template, request, jsonify, url_for, redirect, flash, current_app
)
from dotenv import load_dotenv
load_dotenv()
# Import SQLAlchemy components
from sqlalchemy import text, func, exc as sqlalchemy_exc, select, over
from sqlalchemy.orm import attributes, joinedload

# Import Flask-Migrate
from flask_migrate import Migrate

import openai

# Import local modules
from extensions import db
# Import ALL your models needed here
from models import Contact, Message, Property, Tenant, NotificationHistory
from webhook_route import webhook_bp # Assuming webhook_bp is correctly defined elsewhere
# Assuming send_email and wrap_email_html are in email_utils.py:
from email_utils import send_email, wrap_email_html

# ──────────────────────────────────────────────────────────────────────────────
#  App configuration
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

# --- Logging Setup ---
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(name)s : %(message)s')
app.logger.setLevel(logging.DEBUG)

# --- Flask App Configuration ---
# Ensure instance folder exists for potential SQLite DB
INSTANCE = Path(app.instance_path)
INSTANCE.mkdir(exist_ok=True)
DB_FILE  = INSTANCE / "messages.db" # Default fallback if DATABASE_URL not set

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", f"sqlite:///{DB_FILE}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = { "pool_pre_ping": True, "pool_recycle": 300 }
app.config["FLASK_SECRET"] = os.getenv("FLASK_SECRET", "a_very_strong_default_secret_key_for_dev_only")
app.secret_key = app.config["FLASK_SECRET"]
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# --- Static and Upload Folder Setup ---
app.static_folder = os.path.join(app.root_path, 'static')
UPLOAD_FOLDER = os.path.join(app.static_folder, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.logger.info(f"ℹ️ Configured UPLOAD_FOLDER: {app.config['UPLOAD_FOLDER']}")

# --- Initialize Extensions ---
db.init_app(app)
app.register_blueprint(webhook_bp) # Assumes prefix='/webhook' is set in blueprint
migrate = Migrate(app, db) # Initialize Flask-Migrate

# --- Configure OpenAI ---
openai.api_key = os.getenv("OPENAI_API_KEY")
# --- End App Setup ---

# --- Helper Function for Sending SMS via OpenPhone API ---
def send_openphone_sms(recipient_phone, message_body):
    """
    Sends an SMS message using the OpenPhone v1 API.
    Returns True on success, False on failure.
    Requires OPENPHONE_API_TOKEN and OPENPHONE_SENDING_NUMBER env vars.
    """
    api_token = os.getenv("OPENPHONE_API_TOKEN")
    # Use OPENPHONE_FROM if you set that, otherwise OPENPHONE_SENDING_NUMBER
    sending_number = os.getenv("OPENPHONE_FROM") or os.getenv("OPENPHONE_SENDING_NUMBER")

    if not api_token or not sending_number:
        current_app.logger.error("OpenPhone API Token or Sending Number ('OPENPHONE_FROM' or 'OPENPHONE_SENDING_NUMBER') not configured.")
        return False

    # --- Use Correct v1 Endpoint ---
    api_url = "https://api.openphone.com/v1/messages"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    # --- Use Correct Payload Structure ---
    payload = {
        "from": sending_number,
        "to": [recipient_phone],  # <--- Key change: recipient must be in a list/array
        "content": message_body,  # <--- Key change: use 'content' not 'body'
        # "setInboxStatus": "done" # Optional: uncomment to mark as done in OpenPhone
    }
    # --- End Corrected Structure ---

    try:
        current_app.logger.debug(f"Sending OpenPhone SMS To: {payload['to']}, From: {payload['from']}")
        response = requests.post(api_url, headers=headers, json=payload, timeout=20)
        response.raise_for_status() # Raises HTTPError for bad responses (4xx or 5xx)

        response_data = response.json()
        # Adjust based on actual successful response structure from OpenPhone v1 docs
        message_id = response_data.get('id', 'N/A')
        status = response_data.get('status', 'N/A')
        current_app.logger.info(f"Successfully sent SMS via OpenPhone to {recipient_phone}. Response ID: {message_id}, Status: {status}")
        # Check your OpenPhone app interface now!
        return True

    except requests.exceptions.RequestException as e:
        current_app.logger.error(f"Error sending OpenPhone SMS to {recipient_phone}: {e}")
        try:
            # Try to log specific error details from OpenPhone if available
            error_details = e.response.json()
            current_app.logger.error(f"OpenPhone API Error Response: Status={e.response.status_code}, Details={error_details}")
        except: # Handle cases where response might not exist or have JSON
             try:
                  current_app.logger.error(f"OpenPhone API Error Response: Status={e.response.status_code}, Body={e.response.text}")
             except:
                   pass # No response context available
        return False
    except Exception as e: # Catch other potential errors
         current_app.logger.error(f"Unexpected error in send_openphone_sms to {recipient_phone}: {e}", exc_info=True)
         return False


# --- Database Initialization Helper ---
def initialize_database(app_context):
    """Initializes the database: creates tables, checks columns, resets sequences."""
    with app_context:
        app.logger.info("🔄 Initializing Database...")
        try:
            db.create_all()
            app.logger.info("✅ Tables created/verified.")
            # Ensure sid column exists
            try:
                db.session.execute(text("ALTER TABLE messages ADD COLUMN sid VARCHAR"))
                db.session.commit(); app.logger.info("✅ Ensured messages.sid column exists.")
            except Exception as alter_err:
                db.session.rollback(); err_str = str(alter_err).lower()
                if "already exists" in err_str or "duplicate column name" in err_str: app.logger.info("✅ messages.sid column already exists.")
                else: app.logger.warning(f"⚠️ Could not add 'sid' column: {alter_err}")
            # Reset sequence (PostgreSQL)
            if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
                try:
                    # ... (Sequence reset logic as before) ...
                     sequence_name_query = text("SELECT pg_get_serial_sequence('messages', 'id');"); result = db.session.execute(sequence_name_query).scalar()
                     if result:
                         sequence_name = result; max_id_query = text("SELECT COALESCE(MAX(id), 0) FROM messages"); max_id = db.session.execute(max_id_query).scalar(); next_val = max_id + 1
                         reset_seq_query = text(f"SELECT setval('{sequence_name}', :next_val, false)"); db.session.execute(reset_seq_query, {'next_val': next_val}); db.session.commit()
                         app.logger.info(f"🔁 messages.id sequence ('{sequence_name}') reset to {next_val}.")
                     else: app.logger.warning("⚠️ Could not determine sequence name for messages.id.")
                except Exception as seq_err: db.session.rollback(); app.logger.error(f"❌ Error resetting PostgreSQL sequence: {seq_err}", exc_info=True)
            else: app.logger.info("ℹ️ Skipping sequence reset (not PostgreSQL).")
            # Property count query commented out as it caused issues before migration
            # prop_count = Property.query.count()
            # app.logger.info(f"👉 Properties in DB: {prop_count}")
            app.logger.info("✅ Database initialization complete.")
        except Exception as e:
            db.session.rollback()
            app.logger.critical(f"❌ FATAL STARTUP ERROR during database initialization: {e}", exc_info=True)

# --- URL Map Helper ---
def print_url_map(app_instance):
     """Logs all registered URL routes."""
     # ... (Keep as before) ...


# ──────────────────────────────────────────────────────────────────────────────
#  INITIALIZE DATABASE & PRINT URL MAP
# ──────────────────────────────────────────────────────────────────────────────
initialize_database(app.app_context())
# Only print map if in debug mode (avoids cluttering production logs)
if os.environ.get("FLASK_DEBUG", "false").lower() in ['true', '1', 't']:
    print_url_map(app)

# ──────────────────────────────────────────────────────────────────────────────
#  ROUTES
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Displays the main dashboard."""
    # ... (Keep the simple count version or add AI summary logic back later) ...
    current_year = datetime.utcnow().year; db_status = "?"; summary_today = "?"; summary_week = "?"
    try:
        db.session.execute(text("SELECT 1")); db_status = "Connected"
        now_utc = datetime.utcnow(); start_today_utc = datetime.combine(now_utc.date(), datetime.min.time())
        start_week_utc  = start_today_utc - timedelta(days=now_utc.weekday())
        count_today = (db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_today_utc).scalar() or 0)
        count_week = (db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_week_utc).scalar() or 0)
        summary_today = f"{count_today} messages today."; summary_week  = f"{count_week} messages this week."
    except Exception as ex: db.session.rollback(); db_status = f"Error: {ex}"; app.logger.error(f"❌ DB Error index: {ex}")
    return render_template("index.html", db_status=db_status, summary_today=summary_today, summary_week=summary_week, current_year=current_year)


# --- Keep /messages, /assign_property, /contacts, /update_contact_name routes ---
# (Using the versions we finalized previously)
@app.route("/messages")
def messages_view():
     # ... (Version handling overview/detail based on request.args.get('phone_number')) ...
     current_year=datetime.utcnow().year; target_phone_number=request.args.get("phone_number")
     try:
         if target_phone_number:
             msgs_for_number = Message.query.filter_by(phone_number=target_phone_number).order_by(Message.timestamp.desc()).all()
             app.logger.debug(f"Query for {target_phone_number} returned {len(msgs_for_number)} message objects.")
             contact = Contact.query.get(target_phone_number); is_known = contact is not None; contact_name = contact.contact_name if is_known else None
             app.logger.debug(f"Contact lookup {target_phone_number}: known={is_known}, name='{contact_name}'")
             return render_template("messages_detail.html", phone_number=target_phone_number, messages=msgs_for_number, is_known=is_known, contact_name=contact_name, current_year=current_year)
         else:
             app.logger.debug("--- Loading OVERVIEW view ---")
             msgs_overview = Message.query.options(joinedload(Message.property)).order_by(Message.timestamp.desc()).limit(100).all()
             app.logger.debug(f"Query overview returned {len(msgs_overview)} messages.")
             known_phones_query = db.session.query(Contact.phone_number).distinct().all(); known_contact_phones_set = {p for (p,) in known_phones_query}
             properties_list = Property.query.order_by(Property.name).all()
             return render_template("messages_overview.html", messages=msgs_overview, known_contact_phones=known_contact_phones_set, properties=properties_list, current_year=current_year)
     except Exception as ex:
         db.session.rollback(); traceback.print_exc(); error_msg = f"Error loading messages page: {ex}"; app.logger.error(f"❌ {error_msg}")
         flash(error_msg, "danger"); template_name = "messages_detail.html" if target_phone_number else "messages_overview.html"
         return render_template(template_name, messages=[], properties=[], known_contact_phones=set(), phone_number=target_phone_number, is_known=False, contact_name=None, error=error_msg, current_year=current_year)

@app.route("/assign_property", methods=["POST"])
def assign_property():
     # ... (Keep working version) ...
     message_id_str = request.form.get("message_id"); property_id_str = request.form.get("property_id", ""); redirect_url = request.referrer or url_for("messages_view")
     if not message_id_str: flash("No message ID provided.", "error"); return redirect(redirect_url)
     try:
         message_id = int(message_id_str); message = Message.query.get(message_id)
         if not message: flash(f"Message ID {message_id} not found.", "error"); return redirect(redirect_url)
         if property_id_str and property_id_str.lower() not in ["none", ""]:
             property_id = int(property_id_str); prop = Property.query.get(property_id)
             if not prop: flash(f"Property ID {property_id} not found.", "error"); return redirect(redirect_url)
             message.property_id = property_id; flash(f"Message assigned to: {prop.name}", "success")
         else: message.property_id = None; flash(f"Message unassigned.", "info")
         db.session.commit()
     except ValueError: flash("Invalid ID.", "error"); db.session.rollback()
     except Exception as e: flash(f"Error assigning: {e}", "danger"); db.session.rollback(); app.logger.error(f"Error assign_property: {e}", exc_info=True)
     if "#" not in redirect_url and 'message_id' in locals() and isinstance(message_id, int): redirect_url += f"#msg-{message_id}"
     return redirect(redirect_url)

@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
     # ... (Keep the working version with separate named/auto, and correct POST logic for add/delete with explicit flush) ...
     current_year=datetime.utcnow().year; error_message=None; dummy_phone_key='0000000000'
     if request.method == "POST":
         action = request.form.get("action"); current_app.logger.debug(f"Contacts POST action: {action}")
         try:
             if action == "add": name = request.form.get("name", "").strip(); phone_key = request.form.get("phone", "").strip()
             elif action == "delete": contact_key_to_delete = request.form.get("contact_id")
             # ... (Full Add/Delete logic from previous correct version, including message updates and flush) ...
             # (This is abbreviated for clarity - make sure the full correct logic is here)
             db.session.commit(); app.logger.info("✅ Contact POST commit successful.")
             if action=="add": flash("Contact added.", "success")
             if action=="delete": flash("Contact deleted.", "success")
         except Exception as ex: db.session.rollback(); app.logger.error(f"❌ Error contacts POST: {ex}", exc_info=True); flash(f"Error processing contact: {ex}", "danger")
         return redirect(url_for("contacts_view"))
     # GET Logic
     properly_known_contacts=[]; recent_auto_named_contacts=[];
     try:
         all_contacts_list = Contact.query.all(); all_contacts_dict = {c.phone_number: c for c in all_contacts_list}; auto_named_contacts_dict = {}
         for contact in all_contacts_list:
             is_default_name=False;
             if contact.contact_name: looks_like_plus_e164 = (contact.contact_name.startswith('+') and len(contact.contact_name) > 1 and contact.contact_name[1:].isdigit()); looks_like_key = (len(contact.contact_name) == 10 and contact.contact_name.isdigit() and contact.contact_name == contact.phone_number); is_default_name = looks_like_plus_e164 or looks_like_key
             if not is_default_name and contact.phone_number != dummy_phone_key: properly_known_contacts.append(contact)
             elif is_default_name: auto_named_contacts_dict[contact.phone_number] = contact
         properly_known_contacts.sort(key=lambda x: x.contact_name.lower() if x.contact_name else "")
         row_num_subq = select(Message.phone_number, Message.timestamp, func.row_number().over(partition_by=Message.phone_number, order_by=Message.timestamp.desc()).label('rn')).subquery('ranked_messages')
         latest_distinct_numbers_query = select(row_num_subq.c.phone_number).where(row_num_subq.c.rn == 1).order_by(row_num_subq.c.timestamp.desc()).limit(50)
         recent_distinct_numbers_result = db.session.execute(latest_distinct_numbers_query).all(); recent_distinct_numbers = [num for (num,) in recent_distinct_numbers_result]
         count = 0; processed_keys = set()
         for number in recent_distinct_numbers:
             if number in auto_named_contacts_dict and number not in processed_keys: recent_auto_named_contacts.append(auto_named_contacts_dict[number]); processed_keys.add(number); count += 1;
             if count >= 10: break
         app.logger.debug(f"Contacts GET: Known={len(properly_known_contacts)}, Auto={len(recent_auto_named_contacts)}")
     except Exception as ex: db.session.rollback(); app.logger.error(f"❌ Error loading contacts GET: {ex}", exc_info=True); error_message = f"Error: {ex}"; flash(error_message, "danger")
     return render_template("contacts.html", properly_known_contacts=properly_known_contacts, recent_auto_named_contacts=recent_auto_named_contacts, error=error_message, current_year=current_year)


@app.route("/update_contact_name", methods=["POST"])
def update_contact_name():
     # ... (Keep working version) ...
     phone_key = request.form.get("phone_key"); new_name = request.form.get("new_name", "").strip()
     if not phone_key or not new_name: flash("Missing key or name.", "error"); return redirect(url_for('contacts_view'))
     try:
         contact_to_update = Contact.query.get(phone_key)
         if contact_to_update:
             is_reverting = ( (new_name.startswith('+') and len(new_name) > 1 and new_name[1:].isdigit()) or (len(new_name) == 10 and new_name.isdigit() and new_name == phone_key) )
             if is_reverting and new_name != contact_to_update.contact_name: flash("Cannot set name to just phone number.", "warning"); return redirect(url_for('contacts_view'))
             old_name = contact_to_update.contact_name; contact_to_update.contact_name = new_name; db.session.commit()
             app.logger.info(f"✅ Updated contact name {phone_key}: '{old_name}' -> '{new_name}'.")
             flash(f"Contact {phone_key} updated to '{new_name}'.", "success")
         else: flash(f"Contact key '{phone_key}' not found.", "error")
     except Exception as e: db.session.rollback(); app.logger.error(f"❌ Error update_contact_name {phone_key}: {e}", exc_info=True); flash(f"Error: {e}", "danger")
     return redirect(url_for('contacts_view'))


# ──────────────────────────────────────────────────────────────────────────────
#  Tenant Notifications Page (NOW WITH SEND LOGIC)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/notifications", methods=["GET", "POST"])
def notifications_view():
    """Displays notification form and history, handles sending."""
    current_year = datetime.utcnow().year
    properties = []
    history = []
    error_message = None

    # --- POST Logic (Send Notifications) ---
    if request.method == "POST":
        property_ids = request.form.getlist('property_ids') # Handles multi-select
        subject = request.form.get('subject', '')
        message_body = request.form.get('message_body', '')
        channels = request.form.getlist('channels') # List: ['email'], ['sms'], or ['email', 'sms']

        # Input Validation
        if not property_ids: flash("Please select at least one property.", "error"); return redirect(url_for('notifications_view'))
        if not message_body: flash("Message body cannot be empty.", "error"); return redirect(url_for('notifications_view'))
        if not channels: flash("Please select at least one channel (Email or SMS).", "error"); return redirect(url_for('notifications_view'))

        try:
            # Determine Target Properties
            target_property_ids = [int(pid) for pid in property_ids if pid.isdigit()]
            if not target_property_ids: flash("Invalid property selection.", "error"); return redirect(url_for('notifications_view'))
            target_properties = Property.query.filter(Property.id.in_(target_property_ids)).all()
            properties_targeted_str = ", ".join(sorted([f"'{p.name}' (ID:{p.id})" for p in target_properties]))

            # Fetch Target Tenants
            current_app.logger.info(f"Fetching current tenants for properties: {target_property_ids}")
            target_tenants = Tenant.query.filter(
                Tenant.property_id.in_(target_property_ids),
                Tenant.status == 'current'
            ).all()
            current_app.logger.info(f"Found {len(target_tenants)} potential tenant recipients.")

            if not target_tenants:
                flash(f"No current tenants found for the selected properties.", "warning")
                return redirect(url_for('notifications_view'))

            # Prepare Recipient Lists
            emails_to_send = {t.email for t in target_tenants if t.email}
            phones_to_send = {t.phone for t in target_tenants if t.phone}
            current_app.logger.debug(f"Unique Emails: {emails_to_send}")
            current_app.logger.debug(f"Unique Phones: {phones_to_send}")

            # Send Notifications
            email_success_count = 0; sms_success_count = 0
            email_errors = []; sms_errors = []
            channels_attempted = []
            final_status = "Sent"

            # 1. Send Emails
            if 'email' in channels and emails_to_send:
                channels_attempted.append("Email")
                current_app.logger.info(f"Attempting email to {len(emails_to_send)} addresses...")
                email_subject = subject if subject else message_body[:50] + ("..." if len(message_body) > 50 else "")
                html_body = f"<p>{message_body.replace(os.linesep, '<br>')}</p>" # Basic HTML wrap

                for email in emails_to_send:
                    try:
                        send_email(to_address=email, subject=email_subject, plain_content=message_body, html_content=wrap_email_html(html_body)) # Assuming wrap adds full structure
                        email_success_count += 1
                    except Exception as e:
                        current_app.logger.error(f"Email fail to {email}: {e}", exc_info=True)
                        email_errors.append(f"{email}: Error")
            else:
                 if 'email' in channels: current_app.logger.info("Email channel selected, but no valid tenant emails found.")


            # 2. Send SMS
            if 'sms' in channels and phones_to_send:
                channels_attempted.append("SMS")
                current_app.logger.info(f"Attempting SMS to {len(phones_to_send)} numbers...")
                for phone in phones_to_send:
                    try:
                        sms_sent = send_openphone_sms(recipient_phone=phone, message_body=message_body)
                        if sms_sent: sms_success_count += 1
                        else: sms_errors.append(f"{phone}: Failed")
                    except Exception as e:
                        current_app.logger.error(f"SMS Exception for {phone}: {e}", exc_info=True)
                        sms_errors.append(f"{phone}: Exception")
            else:
                 if 'sms' in channels: current_app.logger.info("SMS channel selected, but no valid tenant phone numbers found.")


            # Determine Overall Status & Log History
            total_email_attempts = len(emails_to_send) if 'email' in channels_attempted else 0
            total_sms_attempts = len(phones_to_send) if 'sms' in channels_attempted else 0
            total_successes = email_success_count + sms_success_count
            total_attempts = total_email_attempts + total_sms_attempts

            recipients_summary = f"Email: {email_success_count}/{total_email_attempts}. SMS: {sms_success_count}/{total_sms_attempts}."
            error_details = []
            if email_errors: error_details.append(f"{len(email_errors)} Email errors")
            if sms_errors: error_details.append(f"{len(sms_errors)} SMS errors")
            error_info_str = "; ".join(error_details) + " (Check logs)" if error_details else None

            if total_attempts == 0: final_status = "No Recipients Found"; flash("No current tenants with contact info for selected channels/properties.", "warning")
            elif total_successes == total_attempts: final_status = "Sent"; flash(f"Notifications sent successfully! ({recipients_summary})", "success")
            elif total_successes > 0: final_status = "Partial Failure"; flash(f"Notifications sent with some failures. ({recipients_summary})", "warning")
            else: final_status = "Failed"; flash(f"All notifications failed to send.", "danger")

            # Log to DB
            history_log = NotificationHistory(
                subject=email_subject if 'Email' in channels_attempted else None,
                body=message_body, channels=", ".join(channels_attempted), status=final_status,
                properties_targeted=properties_targeted_str, recipients_summary=recipients_summary,
                error_info=error_info_str )
            db.session.add(history_log); db.session.commit()
            current_app.logger.info(f"Notification history logged (ID: {history_log.id}, Status: {final_status}).")

        except Exception as ex:
            db.session.rollback()
            current_app.logger.error(f"❌ Unexpected error during notification POST: {ex}", exc_info=True)
            flash(f"An unexpected error occurred: {ex}", "danger")

        return redirect(url_for('notifications_view'))


    # --- GET Logic ---
    try:
        properties = Property.query.order_by(Property.name).all()
        history = NotificationHistory.query.order_by(NotificationHistory.timestamp.desc()).limit(20).all()
    except Exception as ex:
        db.session.rollback(); app.logger.error(f"❌ Error loading notifications GET: {ex}", exc_info=True)
        error_message = f"Error loading page data: {ex}"; flash(error_message, "danger")

    return render_template(
        "notifications.html", properties=properties, history=history, error=error_message, current_year=current_year
    )

# --- Keep other routes (galleries, ask, debug, etc.) ---
# ... (Make sure they are using correct model fields if you intend to use them) ...
@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    # ... (Keep previous version, but NOTE it uses incorrect Message fields currently) ...
    return render_template("ask.html", current_year=datetime.utcnow().year) # Simplified fallback

@app.route("/gallery_static")
def gallery_static():
    # ... (Keep previous version) ...
    return render_template("gallery.html", image_items=[], gallery_title="Static Uploads Gallery", current_year=datetime.utcnow().year)

@app.route("/unsorted")
def unsorted_gallery():
     # ... (Keep previous version) ...
     return render_template("unsorted.html", unsorted_items=[], properties=[], current_year=datetime.utcnow().year)

@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
     # ... (Keep previous version, but NOTE it might have issues if local_media_paths isn't correctly populated) ...
     prop = Property.query.get_or_404(property_id); return render_template("gallery.html", image_items=[], property=prop, gallery_title=f"Gallery for {prop.name}", current_year=datetime.utcnow().year)

@app.route("/galleries")
def galleries_overview():
     # ... (Keep previous version) ...
     return render_template("galleries_overview.html", gallery_summaries=[], unsorted_count=0, current_year=datetime.utcnow().year)

@app.route("/gallery", endpoint="gallery_view")
def gallery_view():
     # ... (Keep previous version) ...
     return render_template("gallery.html", image_items=[], gallery_title="All Media Gallery", current_year=datetime.utcnow().year)

@app.route("/ping")
def ping_route():
     # ... (Keep previous version) ...
     try: db.session.execute(text("SELECT 1")); return "Pong! DB OK.", 200
     except Exception as e: app.logger.error(f"DB Ping Fail: {e}"); return f"Pong! DB Error: {e}", 503

@app.route("/list-uploads")
def list_uploads():
     # ... (Keep previous version) ...
     return jsonify({"error": "Not fully implemented"}), 500

@app.route("/clear-contacts-debug", methods=['POST'])
def clear_contacts_debug():
     # ... (Keep the SAFER version with expire_all) ...
     # (Abbreviated for clarity - ensure full safe logic is here)
     flash("Debug clear executed (ensure safe version).", "info")
     return redirect(url_for('messages_view'))

# --- Keep __main__ block ---
if __name__ == "__main__":
    # ... (Keep as before) ...
    app.run(host=host, port=port, debug=debug_mode)