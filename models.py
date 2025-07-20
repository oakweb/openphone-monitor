# models.py
# Revised Version with Tenant and NotificationHistory

from sqlalchemy import Numeric
from extensions import db
from datetime import datetime, timezone, timedelta

# Association Table (If deciding later on Many-to-Many for Property<->Contact)
# property_contacts = db.Table('property_contacts',
#     db.Column('property_id', db.Integer, db.ForeignKey('properties.id'), primary_key=True),
#     db.Column('contact_phone_number', db.String(20), db.ForeignKey('contacts.phone_number'), primary_key=True)
# )

# Defines the 'contacts' table (for incoming/general contacts)
class Contact(db.Model):
    # Keep this model as it was (unless merging with Tenant later)
    __tablename__ = "contacts"
    phone_number = db.Column(db.String(20), primary_key=True)
    contact_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship(
        "Message",
        backref="contact",
        lazy=True,
        foreign_keys="Message.phone_number",
        passive_deletes=True
    )
    # Consider adding 'contact_type' (e.g., 'Incoming', 'Tenant', 'Neighbor', 'Manual') if merging later

    def __repr__(self):
        return f"<Contact {self.contact_name} ({self.phone_number})>"


# Defines the 'properties' table - ENHANCED VERSION
class Property(db.Model):
    __tablename__ = "properties"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True) # Address or Name
    address = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Property management details
    hoa_name = db.Column(db.String(200))
    hoa_phone = db.Column(db.String(15))
    hoa_email = db.Column(db.String(200))
    hoa_website = db.Column(db.String(200))
    hoa_contact_info = db.Column(db.Text, nullable=True) # Store additional phone/email/website etc.
    
    # Neighbor information
    neighbor_name = db.Column(db.String(200))
    neighbor_phone = db.Column(db.String(15))
    neighbor_email = db.Column(db.String(200))
    neighbor_notes = db.Column(db.Text)
    
    # Financial information
    year_purchased = db.Column(db.Integer)
    purchase_amount = db.Column(db.Numeric(12, 2))
    redfin_current_value = db.Column(db.Numeric(12, 2))
    monthly_rent = db.Column(db.Numeric(10, 2))
    property_taxes = db.Column(db.Numeric(10, 2))
    
    # Property details
    bedrooms = db.Column(db.Integer)
    bathrooms = db.Column(db.Numeric(3, 1))  # Allows 2.5 baths
    square_feet = db.Column(db.Integer)
    lot_size = db.Column(db.String(50))
    
    # Management notes
    notes = db.Column(db.Text)
    maintenance_notes = db.Column(db.Text)
    tenant_notes = db.Column(db.Text)
    access_notes = db.Column(db.Text, nullable=True) # Gate codes, alarm info brief, etc.
    
    # Key information
    lockbox_code = db.Column(db.String(20))
    garage_code = db.Column(db.String(20))
    wifi_network = db.Column(db.String(100))
    wifi_password = db.Column(db.String(100))
    
    # Metadata
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationship to Tenants (One Property -> Many Tenants) defined via Tenant.property backref
    # Relationship to Messages (defined via Message.property backref)

    def __repr__(self):
        return f"<Property {self.name}>"
    
    @property
    def current_tenants(self):
        """Get current tenants for this property"""
        return self.tenants.filter_by(status='current').all()
    
    @property
    def media_count(self):
        """Count of messages with media for this property"""
        return self.messages.filter(Message.local_media_paths.isnot(None)).count()
    
    @property
    def recent_messages_count(self):
        """Count of messages in last 30 days"""
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        return self.messages.filter(Message.timestamp >= thirty_days_ago).count()


# --- NEW TENANT MODEL ---
class Tenant(db.Model):
    __tablename__ = "tenants"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), nullable=True, index=True)
    phone = db.Column(db.String(25), nullable=True, index=True) # Store E.164 format like +1...
    status = db.Column(db.String(20), default='current', nullable=False, index=True) # 'current', 'vacated'
    move_in_date = db.Column(db.Date, nullable=True)
    lease_start_date = db.Column(db.Date, nullable=True)
    lease_end_date = db.Column(db.Date, nullable=True)
    rent_due_day = db.Column(db.Integer, nullable=True) # Day of month (1-31)
    pets_info = db.Column(db.Text, nullable=True) # Describe pets
    notes = db.Column(db.Text, nullable=True) # General notes about tenant
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    vacated_at = db.Column(db.DateTime, nullable=True) # Set when status changes to 'vacated'

    # Foreign Key to link Tenant to a Property
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False, index=True)

    # Relationship back to Property
    property = db.relationship("Property", backref=db.backref("tenants", lazy="dynamic", order_by='Tenant.name')) # 'dynamic' good for many tenants

    def __repr__(self):
        return f"<Tenant {self.name} (Property: {self.property_id}, Status: {self.status})>"


# --- NEW NOTIFICATION HISTORY MODEL ---
class NotificationHistory(db.Model):
    __tablename__ = "notification_history"
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    subject = db.Column(db.String(255), nullable=True) # Email subject
    body = db.Column(db.Text, nullable=False) # Message content
    channels = db.Column(db.String(50), nullable=False) # e.g., "Email", "SMS", "Email, SMS"
    status = db.Column(db.String(50), default='Sent', nullable=False) # e.g., "Sent", "Partial Failure", "Failed"
    properties_targeted = db.Column(db.Text, nullable=True) # e.g., "IDs: 1, 5, 12" or "All"
    recipients_summary = db.Column(db.Text, nullable=True) # e.g., "5 emails, 4 SMS to 6 tenants"
    error_info = db.Column(db.Text, nullable=True) # Store errors if status is not 'Sent'

    def __repr__(self):
        return f"<NotificationHistory {self.id} ({self.timestamp.strftime('%Y-%m-%d %H:%M')}) - Status: {self.status}>"


# Defines the 'messages' table (Keep as is, relationship to Contact already defined)
class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String, unique=True, nullable=True, index=True) # Allow NULL
    phone_number = db.Column(db.String(20), db.ForeignKey("contacts.phone_number"), nullable=False, index=True)
    contact_name = db.Column(db.String(100), nullable=True)
    direction = db.Column(db.String(10), nullable=False, index=True)
    message = db.Column(db.Text, nullable=True)
    media_urls = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=True, index=True)
    property = db.relationship("Property", backref=db.backref("messages", lazy="dynamic")) # Relationship to Property
    local_media_paths = db.Column(db.Text, nullable=True)
    # Contact relationship defined via Contact.messages backref

    def __repr__(self):
        return f"<Message {self.id} from {self.contact_name or self.phone_number}>"


# ====== NEW MODELS FOR FLEXIBLE PROPERTY INFORMATION ======

class PropertyCustomField(db.Model):
    """Flexible key-value storage for property-specific information"""
    __tablename__ = 'property_custom_fields'
    
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('properties.id', ondelete='CASCADE'), nullable=False)
    category = db.Column(db.String(100), nullable=False)  # e.g., 'HOA', 'Tenant Info', 'Access', 'Neighbor'
    field_name = db.Column(db.String(200), nullable=False)  # e.g., 'Account Number', 'Entry Checklist'
    field_value = db.Column(db.Text)
    field_type = db.Column(db.String(50), default='text')  # 'text', 'number', 'date', 'url', 'phone', 'email'
    is_encrypted = db.Column(db.Boolean, default=False)  # For sensitive data
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    created_by = db.Column(db.String(100))  # Track who added this
    
    # Relationship
    property = db.relationship('Property', backref=db.backref('custom_fields', lazy='dynamic', cascade='all, delete-orphan'))
    
    __table_args__ = (
        db.UniqueConstraint('property_id', 'category', 'field_name', name='_property_category_field_uc'),
    )

    def __repr__(self):
        return f"<PropertyCustomField {self.field_name}: {self.field_value} (Property: {self.property_id})>"


class PropertyAttachment(db.Model):
    """Store file attachments for properties"""
    __tablename__ = 'property_attachments'
    
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('properties.id', ondelete='CASCADE'), nullable=False)
    category = db.Column(db.String(100), nullable=False)  # e.g., 'HOA Documents', 'Lease', 'Maintenance', 'Photos'
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer)  # in bytes
    file_type = db.Column(db.String(100))  # MIME type
    description = db.Column(db.Text)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    uploaded_by = db.Column(db.String(100))
    
    # Relationship
    property = db.relationship('Property', backref=db.backref('attachments', lazy='dynamic', cascade='all, delete-orphan'))

    def __repr__(self):
        return f"<PropertyAttachment {self.original_filename} (Property: {self.property_id})>"


class PropertyContact(db.Model):
    """Store important contacts for each property"""
    __tablename__ = 'property_contacts'
    
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('properties.id', ondelete='CASCADE'), nullable=False)
    contact_type = db.Column(db.String(100), nullable=False)  # 'HOA', 'Neighbor', 'Vendor', 'Emergency', 'Utility'
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(200))
    company = db.Column(db.String(200))
    role = db.Column(db.String(100))  # e.g., 'President', 'Property Manager', 'Plumber'
    notes = db.Column(db.Text)
    is_primary = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    
    # Relationship
    property = db.relationship('Property', backref=db.backref('contacts', lazy='dynamic', cascade='all, delete-orphan'))

    def __repr__(self):
        return f"<PropertyContact {self.name} - {self.contact_type} (Property: {self.property_id})>"


# ====== VENDOR MANAGEMENT MODELS ======

class Vendor(db.Model):
    """Vendor information and profile"""
    __tablename__ = "vendors"
    
    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.String(20), db.ForeignKey('contacts.phone_number'), unique=True, nullable=False)
    company_name = db.Column(db.String(200))
    vendor_type = db.Column(db.String(100))  # Plumber, Electrician, Landscaper, etc.
    status = db.Column(db.String(20), default='active')  # active/inactive
    email = db.Column(db.String(200))
    license_number = db.Column(db.String(100))
    insurance_info = db.Column(db.Text)
    insurance_expiry = db.Column(db.Date)
    hourly_rate = db.Column(db.Numeric(10,2))
    rating = db.Column(db.Numeric(2,1))  # 1.0-5.0 stars
    notes = db.Column(db.Text)
    preferred_payment_method = db.Column(db.String(50))  # cash, check, venmo, zelle, etc.
    tax_id = db.Column(db.String(50))
    can_text = db.Column(db.Boolean, default=True)  # Whether vendor accepts text messages
    can_email = db.Column(db.Boolean, default=True)  # Whether vendor accepts emails
    example_invoice_path = db.Column(db.String(500))  # Path to uploaded example invoice
    fax_number = db.Column(db.String(20))  # Fax number extracted from invoice or manually added
    
    # Address fields
    phone = db.Column(db.String(20))  # Business phone number
    address = db.Column(db.String(200))  # Street address
    city = db.Column(db.String(100))
    state = db.Column(db.String(50))
    zip_code = db.Column(db.String(20))
    
    # Additional fields
    aka_business_name = db.Column(db.String(200))  # Also Known As / Alternative business name
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    contact = db.relationship('Contact', backref=db.backref('vendor_profile', uselist=False))
    jobs = db.relationship('VendorJob', backref='vendor', lazy='dynamic', cascade='all, delete-orphan')
    comments = db.relationship('VendorComment', backref='vendor', lazy='dynamic', cascade='all, delete-orphan', order_by='VendorComment.created_at.desc()')
    
    @property
    def total_jobs(self):
        """Total number of jobs for this vendor"""
        return self.jobs.count()
    
    @property
    def completed_jobs(self):
        """Number of completed jobs"""
        return self.jobs.filter_by(status='completed').count()
    
    @property
    def total_revenue(self):
        """Total revenue from this vendor"""
        return db.session.query(db.func.sum(VendorJob.cost)).filter(
            VendorJob.vendor_id == self.id,
            VendorJob.status == 'completed'
        ).scalar() or 0
    
    @property
    def average_job_rating(self):
        """Average rating across all jobs"""
        avg = db.session.query(db.func.avg(VendorJob.rating)).filter(
            VendorJob.vendor_id == self.id,
            VendorJob.rating.isnot(None)
        ).scalar()
        return round(avg, 1) if avg else None
    
    def __repr__(self):
        return f"<Vendor {self.company_name or self.contact.contact_name}>"


class VendorInvoiceData(db.Model):
    """Store extracted data from vendor invoices"""
    __tablename__ = "vendor_invoice_data"
    
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendors.id'), nullable=False)
    field_name = db.Column(db.String(100), nullable=False)  # e.g., 'business_license', 'insurance_policy'
    field_value = db.Column(db.Text)
    field_type = db.Column(db.String(50), default='text')  # text, number, date, email, phone
    confidence = db.Column(db.Numeric(3,2))  # 0.00-1.00 confidence score
    extracted_at = db.Column(db.DateTime, default=datetime.utcnow)
    source = db.Column(db.String(200))  # filename that data was extracted from
    
    # Relationship
    vendor = db.relationship('Vendor', backref=db.backref('invoice_data', lazy='dynamic', cascade='all, delete-orphan'))
    
    __table_args__ = (
        db.UniqueConstraint('vendor_id', 'field_name', name='_vendor_field_uc'),
    )
    
    def __repr__(self):
        return f"<VendorInvoiceData {self.field_name}: {self.field_value}>"


class VendorJob(db.Model):
    """Track vendor work at properties"""
    __tablename__ = "vendor_jobs"
    
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendors.id'), nullable=False)
    property_id = db.Column(db.Integer, db.ForeignKey('properties.id'), nullable=False)
    job_date = db.Column(db.Date, default=datetime.utcnow)
    job_type = db.Column(db.String(100))  # Repair, Maintenance, Installation, etc.
    job_description = db.Column(db.Text)
    cost = db.Column(db.Numeric(10,2))
    status = db.Column(db.String(50), default='scheduled')  # scheduled, in-progress, completed, cancelled
    invoice_number = db.Column(db.String(100))
    payment_status = db.Column(db.String(50), default='pending')  # pending, paid, partial
    notes = db.Column(db.Text)
    rating = db.Column(db.Integer)  # 1-5 stars for this specific job
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    # Message thread tracking
    message_thread_start = db.Column(db.DateTime)
    message_thread_end = db.Column(db.DateTime)
    
    # Relationships
    property = db.relationship('Property', backref='vendor_jobs')
    
    def __repr__(self):
        return f"<VendorJob {self.vendor.company_name} at {self.property.name} - {self.status}>"


class VendorComment(db.Model):
    """Comments/notes for vendors with timestamps"""
    __tablename__ = "vendor_comments"
    
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendors.id'), nullable=False)
    comment = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by = db.Column(db.String(100))  # Track who added the comment (future use)
    
    def __repr__(self):
        return f"<VendorComment for vendor {self.vendor_id} at {self.created_at}>"