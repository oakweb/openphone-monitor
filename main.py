# -*- coding: utf-8 -*-
import os
import json
import logging # Import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone # Added timezone for now()
import traceback
import requests # For OpenPhone API calls
import mimetypes # Added for notifications uploads

# Import necessary Flask components
from werkzeug.utils import secure_filename
from flask import (
    Flask, render_template, request, jsonify, url_for, redirect, flash, current_app, abort
) # Added abort
from dotenv import load_dotenv
load_dotenv()
# Import SQLAlchemy components
# Added func, is_, isnot_ for new queries
from sqlalchemy import text, func, exc as sqlalchemy_exc, select, over, String, cast, or_, not_, and_
from sqlalchemy.orm import attributes, joinedload
from sqlalchemy.sql.expression import null # For setting NULL explicitly if needed

# Import Flask-Migrate
from flask_migrate import Migrate

import openai

# Import local modules
from extensions import db
# Import ALL your models needed here
# Ensure Property is imported for the gallery and assignment logic
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
INSTANCE = Path(app.instance_path)
INSTANCE.mkdir(exist_ok=True)
DB_FILE  = INSTANCE / "messages.db"

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
app.register_blueprint(webhook_bp)
migrate = Migrate(app, db)

# --- Configure OpenAI ---
openai.api_key = os.getenv("OPENAI_API_KEY")
# --- End App Setup ---


# --- Helper Function for Sending SMS via OpenPhone API (FIXED HEADER) ---
def send_openphone_sms(recipient_phone, message_body):
    """
    Sends an SMS message using the OpenPhone v1 API.
    Returns True on success, False on failure.
    Requires OPENPHONE_API_TOKEN and OPENPHONE_SENDING_NUMBER/OPENPHONE_FROM env vars.
    """
    api_token = os.getenv("OPENPHONE_API_TOKEN")
    sending_number = os.getenv("OPENPHONE_FROM") or os.getenv("OPENPHONE_SENDING_NUMBER")

    # Use current_app logger if available (preferred), otherwise basic logging
    log_func = getattr(current_app, "logger", logging.getLogger(__name__))

    if not api_token or not sending_number:
        log_func.error("OpenPhone API Token or Sending Number not configured.")
        return False

    api_url = "https://api.openphone.com/v1/messages"
    headers = {
        "Authorization": api_token, # Corrected: No "Bearer " prefix
        "Content-Type": "application/json",
    }
    payload = {
        "from": sending_number,
        "to": [recipient_phone],  # Recipient MUST be in a list/array
        "content": message_body,  # Use 'content' key
    }

    try:
        log_func.debug(f"Sending OpenPhone SMS To: {payload['to']}, From: {payload['from']}")
        log_func.debug(f"Authorization Header Type Sent: Direct Token") # Confirming no Bearer

        response = requests.post(api_url, headers=headers, json=payload, timeout=20)
        response.raise_for_status() # Raises HTTPError for 4xx/5xx responses

        response_data = response.json()
        message_id = response_data.get('id', 'N/A')
        status = response_data.get('status', 'N/A') # e.g., 'queued', 'sent'
        log_func.info(f"Successfully sent SMS via OpenPhone to {recipient_phone}. Response ID: {message_id}, Status: {status}")
        return True

    except requests.exceptions.HTTPError as http_err:
        log_func.error(f"HTTP Error sending OpenPhone SMS to {recipient_phone}: {http_err}")
        try: error_details = http_err.response.json(); log_func.error(f"OpenPhone API Error Response: Status={http_err.response.status_code}, Details={error_details}")
        except:
            try: log_func.error(f"OpenPhone API Error Response: Status={http_err.response.status_code}, Body={http_err.response.text}")
            except: pass # No response details available
        return False
    except requests.exceptions.RequestException as req_err:
        log_func.error(f"Request Exception sending OpenPhone SMS to {recipient_phone}: {req_err}")
        return False
    except Exception as e:
         log_func.error(f"Unexpected error in send_openphone_sms to {recipient_phone}: {e}", exc_info=True)
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
                    sequence_name_query = text("SELECT pg_get_serial_sequence('messages', 'id');"); result = db.session.execute(sequence_name_query).scalar()
                    if result:
                        sequence_name = result; max_id_query = text("SELECT COALESCE(MAX(id), 0) FROM messages"); max_id = db.session.execute(max_id_query).scalar(); next_val = max_id + 1
                        reset_seq_query = text(f"SELECT setval('{sequence_name}', :next_val, false)"); db.session.execute(reset_seq_query, {'next_val': next_val}); db.session.commit()
                        app.logger.info(f"🔁 messages.id sequence ('{sequence_name}') reset to {next_val}.")
                    else: app.logger.warning("⚠️ Could not determine sequence name for messages.id.")
                except Exception as seq_err: db.session.rollback(); app.logger.error(f"❌ Error resetting PostgreSQL sequence: {seq_err}", exc_info=True)
            else: app.logger.info("ℹ️ Skipping sequence reset (not PostgreSQL).")
            # Property count query commented out
            # prop_count = Property.query.count()
            # app.logger.info(f"👉 Properties in DB: {prop_count}")
            app.logger.info("✅ Database initialization complete.")
        except Exception as e:
            db.session.rollback()
            app.logger.critical(f"❌ FATAL STARTUP ERROR during database initialization: {e}", exc_info=True)

# --- URL Map Helper ---
def print_url_map(app_instance):
     """Logs all registered URL routes."""
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
# Use before_first_request or similar if available/needed, otherwise runs on import
# Using app.app_context() is generally safer for operations needing the app
with app.app_context():
    initialize_database(app.app_context()) # Call initialization
# Print map only if debug is enabled via environment variable
if os.environ.get("FLASK_DEBUG", "false").lower() in ['true', '1', 't']:
    print_url_map(app)

# ──────────────────────────────────────────────────────────────────────────────
#  Jinja Context Processors & Custom Filters (Optional)
# ──────────────────────────────────────────────────────────────────────────────
@app.context_processor
def inject_now():
    """Inject current year into all templates."""
    return {'current_year': datetime.now(timezone.utc).year}

# ──────────────────────────────────────────────────────────────────────────────
#  ROUTES
# ──────────────────────────────────────────────────────────────────────────────

# --- INDEX ROUTE ---
@app.route("/")
def index():
    """Displays the main dashboard."""
    # ... (Keep simple count version for now) ...
    db_status = "?"; summary_today = "?"; summary_week = "?"
    try:
        db.session.execute(text("SELECT 1")); db_status = "Connected"
        now_utc = datetime.now(timezone.utc); start_today_utc = datetime.combine(now_utc.date(), datetime.min.time(), tzinfo=timezone.utc) # Use timezone aware
        start_week_utc  = start_today_utc - timedelta(days=start_today_utc.weekday()) # Week starts Monday
        count_today = (db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_today_utc).scalar() or 0)
        count_week = (db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_week_utc).scalar() or 0)
        summary_today = f"{count_today} messages today."; summary_week  = f"{count_week} messages this week."
    except Exception as ex: db.session.rollback(); db_status = f"Error: {ex}"; app.logger.error(f"❌ DB Error index: {ex}")
    return render_template("index.html", db_status=db_status, summary_today=summary_today, summary_week=summary_week) # current_year injected by context processor

# --- PROPERTY ROUTES ---
@app.route('/properties')
def properties_list_view():
    """Displays a list of all properties."""
    # Fetch all properties from the database, ordered by name perhaps
    properties = Property.query.order_by(Property.name).all()
    return render_template('properties_list.html', properties=properties) # current_year injected

@app.route('/property/<int:property_id>')
def property_detail_view(property_id):
    """Displays details for a specific property, its tenants, and communication history."""
    current_app.logger.info(f"--- /property/{property_id} route accessed ---")
    try:
        # 1. Fetch the specific property by its ID
        prop = db.session.get(Property, property_id) # Use db.session.get
        if not prop:
            abort(404, description="Property not found")
        current_app.logger.debug(f" Found property: {prop.name}")

        # 2. Fetch tenants associated with this property
        tenants = Tenant.query.filter_by(property_id=property_id).order_by(Tenant.name).all()
        current_app.logger.debug(f" Found {len(tenants)} tenants for property {prop.id}")

        # 3. Fetch recent communication history relevant to this property
        # Filter directly in the query for efficiency
        property_identifier_string = f'(ID:{property_id})'
        relevant_history = NotificationHistory.query.filter(
            # Use LIKE for substring matching in properties_targeted
            NotificationHistory.properties_targeted.like(f'%{property_identifier_string}%')
        ).order_by(
            NotificationHistory.timestamp.desc()
        ).limit(50).all() # Limit the number of history items fetched
        current_app.logger.debug(f" Found {len(relevant_history)} relevant history entries for property {prop.id}")

        # 4. Render the detail template, passing the data
        return render_template('property_detail.html',
                               prop=prop,
                               tenants=tenants,
                               history=relevant_history) # current_year injected

    except Exception as e:
        current_app.logger.error(f"❌ Error loading property detail page for ID {property_id}: {e}", exc_info=True)
        # traceback.print_exc() # Logging already captures this with exc_info=True
        flash(f"An error occurred while loading property details: {e}", "danger")
        return redirect(url_for('properties_list_view')) # Redirect on error


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

            # Determine contact info (using the first message's contact if available)
            is_known = False
            contact_name = None
            if msgs_for_number and msgs_for_number[0].contact:
                contact = msgs_for_number[0].contact
                is_known = True
                contact_name = contact.get_display_name() # Use helper if it exists

            # Fallback if no messages or no contact linked to first message
            if not is_known:
                 contact = db.session.get(Contact, target_phone_number)
                 if contact:
                      is_known = True
                      contact_name = contact.get_display_name()

            app.logger.debug(f"Contact lookup {target_phone_number}: known={is_known}, name='{contact_name}'")
            # Ensure messages_detail.html exists
            return render_template("messages_detail.html", phone_number=target_phone_number, messages=msgs_for_number, is_known=is_known, contact_name=contact_name) # current_year injected
        else:
            app.logger.debug("--- Loading OVERVIEW view ---")
            # Eager load related property and contact objects using joinedload to avoid N+1 query issues
            msgs_overview = Message.query.options(
                joinedload(Message.property),
                joinedload(Message.contact) # Eager load contact info
            ).order_by(Message.timestamp.desc()).limit(100).all()
            app.logger.debug(f"Query overview returned {len(msgs_overview)} messages.")

            # Fetch properties needed for the dropdowns in the overview
            properties_list = Property.query.order_by(Property.name).all()
            # Ensure messages_overview.html exists
            return render_template("messages_overview.html", messages=msgs_overview, properties=properties_list) # current_year injected
                                     # removed known_contact_phones as it's less efficient than joinedload

     except Exception as ex:
        db.session.rollback(); app.logger.error(f"❌ Error messages_view: {ex}", exc_info=True); error_msg = f"Error: {ex}"; flash(error_msg, "danger")
        template_name = "messages_detail.html" if target_phone_number else "messages_overview.html"
        try: return render_template(template_name, messages=[], properties=[], phone_number=target_phone_number, is_known=False, contact_name=None, error=error_msg) # current_year injected
        except: return redirect(url_for('index')) # Fallback redirect

# !!! REPLACED with the more robust version for unsorted gallery assignment !!!
@app.route("/assign_property", methods=["POST"])
# @login_required # Add if you have authentication
def assign_property():
    """ Handles the form submission from various places (like unsorted gallery or message overview)
        to assign a message to a property.
    """
    message_id_str = request.form.get('message_id')
    property_id_str = request.form.get('property_id') # Get property_id as string first
    # Determine where to redirect back to
    redirect_url = request.referrer or url_for('index') # Default redirect if referrer is missing

    current_app.logger.info(f"Received assignment request via POST: message_id='{message_id_str}', property_id='{property_id_str}', referrer='{request.referrer}'")

    # --- Validation ---
    message_id = None
    if message_id_str and message_id_str.isdigit():
        message_id = int(message_id_str)
    else:
        flash('Invalid or missing Message ID provided.', 'danger')
        current_app.logger.warning("Assign property failed: Invalid or missing message_id")
        return redirect(redirect_url)

    # Use db.session.get for fetching by primary key
    message = db.session.get(Message, message_id)

    if not message:
        flash(f'Message with ID {message_id} not found.', 'warning')
        current_app.logger.warning(f"Assign property failed: Message ID {message_id} not found.")
        return redirect(redirect_url)

    # --- Determine target property_id (could be None to unassign) ---
    target_property_id = None
    prop_name_for_flash = "Unassigned"
    if property_id_str and property_id_str.isdigit() and int(property_id_str) > 0:
        target_property_id = int(property_id_str)
        # Verify the property actually exists
        property_obj = db.session.get(Property, target_property_id)
        if not property_obj:
             flash(f'Selected Property (ID: {target_property_id}) not found.', 'warning')
             current_app.logger.warning(f"Assign property failed: Target Property ID {target_property_id} not found.")
             return redirect(redirect_url)
        prop_name_for_flash = property_obj.name # Get name for flash message
    elif property_id_str is None or property_id_str == "" or property_id_str.lower() == "none":
        target_property_id = None # Explicitly unassigning
        current_app.logger.info(f"Request to unassign property from message {message_id}")
    else:
        # Invalid property_id format submitted (not digit, not empty/None)
        flash(f'Invalid Property ID format: {property_id_str}. Please select a valid property or leave blank to unassign.', 'warning')
        current_app.logger.warning(f"Assign property failed: Invalid property_id format '{property_id_str}' for message {message_id}.")
        return redirect(redirect_url)

    # --- Perform Update ---
    try:
        message.property_id = target_property_id # Assign the integer ID or None
        db.session.commit()
        flash(f'Message #{message_id} assigned to property "{prop_name_for_flash}" successfully.', 'success')
        current_app.logger.info(f"Successfully updated Message ID {message_id} property_id to {target_property_id} ('{prop_name_for_flash}')")
    except Exception as e:
        db.session.rollback()
        flash(f'Database error assigning property: {str(e)}', 'danger')
        current_app.logger.error(f"Error committing property assignment for message {message_id} to property {target_property_id}: {e}", exc_info=True)

    # Add fragment identifier if redirecting back to a page that uses it
    if "#" not in redirect_url and 'msg-' in redirect_url: # Avoid adding if already has fragment
         redirect_url += f"#msg-{message_id}"
         current_app.logger.debug(f"Appending fragment, redirecting to: {redirect_url}")

    return redirect(redirect_url)


@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
    """Manages contacts: add, list/delete named, list/rename auto-added."""
    error_message = None
    dummy_phone_key = '0000000000' # Define dummy key constant

    # --- POST Request Handling ---
    if request.method == "POST":
        action = request.form.get("action")
        current_app.logger.debug(f"Contacts POST action: {action}")
        try:
            if action == "add":
                name = request.form.get("name", "").strip()
                phone_key = request.form.get("phone", "").strip() # Assume this is the 10-digit key
                if name and phone_key:
                    if len(phone_key) != 10 or not phone_key.isdigit(): flash("Invalid phone key format (10 digits).", "error")
                    else:
                        existing = db.session.get(Contact, phone_key) # Use db.session.get
                        if existing:
                            # If contact exists and was auto-named, update it
                            is_default_name = (existing.contact_name.startswith('+') and len(existing.contact_name) > 1 and existing.contact_name[1:].isdigit()) or \
                                              (len(existing.contact_name) == 10 and existing.contact_name.isdigit() and existing.contact_name == existing.phone_number)
                            if is_default_name:
                                existing.contact_name = name
                                current_app.logger.info(f"Contact '{phone_key}' updated with name '{name}'.")
                                flash(f"Auto-named contact {phone_key} updated to '{name}'.", "success")
                            else:
                                flash(f"Contact key {phone_key} ('{existing.contact_name}') already exists.", "warning")
                        else:
                            new_contact = Contact(phone_number=phone_key, contact_name=name)
                            db.session.add(new_contact)
                            current_app.logger.info(f"Contact '{name}' added to session.")
                            flash(f"Contact '{new_contact.contact_name}' added.", "success") # Flash on add success
                else: flash("Name and Phone Key are required.", "error")
            elif action == "delete":
                contact_key_to_delete = request.form.get("contact_id")
                if not contact_key_to_delete: flash("No Contact ID provided.", "error")
                else:
                    contact_to_delete = db.session.get(Contact, contact_key_to_delete) # Use db.session.get
                    if not contact_to_delete: flash("Contact not found.", "error")
                    elif contact_key_to_delete == dummy_phone_key: flash("Cannot delete default reference.", "warning")
                    else:
                        # Ensure dummy contact exists before attempting re-assignment
                        dummy_contact = db.session.get(Contact, dummy_phone_key) # Use db.session.get for PK lookup
                        if not dummy_contact:
                            current_app.logger.error(f"CRITICAL: Dummy contact '{dummy_phone_key}' missing.")
                            flash("Internal error: Default reference missing.", "danger"); contact_to_delete = None # Prevent deletion
                        else:
                            # Re-assign associated messages BEFORE deleting the contact
                            # Use update() for potentially better performance on many messages
                            update_stmt = Message.__table__.update().where(
                                Message.phone_number == contact_key_to_delete
                            ).values(
                                phone_number = dummy_phone_key
                            )
                            result = db.session.execute(update_stmt)
                            updated_count = result.rowcount
                            current_app.logger.info(f"Re-assigned {updated_count} messages from contact {contact_key_to_delete} to dummy contact.")

                            # Now safe to delete the contact
                            current_app.logger.info(f"Marking Contact '{contact_to_delete.contact_name}' ({contact_key_to_delete}) for deletion.")
                            db.session.delete(contact_to_delete)
                            flash(f"Contact '{contact_to_delete.contact_name}' deleted (Messages reassigned).", "success") # Flash on delete success

            # --- Commit Add/Delete Operations ---
            current_app.logger.info("Attempting final db.session.commit() for contacts POST...")
            db.session.commit()
            current_app.logger.info("✅ Final db.session.commit() successful.")

        except Exception as ex:
            db.session.rollback(); app.logger.error(f"❌ Error contacts POST: {ex}", exc_info=True); flash(f"Error processing contact: {ex}", "danger")

        # Redirect after POST handled (or error occurred)
        redirect_target = request.referrer or url_for("contacts_view") # Default back to contacts view
        return redirect(redirect_target)


    # --- GET Request Handling ---
    properly_known_contacts=[]; recent_auto_named_contacts=[];
    try:
        # Fetch all contacts into a dictionary for efficient lookup
        all_contacts_list = Contact.query.all(); all_contacts_dict = {c.phone_number: c for c in all_contacts_list}; auto_named_contacts_dict = {}

        # Separate known vs auto-named
        for contact in all_contacts_list:
            is_default_name=False;
            # Simplified check for default names (is it just the 10-digit key OR does it look like +E164?)
            if contact.contact_name:
                looks_like_plus_e164 = (contact.contact_name.startswith('+') and len(contact.contact_name) > 1 and contact.contact_name[1:].isdigit());
                looks_like_key = (len(contact.contact_name) == 10 and contact.contact_name.isdigit() and contact.contact_name == contact.phone_number);
                is_default_name = looks_like_plus_e164 or looks_like_key
            if not is_default_name and contact.phone_number != dummy_phone_key:
                 properly_known_contacts.append(contact)
            elif is_default_name:
                 auto_named_contacts_dict[contact.phone_number] = contact

        # Sort known contacts by name
        properly_known_contacts.sort(key=lambda x: x.contact_name.lower() if x.contact_name else "")

        # Find recent distinct phone numbers that have interacted (using window function)
        message_alias = Message.__table__.alias('m')
        row_num_col = func.row_number().over(
             partition_by=message_alias.c.phone_number,
             order_by=message_alias.c.timestamp.desc()
         ).label('rn')

        ranked_messages_subq = select(
            message_alias.c.phone_number,
            message_alias.c.timestamp,
            row_num_col
        ).select_from(message_alias).subquery('ranked_messages')

        latest_distinct_numbers_query = select(
            ranked_messages_subq.c.phone_number
        ).where(
            ranked_messages_subq.c.rn == 1
        ).order_by(
            ranked_messages_subq.c.timestamp.desc()
        ).limit(50) # Limit distinct numbers fetched

        recent_distinct_numbers_result = db.session.execute(latest_distinct_numbers_query).all()
        recent_distinct_numbers = [num for (num,) in recent_distinct_numbers_result]

        # Get the top 10 auto-named contacts from this recent list
        count = 0; processed_keys = set()
        for number in recent_distinct_numbers:
            if number in auto_named_contacts_dict and number not in processed_keys:
                recent_auto_named_contacts.append(auto_named_contacts_dict[number])
                processed_keys.add(number)
                count += 1
                if count >= 10: break # Limit to showing 10 recent auto-named

        app.logger.debug(f"Contacts GET: Known={len(properly_known_contacts)}, Auto={len(recent_auto_named_contacts)}")

    except Exception as ex:
        db.session.rollback(); app.logger.error(f"❌ Error loading contacts GET: {ex}", exc_info=True); error_message = f"Error: {ex}"; flash(error_message, "danger")

    return render_template("contacts.html",
                           properly_known_contacts=properly_known_contacts,
                           recent_auto_named_contacts=recent_auto_named_contacts,
                           error=error_message) # current_year injected


@app.route("/update_contact_name", methods=["POST"])
def update_contact_name():
    """Handles inline name updates from the contacts page."""
    phone_key = request.form.get("phone_key"); new_name = request.form.get("new_name", "").strip()
    if not phone_key or not new_name: flash("Missing key or name.", "error"); return redirect(url_for('contacts_view'))
    try:
        contact_to_update = db.session.get(Contact, phone_key) # Use db.session.get
        if contact_to_update:
            # Check if new name looks like a phone number (prevent setting name to number)
            is_reverting = ( (new_name.startswith('+') and len(new_name) > 1 and new_name[1:].isdigit()) or \
                             (len(new_name) == 10 and new_name.isdigit()) ) # Simplified check

            if is_reverting:
                flash("Cannot set name to just a phone number.", "warning")
            else:
                old_name = contact_to_update.contact_name; contact_to_update.contact_name = new_name; db.session.commit()
                app.logger.info(f"✅ Updated contact name {phone_key}: '{old_name}' -> '{new_name}'.")
                flash(f"Contact {phone_key} updated to '{new_name}'.", "success")
        else: flash(f"Contact key '{phone_key}' not found.", "error")
    except Exception as e: db.session.rollback(); app.logger.error(f"❌ Error update_contact_name {phone_key}: {e}", exc_info=True); flash(f"Error: {e}", "danger")
    return redirect(url_for('contacts_view'))

# ──────────────────────────────────────────────────────────────────────────────
#  Tenant Notifications Page (WITH SEND LOGIC + DEBUG LOGGING + ATTACHMENTS)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/notifications", methods=["GET", "POST"])
def notifications_view():
    """Displays notification form and history, handles sending."""
    properties = []
    history = []
    error_message = None

    # --- POST Logic (Send Notifications) ---
    if request.method == "POST":
        property_ids = request.form.getlist('property_ids')
        subject = request.form.get('subject', '')
        message_body = request.form.get('message_body', '')
        channels = request.form.getlist('channels')

        # --- Handle File Uploads ---
        uploaded_files = request.files.getlist('attachments')
        attachments_data = [] # Prepare list to pass to email sender
        for file in uploaded_files:
            if file and file.filename:
                filename = secure_filename(file.filename)
                # Read content directly as bytes
                file_content_bytes = file.read()
                # Get content type
                content_type = file.content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'

                # Append data needed by email_utils.send_email
                attachments_data.append({
                    'content_bytes': file_content_bytes, # Pass raw bytes
                    'filename': filename,
                    'type': content_type
                })
        current_app.logger.debug(f"Processed {len(attachments_data)} attachments for email.")

        # --- Input Validation ---
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
            for t in target_tenants:
                current_app.logger.debug(f"   Tenant Found: ID={t.id}, Name='{t.name}', Status='{t.status}', PropID={t.property_id}, Email='{t.email}', Phone='{t.phone}'")

            # Prepare Recipient Lists
            emails_to_send = {t.email for t in target_tenants if t.email}
            phones_to_send = {t.phone for t in target_tenants if t.phone}
            current_app.logger.debug(f"Unique Emails prepared: {emails_to_send}")
            current_app.logger.debug(f"Unique Phones prepared: {phones_to_send}")

            # Check if any potential recipients actually exist *before* attempting send
            if not emails_to_send and not phones_to_send:
                flash(f"No current tenants with email or phone found for the selected properties.", "warning")
                history_log = NotificationHistory(
                    subject=subject or None, body=message_body, channels=", ".join(channels), status="No Recipients Found",
                    properties_targeted=properties_targeted_str, recipients_summary="Email: 0/0. SMS: 0/0.", error_info=None)
                db.session.add(history_log); db.session.commit()
                current_app.logger.warning(f"Notification attempt logged (ID: {history_log.id}), but no recipients found.")
                return redirect(url_for('notifications_view'))

            # --- Send Notifications ---
            email_success_count = 0; sms_success_count = 0
            email_errors = []; sms_errors = []
            channels_attempted = []
            final_status = "Sent" # Assume success initially

            # 1. Send Emails
            if 'email' in channels and emails_to_send:
                channels_attempted.append("Email")
                current_app.logger.info(f"Attempting email to {len(emails_to_send)} addresses...")
                email_subject = subject if subject else message_body[:50] + ("..." if len(message_body) > 50 else "")
                # Prepare HTML body *once*
                html_body = f"<p>{message_body.replace(os.linesep, '<br>')}</p>" # Use basic <p> and <br>

                for email in emails_to_send:
                    try:
                        # Capture the return value (True/False) from send_email
                        email_sent_successfully = send_email(
                            to_emails=[email],
                            subject=email_subject,
                            html_content=wrap_email_html(html_body), # Wrap HTML content
                            attachments=attachments_data # Pass processed attachment data
                        )
                        # Increment count ONLY if send_email returned True
                        if email_sent_successfully:
                            email_success_count += 1
                        else:
                            email_errors.append(f"{email}: Failed") # Record failure
                    except Exception as e:
                        current_app.logger.error(f"Email Exception for {email}: {e}", exc_info=True)
                        email_errors.append(f"{email}: Exception")
            elif 'email' in channels: current_app.logger.info("Email channel selected, but no valid tenant emails found.")

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
            elif 'sms' in channels: current_app.logger.info("SMS channel selected, but no valid tenant phone numbers found.")

            # --- Determine Overall Status & Log History ---
            total_email_attempts = len(emails_to_send) if 'Email' in channels_attempted else 0
            total_sms_attempts = len(phones_to_send) if 'SMS' in channels_attempted else 0
            total_successes = email_success_count + sms_success_count
            total_attempts = total_email_attempts + total_sms_attempts

            current_app.logger.warning(f"Status Calc: Channels Attempted={channels_attempted}")
            current_app.logger.warning(f"Status Calc: Email Success={email_success_count}, Email Attempts={total_email_attempts}")
            current_app.logger.warning(f"Status Calc: SMS Success={sms_success_count}, SMS Attempts={total_sms_attempts}")
            current_app.logger.warning(f"Status Calc: Total Successes={total_successes}, Total Attempts={total_attempts}")

            recipients_summary = f"Email: {email_success_count}/{total_email_attempts}. SMS: {sms_success_count}/{total_sms_attempts}."
            error_details = []
            if email_errors: error_details.append(f"{len(email_errors)} Email failure(s)") # Be clearer
            if sms_errors: error_details.append(f"{len(sms_errors)} SMS failure(s)")
            error_info_str = "; ".join(error_details) + " (See logs)" if error_details else None # Simpler message

            # Determine final status based on calculated values
            if total_attempts == 0: # This case is handled earlier now
                 final_status = "No Recipients Found" # Should not be reached
                 current_app.logger.error("!!! Reached 'total_attempts == 0' unexpectedly in status calc !!!")
            elif email_errors or sms_errors: # If there were ANY errors/failures
                 if total_successes > 0: # But at least one success
                      final_status = "Partial Failure"
                      current_app.logger.warning(f"Setting final status to 'Partial Failure'. Successes ({total_successes}) < Attempts ({total_attempts}).")
                      flash(f"Notifications sent with some failures. ({recipients_summary})", "warning")
                 else: # No successes, only errors/failures
                      final_status = "Failed"
                      current_app.logger.error(f"Setting final status to 'Failed'. Successes ({total_successes}), Attempts ({total_attempts}).")
                      flash(f"All notifications failed to send. Check logs.", "danger")
            else: # No errors/failures and total_attempts > 0 implies all succeeded
                 final_status = "Sent"
                 current_app.logger.info(f"Setting final status to 'Sent'. Successes ({total_successes}) == Attempts ({total_attempts}).")
                 flash(f"Notifications sent successfully! ({recipients_summary})", "success")

            # Log to DB
            current_app.logger.info(f"Attempting to log history with calculated status: {final_status}")
            history_log = NotificationHistory(
                subject=subject if 'Email' in channels_attempted else None, # Only log subject if email was attempted
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
        "notifications.html", properties=properties, history=history, error=error_message
    ) # current_year injected

# ──────────────────────────────────────────────────────────────────────────────
#  GALLERY ROUTES
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/galleries")
def galleries_overview():
    """Displays a list of properties that have associated media galleries and unsorted media."""
    properties_with_galleries = []
    unsorted_count = 0
    error_message = None

    try:
        # Find distinct property IDs that are linked to messages WITH valid local_media_paths
        # Assuming local_media_paths is a list/JSON type, check if it's not NULL
        # For string type (legacy?), check not NULL and not empty string. Adapt query if needed.
        property_ids_with_media = db.session.query(
            Message.property_id
        ).filter(
            Message.property_id.isnot(None),
            Message.local_media_paths.isnot(None)
            # Add filter for non-empty list/array if needed and using specific DB type functions:
            # e.g., func.array_length(Message.local_media_paths, 1) > 0
            # e.g., func.jsonb_array_length(Message.local_media_paths) > 0
            # If it's text and might be empty string: and_(Message.local_media_paths != '')
        ).distinct().all()

        prop_ids = [pid for (pid,) in property_ids_with_media]
        app.logger.debug(f"Found {len(prop_ids)} property IDs with media: {prop_ids}")

        if prop_ids:
            # Fetch the actual Property objects for these IDs, order by name
            properties_with_galleries = Property.query.filter(
                Property.id.in_(prop_ids)
            ).order_by(Property.name).all()
            app.logger.debug(f"Fetched {len(properties_with_galleries)} property objects for gallery overview.")

        # Count distinct messages with media that are NOT assigned to any property
        # NOTE: This counts MESSAGES, not individual image files within them.
        unsorted_message_count_query = db.session.query(func.count(Message.id)).filter(
            Message.property_id.is_(None),
            Message.local_media_paths.isnot(None)
            # Add non-empty list/array filter here too if needed
        )
        unsorted_count = unsorted_message_count_query.scalar() or 0
        app.logger.debug(f"Found {unsorted_count} unsorted messages with media.")

    except Exception as e:
        db.session.rollback() # Rollback on error during query
        app.logger.error(f"❌ Error loading galleries overview: {e}", exc_info=True)
        error_message = f"Error loading galleries overview: {e}"
        flash(error_message, "danger") # Show error to user

    return render_template("galleries_overview.html",
                           gallery_summaries=properties_with_galleries, # Pass list of Property objects
                           unsorted_count=unsorted_count, # Pass the count of messages
                           error=error_message) # current_year injected


# !!! REPLACED previous gallery_for_property logic !!!
# Assuming local_media_paths is stored as list/JSON in DB now (consistent with webhook)
@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    """Displays media gallery for messages assigned to a specific property."""
    prop = db.session.get(Property, property_id)
    if not prop:
        abort(404, description="Property not found")

    gallery_title = f"Gallery for {prop.name}"
    image_items = []
    error_message = None
    image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.heic') # Define image types

    try:
        # Query messages linked to this property that HAVE local media paths stored
        # Eager load contact for displaying sender info potentially
        messages_with_media = Message.query.options(
            joinedload(Message.contact)
        ).filter(
            Message.property_id == property_id,
            Message.local_media_paths.isnot(None)
             # Add filter for non-empty list/array if needed
             # func.array_length(Message.local_media_paths, 1) > 0 # Example for PostgreSQL ARRAY
        ).order_by(Message.timestamp.desc()).all()

        current_app.logger.debug(f"Found {len(messages_with_media)} messages with media for property {property_id}")

        # Process the paths from the messages found
        for msg in messages_with_media:
            # --- Check if local_media_paths is a string and split it ---
            if msg.local_media_paths and isinstance(msg.local_media_paths, str):
                paths = [p.strip() for p in msg.local_media_paths.split(',') if p.strip()]
                current_app.logger.debug(f"  Paths split from string: {paths} (Msg ID: {msg.id})")

                for idx, path in enumerate(paths): # Iterate the split list
                    trimmed_path = path # Already stripped
                    current_app.logger.debug(f"  Checking path for gallery: '{trimmed_path}' (Msg ID: {msg.id})")
                    is_image = trimmed_path.lower().endswith(image_extensions) if trimmed_path else False
                    current_app.logger.debug(f"  Path ends with valid extension? {is_image}")

                    if trimmed_path and is_image:
                        current_app.logger.debug(f"  >>> Adding image item: {trimmed_path}")
                        image_items.append({
                            "path": trimmed_path,
                            "message_id": msg.id,
                            "index": idx,
                            "timestamp": msg.timestamp,
                            "sender_info": msg.contact.get_display_name() if msg.contact else msg.phone_number
                        })
                    else:
                        current_app.logger.debug(f"  --- Skipping non-image or empty path from split: '{trimmed_path}'")

            # --- Optional: Handle if it's already a list ---
            elif msg.local_media_paths and isinstance(msg.local_media_paths, list):
                current_app.logger.debug(f"  local_media_paths is already a list: {msg.local_media_paths} (Msg ID: {msg.id})")
                for idx, path in enumerate(msg.local_media_paths):
                    trimmed_path = path.strip()
                    current_app.logger.debug(f"  Checking list path for gallery: '{trimmed_path}' (Msg ID: {msg.id})")
                    is_image = trimmed_path.lower().endswith(image_extensions) if trimmed_path else False
                    current_app.logger.debug(f"  List Path ends with valid extension? {is_image}")
                    if trimmed_path and is_image:
                        current_app.logger.debug(f"  >>> Adding image item from list: {trimmed_path}")
                        image_items.append({
                            "path": trimmed_path, "message_id": msg.id, "index": idx,
                            "timestamp": msg.timestamp,
                            "sender_info": msg.contact.get_display_name() if msg.contact else msg.phone_number
                        })
                    else:
                         current_app.logger.debug(f"  --- Skipping list item non-image or empty path: '{trimmed_path}'")
            elif msg.local_media_paths:
                # Log if it's neither string nor list but exists
                current_app.logger.warning(f"  local_media_paths for Msg ID {msg.id} is neither string nor list (Type: {type(msg.local_media_paths)}), Value: {msg.local_media_paths}")


        current_app.logger.debug(f"Prepared {len(image_items)} image items for gallery template")

    except Exception as e:
        db.session.rollback() # Rollback on error
        current_app.logger.error(f"❌ Error loading gallery for property {property_id}: {e}", exc_info=True)
        error_message = f"Error loading gallery: {e}"
        flash(error_message, "danger") # Flash message to user

    # Render the template, passing the list of image item dictionaries
    return render_template("gallery.html",
                           image_items=image_items, # This list now contains image data
                           property=prop, # Pass property object for context
                           gallery_title=gallery_title,
                           error=error_message) # current_year injected


# !!! REPLACED placeholder unsorted_gallery function !!!
@app.route('/unsorted')
# @login_required # Add if you have authentication
def unsorted_gallery():
    """
    Displays media from messages that have associated local_media_paths
    but are not yet assigned to a property (property_id is NULL).
    Provides data structure expected by the existing unsorted.html template.
    """
    current_app.logger.info("Accessing /unsorted route") # Add logging
    error_message = None
    unsorted_items_for_template = []
    properties_list = []

    # --- Define image_extensions OUTSIDE the loop ---
    image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.heic') # Filter for images

    try:
        # Query for messages with media paths but no property_id
        unsorted_messages = Message.query.options(
            joinedload(Message.contact) # Eager load contact
        ).filter(
            Message.property_id.is_(None),
            Message.local_media_paths.isnot(None)
            # Add check for non-empty list if needed/robustness desired
            # func.array_length(Message.local_media_paths, 1) > 0 # Example for PostgreSQL ARRAY
        ).order_by(Message.timestamp.desc()).all()
        current_app.logger.info(f"Found {len(unsorted_messages)} messages with unsorted media.")

        # Prepare the list for the template
        for message in unsorted_messages: # Start the loop
            # --- Check if local_media_paths is a string and split it ---
            if message.local_media_paths and isinstance(message.local_media_paths, str):
                paths = [p.strip() for p in message.local_media_paths.split(',') if p.strip()]
                current_app.logger.debug(f"  Paths split from string: {paths} (Msg ID: {message.id})")

                for path in paths: # Iterate the split list
                    trimmed_path = path # Already stripped
                    current_app.logger.debug(f"  Checking path for unsorted: '{trimmed_path}' (Msg ID: {message.id})")
                    is_image = trimmed_path.lower().endswith(image_extensions) if trimmed_path else False
                    current_app.logger.debug(f"  Path ends with valid extension? {is_image}")

                    if trimmed_path and is_image:
                        current_app.logger.debug(f"  >>> Adding unsorted item: {trimmed_path}")
                        item_data = {
                            'path': trimmed_path, # Use the actual path
                            'msg': message
                        }
                        unsorted_items_for_template.append(item_data)
                    else:
                        current_app.logger.debug(f"  --- Skipping non-image or empty path from split: '{trimmed_path}'")

            # --- Optional: Handle if it's already a list ---
            elif message.local_media_paths and isinstance(message.local_media_paths, list):
                current_app.logger.debug(f"  local_media_paths is already a list: {message.local_media_paths} (Msg ID: {message.id})")
                for path in message.local_media_paths:
                     trimmed_path = path.strip()
                     current_app.logger.debug(f"  Checking list path for unsorted: '{trimmed_path}' (Msg ID: {message.id})")
                     is_image = trimmed_path.lower().endswith(image_extensions) if trimmed_path else False
                     current_app.logger.debug(f"  List Path ends with valid extension? {is_image}")
                     if trimmed_path and is_image:
                         current_app.logger.debug(f"  >>> Adding unsorted item from list: {trimmed_path}")
                         item_data = {'path': trimmed_path, 'msg': message}
                         unsorted_items_for_template.append(item_data)
                     else:
                         current_app.logger.debug(f"  --- Skipping list item non-image or empty path: '{trimmed_path}'")
            elif message.local_media_paths:
                 # Log if it's neither string nor list but exists
                 current_app.logger.warning(f"  local_media_paths for Msg ID {message.id} is neither string nor list (Type: {type(message.local_media_paths)}), Value: {message.local_media_paths}")


        current_app.logger.info(f"Prepared {len(unsorted_items_for_template)} image items for the template.") # Log final count

        # Query for all properties to populate the dropdown
        properties_list = Property.query.order_by(Property.name).all()
        current_app.logger.info(f"Found {len(properties_list)} properties for the dropdown.")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in unsorted_gallery: {e}", exc_info=True)
        error_message = f"An error occurred while loading unsorted media: {e}"
        flash(error_message, "danger")

    # Pass data to the template
    return render_template(
        'unsorted.html',
        unsorted=unsorted_items_for_template, # Pass the structured list
        properties=properties_list,          # Pass the properties list
        error=error_message                  # Pass error message if any
    ) # current_year injected


# ──────────────────────────────────────────────────────────────────────────────
#  OTHER / LEGACY / DEBUG ROUTES
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    # ... (Keep previous version, but NOTE potential issues if Message fields changed) ...
    return render_template("ask.html") # current_year injected

@app.route("/gallery_static")
def gallery_static():
    # ... (Keep previous version) ...
    return render_template("gallery.html", image_items=[], gallery_title="Static Uploads Gallery") # current_year injected


# This route seems redundant if gallery_for_property covers specific galleries
# and unsorted_gallery covers unsorted. Consider removing or defining its purpose.
# @app.route("/gallery", endpoint="gallery_view")
# def gallery_view():
#     # ... (Keep previous version - NEEDS IMPLEMENTATION or removal) ...
#     return render_template("gallery.html", image_items=[], gallery_title="All Media Gallery") # current_year injected


@app.route("/ping")
def ping_route():
     # ... (Keep previous version) ...
     try: db.session.execute(text("SELECT 1")); return "Pong! DB OK.", 200
     except Exception as e: app.logger.error(f"DB Ping Fail: {e}"); return f"Pong! DB Error: {e}", 503

@app.route("/list-uploads")
def list_uploads():
     # ... (Keep previous version) ...
     # Basic implementation to list files in the upload directory
     upload_path = app.config['UPLOAD_FOLDER']
     try:
         files = [f for f in os.listdir(upload_path) if os.path.isfile(os.path.join(upload_path, f))]
         return jsonify({"uploads": files})
     except Exception as e:
         app.logger.error(f"Error listing uploads in {upload_path}: {e}", exc_info=True)
         return jsonify({"error": "Failed to list uploads", "details": str(e)}), 500


@app.route("/clear-contacts-debug", methods=['POST'])
def clear_contacts_debug():
     # ... (Keep the SAFER version with explicit dummy contact handling) ...
     current_app.logger.warning("--- [clear-contacts-debug] Route accessed ---"); dummy_phone_key='0000000000'
     try:
         # Find or create dummy contact
         dummy_contact = db.session.get(Contact, dummy_phone_key)
         if not dummy_contact:
             current_app.logger.info(f"Creating dummy contact: {dummy_phone_key}")
             dummy_contact = Contact(phone_number=dummy_phone_key, contact_name="Deleted Contact Ref")
             db.session.add(dummy_contact)
             db.session.flush() # Ensure dummy exists with PK before re-assigning

         # Update messages pointing to contacts *other than* the dummy contact using UPDATE statement
         update_stmt = Message.__table__.update().where(
             Message.phone_number != dummy_phone_key
         ).values(
             phone_number = dummy_phone_key
         )
         result = db.session.execute(update_stmt)
         updated_count = result.rowcount
         current_app.logger.info(f"Re-assigned {updated_count} messages to dummy contact ({dummy_phone_key}).")

         # Delete all contacts except the dummy one using DELETE statement
         delete_stmt = Contact.__table__.delete().where(
             Contact.phone_number != dummy_phone_key
         )
         result = db.session.execute(delete_stmt)
         deleted_count = result.rowcount
         current_app.logger.info(f"Deleted {deleted_count} non-dummy contacts.")

         # Commit the changes
         db.session.commit()
         flash(f"Debug: Contacts cleared ({deleted_count} deleted, {updated_count} messages reassigned).", "info")
     except Exception as e:
         db.session.rollback(); current_app.logger.error(f"❌ Error clear_contacts_debug: {e}", exc_info=True); flash(f"Error clearing contacts: {e}", "danger")

     return redirect(url_for('contacts_view')) # Redirect to contacts view after clearing

# --- Keep __main__ block ---
if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8080))
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() in ['true', '1', 't']
    app.run(host=host, port=port, debug=debug_mode)