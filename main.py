import os
import re # Keep this import - might be needed if you add regex later
import json # Keep this import
from flask import Flask, render_template, request, jsonify, url_for, redirect # Corrected import list
from dotenv import load_dotenv
from extensions import db
from models import Contact, Message
from webhook_route import webhook_bp # Assuming webhook_route.py exists
from sqlalchemy import text, func, distinct # Added distinct here just in case
from sqlalchemy.exc import OperationalError # Added this import
from datetime import datetime, timedelta
import openai
import glob # To find files

# --- Load environment variables ----
load_dotenv()

# --- Initialize Flask app ---
# Using 4 spaces for indentation now
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 'sqlite:///instance/messages.db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}
app.secret_key = os.getenv('FLASK_SECRET', 'default_secret')
# Auto-reload templates during development (Gunicorn might handle this differently)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Ensure SQLite instance folder exists
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:') and 'instance' in app.config['SQLALCHEMY_DATABASE_URI']:
    instance_path = os.path.join(app.root_path, 'instance')
    # Use exist_ok=True to avoid error if folder exists
    os.makedirs(instance_path, exist_ok=True)
    print("✅ Instance folder verified/created.")

# Initialize database and register blueprint
db.init_app(app)
app.register_blueprint(webhook_bp)

# Configure OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Create tables on startup
# This is generally okay for development, but consider Flask-Migrate for production
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
    current_year = datetime.utcnow().year # Define here
    try:
        db.session.execute(text('SELECT 1'))
        db_status = 'Connected'

        now = datetime.utcnow()
        start_today = datetime.combine(now.date(), datetime.min.time())
        start_week = start_today - timedelta(days=now.weekday())

        count_today = ( db.session.query(func.count(Message.id))
                        .filter(Message.timestamp >= start_today)
                        .scalar() )
        count_week = ( db.session.query(func.count(Message.id))
                       .filter(Message.timestamp >= start_week)
                       .scalar() )

        summary_today = f"{count_today} message(s) today."
        summary_week = f"{count_week} message(s) this week."
    except Exception as e:
        db.session.rollback()
        db_status = f"Error: {e}"

    return render_template(
        'index.html',
        db_status=db_status,
        summary_today=summary_today,
        summary_week=summary_week,
        current_year=current_year # Pass year to template
    )

# --- Messages Route (Handles HTML and JSON, Passes Known Contacts for HTML) ---
@app.route('/messages')
def messages_view():
    error = None
    current_year = datetime.utcnow().year # Needed for base template footer
    try:
        messages_query = Message.query.order_by(Message.timestamp.desc()).limit(50).all()

        if request.args.get('format') == 'json':
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
                    'is_read': True # Placeholder
                })
            # Calculate Stats for JSON
            now = datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week = start_today - timedelta(days=now.weekday())
            count_today = db.session.query(func.count(Message.id)).filter(
                Message.timestamp >= start_today).scalar()
            count_week = db.session.query(func.count(Message.id)).filter(
                Message.timestamp >= start_week).scalar()
            summary = f"{count_today} message(s) received today."
            stats_data = {
                'messages_today': count_today, 'messages_week': count_week,
                'unread_messages': 0, 'response_rate': 93, # Placeholders
                'summary_today': summary
            }
            return jsonify({'messages': messages_list, 'stats': stats_data})

        else:
            # --- RENDER HTML ---
            known_contact_phones_query = db.session.query(Contact.phone_number).all()
            known_contact_phones = {phone for (phone,) in known_contact_phones_query}
            print(f"--- DEBUG: Passing {len(known_contact_phones)} known numbers to messages template ---")

            # Calculate stats for HTML template
            now = datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week = start_today - timedelta(days=now.weekday())
            count_today = db.session.query(func.count(Message.id)).filter(
                Message.timestamp >= start_today).scalar()
            count_week = db.session.query(func.count(Message.id)).filter(
                Message.timestamp >= start_week).scalar()
            summary_for_html = f"{count_today} message(s) today."

            return render_template(
                'messages.html',
                messages=messages_query,
                error=error,
                messages_today=count_today,
                messages_week=count_week,
                summary_today=summary_for_html,
                known_contact_phones=known_contact_phones,
                current_year=current_year # Pass year
            )

    except Exception as e:
        db.session.rollback()
        error = f"Error fetching messages: {e}"
        print(f"❌ Error in /messages: {e}")
        if request.args.get('format') == 'json':
             return jsonify({'error': str(e)}), 500
        else:
             return render_template(
                 'messages.html', messages=[], error=error, messages_today=0,
                 messages_week=0, summary_today="Error loading summary.",
                 known_contact_phones=set(), current_year=current_year # Pass defaults
            )


# --- Contacts Route (Shows EXISTING contacts & handles ADD/DELETE) ---
@app.route('/contacts', methods=['GET', 'POST'])
def contacts_view():
    error = None
    known_contacts = []
    current_year = datetime.utcnow().year # Needed for base template footer

    if request.method == 'POST':
        action = request.form.get('action')
        phone = request.form.get('phone')

        if action == 'add':
            name = request.form.get('name', '').strip()
            if not phone:
                error = "Phone number missing from submission."
                print(f"❌ Contact form error: {error}")
            elif not name:
                error = "Name required to add contact."
                print(f"❌ Contact form error: {error}")
            else:
                try:
                    existing_contact = Contact.query.get(phone)
                    if not existing_contact:
                        new_contact = Contact(phone_number=phone, contact_name=name)
                        db.session.add(new_contact)
                        db.session.commit()
                        print(f"✅ Added contact via messages page: {name} ({phone})")
                        return redirect(url_for('messages_view')) # Redirect after add
                    else:
                        print(f"ℹ️ Contact already exists, attempted add ignored: {name} ({phone})")
                        error = f"Contact {phone} already exists."
                        return redirect(url_for('messages_view')) # Still redirect

                except Exception as e:
                    db.session.rollback()
                    print(f"❌ Error processing add request: {e}")
                    error = f"Error processing add: {e}"

        elif action == 'delete':
            phone_to_delete = phone
            if phone_to_delete:
                try:
                    contact_to_delete = Contact.query.get(phone_to_delete)
                    if contact_to_delete:
                        db.session.delete(contact_to_delete)
                        db.session.commit()
                        print(f"✅ Deleted contact: {contact_to_delete.contact_name} ({phone_to_delete})")
                    else:
                        print(f"⚠️ Attempted to delete non-existent contact: {phone_to_delete}")
                        error = f"Contact {phone_to_delete} not found."
                    return redirect(url_for('contacts_view')) # Redirect after delete
                except Exception as e:
                    db.session.rollback()
                    print(f"❌ Error processing delete request: {e}")
                    error = f"Error processing delete: {e}"
            else:
                 error = "Phone number missing for delete action."
                 print(f"❌ Contact form error: {error}")

        else:
            error = "Invalid action specified."
            print(f"❌ Contact form error: {error}")
            # Fall through to GET render with error

    # --- Handle GET requests (or fall through from POST error) ---
    try:
        known_contacts = Contact.query.order_by(Contact.contact_name).all()
        print(f"--- DEBUG: Fetched {len(known_contacts)} known contacts for display ---")
        recent_calls = [] # Add logic here if needed later
    except Exception as e:
        db.session.rollback()
        error = f"Error fetching contacts: {e}" if not error else error
        print(f"❌ Error in /contacts GET: {e}")
        known_contacts = []
        recent_calls = []

    # Pass current_year for footer in base template
    return render_template('contacts.html',
                           known_contacts=known_contacts,
                           recent_calls=recent_calls,
                           error=error,
                           current_year=current_year) # Pass year


# --- Ask Route ---
@app.route('/ask', methods=['GET', 'POST'])
def ask_view():
    response_text = None
    error_message = None
    query = ""
    current_year = datetime.utcnow().year # Needed for base template footer---

    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        if not query:
            error_message = "Please enter a question."
        elif not os.getenv("OPENAI_API_KEY"):
             error_message = "❌ OpenAI API key is not configured."
        else:
            try:
                # Using OpenAI v0.x syntax as per previous code
                completion = openai.ChatCompletion.create(
                    model="gpt-4", # Or specify the model you have access to
                    messages=[{"role": "user", "content": query}]
                )
                response_text = completion.choices[0].message['content']
            except Exception as e:
                error_message = f"❌ Error communicating with OpenAI: {e}"
                print(f"❌ OpenAI API Error: {e}")

    return render_template('ask.html',
                           response=response_text,
                           error=error_message,
                           current_query=query,
                           current_year=current_year) # Pass year


# --- Gallery Route ---
@app.route('/gallery')
def gallery_view():
    error = None
    image_files = []
    upload_folder_name = 'uploads' # Folder name inside 'static'
    # Construct the absolute path to the upload folder
    upload_folder_path = os.path.join(app.static_folder, upload_folder_name)
    print(f"--- DEBUG: Looking for images in: {upload_folder_path} ---")

    try:
        # Check if the upload directory exists
        if os.path.isdir(upload_folder_path):
            print(f"--- DEBUG: Scanning folder: {upload_folder_path} ---")
            # Define allowed image extensions (case-insensitive)
            allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp'} # Added .webp

            # List all items in the directory
            all_items_in_dir = os.listdir(upload_folder_path)
            print(f"--- DEBUG: Found {len(all_items_in_dir)} total items in folder. ---")

            # Filter for files with allowed extensions
            for filename in all_items_in_dir:
                full_path = os.path.join(upload_folder_path, filename)
                # Make sure it's a file (not a subdirectory)
                if os.path.isfile(full_path):
                    file_ext = os.path.splitext(filename)[1].lower() # Get lowercased extension
                    if file_ext in allowed_extensions:
                        # Construct path relative to 'static' folder for url_for
                        relative_path = os.path.join(upload_folder_name, filename).replace(os.path.sep, '/')
                        image_files.append(relative_path)
                        # print(f"--- DEBUG: Added image file: {relative_path} ---") # Optional: log each found file
                # else: # Optional: log skipped directories
                #     print(f"--- DEBUG: Skipping non-file item: {filename} ---")

            print(f"--- DEBUG: Found {len(image_files)} image files matching extensions using os.listdir. ---")

            # Optional: Sort images by modification time (newest first)
            try:
                image_files.sort(key=lambda f: os.path.getmtime(os.path.join(app.static_folder, f)), reverse=True)
                print("--- DEBUG: Sorted image files by modification time. ---")
            except Exception as sort_e:
                 print(f"--- DEBUG: Error sorting image files: {sort_e} ---") # Non-critical if sorting fails

        else:
            print(f"--- DEBUG: Upload folder not found: {upload_folder_path} ---")
            error = "Upload folder not found. Ensure webhook is saving files to static/uploads."

    except Exception as e:
        print(f"❌ Error accessing gallery images: {e}")
        traceback.print_exc() # Print full traceback for gallery errors
        error = "Error loading gallery."
        image_files = [] # Ensure list is empty on error

    # Pass the list of relative image paths and other context
    return render_template(
        'gallery.html',
        image_files=image_files,
        error=error,
        current_year=datetime.utcnow().year
    )
    


# --- DEBUG URL MAP ---
# Added AFTER all routes and blueprint registrations
try:
    with app.app_context():
        print("\n--- Registered URL Endpoints ---")
        # Sort rules for easier reading
        rules = sorted(list(app.url_map.iter_rules()), key=lambda rule: rule.endpoint)
        for rule in rules:
            # Limit methods shown for brevity if needed, or use str(rule.methods)
            methods = ','.join(sorted([m for m in rule.methods if m not in ('HEAD', 'OPTIONS')]))
            print(f"Endpoint: {rule.endpoint:<30} Methods: {methods:<20} Rule: {rule}")
        print("--- End Registered URL Endpoints ---\n")
except Exception as map_e:
    print(f"--- Error inspecting URL map: {map_e} ---")
# --- END DEBUG URL MAP ---


# --- Main execution block ---
if __name__ == '__main__':
    # This block is mainly for local development testing.
    # Gunicorn (or another WSGI server) specified in .replit run command
    # will directly interact with the 'app' object in deployment.
    print("Starting Flask development server (DO NOT USE IN PRODUCTION DEPLOYMENT)...")
    port = int(os.getenv("PORT", 8080)) # Use PORT for local consistency if needed
    # app.run(host='0.0.0.0', port=port, debug=False) # Commented out - Gunicorn runs the app
    # If you *want* to run locally using `python main.py`, uncomment app.run
    # but ensure debug=False if testing production-like behavior.
    pass # Keep the block structure valid even with app.run commented out