import os
import logging
import json
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, send_file, session
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from sqlalchemy import text, func, select, inspect
from sqlalchemy.orm import joinedload, aliased
from werkzeug.utils import secure_filename

load_dotenv()

# Import local modules
from extensions import db
from models import Contact, Message, Property, Tenant, NotificationHistory, PropertyCustomField, PropertyAttachment, PropertyContact, Vendor, VendorJob, VendorInvoiceData, VendorComment
from webhook_route import webhook_bp

app = Flask(__name__)

# Set secret key for sessions
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

# Logging Setup
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
app.logger.setLevel(logging.DEBUG)
app.logger.info("App starting - Version with vendor management fix")

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

# Configure upload folder for media files
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", os.path.join(app.instance_path, "uploads"))
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.logger.info(f"✅ Upload folder configured: {UPLOAD_FOLDER}")

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
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
            "ALTER TABLE properties ADD COLUMN IF NOT EXISTS thumbnail_path TEXT"
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
        
        # Add vendor enhancement columns
        vendor_columns = [
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS can_text BOOLEAN DEFAULT TRUE",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS can_email BOOLEAN DEFAULT TRUE",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS example_invoice_path VARCHAR(500)",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS fax_number VARCHAR(20)",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS phone VARCHAR(20)",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS address VARCHAR(200)",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS city VARCHAR(100)",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS state VARCHAR(50)",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS zip_code VARCHAR(20)",
            "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS aka_business_name VARCHAR(200)"
        ]
        
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
            for sql in vendor_columns:
                try:
                    db.session.execute(text(sql))
                    app.logger.info(f"✅ {sql}")
                except Exception as e:
                    app.logger.warning(f"⚠️ {sql} - {e}")
            
            try:
                db.session.commit()
                app.logger.info("✅ Vendor enhancement fields added to production database.")
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"❌ Error adding vendor enhancement fields: {e}")
        
        # Create VendorInvoiceData table if it doesn't exist
        try:
            db.create_all()  # This will create any missing tables
            app.logger.info("✅ All database tables verified/created.")
        except Exception as e:
            app.logger.error(f"❌ Error creating tables: {e}")
        
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
        
        # Add vendor migrations for production
        try:
            # Add aka_business_name column if it doesn't exist
            db.session.execute(text("ALTER TABLE vendors ADD COLUMN aka_business_name VARCHAR(200)"))
            db.session.commit()
            app.logger.info("✅ Added vendors.aka_business_name column.")
        except Exception as e:
            db.session.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                app.logger.info("✅ vendors.aka_business_name column already exists.")
            else:
                app.logger.warning(f"⚠️ Could not add 'aka_business_name' column: {e}")
                
        # Create vendor_comments table if it doesn't exist
        try:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS vendor_comments (
                    id SERIAL PRIMARY KEY,
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    comment TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    created_by VARCHAR(100)
                )
            """))
            db.session.commit()
            app.logger.info("✅ Created vendor_comments table.")
        except Exception as e:
            db.session.rollback()
            if "already exists" in str(e).lower():
                app.logger.info("✅ vendor_comments table already exists.")
            else:
                app.logger.warning(f"⚠️ Could not create vendor_comments table: {e}")
            
        app.logger.info("✅ Database initialization complete.")
    except Exception as e:
        db.session.rollback()
        app.logger.critical(f"❌ Database initialization error: {e}")

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

@app.route("/vendors")
def vendors_list():
    """Display list of vendors with filtering"""
    status_filter = request.args.get('status', 'active')  # active, inactive, all
    vendor_type = request.args.get('type', '')
    
    try:
        # Base query - removed joinedload for jobs since it's a dynamic relationship
        query = Vendor.query.options(
            joinedload(Vendor.contact)
        )
        
        # Apply status filter
        if status_filter != 'all':
            query = query.filter_by(status=status_filter)
        
        # Apply type filter
        if vendor_type:
            query = query.filter_by(vendor_type=vendor_type)
        
        # Get all vendors
        vendors = query.order_by(Vendor.company_name).all()
        
        # Get vendor types for filter dropdown
        vendor_types = db.session.query(Vendor.vendor_type).distinct().filter(
            Vendor.vendor_type.isnot(None)
        ).order_by(Vendor.vendor_type).all()
        vendor_types = [vt[0] for vt in vendor_types]
        
        # Calculate statistics
        active_count = Vendor.query.filter_by(status='active').count()
        inactive_count = Vendor.query.filter_by(status='inactive').count()
        
        return render_template('vendors_list.html',
                             vendors=vendors,
                             vendor_types=vendor_types,
                             status_filter=status_filter,
                             vendor_type_filter=vendor_type,
                             active_count=active_count,
                             inactive_count=inactive_count)
    
    except Exception as e:
        app.logger.error(f"Error in vendors_list: {e}")
        flash(f"Error loading vendors: {e}", "danger")
        return redirect(url_for('index'))


@app.route("/vendor/<int:vendor_id>")
def vendor_detail(vendor_id):
    """Display vendor profile with job history"""
    try:
        vendor = Vendor.query.options(
            joinedload(Vendor.contact)
        ).get_or_404(vendor_id)
        
        # Get job statistics
        jobs_by_property = db.session.query(
            Property.name,
            Property.id,
            func.count(VendorJob.id).label('job_count'),
            func.sum(VendorJob.cost).label('total_cost'),
            func.max(VendorJob.job_date).label('last_job')
        ).join(
            VendorJob, VendorJob.property_id == Property.id
        ).filter(
            VendorJob.vendor_id == vendor_id
        ).group_by(Property.id, Property.name).all()
        
        # Get recent messages
        recent_messages = Message.query.filter_by(
            phone_number=vendor.contact_id
        ).order_by(Message.timestamp.desc()).limit(10).all()
        
        return render_template('vendor_detail.html',
                             vendor=vendor,
                             jobs_by_property=jobs_by_property,
                             recent_messages=recent_messages)
    
    except Exception as e:
        app.logger.error(f"Error in vendor_detail: {e}")
        flash(f"Error loading vendor: {e}", "danger")
        return redirect(url_for('vendors_list'))


@app.route("/vendor/create", methods=["GET", "POST"])
def vendor_create():
    """Create a new vendor"""
    if request.method == "POST":
        try:
            # Get form data
            contact_type = request.form.get('contact_type', 'new')
            
            # Get phone number based on contact type
            if contact_type == 'existing':
                phone_number = request.form.get('selected_phone_number', '').strip()
            else:
                phone_number = request.form.get('phone_number', '').strip()
            
            contact_name = request.form.get('contact_name', '').strip()
            company_name = request.form.get('company_name', '').strip()
            vendor_type = request.form.get('vendor_type', '').strip()
            email = request.form.get('email', '').strip()
            hourly_rate = request.form.get('hourly_rate', type=float)
            
            # Get communication preferences
            can_text = request.form.get('can_text') == 'true'
            can_email = request.form.get('can_email') == 'true'
            
            # Validate phone number
            if not phone_number:
                flash("Phone number is required", "danger")
                return redirect(url_for('vendor_create'))
            
            # Check if vendor already exists
            existing_vendor = Vendor.query.filter_by(contact_id=phone_number).first()
            if existing_vendor:
                flash("A vendor with this phone number already exists", "warning")
                return redirect(url_for('vendor_detail', vendor_id=existing_vendor.id))
            
            # Create or update contact
            contact = Contact.query.filter_by(phone_number=phone_number).first()
            if not contact:
                contact = Contact(
                    phone_number=phone_number,
                    contact_name=contact_name or company_name
                )
                db.session.add(contact)
            elif contact_name and not contact.contact_name:
                contact.contact_name = contact_name
            
            # Handle file upload for example invoice
            example_invoice_path = None
            if 'example_invoice' in request.files:
                file = request.files['example_invoice']
                if file and file.filename:
                    from werkzeug.utils import secure_filename
                    import uuid
                    
                    # Generate unique filename
                    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'pdf'
                    filename = f"vendor_invoice_{uuid.uuid4().hex[:8]}.{ext}"
                    
                    # Save file
                    upload_folder = app.config.get('UPLOAD_FOLDER', '/app/static/uploads')
                    os.makedirs(upload_folder, exist_ok=True)
                    filepath = os.path.join(upload_folder, filename)
                    file.save(filepath)
                    example_invoice_path = f"uploads/{filename}"
                    
                    # TODO: Extract information from invoice using AI
            
            # Create vendor
            vendor = Vendor(
                contact_id=phone_number,
                company_name=company_name,
                aka_business_name=request.form.get('aka_business_name', '').strip(),
                vendor_type=vendor_type,
                email=email,
                hourly_rate=hourly_rate,
                status='active',
                notes=request.form.get('notes', ''),
                can_text=can_text,
                can_email=can_email,
                example_invoice_path=example_invoice_path,
                license_number=request.form.get('license_number', '').strip(),
                tax_id=request.form.get('tax_id', '').strip(),
                phone=request.form.get('phone', '').strip(),
                fax_number=request.form.get('fax_number', '').strip(),
                address=request.form.get('address', '').strip(),
                city=request.form.get('city', '').strip(),
                state=request.form.get('state', '').strip(),
                zip_code=request.form.get('zip_code', '').strip()
            )
            
            db.session.add(vendor)
            db.session.commit()
            
            flash(f"Vendor '{company_name or contact_name}' created successfully", "success")
            return redirect(url_for('vendor_detail', vendor_id=vendor.id))
            
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error creating vendor: {e}")
            flash(f"Error creating vendor: {e}", "danger")
    
    # GET request - show form
    # Get pre-filled data from query parameters
    prefilled_phone = request.args.get('phone', '').strip()
    prefilled_name = request.args.get('name', '').strip()
    
    # Get contacts that aren't already vendors
    existing_vendor_phones = [v.contact_id for v in Vendor.query.all()]
    available_contacts = Contact.query.filter(
        ~Contact.phone_number.in_(existing_vendor_phones)
    ).order_by(Contact.contact_name).all()
    
    return render_template('vendor_create.html', 
                         available_contacts=available_contacts,
                         prefilled_phone=prefilled_phone,
                         prefilled_name=prefilled_name)


@app.route("/vendor/<int:vendor_id>/edit", methods=["GET", "POST"])
def vendor_edit(vendor_id):
    """Edit vendor information"""
    vendor = Vendor.query.get_or_404(vendor_id)
    
    if request.method == "POST":
        try:
            # Update vendor fields
            vendor.company_name = request.form.get('company_name', '').strip()
            vendor.aka_business_name = request.form.get('aka_business_name', '').strip()
            vendor.vendor_type = request.form.get('vendor_type', '').strip()
            vendor.email = request.form.get('email', '').strip()
            vendor.license_number = request.form.get('license_number', '').strip()
            vendor.insurance_info = request.form.get('insurance_info', '').strip()
            vendor.hourly_rate = request.form.get('hourly_rate', type=float)
            vendor.status = request.form.get('status', 'active')
            vendor.preferred_payment_method = request.form.get('preferred_payment_method', '').strip()
            vendor.notes = request.form.get('notes', '')
            vendor.tax_id = request.form.get('tax_id', '').strip()
            vendor.phone = request.form.get('phone', '').strip()
            vendor.fax_number = request.form.get('fax_number', '').strip()
            vendor.address = request.form.get('address', '').strip()
            vendor.city = request.form.get('city', '').strip()
            vendor.state = request.form.get('state', '').strip()
            vendor.zip_code = request.form.get('zip_code', '').strip()
            
            # Update communication preferences
            vendor.can_text = request.form.get('can_text') == 'true'
            vendor.can_email = request.form.get('can_email') == 'true'
            
            # Handle file upload for example invoice
            if 'example_invoice' in request.files:
                file = request.files['example_invoice']
                if file and file.filename:
                    from werkzeug.utils import secure_filename
                    import uuid
                    
                    # Generate unique filename
                    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'pdf'
                    filename = f"vendor_invoice_{vendor.id}_{uuid.uuid4().hex[:8]}.{ext}"
                    
                    # Save file
                    upload_folder = app.config.get('UPLOAD_FOLDER', '/app/static/uploads')
                    os.makedirs(upload_folder, exist_ok=True)
                    filepath = os.path.join(upload_folder, filename)
                    file.save(filepath)
                    vendor.example_invoice_path = f"uploads/{filename}"
                    
                    # TODO: Extract information from invoice using AI
            
            # Update contact name if provided
            contact_name = request.form.get('contact_name', '').strip()
            if contact_name and vendor.contact:
                vendor.contact.contact_name = contact_name
            
            db.session.commit()
            flash("Vendor updated successfully", "success")
            return redirect(url_for('vendor_detail', vendor_id=vendor_id))
            
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error updating vendor: {e}")
            flash(f"Error updating vendor: {e}", "danger")
    
    return render_template('vendor_edit.html', vendor=vendor)


@app.route("/vendor/<int:vendor_id>/process-invoice", methods=["POST"])
def process_vendor_invoice(vendor_id):
    """Process vendor invoice with AI to extract information"""
    vendor = Vendor.query.get_or_404(vendor_id)
    
    if not vendor.example_invoice_path:
        return jsonify({"error": "No invoice uploaded for this vendor"}), 400
    
    try:
        # Get the invoice file path
        upload_folder = app.config.get('UPLOAD_FOLDER', '/app/static/uploads')
        invoice_path = os.path.join(upload_folder, vendor.example_invoice_path.replace('uploads/', ''))
        
        # Check if file exists
        if not os.path.exists(invoice_path):
            return jsonify({"error": "Invoice file not found"}), 404
        
        # Process the invoice based on file type
        extracted_text = ""
        file_ext = invoice_path.lower().rsplit('.', 1)[1] if '.' in invoice_path else ''
        
        if file_ext == 'pdf':
            # Extract text from PDF
            import PyPDF2
            with open(invoice_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                for page in pdf_reader.pages:
                    extracted_text += page.extract_text() + "\n"
        else:
            # For images, we'll need OCR (placeholder for now)
            extracted_text = "[Image processing not yet implemented]"
        
        # Use OpenAI to extract structured data
        from openai import OpenAI
        
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return jsonify({"error": "OpenAI API key not configured"}), 500
            
        client = OpenAI(api_key=api_key)
        
        # Hardcoded list of YOUR property addresses to ALWAYS exclude
        BLOCKED_ADDRESSES = [
            # Primary variations of problem addresses
            "4365 n campbell rd",
            "4365 n campbell",
            "4365 campbell",
            "4365 n. campbell",
            "4365 north campbell",
            "4365 north campbell road",
            
            # All property addresses from your list
            "4321 west flamingo road",
            "704 lacey tree st",
            "900 south las vegas blvd",
            "900 s las vegas blvd",
            "42 gulf pines avenue",
            "113 woodley street",
            "113 woodley st",
            "711 bonita avenue",
            "711 bonita ave",
            "1007 francis avenue",
            "1007 francis ave",
            "1030 bracken ave",
            "1051 east oakey boulevard",
            "1051 e oakey blvd",
            "1053 creeping zinnia court",
            "1067 sweeney avenue",
            "1524 stonefield street",
            "1524 stonefield st",
            "2113 santa ynez drive",
            "2208 glen heather way",
            "2208 sunland ave",
            "3540 cactus shadow st",
            "4701 leilani ln",
            "4750 north jensen",
            "4750 n jensen",
            "4801 jay ave",
            "5108 del rey avenue",
            "5300 byron nelson court",
            "5300 byron nelson lane",
            "5491 indian cedar drive",
            "5625 auborn ave",
            "5625 west auborn avenue",
            "5625 w auborn ave",
            "6351 maratea avenue",
            "7649 sierra paseo lane",
            "8008 ducharme avenue",
            "8008 ducharme ave",
            "8516 copper knoll avenue",
            "8516 copper knoll ave",
            "10650 calico mountain ave",
            "11509 crimson rose avenue",
            "11509 crimson rose ave",
            
            # Common zip codes for your properties
            "las vegas, nv 89103",
            "las vegas, nv 89145",
            "las vegas, nv 89101",
            "las vegas, nv 89148",
            "las vegas, nv 89106",
            "las vegas, nv 89104",
            "las vegas, nv 89138",
            "las vegas, nv 89144",
            "las vegas, nv 89102",
            "las vegas, nv 89129",
            "las vegas, nv 89130",
            "las vegas, nv 89146",
            "las vegas, nv 89149",
            "las vegas, nv 89135",
            "las vegas, nv 89108",
            "las vegas, nv 89128",
            "bonita springs, fl 34134",
        ]
        
        # Also get any additional property addresses from database
        properties = Property.query.all()
        property_addresses = BLOCKED_ADDRESSES.copy()
        for prop in properties:
            if prop.address:
                property_addresses.append(prop.address.lower())
            if prop.name:
                property_addresses.append(prop.name.lower())
        
        # Create a prompt for extraction
        prompt = f"""Extract all relevant VENDOR/COMPANY information from this invoice text. 
        Return the data as a JSON object with clear field names and values.
        
        CRITICAL: These are the CUSTOMER (not vendor) - NEVER extract these:
        - Company: "Sin City Rentals" or "Sin City Rentals LLC" 
        - Email: "sincityrentalsllc@gmail.com"
        - Phone: "7028199266" or "(702)8199266" or "702-819-9266"
        - Any info under "BILL TO:", "Bill To:", "Customer:", "Bill 2:" sections
        - Any info that appears AFTER or UNDER these headers
        
        The VENDOR is the company PROVIDING the service (e.g., Victor Iron Gates, Swift Garage Doors, etc.)
        Look for vendor info in:
        - Company letterhead (usually top-left of invoice)
        - "From:" section
        - Left side of invoice (before any "Bill To" section)
        - Company logo area
        
        CRITICAL RULES FOR ADDRESS EXTRACTION:
        1. NEVER extract addresses that appear after "Service Address:", "Property:", "Location:", "Job Site:", or "Bill To:"
        2. ONLY extract addresses from:
           - Company letterhead at the top of invoice
           - "Remit to:" or "Mail payments to:" sections
           - Company footer information
           - "From:" or vendor info boxes
        
        3. CRITICAL: These addresses are PROPERTY/SERVICE addresses and MUST NEVER be extracted as vendor addresses:
        - ANY address containing: 4365, 4321, 704, 900, 42 gulf, 113, 711, 1007, 1030, 1051, 1053, 1067, 1524, 2113, 2208, 3540, 4701, 4750, 4801, 5108, 5300, 5491, 5625, 6351, 7649, 8008, 8516, 10650, 11509
        - ANY address with these street names: Campbell, Flamingo, Lacey Tree, Las Vegas Blvd, Gulf Pines, Woodley, Bonita, Francis, Bracken, Oakey, Creeping Zinnia, Sweeney, Stonefield, Santa Ynez, Glen Heather, Sunland, Cactus Shadow, Leilani, Jensen, Jay, Del Rey, Byron Nelson, Indian Cedar, Auborn, Maratea, Sierra Paseo, Ducharme, Copper Knoll, Calico Mountain, Crimson Rose
        - Any address that appears as "Service Address" or "Property Address"
        - Any address under "Job Site" or "Work Location"
        
        4. Common patterns to AVOID:
           - Service Address: [address] <- This is WHERE work was done, not vendor location
           - Bill To: Sin City Rentals [address] <- This is customer address
           - Property: [address] <- This is customer property
        
        5. Common patterns to EXTRACT:
           - [Company Name]\\n[Address] <- At top of invoice (letterhead)
           - From: [Company]\\n[Address] <- Vendor info box
           - Remit Payment to: [Address] <- Where to send payment
        
        Invoice Layout Understanding:
        - LEFT SIDE: Usually contains VENDOR info (company providing service)
        - RIGHT SIDE: Usually contains CUSTOMER info (Bill To, Sin City Rentals)
        - Focus on LEFT SIDE vendor information ONLY
        
        Required fields to extract if available:
        - name (vendor/company name - from LEFT side or top of invoice)
        - phone (vendor's phone - NOT 702-819-9266)
        - email (vendor's email - NOT sincityrentalsllc@gmail.com)
        - address (vendor's BUSINESS address - from LEFT side, NOT Bill To address)
        - city (vendor's city)
        - state (vendor's state)
        - zip_code (vendor's zip code)
        - business_license (often starts with "License #" or "Lic #")
        - tax_id (EIN or tax ID)
        - fax_number
        - insurance_info
        - payment_terms
        
        IMPORTANT: If ANY address contains "4365" in any form, DO NOT extract it. Leave all address fields empty instead.
        
        Invoice text:
        {extracted_text[:3500]}  # Reduced to make room for property list
        
        Return only valid JSON, no other text."""
        
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a data extraction assistant specializing in vendor invoices. Extract the VENDOR/COMPANY information (not customer/service location). Look for letterhead, 'From' sections, company info. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=1000
        )
        
        # Parse the extracted data
        extracted_data = json.loads(response.choices[0].message.content)
        
        # Log extracted data for debugging
        app.logger.info(f"Extracted vendor data: {extracted_data}")
        
        # Remove Sin City Rentals if it was extracted
        if 'name' in extracted_data:
            name_lower = extracted_data['name'].lower()
            if 'sin city rentals' in name_lower:
                app.logger.warning("Detected Sin City Rentals as vendor name, removing")
                extracted_data.pop('name', None)
                extracted_data.pop('company_name', None)
        
        if 'company_name' in extracted_data:
            name_lower = extracted_data['company_name'].lower()
            if 'sin city rentals' in name_lower:
                app.logger.warning("Detected Sin City Rentals as company_name, removing")
                extracted_data.pop('company_name', None)
        
        if 'email' in extracted_data:
            if extracted_data['email'].lower() == 'sincityrentalsllc@gmail.com':
                app.logger.warning("Detected Sin City Rentals email, removing")
                extracted_data.pop('email', None)
        
        if 'phone' in extracted_data:
            phone_digits = ''.join(filter(str.isdigit, extracted_data['phone']))
            if phone_digits in ['7028199266', '17028199266']:
                app.logger.warning("Detected Sin City Rentals phone, removing")
                extracted_data.pop('phone', None)
        
        # Validate extracted address isn't a property address
        if 'address' in extracted_data:
            extracted_addr_lower = extracted_data['address'].lower()
            
            # AGGRESSIVE CHECK: If address contains any blocked patterns
            blocked_patterns = ['4365', '4321 west flamingo', '704 lacey', '900 south las vegas', 
                               '42 gulf pines', '113 woodley', '711 bonita', '1007 francis',
                               '1030 bracken', '1051 east oakey', '1051 e oakey', '1053 creeping',
                               '1067 sweeney', '1524 stonefield', '2113 santa ynez', '2208 glen heather',
                               '2208 sunland', '3540 cactus', '4701 leilani', '4750 north jensen',
                               '4750 n jensen', '4801 jay', '5108 del rey', '5300 byron', '5491 indian',
                               '5625 auborn', '6351 maratea', '7649 sierra', '8008 ducharme',
                               '8516 copper', '10650 calico', '11509 crimson']
            
            for pattern in blocked_patterns:
                if pattern in extracted_addr_lower:
                    app.logger.warning(f"Detected blocked pattern '{pattern}' in address, removing: {extracted_data['address']}")
                    extracted_data.pop('address', None)
                    extracted_data.pop('city', None)
                    extracted_data.pop('state', None)
                    extracted_data.pop('zip_code', None)
                    break
            else:
                # Also check against all property addresses
                for prop_addr in property_addresses:
                    if prop_addr in extracted_addr_lower or extracted_addr_lower in prop_addr:
                        app.logger.warning(f"Detected property address in extraction, removing: {extracted_data['address']}")
                        # Remove address fields if they match a property
                        extracted_data.pop('address', None)
                        extracted_data.pop('city', None)
                        extracted_data.pop('state', None)
                        extracted_data.pop('zip_code', None)
                        break
        
        # If address was blocked, try to find alternative addresses
        if 'address' not in extracted_data or not extracted_data.get('address'):
            app.logger.info("Primary address was blocked, looking for alternative vendor addresses...")
            
            # Ask AI to find any other addresses that might be vendor addresses
            alt_prompt = f"""The invoice contains a service address that we've excluded. 
            Please look for OTHER addresses in this invoice that might be the vendor's business address.
            Look for addresses in letterhead, "From:", "Remit to:", footer, or company info sections.
            Exclude any address containing: {', '.join(blocked_patterns[:5])}
            
            Return a JSON object with 'alternative_addresses' array containing any vendor addresses found.
            Each should have: address, city, state, zip_code, location (where found)
            
            Invoice text: {extracted_text[:2000]}"""
            
            try:
                alt_response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Extract alternative vendor addresses."},
                        {"role": "user", "content": alt_prompt}
                    ],
                    temperature=0.1,
                    max_tokens=500
                )
                alt_data = json.loads(alt_response.choices[0].message.content)
                if alt_data.get('alternative_addresses'):
                    # Use the first alternative address if found
                    alt_addr = alt_data['alternative_addresses'][0]
                    extracted_data['address'] = alt_addr.get('address', '')
                    extracted_data['city'] = alt_addr.get('city', '')
                    extracted_data['state'] = alt_addr.get('state', '')
                    extracted_data['zip_code'] = alt_addr.get('zip_code', '')
                    extracted_data['address_source'] = alt_addr.get('location', 'alternative')
                    app.logger.info(f"Found alternative address: {alt_addr}")
            except Exception as e:
                app.logger.error(f"Error finding alternative addresses: {e}")
        
        # Also check city/state/zip independently for "4365" or blocked patterns
        for field in ['city', 'state', 'zip_code']:
            if field in extracted_data and extracted_data[field]:
                field_value = str(extracted_data[field]).lower()
                if '4365' in field_value:
                    app.logger.warning(f"Detected '4365' in {field}, removing all address fields")
                    extracted_data.pop('address', None)
                    extracted_data.pop('city', None)
                    extracted_data.pop('state', None)
                    extracted_data.pop('zip_code', None)
                    break
        
        # Clear any existing session data for this vendor
        session.pop(f'vendor_{vendor_id}_extracted_data', None)
        session.pop(f'vendor_{vendor_id}_mapped_data', None)
        
        # Clear existing invoice data for this vendor
        VendorInvoiceData.query.filter_by(vendor_id=vendor_id).delete()
        
        # Store extracted data in the database
        fields_updated = []
        for field_name, field_value in extracted_data.items():
            if field_value and str(field_value).strip():
                # Check if this maps to a standard vendor field
                standard_fields = {
                    'name': 'company_name',
                    'company_name': 'company_name',
                    'business_name': 'company_name',
                    'email': 'email',
                    'email_address': 'email',
                    'fax': 'fax_number',
                    'fax_number': 'fax_number',
                    'license': 'license_number',
                    'business_license': 'license_number',
                    'license_number': 'license_number',
                    'tax_id': 'tax_id',
                    'ein': 'tax_id',
                    'insurance': 'insurance_info',
                    'insurance_info': 'insurance_info',
                    'insurance_policy': 'insurance_info'
                }
                
                # Update standard fields
                standard_field = None
                for key, attr in standard_fields.items():
                    if key in field_name.lower():
                        standard_field = attr
                        break
                
                if standard_field and hasattr(vendor, standard_field):
                    current_value = getattr(vendor, standard_field)
                    if not current_value or current_value == '':
                        setattr(vendor, standard_field, str(field_value))
                        fields_updated.append(f"{standard_field}: {field_value}")
                
                # Store all extracted data
                invoice_data = VendorInvoiceData(
                    vendor_id=vendor_id,
                    field_name=field_name,
                    field_value=str(field_value),
                    confidence=0.9,  # High confidence for now
                    source=vendor.example_invoice_path
                )
                db.session.add(invoice_data)
        
        db.session.commit()
        
        # Store the extracted data in session for review
        session['extracted_invoice_data'] = extracted_data
        session['vendor_id'] = vendor_id
        
        return jsonify({
            "success": True,
            "message": f"Extracted {len(extracted_data)} fields from invoice",
            "redirect": url_for('vendor_review_invoice', vendor_id=vendor_id)
        })
        
    except json.JSONDecodeError as e:
        app.logger.error(f"JSON decode error: {e}")
        return jsonify({"error": "Failed to parse AI response"}), 500
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error processing invoice: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/vendor/<int:vendor_id>/review-invoice", methods=["GET", "POST"])
def vendor_review_invoice(vendor_id):
    """Review and confirm extracted invoice data before updating vendor"""
    vendor = Vendor.query.get_or_404(vendor_id)
    
    if request.method == "POST":
        try:
            # Update vendor with reviewed data
            vendor.company_name = request.form.get('company_name', '').strip() or vendor.company_name
            vendor.vendor_type = request.form.get('vendor_type', '').strip() or vendor.vendor_type
            vendor.email = request.form.get('email', '').strip() or vendor.email
            vendor.license_number = request.form.get('license_number', '').strip() or vendor.license_number
            vendor.tax_id = request.form.get('tax_id', '').strip() or vendor.tax_id
            vendor.fax_number = request.form.get('fax_number', '').strip() or vendor.fax_number
            vendor.phone = request.form.get('phone', '').strip() or vendor.phone
            vendor.address = request.form.get('address', '').strip() or vendor.address
            vendor.city = request.form.get('city', '').strip() or vendor.city
            vendor.state = request.form.get('state', '').strip() or vendor.state
            vendor.zip_code = request.form.get('zip_code', '').strip() or vendor.zip_code
            
            # Store additional fields in VendorInvoiceData
            additional_fields = request.form.get('additional_fields', '')
            if additional_fields:
                additional_data = json.loads(additional_fields)
                
                # Clear existing invoice data
                VendorInvoiceData.query.filter_by(vendor_id=vendor_id).delete()
                
                # Store all data including additional fields
                for field_name, field_value in additional_data.items():
                    if field_value and str(field_value).strip():
                        invoice_data = VendorInvoiceData(
                            vendor_id=vendor_id,
                            field_name=field_name,
                            field_value=str(field_value),
                            confidence=0.9,
                            source=vendor.example_invoice_path
                        )
                        db.session.add(invoice_data)
            
            db.session.commit()
            flash("Vendor information updated successfully from invoice", "success")
            return redirect(url_for('vendor_detail', vendor_id=vendor_id))
            
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error updating vendor from invoice: {e}")
            flash(f"Error updating vendor: {e}", "danger")
            return redirect(url_for('vendor_detail', vendor_id=vendor_id))
    
    # GET request - show review form
    extracted_data = session.get('extracted_invoice_data', {})
    if not extracted_data:
        flash("No extracted data found. Please process the invoice first.", "warning")
        return redirect(url_for('vendor_detail', vendor_id=vendor_id))
    
    # Map extracted fields to vendor fields
    field_mapping = {
        'name': 'company_name',
        'company_name': 'company_name',
        'business_name': 'company_name',
        'email': 'email',
        'email_address': 'email',
        'fax': 'fax_number',
        'fax_number': 'fax_number',
        'license': 'license_number',
        'business_license': 'license_number',
        'license_number': 'license_number',
        'tax_id': 'tax_id',
        'ein': 'tax_id',
        'phone': 'phone',
        'phone_number': 'phone',
        'address': 'address',
        'street_address': 'address',
        'business_address': 'address',
        'city': 'city',
        'state': 'state',
        'zip': 'zip_code',
        'zip_code': 'zip_code',
        'postal_code': 'zip_code'
    }
    
    # Prepare mapped data and additional fields
    mapped_data = {}
    additional_data = {}
    
    for key, value in extracted_data.items():
        mapped = False
        for extract_key, vendor_field in field_mapping.items():
            if extract_key in key.lower():
                mapped_data[vendor_field] = value
                mapped = True
                break
        
        if not mapped:
            additional_data[key] = value
    
    # Clear session data
    session.pop('extracted_invoice_data', None)
    
    return render_template('vendor_review_invoice.html', 
                         vendor=vendor, 
                         mapped_data=mapped_data,
                         additional_data=additional_data,
                         all_data=extracted_data)


@app.route("/contact/update", methods=["POST"])
def update_contact():
    """Update contact name for a phone number."""
    try:
        data = request.get_json()
        phone_number = data.get('phone_number')
        new_name = data.get('name', '').strip()
        
        if not phone_number:
            return jsonify({"success": False, "error": "Phone number required"}), 400
            
        if not new_name:
            return jsonify({"success": False, "error": "Contact name required"}), 400
        
        # Check if contact exists
        contact = Contact.query.filter_by(phone_number=phone_number).first()
        
        if contact:
            # Update existing contact
            old_name = contact.contact_name
            contact.contact_name = new_name
            app.logger.info(f"Updated contact {phone_number}: '{old_name}' -> '{new_name}'")
        else:
            # Create new contact
            contact = Contact(
                phone_number=phone_number,
                contact_name=new_name
            )
            db.session.add(contact)
            app.logger.info(f"Created new contact {phone_number}: '{new_name}'")
        
        db.session.commit()
        
        return jsonify({
            "success": True, 
            "message": f"Contact updated successfully",
            "contact": {
                "phone_number": phone_number,
                "name": new_name
            }
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error updating contact: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/messages")
def messages_view():
    """Displays message overview or detail for a specific number."""
    target_phone_number = request.args.get("phone_number")
    view_type = request.args.get("view", "list")  # list or conversation
    
    try:
        if target_phone_number:
            # Detail view for specific phone number (unchanged)
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
            # Overview of all messages with filtering and pagination
            page = request.args.get('page', 1, type=int)
            per_page = request.args.get('per_page', 50, type=int)
            filter_type = request.args.get('filter', 'all')  # all, with_media, without_property
            property_filter = request.args.get('property_id', type=int)
            search_query = request.args.get('search', '')
            
            # Start with base query
            query = Message.query.options(
                joinedload(Message.property), 
                joinedload(Message.contact)
            )
            
            # Apply search filter if provided
            if search_query:
                query = query.filter(
                    db.or_(
                        Message.message.ilike(f'%{search_query}%'),
                        Message.phone_number.ilike(f'%{search_query}%'),
                        Message.contact_name.ilike(f'%{search_query}%')
                    )
                )
            
            # Apply filters
            if filter_type == 'with_media':
                # Only messages with media
                query = query.filter(
                    Message.local_media_paths.isnot(None),
                    Message.local_media_paths != '',
                    Message.local_media_paths != '[]'
                )
            elif filter_type == 'unsorted_media':
                # Messages with media but no property
                query = query.filter(
                    Message.local_media_paths.isnot(None),
                    Message.local_media_paths != '',
                    Message.local_media_paths != '[]',
                    Message.property_id.is_(None)
                )
            elif filter_type == 'no_property':
                # All messages without property
                query = query.filter(Message.property_id.is_(None))
            
            # Property filter
            if property_filter:
                query = query.filter(Message.property_id == property_filter)
            
            # Order and paginate
            query = query.order_by(Message.timestamp.desc())
            pagination = query.paginate(page=page, per_page=per_page, error_out=False)
            
            # Parse media paths for each message
            import json
            for msg in pagination.items:
                msg.parsed_media_paths = []
                if msg.local_media_paths and msg.local_media_paths.startswith('['):
                    try:
                        msg.parsed_media_paths = json.loads(msg.local_media_paths)
                    except:
                        pass
            
            # Get properties for filter dropdown
            properties_list = Property.query.order_by(Property.name).all()
            
            # Get known contact phones for the template
            known_contacts = Contact.query.all()
            known_contact_phones = {c.phone_number for c in known_contacts}
            
            # Count statistics
            total_messages = Message.query.count()
            messages_with_media = Message.query.filter(
                Message.local_media_paths.isnot(None),
                Message.local_media_paths != '',
                Message.local_media_paths != '[]'
            ).count()
            unsorted_media = Message.query.filter(
                Message.local_media_paths.isnot(None),
                Message.local_media_paths != '',
                Message.local_media_paths != '[]',
                Message.property_id.is_(None)
            ).count()
            
            # Check if conversation view is requested
            if view_type == "conversation":
                # Group messages by phone number for conversation view
                from collections import defaultdict
                conversations = []
                
                # Get all unique phone numbers with their latest message
                phone_numbers = db.session.query(
                    Message.phone_number,
                    func.max(Message.timestamp).label('last_message_time')
                ).group_by(Message.phone_number)
                
                # Apply property filter if needed
                if property_filter:
                    phone_numbers = phone_numbers.filter(Message.property_id == property_filter)
                
                phone_numbers = phone_numbers.all()
                
                for phone, last_time in phone_numbers:
                    # Get all messages for this phone number
                    conv_messages = Message.query.filter_by(phone_number=phone).options(
                        joinedload(Message.property),
                        joinedload(Message.contact)
                    ).order_by(Message.timestamp.asc()).all()
                    
                    if conv_messages:
                        last_msg = conv_messages[-1]
                        contact = last_msg.contact
                        
                        # Format messages for JavaScript
                        formatted_messages = []
                        for msg in conv_messages:
                            formatted_msg = {
                                'id': msg.id,
                                'message': msg.message,
                                'timestamp': msg.timestamp.isoformat(),
                                'direction': msg.direction,
                                'contact_name': contact.contact_name if contact else None,
                                'media_urls': []
                            }
                            
                            # Parse media URLs
                            if msg.local_media_paths and msg.local_media_paths.startswith('['):
                                try:
                                    import json
                                    media_paths = json.loads(msg.local_media_paths)
                                    formatted_msg['media_urls'] = [url_for('serve_media', filename=path.replace('\\', '/')) for path in media_paths]
                                except:
                                    pass
                            
                            formatted_messages.append(formatted_msg)
                        
                        conversation = {
                            'phone_number': phone,
                            'contact_name': contact.contact_name if contact else None,
                            'property_name': last_msg.property.name if last_msg.property else None,
                            'property_id': last_msg.property_id,
                            'last_message': (last_msg.message[:50] + '...') if last_msg.message and len(last_msg.message) > 50 else last_msg.message,
                            'last_message_time': last_msg.timestamp.strftime('%I:%M %p'),
                            'last_direction': last_msg.direction,
                            'unread_count': 0,  # You can implement unread logic later
                            'messages': formatted_messages
                        }
                        conversations.append(conversation)
                
                # Sort conversations by last message time
                conversations.sort(key=lambda x: x['messages'][-1]['timestamp'], reverse=True)
                
                return render_template("messages_conversation.html",
                                     conversations=conversations,
                                     properties=properties_list,
                                     property_filter=property_filter)
            
            # Default list view
            return render_template("messages_overview.html", 
                                 messages=pagination.items,
                                 pagination=pagination,
                                 properties=properties_list,
                                 known_contact_phones=known_contact_phones,
                                 filter_type=filter_type,
                                 property_filter=property_filter,
                                 search_query=search_query,
                                 total_messages=total_messages,
                                 messages_with_media=messages_with_media,
                                 unsorted_media=unsorted_media)
                                 
    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error in messages_view: {ex}")
        flash(f"Error: {ex}", "danger")
        return redirect(url_for('index'))

@app.route("/messages/ai-search", methods=["POST"])
def ai_search_messages():
    """Use AI to search and analyze messages with better property context"""
    try:
        from openai import OpenAI
        import os
        import json
        import re
        from datetime import datetime, timedelta
        
        # Get OpenAI API key from environment
        openai_api_key = os.getenv('OPENAI_API_KEY')
        if not openai_api_key:
            return jsonify({"error": "OpenAI API key not configured"}), 400
        
        # Initialize OpenAI client
        client = OpenAI(api_key=openai_api_key)
        
        # Get the search query
        query = request.json.get('query', '')
        if not query:
            return jsonify({"error": "No query provided"}), 400
        
        app.logger.info(f"AI Search Query: '{query}'")
        
        # Extract property name/address from query if mentioned
        query_lower = query.lower()
        target_property = None
        
        # Check if query mentions a specific property
        properties = Property.query.all()
        for prop in properties:
            if prop.name and prop.name.lower() in query_lower:
                target_property = prop
                break
            if prop.address and any(part.lower() in query_lower for part in prop.address.split() if len(part) > 3):
                target_property = prop
                break
        
        # Build query for messages
        messages_query = Message.query.options(
            joinedload(Message.property),
            joinedload(Message.contact)
        ).filter(Message.message.isnot(None))
        
        # If we identified a specific property, filter by it
        if target_property:
            messages_query = messages_query.filter(Message.property_id == target_property.id)
            app.logger.info(f"AI Search filtered to property: {target_property.name}")
        
        # Check if query is asking for recent/time-based information
        time_indicators = ['week', 'recent', 'lately', 'today', 'yesterday', 'past', 'last']
        is_time_query = any(indicator in query_lower for indicator in time_indicators)
        
        if is_time_query:
            # For time-based queries, get messages from the last 2 weeks
            two_weeks_ago = datetime.now() - timedelta(days=14)
            messages_query = messages_query.filter(Message.timestamp >= two_weeks_ago)
            app.logger.info(f"AI Search filtered to messages since: {two_weeks_ago}")
        
        # Get messages (more for time-based queries)
        message_limit = 100 if is_time_query else 50
        messages = messages_query.order_by(Message.timestamp.desc()).limit(message_limit).all()
        
        app.logger.info(f"Found {len(messages)} messages to analyze")
        
        # For broad queries like "problems this week", include most messages
        broad_indicators = ['general', 'problems', 'issues', 'what', 'anything', 'tell me about']
        is_broad_query = any(indicator in query_lower for indicator in broad_indicators)
        
        relevant_messages = []
        
        if is_broad_query or is_time_query:
            # For broad/time queries, include most messages with minimal filtering
            relevant_messages = messages[:30]
            app.logger.info(f"Broad/time query - using {len(relevant_messages)} messages")
        else:
            # For specific queries, do keyword filtering
            issue_keywords = []
            common_issues = {
                'ac': ['ac', 'air conditioning', 'hvac', 'cooling', 'heat pump', 'hot', 'cold'],
                'refrigerator': ['fridge', 'refrigerator', 'freezer', 'ice'],
                'microwave': ['microwave'],
                'appliance': ['appliance', 'dishwasher', 'washer', 'dryer', 'oven', 'stove'],
                'plumbing': ['plumbing', 'leak', 'water', 'toilet', 'sink', 'faucet', 'pipe', 'drain'],
                'electrical': ['electrical', 'power', 'outlet', 'light', 'switch', 'breaker'],
                'lawn': ['lawn', 'grass', 'yard', 'landscaping', 'mowing'],
                'roof': ['roof', 'roofing', 'shingle', 'gutter'],
                'security': ['security', 'camera', 'alarm', 'lock'],
                'repair': ['repair', 'fix', 'broken', 'maintenance', 'replace', 'install'],
                'problem': ['problem', 'issue', 'trouble', 'concern', 'complaint', 'broken', 'not working']
            }
            
            for category, keywords in common_issues.items():
                if any(keyword in query_lower for keyword in keywords):
                    issue_keywords.extend(keywords)
            
            # Filter messages by keywords
            for msg in messages:
                if msg.message:
                    msg_lower = msg.message.lower()
                    if any(keyword in msg_lower for keyword in issue_keywords):
                        relevant_messages.append(msg)
            
            app.logger.info(f"Keyword filtering found {len(relevant_messages)} relevant messages")
            
            # If very few found, add recent messages
            if len(relevant_messages) < 5:
                recent_additions = [msg for msg in messages[:20] if msg not in relevant_messages]
                relevant_messages.extend(recent_additions[:10])
                app.logger.info(f"Added {len(recent_additions[:10])} recent messages as context")
        
        # Build context for AI
        message_context = []
        for msg in relevant_messages:
            message_context.append({
                'id': msg.id,
                'date': msg.timestamp.strftime('%Y-%m-%d %H:%M'),
                'property': msg.property.name if msg.property else 'Unknown Property',
                'message': msg.message[:400],
                'contact': msg.contact.contact_name if msg.contact else msg.phone_number
            })
        
        if not message_context:
            return jsonify({
                "response": "No relevant messages found for your query. Try broadening your search terms or check if there are any messages in the selected time period.",
                "relevant_messages": [],
                "messages_analyzed": 0,
                "debug_info": f"Total messages in DB: {Message.query.count()}, Messages after filtering: {len(messages)}"
            })
        
        # Create AI prompt
        property_context = f" at {target_property.name}" if target_property else ""
        time_context = " in the recent period" if is_time_query else ""
        
        prompt = f"""
        You are analyzing property management messages{property_context}{time_context}.
        
        Query: "{query}"
        
        Based on these {len(message_context)} messages, provide a helpful answer:

        Messages:
        {json.dumps(message_context, indent=2)}
        
        Instructions:
        1. Summarize what you find in these messages
        2. Be specific about dates, properties, and issues mentioned
        3. If few issues are found, mention that things seem quiet
        4. Group similar issues together
        5. Provide actionable insights when possible
        """
        
        # Call OpenAI
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful property management assistant. Analyze the provided messages and give useful insights."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=800,
            temperature=0.2
        )
        
        ai_response = response.choices[0].message.content
        relevant_msg_ids = [msg.id for msg in relevant_messages]
        
        result = {
            "response": ai_response,
            "relevant_messages": relevant_msg_ids,
            "property_filtered": target_property.name if target_property else None,
            "messages_analyzed": len(relevant_messages),
            "total_messages_found": len(messages),
            "query_type": "broad/time" if (is_broad_query or is_time_query) else "specific"
        }
        
        app.logger.info(f"AI Search returning: {len(ai_response)} char response, {len(relevant_msg_ids)} relevant messages")
        
        return jsonify(result)
        
    except Exception as e:
        app.logger.error(f"AI Search error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/messages/assign-property", methods=["POST"])
def assign_property_to_message():
    """Assign a property to a message"""
    try:
        app.logger.info(f"Property assignment request: {request.get_json()}")
        
        data = request.get_json()
        if not data:
            app.logger.error("No JSON data received")
            return jsonify({"error": "No JSON data received"}), 400
            
        message_id = data.get('message_id')
        property_id = data.get('property_id')
        
        app.logger.info(f"Assigning message {message_id} to property {property_id}")
        
        if not message_id:
            return jsonify({"error": "Missing message_id"}), 400
        
        message = db.session.get(Message, message_id)
        if not message:
            return jsonify({"error": "Message not found"}), 404
        
        # Handle empty string as null
        if property_id == "":
            property_id = None
            
        if property_id:
            property_obj = db.session.get(Property, property_id)
            if not property_obj:
                return jsonify({"error": "Property not found"}), 404
        
        # Update the message
        old_property_id = message.property_id
        message.property_id = property_id
        db.session.commit()
        
        property_name = "Unassigned"
        if property_id:
            property_obj = db.session.get(Property, property_id)
            property_name = property_obj.name if property_obj else f"Property {property_id}"
        
        app.logger.info(f"Successfully assigned message {message_id} to property {property_name} (was: {old_property_id})")
        
        return jsonify({
            "success": True,
            "message": f"Message assigned to {property_name}",
            "old_property_id": old_property_id,
            "new_property_id": property_id
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error assigning property: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/property/<int:property_id>/set-thumbnail", methods=["POST"])
def set_property_thumbnail(property_id):
    """Set a property thumbnail from a gallery image"""
    try:
        app.logger.info(f"Setting thumbnail for property {property_id}")
        
        data = request.get_json()
        thumbnail_path = data.get('thumbnail_path')
        
        if not thumbnail_path:
            return jsonify({"error": "No thumbnail path provided"}), 400
        
        property_obj = db.session.get(Property, property_id)
        if not property_obj:
            return jsonify({"error": "Property not found"}), 404
        
        # Add thumbnail_path column if it doesn't exist (for migration)
        try:
            # Check if the column exists
            inspector = inspect(db.engine)
            columns = [column['name'] for column in inspector.get_columns('properties')]
            if 'thumbnail_path' not in columns:
                # Add the column
                db.session.execute(text("ALTER TABLE properties ADD COLUMN thumbnail_path TEXT"))
                db.session.commit()
                app.logger.info("Added thumbnail_path column to properties table")
        except Exception as e:
            app.logger.warning(f"Could not add thumbnail_path column: {e}")
        
        # Update the property thumbnail
        property_obj.thumbnail_path = thumbnail_path
        db.session.commit()
        
        app.logger.info(f"Set thumbnail for property {property_obj.name}: {thumbnail_path}")
        
        return jsonify({
            "success": True,
            "message": f"Thumbnail set for {property_obj.name}",
            "thumbnail_path": thumbnail_path
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error setting property thumbnail: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/property/<int:property_id>/remove-thumbnail", methods=["POST"])
def remove_property_thumbnail(property_id):
    """Remove a property thumbnail"""
    try:
        property_obj = db.session.get(Property, property_id)
        if not property_obj:
            return jsonify({"error": "Property not found"}), 404
        
        # Remove the thumbnail
        old_thumbnail = getattr(property_obj, 'thumbnail_path', None)
        property_obj.thumbnail_path = None
        db.session.commit()
        
        app.logger.info(f"Removed thumbnail for property {property_obj.name}: {old_thumbnail}")
        
        return jsonify({
            "success": True,
            "message": f"Thumbnail removed from {property_obj.name}"
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error removing property thumbnail: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/debug/message-counts")
def debug_message_counts():
    """Debug route to check message counts"""
    try:
        from datetime import datetime, timedelta
        
        total_messages = Message.query.count()
        messages_with_content = Message.query.filter(Message.message.isnot(None), Message.message != '').count()
        
        # Messages from last week
        one_week_ago = datetime.now() - timedelta(days=7)
        recent_messages = Message.query.filter(Message.timestamp >= one_week_ago).count()
        recent_with_content = Message.query.filter(
            Message.timestamp >= one_week_ago,
            Message.message.isnot(None), 
            Message.message != ''
        ).count()
        
        # Sample recent messages
        sample_messages = Message.query.filter(
            Message.timestamp >= one_week_ago,
            Message.message.isnot(None)
        ).order_by(Message.timestamp.desc()).limit(5).all()
        
        sample_data = []
        for msg in sample_messages:
            sample_data.append({
                'id': msg.id,
                'date': msg.timestamp.strftime('%Y-%m-%d %H:%M'),
                'message_preview': msg.message[:100] if msg.message else 'No message',
                'property': msg.property.name if msg.property else 'No property'
            })
        
        return jsonify({
            "total_messages": total_messages,
            "messages_with_content": messages_with_content,
            "recent_messages_7_days": recent_messages,
            "recent_with_content_7_days": recent_with_content,
            "sample_recent_messages": sample_data
        })
        
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/test-json", methods=["POST"])
def test_json():
    """Test endpoint to verify JSON responses work"""
    try:
        data = request.get_json()
        app.logger.info(f"Test JSON received: {data}")
        
        return jsonify({
            "success": True,
            "received_data": data,
            "message": "JSON endpoint working correctly"
        })
    except Exception as e:
        app.logger.error(f"Test JSON error: {e}")
        return jsonify({"error": str(e)}), 500

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
        
        # Get custom fields count
        custom_fields_count = PropertyCustomField.query.filter_by(property_id=property_id).count()
        
        # Get attachments count
        attachments_count = PropertyAttachment.query.filter_by(property_id=property_id).count()
        
        # Get contacts count
        contacts_count = PropertyContact.query.filter_by(property_id=property_id).count()
        
        return render_template('property_detail.html', 
                             property=property_obj,
                             recent_messages=recent_messages,
                             custom_fields_count=custom_fields_count,
                             attachments_count=attachments_count,
                             contacts_count=contacts_count)
        
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
    """Display galleries overview with thumbnails."""
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
        
        # Get sample images for properties and set thumbnail info
        for prop in properties_with_galleries:
            # Check if property has thumbnail_path attribute and value
            thumbnail_path = getattr(prop, 'thumbnail_path', None)
            prop.has_thumbnail = bool(thumbnail_path)
            
            if not thumbnail_path:
                # Get the first available image for this property
                sample_message = (
                    Message.query.filter(
                        Message.property_id == prop.id,
                        Message.local_media_paths.isnot(None),
                        Message.local_media_paths != '',
                        Message.local_media_paths != '[]'
                    )
                    .order_by(Message.timestamp.desc())
                    .first()
                )
                
                if sample_message and sample_message.local_media_paths:
                    try:
                        import json
                        if sample_message.local_media_paths.startswith('['):
                            media_paths = json.loads(sample_message.local_media_paths)
                        else:
                            media_paths = [sample_message.local_media_paths]
                        
                        if media_paths and media_paths[0]:
                            prop.sample_image = media_paths[0]
                        else:
                            prop.sample_image = None
                    except:
                        prop.sample_image = None
                else:
                    prop.sample_image = None
            else:
                prop.sample_image = thumbnail_path
        
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
    
@app.route("/gallery/unsorted")
def unsorted_gallery():
    """Display gallery for unsorted media (messages without property assignment)."""
    try:
        # Get messages with media but no property assignment
        unsorted_messages = (
            Message.query.options(joinedload(Message.contact))
            .filter(
                Message.property_id.is_(None),
                Message.local_media_paths.isnot(None)
            )
            .order_by(Message.timestamp.desc())
            .all()
        )
        
        # Process media paths for display
        image_items = []
        for msg in unsorted_messages:
            if msg.local_media_paths:
                # Parse the media paths (assuming they're stored as JSON or comma-separated)
                try:
                    import json
                    if msg.local_media_paths.startswith('['):
                        media_paths = json.loads(msg.local_media_paths)
                    else:
                        media_paths = [path.strip() for path in msg.local_media_paths.split(',')]
                    
                    for path in media_paths:
                        if path:  # Skip empty paths
                            image_items.append({
                                'path': path,
                                'message': msg,
                                'contact': msg.contact,
                                'timestamp': msg.timestamp
                            })
                except (json.JSONDecodeError, AttributeError):
                    # Handle parsing errors gracefully
                    pass
        
        return render_template("gallery.html", 
                             image_items=image_items, 
                             property=None, 
                             gallery_title="Unsorted Media")
    except Exception as e:
        app.logger.error(f"Error loading unsorted gallery: {e}")
        flash(f"Error loading unsorted gallery: {e}", "danger")
        return redirect(url_for("galleries_overview"))

@app.route("/gallery/<int:property_id>")
def gallery_for_property(property_id):
    """Display gallery for specific property."""
    try:
        prop = db.session.get(Property, property_id)
        if not prop:
            flash("Property not found.", "warning")
            return redirect(url_for("galleries_overview"))
        
        # Add thumbnail info to property object
        thumbnail_path = getattr(prop, 'thumbnail_path', None)
        prop.has_thumbnail = bool(thumbnail_path)
        # Ensure thumbnail_path is accessible even if None
        if not hasattr(prop, 'thumbnail_path'):
            prop.thumbnail_path = None
        
        # Get messages with media for this property
        messages_with_media = (
            Message.query.options(joinedload(Message.contact))
            .filter(
                Message.property_id == property_id,
                Message.local_media_paths.isnot(None),
                Message.local_media_paths != '',
                Message.local_media_paths != '[]'
            )
            .order_by(Message.timestamp.desc())
            .all()
        )
        
        # Process media paths for display
        image_items = []
        for msg in messages_with_media:
            if msg.local_media_paths:
                try:
                    import json
                    if msg.local_media_paths.startswith('['):
                        media_paths = json.loads(msg.local_media_paths)
                    else:
                        media_paths = [path.strip() for path in msg.local_media_paths.split(',')]
                    
                    for path in media_paths:
                        if path:
                            image_items.append({
                                'path': path,
                                'message': msg,
                                'contact': msg.contact,
                                'timestamp': msg.timestamp
                            })
                except (json.JSONDecodeError, AttributeError):
                    pass
        
        return render_template("gallery.html", 
                             image_items=image_items, 
                             property=prop, 
                             gallery_title=f"Gallery for {prop.name}")
    except Exception as e:
        app.logger.error(f"Error loading gallery for property {property_id}: {e}")
        flash(f"Error loading gallery: {e}", "danger")
        return redirect(url_for("galleries_overview"))

@app.route("/media/<path:filename>")
def serve_media(filename):
    """Serve uploaded media files."""
    try:
        from flask import send_from_directory
        # Use the configured UPLOAD_FOLDER which is now /app/static/uploads
        upload_folder = app.config.get("UPLOAD_FOLDER", "/app/static/uploads")
        return send_from_directory(upload_folder, filename)
    except Exception as e:
        app.logger.error(f"Error serving media file {filename}: {e}")
        return "File not found", 404

@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    """Display Ask AI page and handle AI queries."""
    try:
        if request.method == "POST":
            from openai import OpenAI
            
            # Get the query from form
            query = request.form.get('query', '').strip()
            if not query:
                flash("Please enter a question.", "warning")
                return render_template('ask.html')
            
            # Get OpenAI API key
            openai_api_key = os.getenv('OPENAI_API_KEY')
            if not openai_api_key:
                flash("OpenAI API key not configured.", "danger")
                return render_template('ask.html')
            
            try:
                # Initialize OpenAI client
                client = OpenAI(api_key=openai_api_key)
                
                app.logger.info(f"Ask AI Query: '{query}'")
                
                # Extract time context
                query_lower = query.lower()
                time_indicators = ['today', 'yesterday', 'week', 'recent', 'lately', 'past', 'last']
                is_time_query = any(indicator in query_lower for indicator in time_indicators)
                
                # Determine time range
                if 'today' in query_lower:
                    time_filter = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    time_desc = "today"
                elif 'yesterday' in query_lower:
                    time_filter = (datetime.now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    time_desc = "yesterday"
                elif 'week' in query_lower:
                    time_filter = datetime.now() - timedelta(days=7)
                    time_desc = "the past week"
                elif is_time_query:
                    time_filter = datetime.now() - timedelta(days=14)
                    time_desc = "the past two weeks"
                else:
                    time_filter = datetime.now() - timedelta(days=30)
                    time_desc = "the past month"
                
                # Get relevant data based on query
                messages_query = Message.query.options(
                    joinedload(Message.property),
                    joinedload(Message.contact)
                ).filter(
                    Message.timestamp >= time_filter,
                    Message.message.isnot(None)
                )
                
                # Check if asking about properties
                if any(word in query_lower for word in ['property', 'properties', 'unit', 'units', 'tenant', 'tenants']):
                    properties = Property.query.all()
                    property_info = []
                    for prop in properties:
                        tenant_count = PropertyContact.query.filter_by(
                            property_id=prop.id,
                            contact_type='tenant'
                        ).count()
                        message_count = Message.query.filter_by(property_id=prop.id).filter(
                            Message.timestamp >= time_filter
                        ).count()
                        property_info.append({
                            'name': prop.name,
                            'address': prop.address,
                            'tenants': tenant_count,
                            'recent_messages': message_count
                        })
                    
                    # Create property summary
                    property_summary = f"Property Overview for {time_desc}:\n\n"
                    for info in property_info:
                        property_summary += f"• {info['name']} ({info['address']})\n"
                        property_summary += f"  - {info['tenants']} tenant(s)\n"
                        property_summary += f"  - {info['recent_messages']} message(s)\n\n"
                    
                    response = property_summary
                else:
                    # Get messages for analysis
                    messages = messages_query.order_by(Message.timestamp.desc()).limit(50).all()
                    
                    if not messages:
                        response = f"No messages found for {time_desc}."
                    else:
                        # Prepare context for AI
                        message_context = f"Analyzing {len(messages)} messages from {time_desc}:\n\n"
                        for msg in messages[:20]:  # Limit context size
                            contact_name = msg.contact.contact_name if msg.contact else "Unknown"
                            property_name = msg.property.name if msg.property else "No Property"
                            timestamp = msg.timestamp.strftime("%Y-%m-%d %H:%M")
                            direction = "from" if msg.direction == "incoming" else "to"
                            message_context += f"[{timestamp}] {direction} {contact_name} (Property: {property_name}): {msg.message[:100]}...\n"
                        
                        # Ask AI to analyze
                        ai_prompt = f"""Based on these recent messages, answer this question: {query}
                        
Context:
{message_context}

Provide a helpful, concise answer focusing on the specific question asked."""
                        
                        completion = client.chat.completions.create(
                            model="gpt-3.5-turbo",
                            messages=[
                                {"role": "system", "content": "You are a helpful assistant analyzing property management messages. Be concise and specific."},
                                {"role": "user", "content": ai_prompt}
                            ],
                            temperature=0.3,
                            max_tokens=500
                        )
                        
                        response = completion.choices[0].message.content.strip()
                
                return render_template('ask.html', response=response)
                
            except Exception as ai_error:
                app.logger.error(f"AI processing error: {ai_error}")
                flash(f"Error processing your question: {str(ai_error)}", "danger")
                return render_template('ask.html')
        
        # GET request - just show the form
        return render_template('ask.html')
        
    except Exception as e:
        app.logger.error(f"Error in ask_view: {e}")
        flash(f"Error: {e}", "danger")
        return redirect(url_for('index'))

@app.route("/notifications", methods=["GET", "POST"])
def notifications_view():
    """Displays notification form and history, handles sending."""
    properties = []
    history = []
    error_message = None
    
    if request.method == "POST":
        # --- Get form data ---
        property_ids = request.form.getlist("property_ids")
        subject = request.form.get("subject", "")
        message_body = request.form.get("message_body", "")
        channels = request.form.getlist("channels")
        uploaded_files = request.files.getlist("attachments")
        
        app.logger.info(f"Notification POST: properties={property_ids}, channels={channels}, subject='{subject}'")
        
        # --- Input Validation ---
        if not property_ids:
            flash("Please select at least one property.", "error")
            return redirect(url_for("notifications_view"))
        if not message_body:
            flash("Message body cannot be empty.", "error")
            return redirect(url_for("notifications_view"))
        if not channels:
            flash("Please select at least one channel (Email or SMS).", "error")
            return redirect(url_for("notifications_view"))
        
        try:
            # Get target properties and tenants
            target_property_ids = [int(pid) for pid in property_ids if pid.isdigit()]
            target_properties = Property.query.filter(Property.id.in_(target_property_ids)).all()
            properties_targeted_str = ", ".join([f"'{p.name}' (ID:{p.id})" for p in target_properties])
            
            # Get current tenants for selected properties
            target_tenants = Tenant.query.filter(
                Tenant.property_id.in_(target_property_ids), 
                Tenant.status == 'current'
            ).all()
            
            app.logger.info(f"Found {len(target_tenants)} current tenants for notification")
            
            # Collect unique emails and phones
            emails_to_send = {t.email for t in target_tenants if t.email}
            phones_to_send = {t.phone for t in target_tenants if t.phone}
            
            if not emails_to_send and not phones_to_send:
                flash("No current tenants with email or phone found for the selected properties.", "warning")
                
                # Log the attempt even if no recipients
                history_log = NotificationHistory(
                    subject=subject or None,
                    body=message_body,
                    channels=", ".join(channels),
                    status="No Recipients Found",
                    properties_targeted=properties_targeted_str,
                    recipients_summary="Email: 0/0. SMS: 0/0.",
                    error_info=None
                )
                db.session.add(history_log)
                db.session.commit()
                return redirect(url_for('notifications_view'))
            
            # Initialize counters
            email_success_count = 0
            sms_success_count = 0
            email_errors = []
            sms_errors = []
            channels_attempted = []
            
            # Send emails if requested
            if 'email' in channels and emails_to_send:
                channels_attempted.append("Email")
                app.logger.info(f"Attempting email to {len(emails_to_send)} addresses...")
                
                email_subject = subject if subject else message_body[:50] + ("..." if len(message_body) > 50 else "")
                
                for email in emails_to_send:
                    try:
                        # Import wrap_email_html for better formatting
                        from email_utils import wrap_email_html
                        
                        # Get property names for this email recipient
                        tenant_properties = []
                        for prop_id in property_ids:
                            prop = Property.query.get(prop_id)
                            if prop:
                                tenant_properties.append(f"{prop.name} - {prop.address}")
                        
                        properties_html = "<br>".join(tenant_properties) if tenant_properties else "All Properties"
                        
                        # Format the email content
                        html_content = wrap_email_html(f"""
                            <h3 style="color: #212529; margin-bottom: 20px;">Property Management Notification</h3>
                            
                            <div style="background-color: #f8f9fa; padding: 15px; border-radius: 4px; margin-bottom: 20px;">
                                <div class="info-row">
                                    <span class="info-label">Properties:</span>
                                    <span class="info-value">{properties_html}</span>
                                </div>
                                <div class="info-row" style="border-bottom: none;">
                                    <span class="info-label">Date:</span>
                                    <span class="info-value">{datetime.now().strftime('%B %d, %Y at %I:%M %p')}</span>
                                </div>
                            </div>
                            
                            <div class="message-box">
                                <h4 style="margin: 0 0 10px 0; color: #495057; font-size: 14px;">Message:</h4>
                                <p class="message-text">{message_body}</p>
                            </div>
                            
                            <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #e9ecef;">
                                <p style="color: #6c757d; font-size: 14px; margin: 0;">
                                    This notification was sent via the Sin City Rentals property management system.
                                </p>
                            </div>
                        """)
                        
                        # Use your existing send_email function
                        email_sent_successfully = send_email(
                            to_emails=[email], 
                            subject=email_subject, 
                            html_content=html_content,
                            attachments=[]  # Handle attachments later if needed
                        )
                        if email_sent_successfully:
                            email_success_count += 1
                        else:
                            email_errors.append(f"{email}: Failed")
                    except Exception as e:
                        app.logger.error(f"Email Exception for {email}: {e}")
                        email_errors.append(f"{email}: Exception")
            
            # Send SMS if requested
            if 'sms' in channels and phones_to_send:
                channels_attempted.append("SMS")
                app.logger.info(f"Attempting SMS to {len(phones_to_send)} numbers...")
                
                for phone in phones_to_send:
                    try:
                        # Use your existing send_openphone_sms function
                        sms_sent = send_openphone_sms(
                            recipient_phone=phone, 
                            message_body=message_body
                        )
                        if sms_sent:
                            sms_success_count += 1
                        else:
                            sms_errors.append(f"{phone}: Failed")
                    except Exception as e:
                        app.logger.error(f"SMS Exception for {phone}: {e}")
                        sms_errors.append(f"{phone}: Exception")
            
            # Calculate status and summary
            total_email_attempts = len(emails_to_send) if 'Email' in channels_attempted else 0
            total_sms_attempts = len(phones_to_send) if 'SMS' in channels_attempted else 0
            total_successes = email_success_count + sms_success_count
            total_attempts = total_email_attempts + total_sms_attempts
            
            recipients_summary = f"Email: {email_success_count}/{total_email_attempts}. SMS: {sms_success_count}/{total_sms_attempts}."
            
            # Determine final status
            if total_attempts == 0:
                final_status = "No Recipients Found"
            elif email_errors or sms_errors:
                if total_successes > 0:
                    final_status = "Partial Failure"
                    flash(f"Notifications sent with some failures. ({recipients_summary})", "warning")
                else:
                    final_status = "Failed"
                    flash("All notifications failed to send. Check logs.", "danger")
            else:
                final_status = "Sent"
                flash(f"Notifications sent successfully! ({recipients_summary})", "success")
            
            # Log to history
            error_details = []
            if email_errors:
                error_details.append(f"{len(email_errors)} Email failure(s)")
            if sms_errors:
                error_details.append(f"{len(sms_errors)} SMS failure(s)")
            error_info_str = "; ".join(error_details) + " (See logs)" if error_details else None
            
            history_log = NotificationHistory(
                subject=subject if 'Email' in channels_attempted else None,
                body=message_body,
                channels=", ".join(channels_attempted),
                status=final_status,
                properties_targeted=properties_targeted_str,
                recipients_summary=recipients_summary,
                error_info=error_info_str
            )
            db.session.add(history_log)
            db.session.commit()
            
            app.logger.info(f"Notification logged (ID: {history_log.id}, Status: {final_status})")
            
        except Exception as ex:
            db.session.rollback()
            app.logger.error(f"❌ Unexpected error during notification POST: {ex}", exc_info=True)
            flash(f"An unexpected error occurred: {ex}", "danger")
        
        return redirect(url_for('notifications_view'))
    
    # GET request - load properties and history
    try:
        properties = Property.query.order_by(Property.name).all()
        history = NotificationHistory.query.order_by(NotificationHistory.timestamp.desc()).limit(20).all()
        
    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"❌ Error loading notifications GET: {ex}", exc_info=True)
        error_message = f"Error loading page data: {ex}"
        flash(error_message, "danger")
        properties = []
        history = []
    
    return render_template("notifications.html", 
                         properties=properties, 
                         history=history, 
                         error=error_message)

# Property management routes
@app.route('/property/<int:property_id>/custom-fields', methods=['GET', 'POST'])
def property_custom_fields(property_id):
    """Manage custom fields for a property"""
    property_obj = db.session.get(Property, property_id)
    if not property_obj:
        flash("Property not found.", "warning")
        return redirect(url_for("properties_list_view"))
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add':
            category = request.form.get('category', 'General')
            field_name = request.form.get('field_name')
            field_value = request.form.get('field_value')
            field_type = request.form.get('field_type', 'text')
            
            if field_name and field_value:
                try:
                    # Check if field already exists
                    existing = PropertyCustomField.query.filter_by(
                        property_id=property_id,
                        category=category,
                        field_name=field_name
                    ).first()
                    
                    if existing:
                        existing.field_value = field_value
                        existing.field_type = field_type
                        existing.updated_at = datetime.now(timezone.utc)
                    else:
                        new_field = PropertyCustomField(
                            property_id=property_id,
                            category=category,
                            field_name=field_name,
                            field_value=field_value,
                            field_type=field_type
                        )
                        db.session.add(new_field)
                    
                    db.session.commit()
                    flash(f"Custom field '{field_name}' saved successfully!", "success")
                except Exception as e:
                    db.session.rollback()
                    flash(f"Error saving custom field: {e}", "danger")
            else:
                flash("Field name and value are required.", "warning")
        
        elif action == 'delete':
            field_id = request.form.get('field_id')
            if field_id:
                field = db.session.get(PropertyCustomField, field_id)
                if field and field.property_id == property_id:
                    db.session.delete(field)
                    db.session.commit()
                    flash("Custom field deleted.", "success")
        
        return redirect(url_for('property_custom_fields', property_id=property_id))
    
    # GET request - load custom fields grouped by category
    custom_fields = PropertyCustomField.query.filter_by(property_id=property_id).order_by(
        PropertyCustomField.category, PropertyCustomField.field_name
    ).all()
    
    # Group fields by category
    fields_by_category = {}
    for field in custom_fields:
        if field.category not in fields_by_category:
            fields_by_category[field.category] = []
        fields_by_category[field.category].append(field)
    
    # Predefined categories for the dropdown
    categories = ['HOA', 'Tenant Info', 'Access Codes', 'Neighbors', 'Utilities', 'Maintenance', 'Financial', 'General']
    
    return render_template('property_custom_fields.html', 
                         property=property_obj, 
                         fields_by_category=fields_by_category,
                         categories=categories)

@app.route('/property/<int:property_id>/contacts', methods=['GET', 'POST'])
def property_contacts(property_id):
    """Manage contacts for a property"""
    property_obj = db.session.get(Property, property_id)
    if not property_obj:
        flash("Property not found.", "warning")
        return redirect(url_for("properties_list_view"))
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add':
            try:
                contact = PropertyContact(
                    property_id=property_id,
                    contact_type=request.form.get('contact_type', 'General'),
                    name=request.form.get('name'),
                    phone=request.form.get('phone'),
                    email=request.form.get('email'),
                    company=request.form.get('company'),
                    role=request.form.get('role'),
                    notes=request.form.get('notes'),
                    is_primary=request.form.get('is_primary') == 'on'
                )
                db.session.add(contact)
                db.session.commit()
                flash("Contact added successfully!", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Error adding contact: {e}", "danger")
        
        elif action == 'delete':
            contact_id = request.form.get('contact_id')
            if contact_id:
                contact = db.session.get(PropertyContact, contact_id)
                if contact and contact.property_id == property_id:
                    db.session.delete(contact)
                    db.session.commit()
                    flash("Contact deleted.", "success")
        
        return redirect(url_for('property_contacts', property_id=property_id))
    
    # GET request
    contacts = PropertyContact.query.filter_by(property_id=property_id).order_by(
        PropertyContact.contact_type, PropertyContact.name
    ).all()
    
    # Group contacts by type
    contacts_by_type = {}
    for contact in contacts:
        if contact.contact_type not in contacts_by_type:
            contacts_by_type[contact.contact_type] = []
        contacts_by_type[contact.contact_type].append(contact)
    
    contact_types = ['HOA', 'Neighbor', 'Vendor', 'Emergency', 'Utility', 'Property Manager', 'Other']
    
    return render_template('property_contacts.html', 
                         property=property_obj, 
                         contacts_by_type=contacts_by_type,
                         contact_types=contact_types)

@app.route('/property/<int:property_id>/attachments', methods=['GET', 'POST'])
def property_attachments(property_id):
    """Manage file attachments for a property"""
    property_obj = db.session.get(Property, property_id)
    if not property_obj:
        flash("Property not found.", "warning")
        return redirect(url_for("properties_list_view"))
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'upload':
            if 'file' not in request.files:
                flash('No file selected', 'warning')
                return redirect(request.url)
            
            file = request.files['file']
            if file.filename == '':
                flash('No file selected', 'warning')
                return redirect(request.url)
            
            if file:
                try:
                    # Create property-specific upload folder
                    property_folder = os.path.join(app.config['UPLOAD_FOLDER'], 'properties', str(property_id))
                    os.makedirs(property_folder, exist_ok=True)
                    
                    # Generate unique filename
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    file_ext = os.path.splitext(file.filename)[1]
                    safe_filename = f"{timestamp}_{secure_filename(file.filename)}"
                    file_path = os.path.join(property_folder, safe_filename)
                    
                    # Save file
                    file.save(file_path)
                    
                    # Create database record
                    attachment = PropertyAttachment(
                        property_id=property_id,
                        category=request.form.get('category', 'General'),
                        filename=safe_filename,
                        original_filename=file.filename,
                        file_path=file_path,
                        file_size=os.path.getsize(file_path),
                        file_type=file.content_type,
                        description=request.form.get('description')
                    )
                    db.session.add(attachment)
                    db.session.commit()
                    
                    flash(f"File '{file.filename}' uploaded successfully!", "success")
                except Exception as e:
                    db.session.rollback()
                    flash(f"Error uploading file: {e}", "danger")
        
        elif action == 'delete':
            attachment_id = request.form.get('attachment_id')
            if attachment_id:
                attachment = db.session.get(PropertyAttachment, attachment_id)
                if attachment and attachment.property_id == property_id:
                    # Delete file from disk
                    try:
                        if os.path.exists(attachment.file_path):
                            os.remove(attachment.file_path)
                    except Exception as e:
                        app.logger.error(f"Error deleting file: {e}")
                    
                    # Delete database record
                    db.session.delete(attachment)
                    db.session.commit()
                    flash("Attachment deleted.", "success")
        
        return redirect(url_for('property_attachments', property_id=property_id))
    
    # GET request
    attachments = PropertyAttachment.query.filter_by(property_id=property_id).order_by(
        PropertyAttachment.category, PropertyAttachment.uploaded_at.desc()
    ).all()
    
    # Group attachments by category
    attachments_by_category = {}
    for attachment in attachments:
        if attachment.category not in attachments_by_category:
            attachments_by_category[attachment.category] = []
        attachments_by_category[attachment.category].append(attachment)
    
    categories = ['HOA Documents', 'Lease Agreement', 'Maintenance Records', 'Photos', 'Insurance', 'Financial', 'Other']
    
    return render_template('property_attachments.html', 
                         property=property_obj, 
                         attachments_by_category=attachments_by_category,
                         categories=categories)

@app.route('/property/<int:property_id>/download/<int:attachment_id>')
def download_attachment(property_id, attachment_id):
    """Download a property attachment"""
    attachment = PropertyAttachment.query.filter_by(id=attachment_id, property_id=property_id).first()
    if not attachment:
        flash("Attachment not found.", "warning")
        return redirect(url_for('property_attachments', property_id=property_id))
    
    try:
        return send_file(attachment.file_path, as_attachment=True, download_name=attachment.original_filename)
    except Exception as e:
        app.logger.error(f"Error downloading attachment: {e}")
        flash("Error downloading file.", "danger")
        return redirect(url_for('property_attachments', property_id=property_id))

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

# Debug and admin routes
@app.route("/debug/properties")
def debug_properties():
    """Simple debug route to test if properties load at all"""
    try:
        properties = Property.query.all()
        return f"<h3>Properties Debug</h3><p>Found {len(properties)} properties:</p>" + \
               "<ul>" + "".join([f"<li>{p.name} (ID: {p.id})</li>" for p in properties]) + "</ul>"
    except Exception as e:
        return f"<h3>Error</h3><p>{str(e)}</p>"

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

@app.route("/ping")
def ping_route():
    try:
        db.session.execute(text("SELECT 1"))
        return "Pong! DB OK.", 200
    except Exception as e:
        app.logger.error(f"DB Ping Failed: {e}")
        return f"Pong! DB Error: {e}", 503

@app.route("/debug/volume")
def debug_volume():
    """Check what's actually in the volume"""
    import os
    import json
    
    upload_folder = app.config.get("UPLOAD_FOLDER", "/app/static/uploads")
    results = {
        "upload_folder": upload_folder,
        "exists": os.path.exists(upload_folder),
        "files": [],
        "sample_db_paths": []
    }
    
    # List files in upload folder
    if os.path.exists(upload_folder):
        try:
            files = os.listdir(upload_folder)
            results["total_files"] = len(files)
            results["files"] = files[:20]  # First 20 files
        except Exception as e:
            results["error"] = str(e)
    
    # Get sample paths from database
    messages = Message.query.filter(
        Message.local_media_paths.isnot(None)
    ).limit(5).all()
    
    for msg in messages:
        results["sample_db_paths"].append({
            "msg_id": msg.id,
            "local_media_paths": msg.local_media_paths
        })
    
    return f"<pre>{json.dumps(results, indent=2)}</pre>"

@app.route("/admin/fix-paths", methods=["GET", "POST"])
def fix_database_paths():
    """Fix database paths to match files in volume"""
    import os
    import json
    import re
    
    if request.method == "POST":
        upload_folder = app.config.get("UPLOAD_FOLDER", "/app/static/uploads")
        fixed_count = 0
        
        try:
            # Get all files in upload folder
            if os.path.exists(upload_folder):
                files = os.listdir(upload_folder)
                
                # Group files by message ID
                files_by_msg_id = {}
                for filename in files:
                    # Extract message ID from filename (e.g., msg129_4_xxx.jpg -> 129)
                    match = re.match(r'msg(\d+)_', filename)
                    if match:
                        msg_id = int(match.group(1))
                        if msg_id not in files_by_msg_id:
                            files_by_msg_id[msg_id] = []
                        files_by_msg_id[msg_id].append(filename)
                
                # Update database for each message
                for msg_id, filenames in files_by_msg_id.items():
                    message = db.session.get(Message, msg_id)
                    if message:
                        # Create paths relative to static folder
                        paths = [f"uploads/{filename}" for filename in sorted(filenames)]
                        message.local_media_paths = json.dumps(paths)
                        fixed_count += 1
                
                db.session.commit()
                flash(f"Fixed {fixed_count} messages with {len(files)} total files!", "success")
            else:
                flash("Upload folder not found!", "danger")
                
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {str(e)}", "danger")
            app.logger.error(f"Fix paths error: {e}")
        
        return redirect(url_for('fix_database_paths'))
    
    # GET - show info
    upload_folder = app.config.get("UPLOAD_FOLDER", "/app/static/uploads")
    file_count = 0
    empty_path_count = Message.query.filter(
        (Message.local_media_paths == "") | (Message.local_media_paths.is_(None))
    ).count()
    
    if os.path.exists(upload_folder):
        file_count = len(os.listdir(upload_folder))
    
    return f"""
    <html>
    <head><title>Fix Database Paths</title></head>
    <body style="font-family: sans-serif; padding: 20px;">
        <h2>Fix Database Paths</h2>
        <p>Files in volume: <strong>{file_count}</strong></p>
        <p>Messages with empty paths: <strong>{empty_path_count}</strong></p>
        
        <form method="POST">
            <button type="submit" style="padding: 10px 20px; font-size: 16px;">
                Fix Database Paths
            </button>
        </form>
        
        <p><a href="/">Back to Home</a></p>
    </body>
    </html>
    """

@app.route('/vendor/<int:vendor_id>/add-comment', methods=['POST'])
def vendor_add_comment(vendor_id):
    """Add a comment to a vendor"""
    vendor = Vendor.query.get_or_404(vendor_id)
    comment_text = request.form.get('comment', '').strip()
    
    if not comment_text:
        flash('Comment cannot be empty', 'error')
        return redirect(url_for('vendor_detail', vendor_id=vendor_id))
    
    try:
        comment = VendorComment(
            vendor_id=vendor_id,
            comment=comment_text
        )
        db.session.add(comment)
        db.session.commit()
        flash('Comment added successfully', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error adding comment: {str(e)}', 'error')
    
    return redirect(url_for('vendor_detail', vendor_id=vendor_id))


@app.route('/vendor/comment/<int:comment_id>/delete', methods=['POST'])
def vendor_delete_comment(comment_id):
    """Delete a vendor comment"""
    comment = VendorComment.query.get_or_404(comment_id)
    vendor_id = comment.vendor_id
    
    try:
        db.session.delete(comment)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route("/debug/mismatch")
def debug_mismatch():
    """Show mismatch between database and volume files"""
    import os
    import json
    import re
    
    upload_folder = app.config.get("UPLOAD_FOLDER", "/app/static/uploads")
    
    # Get files in volume
    volume_files = set()
    volume_msg_ids = set()
    if os.path.exists(upload_folder):
        for filename in os.listdir(upload_folder):
            volume_files.add(filename)
            # Extract message ID from filename
            match = re.match(r'msg(\d+)_', filename)
            if match:
                volume_msg_ids.add(int(match.group(1)))
    
    # Get expected files from database
    db_files = set()
    db_msg_ids = set()
    messages = Message.query.filter(
        Message.local_media_paths.isnot(None),
        Message.local_media_paths != '',
        Message.local_media_paths != '[]'
    ).limit(20).all()
    
    for msg in messages:
        db_msg_ids.add(msg.id)
        try:
            if msg.local_media_paths.startswith('['):
                paths = json.loads(msg.local_media_paths)
            else:
                paths = [msg.local_media_paths]
            
            for path in paths:
                if path:
                    # Extract filename from path
                    filename = path.replace('uploads/', '')
                    db_files.add(filename)
        except:
            pass
    
    results = {
        "volume_file_count": len(volume_files),
        "volume_msg_ids": sorted(list(volume_msg_ids))[:20],
        "db_expecting_count": len(db_files),
        "db_msg_ids": sorted(list(db_msg_ids))[:20],
        "sample_volume_files": sorted(list(volume_files))[:10],
        "sample_db_expected": sorted(list(db_files))[:10],
        "overlapping_msg_ids": sorted(list(volume_msg_ids & db_msg_ids))[:10]
    }
    
    return f"<pre>{json.dumps(results, indent=2)}</pre>"

@app.route("/admin/redownload-from-google", methods=["GET", "POST"])
def redownload_from_google():
    """Re-download images from Google Storage URLs"""
    import json
    import requests
    
    if request.method == "POST":
        upload_folder = app.config.get("UPLOAD_FOLDER", "/app/static/uploads")
        os.makedirs(upload_folder, exist_ok=True)
        
        success_count = 0
        fail_count = 0
        
        try:
            # Get messages with Google URLs but no local paths
            messages = Message.query.filter(
                Message.media_urls.isnot(None),
                Message.media_urls != '',
                Message.media_urls != '[]'
            ).limit(50).all()  # Start with 50
            
            for msg in messages:
                try:
                    # Parse media URLs
                    if msg.media_urls.startswith('['):
                        urls = json.loads(msg.media_urls)
                    else:
                        urls = [msg.media_urls]
                    
                    local_paths = []
                    
                    for i, url in enumerate(urls):
                        if not url or not url.startswith('http'):
                            continue
                        
                        # Generate filename
                        ext = 'jpg'
                        if '.png' in url.lower():
                            ext = 'png'
                        elif '.gif' in url.lower():
                            ext = 'gif'
                        
                        filename = f"msg{msg.id}_{i+1}_{msg.id:08x}.{ext}"
                        filepath = os.path.join(upload_folder, filename)
                        
                        # Download if not exists
                        if not os.path.exists(filepath):
                            response = requests.get(url, timeout=30)
                            if response.status_code == 200:
                                with open(filepath, 'wb') as f:
                                    f.write(response.content)
                                success_count += 1
                            else:
                                fail_count += 1
                                continue
                        
                        local_paths.append(f"uploads/{filename}")
                    
                    # Update database
                    if local_paths:
                        msg.local_media_paths = json.dumps(local_paths)
                        
                except Exception as e:
                    app.logger.error(f"Error processing msg {msg.id}: {e}")
                    fail_count += 1
            
            db.session.commit()
            flash(f"Downloaded {success_count} images, {fail_count} failed", "success")
            
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {str(e)}", "danger")
        
        return redirect(url_for('redownload_from_google'))
    
    # GET - show info
    google_url_count = Message.query.filter(
        Message.media_urls.isnot(None),
        Message.media_urls != '',
        Message.media_urls != '[]'
    ).count()
    
    return f"""
    <html>
    <head><title>Re-download from Google</title></head>
    <body style="font-family: sans-serif; padding: 20px;">
        <h2>Re-download Images from Google Storage</h2>
        <p>Messages with Google URLs: <strong>{google_url_count}</strong></p>
        
        <form method="POST">
            <button type="submit" style="padding: 10px 20px; font-size: 16px;">
                Re-download First 50 Images
            </button>
        </form>
        
        <p style="color: #666;">This will download images from Google Storage URLs and save them locally.</p>
        <p><a href="/">Back to Home</a></p>
    </body>
    </html>
    """

# Register webhook blueprint
app.register_blueprint(webhook_bp)

# Helper functions for notifications
def send_email(to_emails, subject, html_content, attachments=None):
    """Send email using SendGrid - placeholder implementation"""
    app.logger.info(f"send_email called: {to_emails}, {subject}")
    try:
        # Add your SendGrid implementation here
        return True  # Placeholder - replace with actual implementation
    except Exception as e:
        app.logger.error(f"Email send error: {e}")
        return False

def send_openphone_sms(recipient_phone, message_body):
    """Send SMS using OpenPhone API - placeholder implementation"""
    app.logger.info(f"send_openphone_sms called: {recipient_phone}, {message_body}")
    try:
        # Add your OpenPhone implementation here
        return True  # Placeholder - replace with actual implementation
    except Exception as e:
        app.logger.error(f"SMS send error: {e}")
        return False

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