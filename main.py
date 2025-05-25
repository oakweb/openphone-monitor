import os
import logging
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from sqlalchemy import text, func, select
from sqlalchemy.orm import joinedload, aliased

load_dotenv()

# Import local modules
from extensions import db
from models import Contact, Message, Property, Tenant, NotificationHistory
from webhook_route import webhook_bp

app = Flask(__name__)

# Logging Setup
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
app.logger.setLevel(logging.DEBUG)

# Flask App Configuration
INSTANCE = Path(app.instance_path)
INSTANCE.mkdir(exist_ok=True)
DB_FILE = INSTANCE / "messages.db"

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", f"sqlite:///{DB_FILE}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["FLASK_SECRET"] = os.getenv("FLASK_SECRET", "dev_secret_key")
app.secret_key = app.config["FLASK_SECRET"]
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Initialize Extensions
db.init_app(app)

# Initialize Database
with app.app_context():
    app.logger.info("🔄 Initializing Database...")
    try:
        db.create_all()
        app.logger.info("✅ Tables created/verified.")
        
        # Try to add sid column if it doesn't exist
        try:
            db.session.execute(text("ALTER TABLE messages ADD COLUMN sid VARCHAR"))
            db.session.commit()
            app.logger.info("✅ Added messages.sid column.")
        except Exception as alter_err:
            db.session.rollback()
            if "already exists" in str(alter_err).lower() or "duplicate column" in str(alter_err).lower():
                app.logger.info("✅ messages.sid column already exists.")
            else:
                app.logger.warning(f"⚠️ Could not add 'sid' column: {alter_err}")
        
        # Add enhanced Property fields if they don't exist (for production upgrade)
        enhanced_columns = [
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS address TEXT",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS hoa_name VARCHAR(200)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS hoa_phone VARCHAR(15)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS hoa_email VARCHAR(200)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS hoa_website VARCHAR(200)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS neighbor_name VARCHAR(200)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS neighbor_phone VARCHAR(15)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS neighbor_email VARCHAR(200)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS neighbor_notes TEXT",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS year_purchased INTEGER",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS purchase_amount NUMERIC(12,2)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS redfin_current_value NUMERIC(12,2)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS monthly_rent NUMERIC(10,2)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS property_taxes NUMERIC(10,2)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS bedrooms INTEGER",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS bathrooms NUMERIC(3,1)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS square_feet INTEGER",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS lot_size VARCHAR(50)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS notes TEXT",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS maintenance_notes TEXT",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS tenant_notes TEXT",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS access_notes TEXT",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS lockbox_code VARCHAR(20)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS garage_code VARCHAR(20)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS wifi_network VARCHAR(100)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS wifi_password VARCHAR(100)",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"
        ]
        
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
            for sql in enhanced_columns:
                try:
                    db.session.execute(text(sql))
                    app.logger.info(f"✅ {sql}")
                except Exception as e:
                    app.logger.warning(f"⚠️ {sql} - {e}")
            
            try:
                db.session.commit()
                app.logger.info("✅ Enhanced Property fields added to production database.")
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"❌ Error adding enhanced fields: {e}")
        
        app.logger.info("✅ Database initialization complete.")
    except Exception as e:
        db.session.rollback()
        app.logger.critical(f"❌ Database initialization error: {e}")

# Add this to your database initialization section in main.py, after the enhanced Property fields:

        # Add message tracking fields for tenant communications
        message_tracking_columns = [
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_type VARCHAR(50) DEFAULT 'sms'",  # 'sms', 'email', 'notification'
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS sent_to_tenant BOOLEAN DEFAULT FALSE",      # Track if sent to tenant
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS notification_id INTEGER",                 # Link to NotificationHistory
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivery_status VARCHAR(20) DEFAULT 'sent'" # 'sent', 'delivered', 'failed'
        ]
        
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
            for sql in message_tracking_columns:
                try:
                    db.session.execute(text(sql))
                    app.logger.info(f"✅ {sql}")
                except Exception as e:
                    app.logger.warning(f"⚠️ {sql} - {e}")
            
            try:
                db.session.commit()
                app.logger.info("✅ Message tracking fields added to production database.")
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"❌ Error adding message tracking fields: {e}")

# Define all routes first, then print URL map at the end

@app.context_processor
def inject_now():
    return {"current_year": datetime.now(timezone.utc).year}

@app.route("/")
def index():
    """Displays the main dashboard."""
    db_status = "?"
    summary_today = "?"
    summary_week = "?"
    error_message = None

    try:
        # Basic database check
        db.session.execute(text("SELECT 1"))
        db_status = "Connected"
        
        # Count messages
        now_utc = datetime.now(timezone.utc)
        start_today_utc = datetime.combine(now_utc.date(), datetime.min.time(), tzinfo=timezone.utc)
        start_week_utc = start_today_utc - timedelta(days=start_today_utc.weekday())
        
        count_today = db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_today_utc).scalar() or 0
        count_week = db.session.query(func.count(Message.id)).filter(Message.timestamp >= start_week_utc).scalar() or 0
        
        summary_today = f"{count_today} messages today."
        summary_week = f"{count_week} messages this week."
        
    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error loading index page: {ex}")
        error_message = f"Error loading page data: {ex}"
        if db_status != "Connected":
            db_status = f"Error: {ex}"

    return render_template("index.html",
                         db_status=db_status,
                         summary_today=summary_today,
                         summary_week=summary_week,
                         ai_summaries=[],
                         error=error_message)


@app.route("/messages")
def messages_view():
    """Displays message overview or detail for a specific number."""
    target_phone_number = request.args.get("phone_number")
    
    try:
        if target_phone_number:
            # Detail view for specific phone number
            msgs_for_number = Message.query.options(
                joinedload(Message.property),
                joinedload(Message.contact)
            ).filter(
                Message.phone_number == target_phone_number
            ).order_by(Message.timestamp.desc()).all()
            
            # Determine contact info
            is_known = False
            contact_name = None
            if msgs_for_number and msgs_for_number[0].contact:
                contact = msgs_for_number[0].contact
                is_known = True
                contact_name = contact.contact_name if contact.contact_name else contact.phone_number
            
            if not is_known:
                contact = db.session.get(Contact, target_phone_number)
                if contact:
                    is_known = True
                    contact_name = contact.contact_name if contact.contact_name else contact.phone_number
            
            return render_template("messages_detail.html", 
                                 phone_number=target_phone_number, 
                                 messages=msgs_for_number, 
                                 is_known=is_known, 
                                 contact_name=contact_name)
        else:
            # Overview of all messages
            msgs_overview = (
                Message.query.options(
                    joinedload(Message.property), 
                    joinedload(Message.contact)
                )
                .order_by(Message.timestamp.desc())
                .limit(100)
                .all()
            )
            properties_list = Property.query.order_by(Property.name).all()
            return render_template("messages_overview.html", 
                                 messages=msgs_overview, 
                                 properties=properties_list)
                                 
    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error in messages_view: {ex}")
        flash(f"Error: {ex}", "danger")
        return redirect(url_for('index'))

@app.route('/properties')
def properties_list_view():
    """Displays a list of all properties."""
    try:
        properties = Property.query.order_by(Property.name).all()
        return render_template('properties_list.html', properties=properties)
    except Exception as e:
        app.logger.error(f"Error loading properties: {e}")
        flash(f"Error loading properties: {e}", "danger")
        return redirect(url_for('index'))

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
        
        return render_template('property_detail.html', 
                             property=property_obj,
                             recent_messages=recent_messages)
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error loading property {property_id}: {e}")
        flash(f"Error loading property details: {e}", "danger")
        return redirect(url_for("properties_list_view"))

@app.route("/contacts")
def contacts_view():
    """Display contacts page."""
    try:
        contacts = Contact.query.order_by(Contact.contact_name).all()
        return render_template('contacts.html', contacts=contacts)
    except Exception as e:
        app.logger.error(f"Error loading contacts: {e}")
        flash(f"Error loading contacts: {e}", "danger")
        return redirect(url_for('index'))

@app.route("/galleries")
def galleries_overview():
    """Display galleries overview."""
    try:
        # Get properties that have media
        property_ids_with_media = (
            db.session.query(Message.property_id)
            .filter(
                Message.property_id.isnot(None), 
                Message.local_media_paths.isnot(None)
            )
            .distinct()
            .all()
        )
        prop_ids = [pid for (pid,) in property_ids_with_media]
        
        properties_with_galleries = []
        if prop_ids:
            properties_with_galleries = (
                Property.query.filter(Property.id.in_(prop_ids))
                .order_by(Property.name)
                .all()
            )
        
        # Count unsorted media
        unsorted_count = db.session.query(func.count(Message.id)).filter(
            Message.property_id.is_(None), 
            Message.local_media_paths.isnot(None)
        ).scalar() or 0
        
        return render_template("galleries_overview.html",
                             gallery_summaries=properties_with_galleries,
                             unsorted_count=unsorted_count)
    except Exception as e:
        app.logger.error(f"Error loading galleries: {e}")
        flash(f"Error loading galleries: {e}", "danger")
        return redirect(url_for('index'))

@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    """Display Ask AI page."""
    try:
        return render_template('ask.html')
    except Exception as e:
        app.logger.error(f"Error loading ask page: {e}")
        flash(f"Error loading ask page: {e}", "danger")
        return redirect(url_for('index'))

@app.route("/notifications")
def notifications_view():
    """Display notifications page."""
    try:
        return render_template('notifications.html', properties=[], history=[])
    except Exception as e:
        app.logger.error(f"Error loading notifications: {e}")
        flash(f"Error loading notifications: {e}", "danger")
        return redirect(url_for('index'))

@app.route("/assign_property", methods=["POST"])
def assign_property():
    """Assign property to message."""
    try:
        # Basic implementation - just redirect back
        flash("Property assignment functionality coming soon.", "info")
        return redirect(request.referrer or url_for('index'))
    except Exception as e:
        app.logger.error(f"Error in assign_property: {e}")
        flash(f"Error: {e}", "danger")
        return redirect(url_for('index'))

@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    """Display gallery for specific property."""
    try:
        prop = db.session.get(Property, property_id)
        if not prop:
            flash("Property not found.", "warning")
            return redirect(url_for("galleries_overview"))
        
        return render_template("gallery.html", 
                             image_items=[], 
                             property=prop, 
                             gallery_title=f"Gallery for {prop.name}")
    except Exception as e:
        app.logger.error(f"Error loading gallery for property {property_id}: {e}")
        flash(f"Error loading gallery: {e}", "danger")
        return redirect(url_for("galleries_overview"))
    
# Add these routes to your main.py after the existing property routes

@app.route('/property/<int:property_id>/edit', methods=['GET', 'POST'])
def property_edit_view(property_id):
    """Edit property details."""
    try:
        property_obj = db.session.get(Property, property_id)
        if not property_obj:
            flash(f"Property with ID {property_id} not found.", "warning")
            return redirect(url_for("properties_list_view"))
        
        if request.method == 'POST':
            # Update property with form data
            property_obj.name = request.form.get('name', property_obj.name)
            property_obj.address = request.form.get('address', property_obj.address)
            
            # HOA Information
            property_obj.hoa_name = request.form.get('hoa_name') or None
            property_obj.hoa_phone = request.form.get('hoa_phone') or None
            property_obj.hoa_email = request.form.get('hoa_email') or None
            property_obj.hoa_website = request.form.get('hoa_website') or None
            
            # Neighbor Information
            property_obj.neighbor_name = request.form.get('neighbor_name') or None
            property_obj.neighbor_phone = request.form.get('neighbor_phone') or None
            property_obj.neighbor_email = request.form.get('neighbor_email') or None
            property_obj.neighbor_notes = request.form.get('neighbor_notes') or None
            
            # Financial Information
            year_purchased = request.form.get('year_purchased')
            if year_purchased and year_purchased.isdigit():
                property_obj.year_purchased = int(year_purchased)
            
            purchase_amount = request.form.get('purchase_amount')
            if purchase_amount:
                try:
                    property_obj.purchase_amount = float(purchase_amount.replace(',', ''))
                except ValueError:
                    pass
            
            redfin_value = request.form.get('redfin_current_value')
            if redfin_value:
                try:
                    property_obj.redfin_current_value = float(redfin_value.replace(',', ''))
                except ValueError:
                    pass
            
            monthly_rent = request.form.get('monthly_rent')
            if monthly_rent:
                try:
                    property_obj.monthly_rent = float(monthly_rent.replace(',', ''))
                except ValueError:
                    pass
            
            property_taxes = request.form.get('property_taxes')
            if property_taxes:
                try:
                    property_obj.property_taxes = float(property_taxes.replace(',', ''))
                except ValueError:
                    pass
            
            # Property Details
            bedrooms = request.form.get('bedrooms')
            if bedrooms and bedrooms.isdigit():
                property_obj.bedrooms = int(bedrooms)
            
            bathrooms = request.form.get('bathrooms')
            if bathrooms:
                try:
                    property_obj.bathrooms = float(bathrooms)
                except ValueError:
                    pass
            
            square_feet = request.form.get('square_feet')
            if square_feet and square_feet.isdigit():
                property_obj.square_feet = int(square_feet)
            
            property_obj.lot_size = request.form.get('lot_size') or None
            
            # Access Information
            property_obj.lockbox_code = request.form.get('lockbox_code') or None
            property_obj.garage_code = request.form.get('garage_code') or None
            property_obj.wifi_network = request.form.get('wifi_network') or None
            property_obj.wifi_password = request.form.get('wifi_password') or None
            
            # Notes
            property_obj.notes = request.form.get('notes') or None
            property_obj.maintenance_notes = request.form.get('maintenance_notes') or None
            property_obj.tenant_notes = request.form.get('tenant_notes') or None
            property_obj.access_notes = request.form.get('access_notes') or None
            
            # Update timestamp
            property_obj.updated_at = datetime.now(timezone.utc)
            
            try:
                db.session.commit()
                flash(f"Property '{property_obj.name}' updated successfully!", "success")
                return redirect(url_for('property_detail_view', property_id=property_id))
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Error updating property {property_id}: {e}")
                flash(f"Error updating property: {e}", "danger")
        
        return render_template('property_edit.html', property=property_obj)
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error in property_edit_view {property_id}: {e}")
        flash(f"Error loading property edit page: {e}", "danger")
        return redirect(url_for("properties_list_view"))

@app.route("/ping")
def ping_route():
    try:
        db.session.execute(text("SELECT 1"))
        return "Pong! DB OK.", 200
    except Exception as e:
        app.logger.error(f"DB Ping Failed: {e}")
        return f"Pong! DB Error: {e}", 503

# Register webhook blueprint
app.register_blueprint(webhook_bp)

# Print URL Map after all routes are defined
with app.app_context():
    app.logger.info("\n--- URL MAP ---")
    rules = sorted(app.url_map.iter_rules(), key=lambda r: r.endpoint)
    for rule in rules:
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        app.logger.info(f"{rule.endpoint:30} {methods:<15} {rule.rule}")
    app.logger.info("--- END URL MAP ---\n")

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8080))
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() in ["true", "1", "t"]
    app.run(host=host, port=port, debug=debug_mode)