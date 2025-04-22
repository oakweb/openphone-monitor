import os
import re
import json
from flask import Flask, render_template, request, jsonify, url_for
from dotenv import load_dotenv
from extensions import db
from models import Contact, Message
from webhook_route import webhook_bp
from sqlalchemy import text, func
from datetime import datetime, timedelta
import openai

# Load environment variables
load_dotenv()

# Initialize Flask appp
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
# Auto-reload templates during development
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Ensure SQLite instance folder existss
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:') and 'instance' in app.config['SQLALCHEMY_DATABASE_URI']:
    os.makedirs(os.path.join(app.root_path, 'instance'), exist_ok=True)

# Initialize database and register blueprint
db.init_app(app)
app.register_blueprint(webhook_bp)

# Configure OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Create tables on startup
with app.app_context():
    db.create_all()

# --- Index Route ---
@app.route('/')
def index():
    db_status = 'Unknown'
    summary_today = summary_week = 'Unavailable'
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
        current_year=datetime.utcnow().year
    )

# --- Messages Route ---
@app.route('/messages')
def messages_view():
    error = None
    try:
        msgs = ( Message.query
                 .order_by(Message.timestamp.desc())
                 .limit(50)
                 .all() )
        # Normalize attachments and classify inline URLs
        for m in msgs:
            try:
                m.media_urls = json.loads(m.media_urls) if m.media_urls else []
            except Exception:
                m.media_urls = []
            inline_urls = re.findall(r'https?://\S+', m.message or '')
            image_exts = ('.png', '.jpg', '.jpeg', '.gif')
            inline_images = [u for u in inline_urls if u.lower().endswith(image_exts)]
            inline_links  = [u for u in inline_urls if not u.lower().endswith(image_exts)]
            m.image_urls  = inline_images + m.media_urls
            m.other_links = inline_links

        # JSON API path
        if request.args.get('format') == 'json':
            now = datetime.utcnow()
            start_today = datetime.combine(now.date(), datetime.min.time())
            start_week = start_today - timedelta(days=now.weekday())
            messages_list = [
                {
                    'id':          m.id,
                    'timestamp':   m.timestamp.isoformat() + 'Z',
                    'phone_number':m.phone_number,
                    'contact_name':m.contact_name,
                    'direction':   m.direction,
                    'message':     m.message,
                    'media_urls':  m.media_urls,
                    'image_urls':  m.image_urls,
                    'other_links': m.other_links,
                    'is_read':     True,
                } for m in msgs
            ]
            stats = {
                'messages_today': db.session.query(func.count(Message.id))
                                        .filter(Message.timestamp >= start_today).scalar(),
                'messages_week':  db.session.query(func.count(Message.id))
                                        .filter(Message.timestamp >= start_week).scalar(),
                'unread_messages': 0,
                'response_rate':   93,
                'summary_today':   f"{db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_today).scalar()} message(s) today."
            }
            return jsonify(messages=messages_list, stats=stats)

        # HTML rendering path
        count_today = ( db.session.query(func.count(Message.id))
                       .filter(Message.timestamp >= datetime.combine(datetime.utcnow().date(), datetime.min.time()))
                       .scalar() )
        count_week  = ( db.session.query(func.count(Message.id))
                       .filter(Message.timestamp >= datetime.combine(datetime.utcnow().date(), datetime.min.time()) - timedelta(days=datetime.utcnow().weekday()))
                       .scalar() )
        return render_template(
            'messages.html',
            messages=msgs,
            messages_today=count_today,
            messages_week=count_week,
            summary_today=f"{count_today} message(s) today.",
            error=None
        )
    except Exception as e:
        db.session.rollback()
        error = str(e)
        if request.args.get('format') == 'json':
            return jsonify(error=error), 500
        return render_template(
            'messages.html',
            messages=[],
            messages_today=0,
            messages_week=0,
            summary_today='Error loading summary.',
            error=error
        ), 500

# --- Contacts Route (Shows EXISTING contacts & handles DELETE) ---
# Make sure redirect, url_for, request, render_template, db, Contact are imported
@app.route('/contacts', methods=['GET', 'POST'])
def contacts_view():
    error = None
    known_contacts = [] # Initialize

    # Handle POST requests (Deleting a contact)
    if request.method == 'POST':
        action = request.form.get('action')
        phone_to_delete = request.form.get('phone')

        if action == 'delete' and phone_to_delete:
            try:
                contact_to_delete = Contact.query.get(phone_to_delete)
                if contact_to_delete:
                    db.session.delete(contact_to_delete)
                    db.session.commit()
                    print(f"✅ Deleted contact: {contact_to_delete.contact_name} ({phone_to_delete})")
                    # Optional: Add flash message for success
                else:
                    print(f"⚠️ Attempted to delete non-existent contact: {phone_to_delete}")
                    error = f"Contact {phone_to_delete} not found."
                # Redirect back to the contacts page after POST
                return redirect(url_for('contacts_view'))
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error processing delete request: {e}")
                error = f"Error processing delete: {e}"
                # Fall through to render page with error

        else:
            # Handle other POST actions if any, or invalid POST
            error = "Invalid action specified."
            # Fall through to render page with error

    # --- Handle GET requests (or fall through from POST error) ---
    try:
        # Query all existing contacts, ordered by name
        known_contacts = Contact.query.order_by(Contact.contact_name).all()
        print(f"--- DEBUG: Fetched {len(known_contacts)} known contacts for display ---")

        # --- Optional: Fetch Recent Unknown Numbers ---
        # (You can add the logic here from the previous version if you still
        # want to display a secondary list of recent unknown numbers below
        # the main contact list on the page. For simplicity, we'll omit it for now.)
        recent_calls = [] # Set to empty list if not implementing secondary list

    except Exception as e:
        db.session.rollback()
        error = f"Error fetching contacts: {e}"
        print(f"❌ Error in /contacts GET: {e}")
        # Set known_contacts to empty on error
        known_contacts = []
        recent_calls = []

    # Pass the list of known contacts to the template
    return render_template('contacts.html',
                           known_contacts=known_contacts,
                           recent_calls=recent_calls, # Pass empty or calculated list
                           error=error)