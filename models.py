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