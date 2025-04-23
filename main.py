print("!!!!!!!!!!!!!!!!! MAIN.PY RELOADED AT LATEST TIMESTAMP !!!!!!!!!!!!!!!!")

import os
# ... rest of your imports ...

import os
import re  # Keep this import - might be needed if you add regex later
import json  # Keep this import
from flask import Flask, render_template, request, jsonify, url_for, redirect  # Ensure all needed are here
from dotenv import load_dotenv
from extensions import db
from models import Contact, Message, Property  # Added Property
# --- UNCOMMENTED BELOW ---
from webhook_route import webhook_bp
from sqlalchemy import text, func, distinct
from sqlalchemy.exc import OperationalError  # Added this import
from datetime import datetime, timedelta
import openai
# import glob # Removed - no longer used by gallery route
import traceback  # Needed for gallery error logging

# --- Load environment variables ----------
load_dotenv()

# --- Initialize Flask app ---
# Using 4 regular spaces for indentation now
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 'sqlite:///instance/messages.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}
app.secret_key = os.getenv('FLASK_SECRET', 'default_secret')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Ensure SQLite instance folder exists
if app.config['SQLALCHEMY_DATABASE_URI'].startswith(
        'sqlite:') and 'instance' in app.config['SQLALCHEMY_DATABASE_URI']:
    instance_path = os.path.join(app.root_path, 'instance')
    # Use exist_ok=True to avoid error if folder exists
    os.makedirs(instance_path, exist_ok=True)
    print("✅ Instance folder verified/created.")

# Initialize database and register blueprint
db.init_app(app)
# --- UNCOMMENTED BELOW ---
app.register_blueprint(webhook_bp)

# Configure OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Create tables on startup
print("Attempting to create database tables...")
try:
    with app.app_context():
        db.create_all()
    print("✅ Database tables created/verified.")
except Exception as init_e:
    print(f"❌ Error creating tables: {init_e}")


# --- Index Route ---
@app.route('/')
def index():
    db_status = 'Unknown'
    summary_today = summary_week = 'Unavailable'
    current_year = datetime.utcnow().year
    try:
        db.session.execute(text('SELECT 1'))
        db_status = 'Connected'
        now = datetime.utcnow()
        start_today = datetime.combine(now.date(), datetime.min.time())
        start_week = start_today - timedelta(days=now.weekday())
        count_today = db.session.query(func.count(
            Message.id)).filter(Message.timestamp >= start_today).scalar()
        count_week = db.session.query(func.count(
            Message.id)).filter(Message.timestamp >= start_week).scalar()
        summary_today = f"{count_today} message(s) today."
        summary_week = f"{count_week} message(s) this week."
    except Exception as e:
        db.session.rollback()
        db_status = f"Error: {e}"
    return render_template('index.html',
                           db_status=db_status,
                           summary_today=summary_today,
                           summary_week=summary_week,
                           current_year=current_year)


# --- Messages Route ---
@app.route('/messages')
def messages_view():
    error = None
    current_year = datetime.utcnow().year
    messages_query = []
    properties = []
    known_contact_phones = set()
    count_today = 0
    count_week = 0
    summary_for_html = "N/A"

    try:
        messages_query = Message.query.options(
            db.joinedload(Message.property)  # Correct eager loading syntax
        ).order_by(Message.timestamp.desc()).limit(50).all()

        if request.args.get('format') == 'json':
            # JSON Logic... (kept as is)
            messages_list = []
            for msg in messages_query:
                messages_list.append({
                    'id': msg.id,
                    'timestamp': msg.timestamp.isoformat() + 'Z',
                    'phone_number': msg.phone_number,
                    'contact_name': msg.contact_name,
                    'direction': msg.direction,
                    'message': msg.message,
                    'media_urls': msg.media_urls,
                    'is_read':
                    True  # Assuming all viewed messages are read for JSON
                })
            now = datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week = start_today - timedelta(days=now.weekday())
            count_today = db.session.query(func.count(
                Message.id)).filter(Message.timestamp >= start_today).scalar()
            count_week = db.session.query(func.count(
                Message.id)).filter(Message.timestamp >= start_week).scalar()
            summary = f"{count_today} message(s) received today."
            stats_data = {
                'messages_today': count_today,
                'messages_week': count_week,
                'unread_messages': 0,  # Placeholder
                'response_rate': 93,  # Placeholder
                'summary_today': summary
            }
            return jsonify({'messages': messages_list, 'stats': stats_data})
        else:
            # RENDER HTML
            known_contact_phones_query = db.session.query(
                Contact.phone_number).all()
            known_contact_phones = {
                phone
                for (phone, ) in known_contact_phones_query
            }
            print(
                f"--- DEBUG: Passing {len(known_contact_phones)} known numbers to messages template ---"
            )
            try:
                properties = Property.query.order_by(Property.name).all()
                print(
                    f"--- DEBUG: Fetched {len(properties)} properties for dropdown ---"
                )
            except Exception as prop_e:
                print(f"❌ Error fetching properties: {prop_e}")
                properties = []
                error = error or f"Error fetching properties: {prop_e}"
            now = datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week = start_today - timedelta(days=now.weekday())
            count_today = db.session.query(func.count(
                Message.id)).filter(Message.timestamp >= start_today).scalar()
            count_week = db.session.query(func.count(
                Message.id)).filter(Message.timestamp >= start_week).scalar()
            summary_for_html = f"{count_today} message(s) today."
            return render_template('messages.html',
                                   messages=messages_query,
                                   error=error,
                                   messages_today=count_today,
                                   messages_week=count_week,
                                   summary_today=summary_for_html,
                                   known_contact_phones=known_contact_phones,
                                   properties=properties,
                                   current_year=current_year)
    except Exception as e:
        db.session.rollback()
        error = f"Error fetching messages: {e}"
        print(f"❌ Error in /messages: {e}")
        traceback.print_exc()
        if request.args.get('format') == 'json':
            return jsonify({'error': str(e)}), 500
        else:
            return render_template('messages.html',
                                   messages=[],
                                   error=error,
                                   messages_today=0,
                                   messages_week=0,
                                   summary_today="Error loading messages.",
                                   known_contact_phones=set(),
                                   properties=[],
                                   current_year=current_year)


# In main.py - REPLACE the existing contacts_view function with this ------


# --- Contacts Route ---
@app.route('/contacts', methods=['GET', 'POST'])
def contacts_view():
    error = None
    known_contacts = []
    current_year = datetime.utcnow().year
    recent_calls = []  # Initialize recent_calls

    # --- Handle POST Requests (Add/Delete) ---
    if request.method == 'POST':
        action = request.form.get('action')
        phone = request.form.get('phone')
        if action == 'add':
            name = request.form.get('name', '').strip()
            if not phone or not name:
                error = "Phone/name required."
                print(f"❌ Contact form error: {error}")
            else:
                try:
                    # Normalize phone number before DB operations
                    normalized_phone = ''.join(filter(str.isdigit,
                                                      phone))[-10:]
                    if len(normalized_phone) < 10:
                        normalized_phone = phone  # Fallback

                    existing = Contact.query.get(normalized_phone)
                    if not existing:
                        # Check if number exists in messages (for potential name update)
                        messages_from_num = Message.query.filter_by(
                            phone_number=normalized_phone).all()

                        db.session.add(
                            Contact(phone_number=normalized_phone,
                                    contact_name=name))
                        db.session.commit()
                        print(f"✅ Added contact: {name} ({normalized_phone})")

                        # Optional: Update contact_name in existing messages from this number
                        if messages_from_num:
                            for msg in messages_from_num:
                                msg.contact_name = name
                            try:
                                db.session.commit()
                                print(
                                    f"ℹ️ Updated contact name in {len(messages_from_num)} existing messages for {normalized_phone}"
                                )
                            except Exception as update_err:
                                db.session.rollback()
                                print(
                                    f"⚠️ Error updating names in existing messages: {update_err}"
                                )

                        return redirect(url_for(
                            'contacts_view'))  # Redirect after successful add
                    else:
                        print(
                            f"ℹ️ Contact already exists: {name} ({normalized_phone})"
                        )
                        error = f"Contact {normalized_phone} already exists."
                        # Don't redirect here, let the GET request handle showing the error below
                except Exception as e:
                    db.session.rollback()
                    print(f"❌ Error processing add contact: {e}")
                    traceback.print_exc()
                    error = f"Error adding contact: {e}"  # Show error on page
        elif action == 'delete':
            if phone:
                try:
                    # Normalize phone number before DB operations
                    normalized_phone = ''.join(filter(str.isdigit,
                                                      phone))[-10:]
                    if len(normalized_phone) < 10:
                        normalized_phone = phone  # Fallback

                    contact = Contact.query.get(normalized_phone)
                    if contact:
                        db.session.delete(contact)
                        db.session.commit()
                        print(
                            f"✅ Deleted contact: {getattr(contact, 'contact_name', 'N/A')} ({normalized_phone})"
                        )
                    else:
                        print(
                            f"⚠️ Delete non-existent contact: {normalized_phone}"
                        )
                        error = f"Contact {normalized_phone} not found for deletion."
                    return redirect(url_for(
                        'contacts_view'))  # Redirect after delete attempt
                except Exception as e:
                    db.session.rollback()
                    print(f"❌ Error processing delete contact: {e}")
                    traceback.print_exc()
                    error = f"Error deleting contact: {e}"  # Show error on page
            else:
                error = "Phone number missing for delete action."
                print(f"❌ Contact form error: {error}")
        else:
            error = "Invalid action specified for contacts."
            print(f"❌ Contact form error: {error}")
    # --- End Handle POST ---

    # --- Handle GET Request (Fetch known contacts AND recent unknown numbers) ---
    try:
        # Fetch all known contacts
        known_contacts = Contact.query.order_by(Contact.contact_name).all()
        known_contact_phones = {c.phone_number for c in known_contacts}
        # <<< LOOK FOR THIS LOG >>>
        print(f"--- DEBUG: Fetched {len(known_contacts)} known contacts ---")

        # Find recent messages from numbers NOT in known contacts
        cutoff_time = datetime.utcnow() - timedelta(
            days=30)  # Look back 30 days (adjust as needed)

        # Subquery to get the latest timestamp for each unknown incoming number
        subq = db.session.query(
            Message.phone_number,
            func.max(Message.timestamp).label('max_ts')).filter(
                Message.direction == 'incoming',
                Message.timestamp >= cutoff_time,
                ~Message.phone_number.in_(
                    known_contact_phones)  # The ~ means NOT IN
            ).group_by(Message.phone_number).subquery()

        # Main query to get the message details for those latest timestamps
        unknown_messages = db.session.query(Message).join(
            subq, (Message.phone_number == subq.c.phone_number) &
            (Message.timestamp == subq.c.max_ts)).order_by(
                subq.c.max_ts.desc()).limit(20).all()  # Limit results

        # Prepare the recent_calls list for the template
        recent_calls = []
        if unknown_messages:
            # <<< LOOK FOR THIS LOG >>>
            print(
                f"--- DEBUG: Found {len(unknown_messages)} recent messages from unknown numbers ---"
            )
            for msg in unknown_messages:
                recent_calls.append({
                    'phone_number':
                    msg.phone_number,  # Pass the number to the template
                    'message': msg.message,  # Pass the last message text
                    'timestamp': msg.timestamp  # Pass the timestamp
                })
        else:
            # <<< OR LOOK FOR THIS LOG >>>
            print(
                "--- DEBUG: No recent messages from unknown numbers found ---")

    except Exception as e:
        db.session.rollback()  # Rollback on error during GET
        error = f"Error fetching contacts/messages: {e}" if not error else error  # Preserve POST errors
        print(f"❌ Error in /contacts GET: {e}")
        traceback.print_exc()
        known_contacts = []  # Ensure lists are empty on error
        recent_calls = []

    # Render the template, passing both known and unknown contacts/calls
    return render_template(
        'contacts.html',
        known_contacts=known_contacts,
        recent_calls=recent_calls,  # Pass the populated or empty list
        error=error,  # Pass any error message from POST or GET
        current_year=current_year)


# --- End contacts_view ---


# --- Assign Property Route ---
@app.route('/assign_property', methods=['POST'])
def assign_property():
    # error = None # Not used here
    message_id = request.form.get('message_id')
    property_id = request.form.get('property_id')
    redirect_url = url_for('messages_view')  # Default redirect

    if not message_id:
        print("❌ Assign Property Error: message_id missing.")
        # Consider flashing a message here instead of just redirecting
        return redirect(redirect_url)

    try:
        message = Message.query.get(message_id)
        if not message:
            print(
                f"❌ Assign Property Error: Message ID {message_id} not found.")
            return redirect(redirect_url)  # Redirect even if message not found

        redirect_url += f"#msg-{message_id}"  # Add fragment for successful find

        if not property_id or property_id == '':  # Unassign
            if message.property_id is not None:
                message.property_id = None
                db.session.commit()
                print(f"ℹ️ Unassigned property from msg ID {message_id}")
            else:
                print(f"ℹ️ Msg {message_id} already unassigned.")
        else:  # Assign
            try:
                prop_id_int = int(property_id)
                prop = Property.query.get(prop_id_int)
                if prop:
                    if message.property_id != prop_id_int:
                        message.property_id = prop_id_int
                        db.session.commit()
                        print(
                            f"✅ Assigned property '{prop.name}' to msg ID {message_id}"
                        )
                    else:
                        print(
                            f"ℹ️ Msg {message_id} already assigned to prop {prop_id_int}."
                        )
                else:
                    print(
                        f"❌ Assign Property Error: Property ID {prop_id_int} not found."
                    )
                    # Consider flashing an error message
            except ValueError:
                print(
                    f"❌ Assign Property Error: Invalid Property ID format '{property_id}'."
                )
                # Consider flashing an error message
    except Exception as e:
        db.session.rollback()
        print(f"❌ Assign Property Error: {e}")
        traceback.print_exc()
        # Consider flashing an error message
        redirect_url = url_for('messages_view')  # Reset redirect URL on error

    return redirect(redirect_url)


# --- Ask Route ---
@app.route('/ask', methods=['GET', 'POST'])
def ask_view():
    response_text = None
    error_message = None
    query = ""
    current_year = datetime.utcnow().year
    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        if not query:
            error_message = "Please enter a question."
        elif not os.getenv("OPENAI_API_KEY"):
            error_message = "❌ OpenAI API key not configured."
            print(error_message)  # Also log it
        else:
            try:
                print(f"--- Sending query to OpenAI: '{query[:50]}...' ---")
                completion = openai.ChatCompletion.create(
                    model=
                    "gpt-4",  # Consider using a newer/cheaper model if applicable
                    messages=[{
                        "role": "user",
                        "content": query
                    }])
                response_text = completion.choices[0].message['content']
                print("--- Received response from OpenAI ---")
            except Exception as e:
                error_message = f"❌ Error communicating with OpenAI: {e}"
                print(f"❌ OpenAI API Error: {e}")
                traceback.print_exc()  # Log the full error

    return render_template('ask.html',
                           response=response_text,
                           error=error_message,
                           current_query=query,
                           current_year=current_year)


# --- Gallery Route ---
@app.route('/gallery')
def gallery_view():
    error = None
    image_files = []
    upload_folder_name = 'uploads'
    current_year = datetime.utcnow().year
    # Use app.static_folder which is Flask's way of knowing the static path
    upload_folder_path = os.path.join(app.static_folder, upload_folder_name)
    print(f"--- DEBUG: Looking for images in: {upload_folder_path} ---")

    try:
        if os.path.isdir(upload_folder_path):
            print(f"--- DEBUG: Scanning folder: {upload_folder_path} ---")
            allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
            all_items_in_dir = os.listdir(upload_folder_path)
            print(f"--- DEBUG: Found {len(all_items_in_dir)} total items. ---")

            valid_image_files = []
            for filename in all_items_in_dir:
                # Check if it looks like a file generated by the webhook
                # Adjust this check if your unique_filename format changes
                if filename.startswith('msg') and '_' in filename:
                    full_path = os.path.join(upload_folder_path, filename)
                    if os.path.isfile(full_path):
                        file_ext = os.path.splitext(filename)[1].lower()
                        if file_ext in allowed_extensions:
                            # Create the relative path needed by url_for('static', ...)
                            relative_path = os.path.join(
                                upload_folder_name,
                                filename).replace(os.path.sep, '/')
                            valid_image_files.append({
                                'path':
                                relative_path,
                                'mtime':
                                os.path.getmtime(
                                    full_path)  # Store mod time for sorting
                            })

            print(
                f"--- DEBUG: Found {len(valid_image_files)} valid image files matching pattern/extensions. ---"
            )

            # Sort by modification time, newest first
            valid_image_files.sort(key=lambda f: f['mtime'], reverse=True)
            # Extract just the paths after sorting
            image_files = [f['path'] for f in valid_image_files]

            if valid_image_files:
                print(
                    "--- DEBUG: Sorted image files by modification time. ---")

        else:
            print(
                f"--- DEBUG: Upload folder not found or is not a directory: {upload_folder_path} ---"
            )
            error = "Upload folder not found. Ensure 'static/uploads' directory exists."

    except Exception as e:
        print(f"❌ Error accessing gallery images: {e}")
        traceback.print_exc()
        error = "Error loading gallery images."
        image_files = []  # Ensure it's empty on error

    return render_template('gallery.html',
                           image_files=image_files,
                           error=error,
                           current_year=current_year)


# --- Test Route ---  <<<<<<<<<< MODIFIED BELOW >>>>>>>>>>
@app.route('/ping')  # Changed path to /ping
def ping_route():  # Changed function name
    print("--- /ping route accessed ---")  # Changed print message
    return "Pong!", 200  # Changed return message


# --- DEBUG URL MAP ---
# Keep this block for verifying routes on startup
try:
    with app.app_context():
        print("\n--- Registered URL Endpoints ---")
        rules = sorted(list(app.url_map.iter_rules()),
                       key=lambda rule: rule.endpoint)
        for rule in rules:
            # Exclude built-in endpoints like 'static' if desired for cleaner output
            # if rule.endpoint == 'static': continue
            methods = ','.join(
                sorted(
                    [m for m in rule.methods if m not in ('HEAD', 'OPTIONS')]))
            print(
                f"Endpoint: {rule.endpoint:<30} Methods: {methods:<20} Rule: {rule}"
            )
        print("--- End Registered URL Endpoints ---\n")
except Exception as map_e:
    print(f"--- Error inspecting URL map: {map_e} ---")
# --- END DEBUG URL MAP ---

# --- Main execution block ---
if __name__ == '__main__':
    # Gunicorn runs the app in deployment via the .replit file config.
    # For local development testing (optional):
    # app.run(debug=True, host='0.0.0.0', port=8080)
    pass  # Keep structure valid for Gunicorn
