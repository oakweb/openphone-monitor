import os
import json
from pathlib import Path
from datetime import datetime, timedelta
import traceback

# Import necessary Flask components, including 'flash'
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    url_for,
    redirect,
    flash, # Added flash import
)
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import text, func
import openai


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
# Make sure FLASK_SECRET is set in your environment variables for production
app.secret_key = os.getenv("FLASK_SECRET", "a_strong_default_secret_key_for_dev")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

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
            db.create_all()
            print("✅ Tables created/verified.")

            # 2) Ensure the `sid` column exists on messages (SQLite/Postgres compatible)
            # Using try-except for broader compatibility
            try:
                db.session.execute(text("ALTER TABLE messages ADD COLUMN sid VARCHAR"))
                db.session.commit()
                print("✅ Ensured messages.sid column exists (added if missing).")
            except Exception as alter_err:
                db.session.rollback()
                # Ignore error if column already exists (common case)
                if "already exists" not in str(alter_err).lower():
                     print(f"⚠️  Could not add 'sid' column (may already exist): {alter_err}")
                else:
                    print("✅ messages.sid column already exists.")


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
                        # Get the maximum current ID
                        max_id_query = text("SELECT COALESCE(MAX(id), 0) FROM messages")
                        max_id = db.session.execute(max_id_query).scalar()
                        # Set the sequence value to max_id + 1
                        reset_seq_query = text(f"SELECT setval('{sequence_name}', :max_id + 1, false)")
                        db.session.execute(reset_seq_query, {'max_id': max_id})
                        db.session.commit()
                        print(f"🔁 messages.id sequence ('{sequence_name}') reset.")
                    else:
                        print("⚠️ Could not determine sequence name for messages.id.")

                except Exception as seq_err:
                    db.session.rollback()
                    print(f"❌ Error resetting PostgreSQL sequence: {seq_err}")
            else:
                print("ℹ️ Skipping sequence reset (not PostgreSQL).")


            print(f"👉 Properties in DB: {Property.query.count()}")
            print("✅ Database initialization complete.")

        except Exception as e:
            print(f"❌ FATAL STARTUP ERROR: {e}")
            traceback.print_exc()

# Call initialization within app context
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
        # Check DB connection
        db.session.execute(text("SELECT 1"))
        db_status = "Connected"

        # Calculate stats
        now = datetime.utcnow()
        start_today = datetime.combine(now.date(), datetime.min.time())
        start_week  = start_today - timedelta(days=now.weekday()) # Monday as start

        count_today = (
            db.session.query(func.count(Message.id))
            .filter(Message.timestamp >= start_today)
            .scalar() or 0 # Ensure it's 0 if None
        )
        count_week = (
            db.session.query(func.count(Message.id))
            .filter(Message.timestamp >= start_week)
            .scalar() or 0 # Ensure it's 0 if None
        )

        summary_today = f"{count_today} message(s) today."
        summary_week  = f"{count_week} message(s) this week."

    except Exception as ex:
        db.session.rollback() # Rollback in case of error during query
        db_status     = f"Error: {ex}"
        print(f"❌ DB Error on index: {ex}") # Log the error
        # Keep summaries as "Unavailable"

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
# This is the primary delete route associated with messages
@app.route('/delete-media/<int:message_id>/<int:file_index>', methods=['POST'])
def delete_media_for_message(message_id, file_index): # Renamed function
    message = Message.query.get_or_404(message_id)
    redirect_url = request.referrer or url_for('galleries_overview') # Default redirect

    if not message.local_media_paths:
        flash("No media associated with this message to delete.", "warning")
        return redirect(redirect_url)

    # Split paths, handling potential whitespace
    media_paths = [p.strip() for p in message.local_media_paths.split(',') if p.strip()]

    if 0 <= file_index < len(media_paths):
        relative_path_to_delete = media_paths[file_index]
        # Construct full path relative to the static folder
        full_file_path = os.path.join(app.static_folder, relative_path_to_delete)
        # Sanity check: prevent deleting outside static folder (basic check)
        if not os.path.abspath(full_file_path).startswith(os.path.abspath(app.static_folder)):
             flash("Invalid file path specified.", "danger")
             return redirect(redirect_url)


        if os.path.exists(full_file_path):
            try:
                os.remove(full_file_path)
                flash(f"Successfully deleted media file: {os.path.basename(relative_path_to_delete)}.", "success")

                # Remove the path from the list and update the database
                media_paths.pop(file_index)
                message.local_media_paths = ','.join(media_paths) if media_paths else None
                db.session.commit()

            except OSError as e:
                flash(f"Error deleting file from disk: {e}", "danger")
                # Don't commit DB change if file deletion failed
                db.session.rollback()
            except Exception as e:
                 flash(f"An unexpected error occurred during deletion: {e}", "danger")
                 db.session.rollback()
        else:
            flash(f"Media file not found on disk: {os.path.basename(relative_path_to_delete)}. Updating database record.", "warning")
            # File doesn't exist, but remove it from the DB record anyway
            media_paths.pop(file_index)
            message.local_media_paths = ','.join(media_paths) if media_paths else None
            db.session.commit() # Commit DB change even if file was already gone

    else:
        flash("Invalid media index provided.", "error")

    # Redirect back, potentially to a specific gallery or overview
    # Consider redirecting to the specific property gallery if available
    if message.property_id:
        redirect_url = url_for('gallery_for_property', property_id=message.property_id)
    return redirect(redirect_url)


# ──────────────────────────────────────────────────────────────────────────────
#   Messages & Assignment
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/messages")
def messages_view():
    current_year = datetime.utcnow().year
    try:
        msgs = (
            Message.query
            .options(db.joinedload(Message.property)) # Eager load property
            .order_by(Message.timestamp.desc())
            .limit(100) # Increased limit slightly
            .all()
        )

        # JSON endpoint support
        if request.args.get("format") == "json":
            now = datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week  = start_today - timedelta(days=now.weekday())

            # *** FIX: Calculate counts here for the JSON response ***
            count_today_json = (
                db.session.query(func.count(Message.id))
                .filter(Message.timestamp >= start_today)
                .scalar() or 0
            )
            count_week_json = (
                db.session.query(func.count(Message.id))
                .filter(Message.timestamp >= start_week)
                .scalar() or 0
            )

            data = [{
                "id": m.id,
                "timestamp": m.timestamp.isoformat() + "Z", # Standard ISO format
                "phone_number": m.phone_number,
                "contact_name": m.contact_name,
                "direction": m.direction,
                "message": m.message,
                "media_urls": m.media_urls, # Original URLs from Twilio etc.
                "local_media_paths": m.local_media_paths, # Paths to saved files
                "property_id": m.property_id,
                "property_name": m.property.name if m.property else None,
            } for m in msgs]

            stats = {
                "messages_today": count_today_json, # Use calculated value
                "messages_week": count_week_json,   # Use calculated value
                "summary_today": f"{count_today_json} message(s) today.",
                "summary_week": f"{count_week_json} message(s) this week.",
            }
            return jsonify({"messages": data, "stats": stats})

        # For HTML view: Get contacts and properties for dropdowns/linking
        phones = db.session.query(Contact.phone_number).distinct().all()
        known  = {p for (p,) in phones} # Set of known phone numbers
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
        flash(f"Error loading messages: {ex}", "danger") # Flash error to user
        return render_template(
            "messages.html",
            messages=[],
            properties=[], # Pass empty lists to avoid template errors
            known_contact_phones=set(),
            error=str(ex), # Keep error for potential display
            current_year=current_year,
        )


@app.route("/assign_property", methods=["POST"])
def assign_property():
    message_id = request.form.get("message_id")
    property_id_str = request.form.get("property_id", "") # Get as string

    if not message_id:
         flash("No message ID provided.", "error")
         return redirect(request.referrer or url_for("messages_view"))

    try:
        message = Message.query.get(int(message_id))
        if not message:
            flash(f"Message with ID {message_id} not found.", "error")
            return redirect(request.referrer or url_for("messages_view"))

        # Handle assignment or unassignment
        if property_id_str and property_id_str != "None": # Check if a valid property ID is selected
             property_id = int(property_id_str)
             # Optional: Verify property exists
             prop = Property.query.get(property_id)
             if not prop:
                  flash(f"Property with ID {property_id} not found.", "error")
                  return redirect(request.referrer or url_for("messages_view"))
             message.property_id = property_id
             flash(f"Message assigned to property: {prop.name}", "success")
        else: # Unassign if 'None' or empty string is selected
             message.property_id = None
             flash("Message unassigned from property.", "info")

        db.session.commit()

    except ValueError:
         flash("Invalid Message or Property ID format.", "error")
         db.session.rollback()
    except Exception as e:
         flash(f"Error assigning property: {e}", "danger")
         db.session.rollback()
         traceback.print_exc()

    # Redirect back to the message, either in messages list or unsorted gallery
    redirect_url = url_for("messages_view") + f"#msg-{message_id}"
    # If the referrer was the unsorted page, go back there
    if request.referrer and 'unsorted' in request.referrer:
        redirect_url = url_for('unsorted_gallery')

    return redirect(redirect_url)


# ──────────────────────────────────────────────────────────────────────────────
#   Contacts
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/contacts", methods=["GET", "POST"])
def contacts_view():
    current_year = datetime.utcnow().year
    error = None # Keep error for potential display in template

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add":
                name = request.form.get("name", "").strip()
                phone = request.form.get("phone", "").strip()
                if name and phone:
                    # Basic validation/cleaning could go here (e.g., phone format)
                    existing = Contact.query.filter_by(phone_number=phone).first()
                    if existing:
                         flash(f"Contact with phone number {phone} already exists ({existing.contact_name}).", "warning")
                    else:
                        new_contact = Contact(contact_name=name, phone_number=phone)
                        db.session.add(new_contact)
                        db.session.commit()
                        flash(f"Contact '{name}' added successfully.", "success")
                else:
                    flash("Both name and phone number are required.", "error")

            elif action == "delete":
                contact_id = request.form.get("contact_id")
                contact_to_delete = Contact.query.get(contact_id)
                if contact_to_delete:
                    db.session.delete(contact_to_delete)
                    db.session.commit()
                    flash(f"Contact '{contact_to_delete.contact_name}' deleted.", "success")
                else:
                    flash("Contact not found for deletion.", "error")

            return redirect(url_for("contacts_view")) # Redirect after POST

        except Exception as ex:
             db.session.rollback()
             traceback.print_exc()
             flash(f"Error processing contact action: {ex}", "danger")
             error = str(ex) # Set error for display on reload


    # GET request processing
    known_contacts = []
    recent_calls = [] # Placeholder for recent call logic if needed
    try:
        known_contacts = Contact.query.order_by(Contact.contact_name).all()
        # Example: Build recent calls list (e.g., unique numbers from last N messages)
        # recent_numbers = db.session.query(Message.phone_number)\
        #     .order_by(Message.timestamp.desc())\
        #     .distinct()\
        #     .limit(10).all()
        # recent_calls = [num for (num,) in recent_numbers]

    except Exception as ex:
        db.session.rollback()
        traceback.print_exc()
        error = str(ex)
        flash(f"Error loading contacts: {ex}", "danger")

    return render_template(
        "contacts.html",
        known_contacts=known_contacts,
        recent_calls=recent_calls, # Pass recent calls list to template
        error=error,
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Ask (OpenAI Integration)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    current_year = datetime.utcnow().year
    response = error = query = None

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        if not query:
            error = "Please enter a question."
            flash(error, "warning")
        elif not openai.api_key:
            error = "OpenAI API key is not configured. Please set the OPENAI_API_KEY environment variable."
            flash(error, "danger")
        else:
            try:
                # Consider using a more specific model or adjusting parameters if needed
                completion = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo", # Using a slightly cheaper/faster model as default
                    # model="gpt-4", # Or keep gpt-4 if preferred
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": query}
                    ],
                    temperature=0.7, # Adjust creativity/factuality
                )
                response = completion.choices[0].message["content"]
            except openai.error.AuthenticationError:
                 error = "OpenAI Authentication Error: Invalid API key or organization setup."
                 flash(error, "danger")
                 traceback.print_exc()
            except Exception as ex:
                error = f"An error occurred while contacting OpenAI: {ex}"
                flash(error, "danger")
                traceback.print_exc()

    return render_template(
        "ask.html",
        response=response,
        error=error, # Pass error for display in template
        current_query=query,
        current_year=current_year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Static Gallery (All Uploads - Legacy/Debug?)
#   This route seems redundant if local_media_paths are used consistently.
#   Consider if this is still needed or if /gallery (combined) is sufficient.
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery_static")
def gallery_static():
    error = None
    images = []
    # Ensure the uploads folder exists within the static folder
    upload_folder = os.path.join(app.static_folder, "uploads")
    if not os.path.isdir(upload_folder):
         os.makedirs(upload_folder, exist_ok=True) # Create if missing
         print(f"Created missing upload folder: {upload_folder}")


    try:
        # List files directly in the 'static/uploads' directory
        for fn in os.listdir(upload_folder):
            # Check extension and ensure it's a file
            file_path = os.path.join(upload_folder, fn)
            if os.path.isfile(file_path):
                 if os.path.splitext(fn)[1].lower() in {
                     ".jpg", ".png", ".gif", ".jpeg", ".webp", ".heic", ".avif" # Added more types
                 }:
                     # Create URL path relative to static folder root
                     images.append(f"uploads/{fn}") # Path for url_for('static', filename=...)
    except FileNotFoundError:
        error = f"Upload folder not found: {upload_folder}"
        flash(error, "warning")
    except Exception as ex:
        error = f"Error loading static gallery: {ex}"
        flash(error, "danger")
        traceback.print_exc()

    return render_template(
        "gallery.html", # Reusing gallery.html template
        image_files=images,
        gallery_title="Static Uploads Gallery", # Provide a title
        error=error,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Unsorted Images Gallery (Media from messages without assigned property)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/unsorted")
def unsorted_gallery():
    unsorted_items = []
    props = []
    error = None

    try:
        # 1️⃣ Grab every message with media but no property
        msgs_with_media = (
            Message.query
            .filter(Message.property_id.is_(None)) # No property assigned
            .filter(Message.local_media_paths.isnot(None)) # Has local media paths
            .filter(Message.local_media_paths != '') # Ensure paths string is not empty
            .order_by(Message.timestamp.desc())
            .all()
        )

        # 2️⃣ Flatten to list of {message, path, index} for easier handling
        for m in msgs_with_media:
            paths = [p.strip() for p in (m.local_media_paths or "").split(",") if p.strip()]
            for idx, p in enumerate(paths):
                 # Check if the file actually exists in the static folder
                 full_path = os.path.join(app.static_folder, p)
                 if os.path.isfile(full_path):
                     unsorted_items.append({
                         "message": m,
                         "path": p, # Relative path for URL generation
                         "index": idx # Index within the message's media list
                     })
                 # else:
                 #     print(f"Warning: File path '{p}' listed in message {m.id} not found at '{full_path}'")


        # 3️⃣ Properties list for dropdown assignment
        props = Property.query.order_by(Property.name).all()

    except Exception as ex:
        db.session.rollback()
        traceback.print_exc()
        error = f"Error loading unsorted gallery: {ex}"
        flash(error, "danger")


    return render_template(
        "unsorted.html", # Specific template for unsorted items
        unsorted_items=unsorted_items,
        properties=props,
        error=error,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Per-Property Gallery
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    prop = Property.query.get_or_404(property_id)
    image_items = [] # Store dicts: {path, message_id, index}
    error = None

    try:
        # Get messages for this property that have media
        msgs = (
            Message.query
            .filter_by(property_id=property_id)
            .filter(Message.local_media_paths.isnot(None))
            .filter(Message.local_media_paths != '')
            .order_by(Message.timestamp.desc()) # Show newest first
            .all()
        )

        # Extract valid image paths and associate with message/index
        for m in msgs:
            paths = [p.strip() for p in (m.local_media_paths or "").split(",") if p.strip()]
            for idx, p in enumerate(paths):
                 full_path = os.path.join(app.static_folder, p)
                 if os.path.isfile(full_path):
                     image_items.append({
                         "path": p,
                         "message_id": m.id,
                         "index": idx
                     })
                 # else:
                 #      print(f"Warning: File path '{p}' in property gallery {property_id} (msg {m.id}) not found.")

    except Exception as ex:
         traceback.print_exc()
         error = f"Error loading gallery for property {prop.name}: {ex}"
         flash(error, "danger")


    return render_template(
        "gallery_property.html", # Use a potentially different template for property galleries
        image_items=image_items, # Pass the list of dicts
        property=prop,
        gallery_title=f"Gallery for {prop.name}", # Dynamic title
        error=error,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   “All Galleries” Overview
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/galleries")
def galleries_overview():
    gallery_summaries = []
    error = None
    try:
        properties = Property.query.order_by(Property.name).all()
        for prop in properties:
            # Efficiently count and get one thumbnail per property
            # This avoids loading all messages per property upfront

            # Count messages with non-empty local_media_paths for this property
            media_message_count = db.session.query(func.count(Message.id)).filter(
                 Message.property_id == prop.id,
                 Message.local_media_paths.isnot(None),
                 Message.local_media_paths != ''
             ).scalar() or 0


            thumbnail_path = None
            if media_message_count > 0:
                # Find the most recent message with media for a thumbnail
                latest_message_with_media = Message.query.filter(
                    Message.property_id == prop.id,
                    Message.local_media_paths.isnot(None),
                    Message.local_media_paths != ''
                ).order_by(Message.timestamp.desc()).first()

                if latest_message_with_media and latest_message_with_media.local_media_paths:
                    # Get the first valid path from the latest message
                    potential_thumbs = [p.strip() for p in latest_message_with_media.local_media_paths.split(',') if p.strip()]
                    for thumb in potential_thumbs:
                         if os.path.isfile(os.path.join(app.static_folder, thumb)):
                              thumbnail_path = thumb
                              break # Use the first valid one found


            gallery_summaries.append({
                "property": prop,
                "count": media_message_count, # Count of messages with media
                "thumb": thumbnail_path, # Path relative to static folder
            })

    except Exception as ex:
         traceback.print_exc()
         error = f"Error loading galleries overview: {ex}"
         flash(error, "danger")


    # Calculate count of unsorted images separately
    unsorted_count = 0
    try:
         unsorted_msgs = Message.query.filter(
             Message.property_id.is_(None),
             Message.local_media_paths.isnot(None),
             Message.local_media_paths != ''
         ).all()
         for m in unsorted_msgs:
              paths = [p.strip() for p in (m.local_media_paths or "").split(",") if p.strip()]
              for p in paths:
                  if os.path.isfile(os.path.join(app.static_folder, p)):
                      unsorted_count += 1
    except Exception as ex:
        print(f"Warning: Could not count unsorted images accurately: {ex}")


    return render_template(
        "galleries_overview.html",
        gallery_summaries=gallery_summaries,
        unsorted_count=unsorted_count, # Pass the count of unsorted items
        error=error,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Combined Uploads Gallery (All Media from All Messages)
#   This route might be very resource-intensive if there are many messages/media.
#   Consider pagination or if '/galleries' overview is sufficient.
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/gallery", endpoint="gallery_view") # Explicit endpoint name
def gallery_view():
    all_image_items = [] # List of dicts {path, message_id, index, property_name}
    error = None

    try:
        # Get all messages that have media, ordered by time
        all_msgs_with_media = (
            Message.query
            .options(db.joinedload(Message.property)) # Eager load property info
            .filter(Message.local_media_paths.isnot(None))
            .filter(Message.local_media_paths != '')
            .order_by(Message.timestamp.desc())
            .all()
        )

        # Extract all valid paths
        for m in all_msgs_with_media:
            paths = [p.strip() for p in (m.local_media_paths or "").split(",") if p.strip()]
            for idx, p in enumerate(paths):
                 full_path = os.path.join(app.static_folder, p)
                 if os.path.isfile(full_path):
                     all_image_items.append({
                         "path": p,
                         "message_id": m.id,
                         "index": idx,
                         "property_name": m.property.name if m.property else "Unsorted",
                         "timestamp": m.timestamp # For potential sorting/display
                     })

    except Exception as ex:
         traceback.print_exc()
         error = f"Error loading combined gallery: {ex}"
         flash(error, "danger")


    return render_template(
        "gallery_combined.html", # Use a specific template if layout differs
        image_items=all_image_items,
        gallery_title="All Media Gallery",
        error=error,
        current_year=datetime.utcnow().year,
    )


# ──────────────────────────────────────────────────────────────────────────────
#   Health check
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/ping")
def ping_route():
    # Could add a quick DB check here too if desired
    # try:
    #     db.session.execute(text("SELECT 1"))
    #     return "Pong! DB OK.", 200
    # except Exception as e:
    #     return f"Pong! DB Error: {e}", 500
    return "Pong!", 200


# ──────────────────────────────────────────────────────────────────────────────
#   Debug URL map (Runs only once at startup)
# ──────────────────────────────────────────────────────────────────────────────
# Use a function to avoid re-running this logic on every import/reload in dev
def print_url_map(app_instance):
     with app_instance.app_context():
         print("\n--- URL MAP ---")
         rules = sorted(app_instance.url_map.iter_rules(), key=lambda r: r.endpoint)
         for rule in rules:
             # Exclude HEAD, OPTIONS unless explicitly needed
             methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
             print(f"{rule.endpoint:30} {methods:<15} {rule.rule}")
         print("--- END URL MAP ---\n")

# Print the map once when the script is initially run
print_url_map(app)


# ──────────────────────────────────────────────────────────────────────────────
#   Conflicting Route Removed
# ──────────────────────────────────────────────────────────────────────────────
# *** REMOVED the duplicate @app.route('/delete_media', ...) definition ***
# The following route conflicted with delete_media_for_message and caused the startup error.
# It seemed less specific and potentially redundant. If you need functionality
# to delete arbitrary files from static based on path, a new, distinct route
# and function name should be created with proper security checks.

# @app.route('/delete_media', methods=['POST'])
# def delete_media():
#     file_path = request.form.get('file_path')
#     # ... (rest of the old implementation) ...
#     return redirect(request.referrer or url_for('gallery_view'))


# ──────────────────────────────────────────────────────────────────────────────
#   Entry Point for Development Server (Optional)
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Note: Gunicorn or another WSGI server is recommended for production
    # Use flask run command or this block for local development
    print("🚀 Starting Flask development server...")
    # Host 0.0.0.0 makes it accessible on the network
    # Debug=True enables auto-reloading and debugger (DISABLE IN PRODUCTION)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
