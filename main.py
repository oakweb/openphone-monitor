import os
import json
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
from sqlalchemy import text, func
import openai


from extensions import db
from models import Contact, Message, Property
from webhook_route import webhook_bp # Assuming webhook_bp is correctly defined elsewhere

# ──────────────────────────────────────────────────────────────────────────────
#   App configuration
# ──────────────────────────────────────────────────────────────────────────────


app = Flask(__name__)

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
# Ensure it's an absolute path within the container context if needed
app.static_folder = os.path.join(app.root_path, 'static') # More robust way to define static folder
# Ensure the upload directory exists within the static folder
UPLOAD_FOLDER = os.path.join(app.static_folder, 'uploads')
# Create the directory on app startup if it doesn't exist
# This helps ensure the target exists before the first request
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
print(f"ℹ️ Configured UPLOAD_FOLDER: {app.config['UPLOAD_FOLDER']}") # Log the final path


# initialize extensions & webhook blueprint
db.init_app(app)
app.register_blueprint(webhook_bp)

# Configure OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")


# ──────────────────────────────────────────────────────────────────────────────
#   Startup: create tables, ensure `sid`, reset sequence
# ──────────────────────────────────────────────────────────────────────────────
# Use a function for startup logic to avoid running it multiple times if imported
def initialize_database(app_context):
    with app_context:
        print("🔄 Initializing Database...")
        try:
            # 1) Create all tables if missing
            # This requires the application context
            db.create_all()
            print("✅ Tables created/verified.")

            # 2) Ensure the `sid` column exists on messages (SQLite/Postgres compatible)
            # Using try-except for broader compatibility
            try:
                # Use text() for raw SQL execution within a session
                db.session.execute(text("ALTER TABLE messages ADD COLUMN sid VARCHAR"))
                db.session.commit()
                print("✅ Ensured messages.sid column exists (added if missing).")
            except Exception as alter_err:
                db.session.rollback()
                # Ignore error if column already exists (common case)
                # Error messages vary between DBs, check common substrings
                err_str = str(alter_err).lower()
                if "already exists" in err_str or "duplicate column name" in err_str:
                    print("✅ messages.sid column already exists.")
                else:
                     print(f"⚠️  Could not add 'sid' column (may already exist or other issue): {alter_err}")


            # 3) Reset the `id` sequence (PostgreSQL only; safely ignore on others)
            if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
                try:
                    # Find the actual sequence name associated with messages.id
                    sequence_name_query = text("""
                        SELECT pg_get_serial_sequence('messages', 'id');
                    """)
                    result = db.session.execute(sequence_name_query).scalar()

                    if result:
                        sequence_name = result
                        # Get the maximum current ID, default to 0 if table is empty
                        max_id_query = text("SELECT COALESCE(MAX(id), 0) FROM messages")
                        max_id = db.session.execute(max_id_query).scalar()
                        # Set the sequence value to max_id + 1, ensuring it starts at 1 for empty table
                        next_val = max_id + 1
                        reset_seq_query = text(f"SELECT setval('{sequence_name}', :next_val, false)")
                        db.session.execute(reset_seq_query, {'next_val': next_val})
                        db.session.commit()
                        print(f"🔁 messages.id sequence ('{sequence_name}') reset to {next_val}.")
                    else:
                        print("⚠️ Could not determine sequence name for messages.id.")

                except Exception as seq_err:
                    db.session.rollback()
                    # Provide more context on the error
                    print(f"❌ Error resetting PostgreSQL sequence: {seq_err}")
                    # Check if it's a permission error, common in managed DBs
                    if "permission denied" in str(seq_err).lower():
                        print("ℹ️ Hint: The database user might lack permissions to alter sequences.")
            else:
                print("ℹ️ Skipping sequence reset (not PostgreSQL).")

            # Query property count within the context
            prop_count = Property.query.count()
            print(f"👉 Properties in DB: {prop_count}")
            print("✅ Database initialization complete.")

        except Exception as e:
            # Ensure rollback happens on any initialization error
            db.session.rollback()
            print(f"❌ FATAL STARTUP ERROR during database initialization: {e}")
            traceback.print_exc()

# Call initialization within app context when the app starts
# Using app.app_context() ensures extensions like SQLAlchemy are ready
initialize_database(app.app_context())


# ──────────────────────────────────────────────────────────────────────────────
#   Index / Dashboard
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    current_year = datetime.utcnow().year
    db_status = "Unknown"
    summary_today = "Unavailable"
    summary_week = "Unavailable"
    count_today = 0 # Default value
    count_week = 0 # Default value

    try:
        # Check DB connection using a lightweight query
        db.session.execute(text("SELECT 1"))
        db_status = "Connected"

        # Calculate stats using UTC time for consistency
        now_utc = datetime.utcnow()
        # Combine date part with min time (midnight) in UTC
        start_today_utc = datetime.combine(now_utc.date(), datetime.min.time())
        # Calculate the start of the week (Monday) based on UTC date
        start_week_utc  = start_today_utc - timedelta(days=now_utc.weekday())

        # Query using the Message model and filter by timestamp
        count_today = (
            db.session.query(func.count(Message.id))
            .filter(Message.timestamp >= start_today_utc)
            .scalar() or 0 # Ensure it's 0 if query returns None
        )
        count_week = (
            db.session.query(func.count(Message.id))
            .filter(Message.timestamp >= start_week_utc)
            .scalar() or 0 # Ensure it's 0 if query returns None
        )

        # Format summary strings
        summary_today = f"{count_today} message(s) today."
        summary_week  = f"{count_week} message(s) this week."

    except Exception as ex:
        db.session.rollback() # Rollback in case of error during query
        db_status     = f"Error connecting or querying DB: {ex}"
        print(f"❌ DB Error on index page: {ex}") # Log the error for debugging
        # Keep summaries as "Unavailable"

    # Render the index template with collected data
    return render_template(
        "index.html",
        db_status=db_status,
        summary_today=summary_today,
        summary_week=summary_week,
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Media Deletion (Associated with a Message)
# ──────────────────────────────────────────────────────────────────────────────
# This route handles deleting a specific media file linked to a message.
@app.route('/delete-media/<int:message_id>/<int:file_index>', methods=['POST'])
def delete_media_for_message(message_id, file_index): # Renamed function for clarity
    # Fetch the message or return 404 if not found
    message = Message.query.get_or_404(message_id)
    # Determine where to redirect after deletion, default to overview
    redirect_url = request.referrer or url_for('galleries_overview')
    # If the message belongs to a property, redirect to that property's gallery
    if message.property_id:
        redirect_url = url_for('gallery_for_property', property_id=message.property_id)


    # Check if the message actually has media paths stored
    if not message.local_media_paths:
        flash("No media associated with this message to delete.", "warning")
        return redirect(redirect_url)

    # Split the comma-separated paths string into a list, removing empty strings and whitespace
    media_paths = [p.strip() for p in message.local_media_paths.split(',') if p.strip()]

    # Validate the provided file index
    if 0 <= file_index < len(media_paths):
        relative_path_to_delete = media_paths[file_index] # e.g., "uploads/filename.jpg"
        # Construct the full absolute path to the file on the server's filesystem
        # Use current_app.static_folder which should be the absolute path to /app/static
        full_file_path = os.path.join(current_app.static_folder, relative_path_to_delete)

        # Basic security check: ensure the path is within the static folder
        # Use os.path.normpath to handle potential path separators consistently
        if not os.path.normpath(full_file_path).startswith(os.path.normpath(current_app.static_folder)):
             flash("Invalid file path specified (attempt to access outside static folder).", "danger")
             return redirect(redirect_url)

        # Check if the file exists on the filesystem using the constructed absolute path
        print(f"ℹ️ [delete-media] Checking for file existence at: {full_file_path}")
        if os.path.exists(full_file_path):
            try:
                # Attempt to delete the file from the filesystem
                os.remove(full_file_path)
                print(f"✅ [delete-media] Successfully deleted file: {full_file_path}")
                flash(f"Successfully deleted media file: {os.path.basename(relative_path_to_delete)}.", "success")

                # If file deletion is successful, remove the path from the list
                media_paths.pop(file_index)
                # Update the database record with the modified list of paths
                # Set to None if the list becomes empty
                message.local_media_paths = ','.join(media_paths) if media_paths else None
                db.session.commit() # Commit the change to the database
                print(f"✅ [delete-media] Updated database for message {message_id}")

            except OSError as e:
                # Handle potential OS errors during file deletion (e.g., permissions)
                print(f"❌ [delete-media] OS error deleting file {full_file_path}: {e}")
                flash(f"Error deleting file from disk: {e}", "danger")
                db.session.rollback() # Rollback DB changes if file deletion failed
            except Exception as e:
                 # Handle any other unexpected errors during deletion
                 print(f"❌ [delete-media] Unexpected error deleting file {full_file_path}: {e}")
                 flash(f"An unexpected error occurred during deletion: {e}", "danger")
                 db.session.rollback()
        else:
            # If the file doesn't exist on disk, inform the user but still update the DB record
            print(f"⚠️ [delete-media] File not found on disk at {full_file_path}, but removing from DB record.")
            flash(f"Media file not found on disk: {os.path.basename(relative_path_to_delete)}. Removing from database record.", "warning")
            # Remove the path from the list even if the file was already gone
            media_paths.pop(file_index)
            message.local_media_paths = ','.join(media_paths) if media_paths else None
            db.session.commit() # Commit the DB change
            print(f"✅ [delete-media] Updated database for message {message_id} (file was already missing).")


    else:
        # If the provided index is out of bounds
        flash("Invalid media index provided.", "error")

    # Redirect the user back to the appropriate page
    return redirect(redirect_url)


# ──────────────────────────────────────────────────────────────────────────────
#   Messages & Assignment
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/messages")
def messages_view():
    current_year = datetime.utcnow().year
    try:
        # Query messages, eager load related property, order by newest first
        msgs = (
            Message.query
            .options(db.joinedload(Message.property))
            .order_by(Message.timestamp.desc())
            .limit(100) # Limit the number of messages fetched for performance
            .all()
        )

        # --- JSON Endpoint Support ---
        # Check if the request asks for JSON format
        if request.args.get("format") == "json":
            now_utc = datetime.utcnow()
            start_today_utc = datetime.combine(now_utc.date(), datetime.min.time())
            start_week_utc  = start_today_utc - timedelta(days=now_utc.weekday())

            # Calculate counts specifically for the JSON response
            count_today_json = (
                db.session.query(func.count(Message.id))
                .filter(Message.timestamp >= start_today_utc)
                .scalar() or 0
            )
            count_week_json = (
                db.session.query(func.count(Message.id))
                .filter(Message.timestamp >= start_week_utc)
                .scalar() or 0
            )

            # Prepare data structure for messages
            message_data = [{
                "id": m.id,
                "timestamp": m.timestamp.isoformat() + "Z", # Use ISO 8601 format with Z for UTC
                "phone_number": m.phone_number,
                "contact_name": m.contact_name,
                "direction": m.direction,
                "message": m.message,
                "media_urls": m.media_urls, # Original URLs (e.g., from Twilio)
                "local_media_paths": m.local_media_paths, # Paths to locally saved files
                "property_id": m.property_id,
                "property_name": m.property.name if m.property else None, # Include property name if available
                "sid": m.sid # Include Twilio SID if available
            } for m in msgs]

            # Prepare statistics structure
            stats_data = {
                "messages_today": count_today_json,
                "messages_week": count_week_json,
                "summary_today": f"{count_today_json} message(s) today.",
                "summary_week": f"{count_week_json} message(s) this week.",
            }
            # Return JSON response
            return jsonify({"messages": message_data, "stats": stats_data})

        # --- HTML View ---
        # Get distinct known contact phone numbers for highlighting/linking
        known_phones_query = db.session.query(Contact.phone_number).distinct().all()
        known_contact_phones_set = {p for (p,) in known_phones_query} # Use a set for efficient lookup

        # Get all properties for the assignment dropdown, ordered by name
        properties_list = Property.query.order_by(Property.name).all()

        # Render the HTML template
        return render_template(
            "messages.html",
            messages=msgs,
            known_contact_phones=known_contact_phones_set,
            properties=properties_list,
            current_year=current_year,
        )

    except Exception as ex:
        # Handle potential errors during database query or processing
        db.session.rollback() # Rollback any partial transaction
        traceback.print_exc() # Print detailed error to console/logs
        flash(f"Error loading messages page: {ex}", "danger") # Show user-friendly error
        # Render the template with empty data to prevent further errors
        return render_template(
            "messages.html",
            messages=[],
            properties=[],
            known_contact_phones=set(),
            error=str(ex), # Pass error message to template if needed
            current_year=current_year,
        )


@app.route("/assign_property", methods=["POST"])
def assign_property():
    # Get message ID and property ID from the submitted form
    message_id_str = request.form.get("message_id")
    property_id_str = request.form.get("property_id", "") # Default to empty string if not provided

    # Determine the redirect URL (go back where the user came from)
    # Default to messages view if referrer is not available
    redirect_url = request.referrer or url_for("messages_view")

    # Validate message ID
    if not message_id_str:
         flash("No message ID provided for assignment.", "error")
         return redirect(redirect_url)

    try:
        message_id = int(message_id_str) # Convert message ID to integer
        message = Message.query.get(message_id) # Fetch the message object

        # Check if the message exists
        if not message:
            flash(f"Message with ID {message_id} not found.", "error")
            return redirect(redirect_url)

        # Handle assignment or unassignment based on property_id_str
        if property_id_str and property_id_str.lower() != "none" and property_id_str != "":
             # Assigning to a specific property
             property_id = int(property_id_str) # Convert property ID to integer
             # Optional: Verify the selected property exists
             prop = Property.query.get(property_id)
             if not prop:
                  flash(f"Property with ID {property_id} not found.", "error")
                  # Don't commit if property doesn't exist
                  return redirect(redirect_url)

             message.property_id = property_id # Assign the property ID to the message
             flash(f"Message (ID: {message_id}) assigned to property: {prop.name}", "success")
        else:
             # Unassigning the property (if 'None' or empty string was selected)
             message.property_id = None # Set property_id to None in the database
             flash(f"Message (ID: {message_id}) unassigned from property.", "info")

        # Commit the changes to the database
        db.session.commit()

    except ValueError:
         # Handle cases where message_id or property_id are not valid integers
         flash("Invalid Message or Property ID format provided.", "error")
         db.session.rollback() # Rollback any potential partial changes
    except Exception as e:
         # Handle any other unexpected errors during assignment
         flash(f"An error occurred while assigning property: {e}", "danger")
         db.session.rollback()
         traceback.print_exc() # Log the full error details

    # Redirect back to the previous page, trying to jump to the specific message anchor
    # Check if the redirect URL already has a fragment
    if "#" not in redirect_url:
        # Add fragment only if message_id is validly processed
        if 'message_id' in locals() and isinstance(message_id, int):
             redirect_url += f"#msg-{message_id}"

    return redirect(redirect_url)


# ──────────────────────────────────────────────────────────────────────────────
#   Contacts Management
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
    current_year = datetime.utcnow().year
    error_message = None # Variable to hold potential error messages for the template

    # --- POST Request Handling (Add/Delete Contacts) ---
    if request.method == "POST":
        action = request.form.get("action") # Determine if adding or deleting
        try:
            if action == "add":
                # Get name and phone from form, stripping whitespace
                name = request.form.get("name", "").strip()
                phone = request.form.get("phone", "").strip()

                # Basic validation: ensure both fields are provided
                if name and phone:
                    # TODO: Add more robust phone number validation/normalization here
                    # Extract last 10 digits for the key
                    phone_key = "".join(filter(str.isdigit, phone))[-10:]
                    if len(phone_key) != 10:
                         flash("Invalid phone number format. Please provide at least 10 digits.", "error")
                         return redirect(url_for("contacts_view"))

                    # Check if a contact with this key already exists
                    existing = Contact.query.get(phone_key)
                    if existing:
                         # Inform user if contact already exists
                         flash(f"Contact with phone number ending in ...{phone_key[-4:]} already exists: '{existing.contact_name}'.", "warning")
                    else:
                        # Create and save the new contact
                        new_contact = Contact(phone_number=phone_key, contact_name=name)
                        db.session.add(new_contact)
                        db.session.commit()
                        flash(f"Contact '{name}' ({phone}) added successfully.", "success")
                else:
                    # Inform user if required fields are missing
                    flash("Both Name and Phone Number are required to add a contact.", "error")

            elif action == "delete":
                # Get the ID (phone key) of the contact to delete
                contact_key_to_delete = request.form.get("contact_id") # Assuming form sends phone_key as contact_id
                if contact_key_to_delete:
                    contact_to_delete = Contact.query.get(contact_key_to_delete)
                    if contact_to_delete:
                        # Delete the contact and commit
                        db.session.delete(contact_to_delete)
                        db.session.commit()
                        flash(f"Contact '{contact_to_delete.contact_name}' deleted.", "success")
                    else:
                        # Inform user if contact ID is invalid
                        flash("Contact not found for deletion.", "error")
                else:
                    flash("No Contact ID provided for deletion.", "error")

            # Redirect back to the contacts page after processing the POST request
            return redirect(url_for("contacts_view"))

        except ValueError:
            flash("Invalid Contact ID provided for deletion.", "error")
            db.session.rollback()
        except Exception as ex:
             # Handle any unexpected errors during POST processing
             db.session.rollback()
             traceback.print_exc()
             flash(f"An error occurred while processing the contact action: {ex}", "danger")
             error_message = str(ex) # Store error for potential display on GET reload

    # --- GET Request Handling (Display Contacts) ---
    known_contacts_list = []
    recent_calls_list = [] # Placeholder for potential future implementation

    try:
        # Fetch all known contacts, ordered alphabetically by name
        known_contacts_list = Contact.query.order_by(Contact.contact_name).all()

        # Example: Fetch recent unique phone numbers from messages (optional)
        # recent_numbers_query = db.session.query(Message.phone_number)\
        #     .order_by(Message.timestamp.desc())\
        #     .distinct()\
        #     .limit(15).all() # Get last 15 unique numbers
        # recent_calls_list = [num for (num,) in recent_numbers_query]

    except Exception as ex:
        # Handle errors during GET request processing
        db.session.rollback()
        traceback.print_exc()
        error_message = f"Error loading contacts list: {ex}"
        flash(error_message, "danger")

    # Render the contacts template
    return render_template(
        "contacts.html",
        known_contacts=known_contacts_list,
        recent_calls=recent_calls_list, # Pass the list (even if empty)
        error=error_message, # Pass any error message for display
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Ask (OpenAI Integration)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    current_year = datetime.utcnow().year
    openai_response = None # Variable for the response from OpenAI
    error_message = None # Variable for error messages
    current_query = None # Variable to hold the user's query for redisplay

    # --- POST Request Handling (Submit Query to OpenAI) ---
    if request.method == "POST":
        current_query = request.form.get("query", "").strip() # Get and strip query

        # Validate query presence
        if not current_query:
            error_message = "Please enter a question or prompt."
            flash(error_message, "warning")
        # Check if OpenAI API key is configured
        elif not openai.api_key:
            error_message = "OpenAI API key is not configured. Please set the OPENAI_API_KEY environment variable."
            # Log this error server-side as well for easier debugging
            print(f"ERROR: {error_message}")
            flash(error_message, "danger")
        else:
            # --- Call OpenAI API ---
            try:
                print(f"Sending query to OpenAI: '{current_query}'") # Log the query being sent
                # Use the ChatCompletion endpoint
                completion = openai.ChatCompletion.create(
                    # Consider making the model configurable via env var or settings
                    model=os.getenv("OPENAI_MODEL", "gpt-3.5-turbo"), # Default to cheaper model
                    # model="gpt-4",
                    messages=[
                        {"role": "system", "content": os.getenv("OPENAI_SYSTEM_PROMPT", "You are a helpful assistant.")}, # Configurable system prompt
                        {"role": "user", "content": current_query}
                    ],
                    temperature=float(os.getenv("OPENAI_TEMPERATURE", 0.7)), # Configurable temperature
                    max_tokens=int(os.getenv("OPENAI_MAX_TOKENS", 150)), # Limit response length
                )
                # Extract the response content
                openai_response = completion.choices[0].message["content"].strip()
                print("Received response from OpenAI.") # Log success

            # Handle specific OpenAI errors
            except openai.error.AuthenticationError as auth_err:
                 error_message = f"OpenAI Authentication Error: {auth_err}. Check your API key."
                 print(f"ERROR: {error_message}")
                 flash(error_message, "danger")
                 traceback.print_exc()
            except openai.error.RateLimitError as rate_err:
                 error_message = f"OpenAI Rate Limit Exceeded: {rate_err}. Please try again later or check your plan."
                 print(f"ERROR: {error_message}")
                 flash(error_message, "warning")
            except openai.error.OpenAIError as api_err: # Catch other OpenAI specific errors
                 error_message = f"OpenAI API Error: {api_err}"
                 print(f"ERROR: {error_message}")
                 flash(error_message, "danger")
                 traceback.print_exc()
            # Handle general exceptions
            except Exception as ex:
                error_message = f"An unexpected error occurred while contacting OpenAI: {ex}"
                print(f"ERROR: {error_message}")
                flash(error_message, "danger")
                traceback.print_exc()

    # --- Render Template (GET or after POST) ---
    return render_template(
        "ask.html",
        response=openai_response, # Pass the OpenAI response (or None)
        error=error_message, # Pass any error message
        current_query=current_query, # Pass the user's query back for display
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Static Gallery (All Uploads in /static/uploads - Legacy/Debug?)
#   NOTE: This route directly lists files from the 'static/uploads' folder.
#   It does NOT use the database `local_media_paths`. Use with caution.
#   Consider if '/gallery' (Combined DB Gallery) or '/galleries' (Overview)
#   are better suited for primary use.
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery_static")
def gallery_static():
    error_message = None
    image_paths = [] # List to hold relative paths for the template
    # Define the target folder using app config for consistency
    static_upload_folder = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, "uploads"))

    # Ensure the folder exists before trying to list its contents
    if not os.path.isdir(static_upload_folder):
         # Log a warning if the folder is missing
         print(f"Warning: Static upload folder not found at: {static_upload_folder}")
         # Optionally create it: os.makedirs(static_upload_folder, exist_ok=True)
         flash(f"Static upload folder ('{os.path.basename(static_upload_folder)}') not found.", "warning")
         # Return template with empty list and error
         return render_template(
            "gallery.html", # Reusing the generic gallery template
            image_items=[], # Pass empty list
            gallery_title="Static Uploads Gallery (Folder Not Found)",
            error=f"Folder not found: {static_upload_folder}",
            current_year=datetime.utcnow().year,
         )

    try:
        # List all files in the specified upload folder
        for filename in os.listdir(static_upload_folder):
            # Construct the full path to check if it's a file
            full_path = os.path.join(static_upload_folder, filename)
            if os.path.isfile(full_path):
                 # Check if the file extension is an allowed image type
                 if os.path.splitext(filename)[1].lower() in {
                     ".jpg", ".png", ".gif", ".jpeg", ".webp", ".heic", ".avif", ".svg" # Added common image types
                 }:
                     # Create the relative path for use in `url_for('static', filename=...)`
                     # Assumes UPLOAD_FOLDER is directly inside static_folder
                     relative_path = os.path.join(os.path.basename(static_upload_folder), filename)
                     # Store as a simple dict for consistency if gallery.html expects items
                     image_paths.append({"path": relative_path, "message_id": None, "index": None})
                     # Or just the path if gallery.html handles strings:
                     # image_paths.append(relative_path)

    except FileNotFoundError:
        # This case should be handled by the isdir check above, but keep as fallback
        error_message = f"Static upload folder not found: {static_upload_folder}"
        flash(error_message, "warning")
    except Exception as ex:
        # Handle any other errors during file listing
        error_message = f"Error loading static gallery: {ex}"
        flash(error_message, "danger")
        traceback.print_exc()

    # Render the gallery template
    return render_template(
        "gallery.html", # Reusing gallery.html template
        # Pass image_paths as image_items if gallery.html expects dicts
        image_items=image_paths,
        # Or pass directly if gallery.html expects a list of path strings:
        # image_files=image_paths,
        gallery_title="Static Uploads Gallery", # Provide a title
        error=error_message,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Unsorted Images Gallery (Media from DB messages without assigned property)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/unsorted")
def unsorted_gallery():
    unsorted_items_list = [] # List to hold dicts {message, path, index}
    properties_list = [] # List for the property assignment dropdown
    error_message = None
    # upload_dir = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, 'uploads')) # Not needed if using static_folder directly


    try:
        # 1️⃣ Query messages that have no property assigned AND have local media paths stored
        msgs_with_unsorted_media = (
            Message.query
            .filter(Message.property_id.is_(None)) # Filter for messages where property_id IS NULL
            .filter(Message.local_media_paths.isnot(None)) # Filter for messages where local_media_paths is NOT NULL
            .filter(Message.local_media_paths != '') # Filter out messages where the path string is empty
            .order_by(Message.timestamp.desc()) # Order by newest first
            .all()
        )

        # 2️⃣ Process each message to extract individual media paths
        for msg in msgs_with_unsorted_media:
            # Split the comma-separated string, strip whitespace, and filter out empty strings
            paths = [p.strip() for p in (msg.local_media_paths or "").split(",") if p.strip()]
            for idx, relative_path in enumerate(paths): # relative_path is like "uploads/filename.jpg"
                 # *** REVISED CHECK ***
                 # Construct the full absolute path using the app's static folder and the relative path from DB
                 full_check_path = os.path.join(current_app.static_folder, relative_path)

                 if os.path.isfile(full_check_path):
                     # If the file exists, add its info to the list for the template
                     unsorted_items_list.append({
                         "message": msg, # Pass the whole message object for context
                         "path": relative_path, # Relative path for URL generation in template
                         "index": idx # Index needed for the delete URL
                     })
                 else:
                     # Log a warning if a path exists in DB but not on disk using the checked path
                     print(f"Warning (Unsorted Gallery): File path '{relative_path}' listed in DB for message {msg.id} not found at checked path '{full_check_path}'")

        # 3️⃣ Fetch the list of properties for the assignment dropdown
        properties_list = Property.query.order_by(Property.name).all()

    except Exception as ex:
        # Handle errors during database query or file checking
        db.session.rollback()
        traceback.print_exc()
        error_message = f"Error loading unsorted media gallery: {ex}"
        flash(error_message, "danger")

    # Render the specific template for the unsorted gallery
    return render_template(
        "unsorted.html",
        unsorted_items=unsorted_items_list, # Pass the list of media items
        properties=properties_list, # Pass the list of properties for dropdown
        error=error_message, # Pass any error message
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Per-Property Gallery (Media associated with a specific property)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    # Fetch the property object or return 404 if the ID is invalid
    prop = Property.query.get_or_404(property_id)
    image_items_list = [] # List to store dicts: {path, message_id, index}
    error_message = None
    # upload_dir = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, 'uploads')) # Not needed if using static_folder directly

    try:
        # 1️⃣ Query messages associated with this specific property_id
        # Also ensure they have non-empty local_media_paths
        msgs_for_property = (
            Message.query
            .filter_by(property_id=property_id) # Filter by the given property ID
            .filter(Message.local_media_paths.isnot(None)) # Must have media paths
            .filter(Message.local_media_paths != '') # Paths string must not be empty
            .order_by(Message.timestamp.desc()) # Show newest first
            .all()
        )

        # 2️⃣ Extract valid image paths and associate with message/index
        for msg in msgs_for_property:
            # Split paths, strip whitespace, remove empty strings
            paths = [p.strip() for p in (msg.local_media_paths or "").split(",") if p.strip()]
            for idx, relative_path in enumerate(paths): # relative_path is like "uploads/filename.jpg"
                 # *** REVISED CHECK ***
                 # Construct the full absolute path using the app's static folder and the relative path from DB
                 full_check_path = os.path.join(current_app.static_folder, relative_path)

                 if os.path.isfile(full_check_path):
                     # Add dict to list if file exists
                     image_items_list.append({
                         "path": relative_path, # Path for image source URL in template
                         "message_id": msg.id, # ID of the message this image belongs to
                         "index": idx # Index of this image within the message's media list
                     })
                 else:
                      # Log warning if DB path doesn't match a file using the checked path
                      print(f"Warning (Property Gallery {property_id}): File path '{relative_path}' in message {msg.id} not found at checked path '{full_check_path}'.")

    except Exception as ex:
         # Handle errors during DB query or file checking
         traceback.print_exc()
         error_message = f"Error loading gallery for property '{prop.name}': {ex}"
         flash(error_message, "danger")

    # Render the generic gallery template, passing the filtered image items
    return render_template(
        "gallery.html", # Use the existing generic gallery template
        image_items=image_items_list, # Pass the list of dicts {path, message_id, index}
        property=prop, # Pass the property object for context (e.g., title)
        gallery_title=f"Gallery for {prop.name}", # Dynamic title for the page
        error=error_message,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   “All Galleries” Overview (Summary of each property's gallery)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/galleries")
def galleries_overview():
    gallery_summaries_list = []
    error_message = None
    # upload_dir = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, 'uploads')) # Not needed if using static_folder directly

    try:
        # Get all properties, ordered by name
        properties = Property.query.order_by(Property.name).all()
        for prop in properties:
            # --- Efficiently count media messages and get one thumbnail ---

            # 1. Count messages with non-empty local_media_paths for this property
            # This avoids loading all message objects just for counting
            media_message_count = db.session.query(func.count(Message.id)).filter(
                 Message.property_id == prop.id,
                 Message.local_media_paths.isnot(None),
                 Message.local_media_paths != ''
             ).scalar() or 0 # Default to 0 if count is None

            # 2. Get a thumbnail path (if any media exists)
            thumbnail_relative_path = None
            if media_message_count > 0:
                # Find the most recent message associated with this property that has media
                latest_message_with_media = Message.query.filter(
                    Message.property_id == prop.id,
                    Message.local_media_paths.isnot(None),
                    Message.local_media_paths != ''
                ).order_by(Message.timestamp.desc()).first() # Get only the latest one

                # If a message is found, try to get the first valid media path from it
                if latest_message_with_media and latest_message_with_media.local_media_paths:
                    # Split paths, strip whitespace, remove empty strings
                    potential_thumbs = [p.strip() for p in latest_message_with_media.local_media_paths.split(',') if p.strip()]
                    # Iterate through paths and use the first one that corresponds to an existing file
                    for thumb_path in potential_thumbs: # thumb_path is like "uploads/filename.jpg"
                         # *** REVISED CHECK ***
                         # Construct the full absolute path using the app's static folder and the relative path from DB
                         full_check_path = os.path.join(current_app.static_folder, thumb_path)
                         if os.path.isfile(full_check_path):
                              thumbnail_relative_path = thumb_path
                              break # Stop after finding the first valid thumbnail

            # Append summary data for this property to the list
            gallery_summaries_list.append({
                "property": prop, # The property object itself
                "count": media_message_count, # Count of messages with media
                "thumb": thumbnail_relative_path, # Relative path to thumbnail (or None)
            })

    except Exception as ex:
         # Handle errors during property query or thumbnail fetching
         traceback.print_exc()
         error_message = f"Error loading galleries overview: {ex}"
         flash(error_message, "danger")

    # --- Calculate count of unsorted images separately ---
    # This involves checking files for all unsorted messages, could be optimized if needed
    unsorted_image_count = 0
    try:
         # Query messages with no property and non-empty media paths
         unsorted_msgs_query = Message.query.filter(
             Message.property_id.is_(None),
             Message.local_media_paths.isnot(None),
             Message.local_media_paths != ''
         ).all()
         # Iterate and count existing files
         for msg in unsorted_msgs_query:
              paths = [p.strip() for p in (msg.local_media_paths or "").split(",") if p.strip()]
              for p in paths: # p is like "uploads/filename.jpg"
                  # *** REVISED CHECK ***
                  # Construct the full absolute path using the app's static folder and the relative path from DB
                  full_check_path = os.path.join(current_app.static_folder, p)
                  # Increment count only if the file exists on disk
                  if os.path.isfile(full_check_path):
                      unsorted_image_count += 1
    except Exception as ex:
        # Log warning if counting unsorted images fails, but don't block the page
        print(f"Warning: Could not accurately count unsorted images: {ex}")
        # Optionally flash a less severe message:
        # flash("Could not accurately count unsorted images.", "info")

    # Render the overview template
    return render_template(
        "galleries_overview.html",
        gallery_summaries=gallery_summaries_list, # Pass the list of property summaries
        unsorted_count=unsorted_image_count, # Pass the count of unsorted items
        error=error_message, # Pass any critical error message
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Combined Uploads Gallery (All Media from All Messages in DB)
#   NOTE: This route queries *all* messages with media. Can be resource-intensive.
#   Consider adding pagination if performance becomes an issue.
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery", endpoint="gallery_view") # Explicit endpoint name for clarity
def gallery_view():
    all_image_items_list = [] # List of dicts {path, message_id, index, property_name, timestamp}
    error_message = None
    upload_dir = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, 'uploads')) # Get upload dir path

    try:
        # *** DEBUG: List directory contents at the start of the request ***
        print(f"--- [Gallery View /gallery] ---")
        try:
            print(f"DEBUG: Checking upload directory: {upload_dir}")
            current_files = os.listdir(upload_dir)
            print(f"DEBUG: Files currently in upload dir ({len(current_files)}): {current_files}")
        except Exception as list_err:
            print(f"DEBUG: Error listing upload directory: {list_err}")
        # *** END DEBUG ***


        # 1️⃣ Get all messages that have non-empty media paths, eager load property info
        all_msgs_with_media = (
            Message.query
            .options(db.joinedload(Message.property)) # Eager load property to avoid N+1 queries
            .filter(Message.local_media_paths.isnot(None))
            .filter(Message.local_media_paths != '')
            .order_by(Message.timestamp.desc()) # Order by newest first
            # TODO: Add pagination here for large datasets
            # .paginate(page=request.args.get('page', 1, type=int), per_page=50)
            .all() # Remove .all() if using pagination
        )
        print(f"DEBUG: Found {len(all_msgs_with_media)} messages with media paths in DB.")

        # 2️⃣ Extract all valid paths from these messages
        for msg in all_msgs_with_media: # Adjust loop if using pagination (iterate over items)
            paths = [p.strip() for p in (msg.local_media_paths or "").split(",") if p.strip()]
            for idx, relative_path in enumerate(paths): # relative_path is like "uploads/filename.jpg"
                 # *** REVISED CHECK ***
                 # Construct the full absolute path using the app's static folder and the relative path from DB
                 full_check_path = os.path.join(current_app.static_folder, relative_path)

                 # *** DEBUG: Print check path and result ***
                 print(f"   DEBUG: Checking for msg {msg.id}, path '{relative_path}' at '{full_check_path}'...")
                 file_exists = os.path.isfile(full_check_path)
                 print(f"   DEBUG: os.path.isfile result: {file_exists}")
                 # *** END DEBUG ***

                 if file_exists:
                     # Add dict with details to the list
                     all_image_items_list.append({
                         "path": relative_path, # Relative path for URL generation in template
                         "message_id": msg.id,
                         "index": idx,
                         "property_name": msg.property.name if msg.property else "Unsorted", # Show property name or 'Unsorted'
                         "timestamp": msg.timestamp # Include timestamp for potential display/sorting in template
                     })
                 else:
                     # Log warning for missing files using the checked path
                     # This log should match the previous warnings if the check is still failing
                     print(f"   Warning (Combined Gallery): File path '{relative_path}' in message {msg.id} not found at checked path '{full_check_path}'.")


    except Exception as ex:
         # Handle errors during query or file checking
         traceback.print_exc()
         error_message = f"Error loading combined media gallery: {ex}"
         flash(error_message, "danger")

    print(f"DEBUG: Added {len(all_image_items_list)} items to be rendered.")
    print(f"--- [End Gallery View /gallery] ---")
    # Render the generic gallery template
    return render_template(
        "gallery.html", # Use the existing generic gallery template
        image_items=all_image_items_list, # Pass the list of all image items
        gallery_title="All Media Gallery (from Database)", # Set appropriate title
        # Optionally pass property=None if the template requires it
        property=None,
        error=error_message,
        current_year=datetime.utcnow().year,
        # If pagination was added, pass pagination object:
        # pagination=all_msgs_with_media
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Health check endpoint
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ping")
def ping_route():
    # Simple "Pong" response indicates the web server is running
    # Optional: Add a quick DB check for a more comprehensive health check
    try:
        db.session.execute(text("SELECT 1"))
        return "Pong! DB OK.", 200
    except Exception as e:
        # Return 503 Service Unavailable if DB connection fails
        print(f"DB Health Check Failed: {e}")
        return f"Pong! DB Error: {e}", 503
    # return "Pong!", 200 # Original simple response


# ──────────────────────────────────────────────────────────────────────────────
#   DEBUG: List Uploaded Files Endpoint
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/list-uploads")
def list_uploads():
    """Debug route to list files currently present in the upload directory."""
    upload_dir = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, 'uploads'))
    print(f"ℹ️ [list-uploads] Checking directory: {upload_dir}")
    try:
        if not os.path.isdir(upload_dir):
             print(f"⚠️ [list-uploads] Upload directory not found.")
             return jsonify({"error": "Upload directory not found.", "path": upload_dir}), 404

        files = os.listdir(upload_dir)
        print(f"✅ [list-uploads] Found {len(files)} items (raw): {files}")
        # Optionally, add file sizes or modification times
        file_details = []
        for f in files:
            try:
                full_path = os.path.join(upload_dir, f)
                if os.path.isfile(full_path): # List only files
                     stat_result = os.stat(full_path)
                     file_details.append({
                         "name": f,
                         "size_bytes": stat_result.st_size,
                         "modified_utc": datetime.utcfromtimestamp(stat_result.st_mtime).isoformat() + "Z"
                     })
                # else: # Optionally log directories found
                #      print(f"ℹ️ [list-uploads] Found directory, skipping: {f}")
            except Exception as stat_err:
                 print(f"⚠️ [list-uploads] Error stating file {f}: {stat_err}")
                 file_details.append({"name": f, "error": str(stat_err)})


        return jsonify({
            "directory": upload_dir,
            "file_count": len(file_details),
            "files": file_details
        })
    except Exception as e:
        print(f"❌ [list-uploads] Error listing directory: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Failed to list uploads: {e}", "path": upload_dir}), 500


# ──────────────────────────────────────────────────────────────────────────────
#   Debug URL map (Prints registered routes at startup)
# ──────────────────────────────────────────────────────────────────────────────
# Use a function to avoid re-running this logic on every import/reload in dev
def print_url_map(app_instance):
     # Ensure this runs within an application context
     with app_instance.app_context():
         print("\n--- URL MAP ---")
         # Sort rules by endpoint name for consistent order
         rules = sorted(app_instance.url_map.iter_rules(), key=lambda r: r.endpoint)
         for rule in rules:
             # Format methods nicely, excluding common HEAD/OPTIONS
             methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
             # Print endpoint, methods, and the URL rule
             print(f"{rule.endpoint:30} {methods:<15} {rule.rule}")
         print("--- END URL MAP ---\n")

# Print the map once when the script is initially run
# This helps verify routes are registered as expected
print_url_map(app)


# ──────────────────────────────────────────────────────────────────────────────
#   Conflicting Route Definition Removed
# ──────────────────────────────────────────────────────────────────────────────
# The duplicate @app.route('/delete_media', ...) definition that caused the
# initial startup error was removed in the previous correction.
# The specific route `/delete-media/<int:message_id>/<int:file_index>` handles
# media deletion now.


# ──────────────────────────────────────────────────────────────────────────────
#   Entry Point for Development Server (if script is run directly)
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # This block executes only when the script is run directly (e.g., `python main.py`)
    # It's common practice for running the Flask development server.
    # NOTE: Gunicorn or another production-ready WSGI server (like Waitress on Windows)
    # should be used for deployment, not the Flask development server.

    print("🚀 Starting Flask development server...")
    # Get host and port from environment variables, with defaults
    host = os.environ.get("FLASK_RUN_HOST", "0.0.0.0") # 0.0.0.0 makes it accessible on network
    port = int(os.environ.get("FLASK_RUN_PORT", 8080)) # Default port 8080
    # Get debug flag from environment variable (important: should be 'False' in production)
    # The string 'true' (case-insensitive) enables debug mode.
    debug_mode = os.environ.get("FLASK_DEBUG", "True").lower() in ['true', '1', 't']

    # Run the Flask development server
    app.run(host=host, port=port, debug=debug_mode)
