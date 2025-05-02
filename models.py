# models.py
# Revised Version with Tenant and NotificationHistory

from extensions import db
from datetime import datetime

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


# Defines the 'properties' table
class Property(db.Model):
    __tablename__ = "properties"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True) # Address or Name
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- NEW/REVISED FIELDS ---
    hoa_name = db.Column(db.String(150), nullable=True)
    hoa_contact_info = db.Column(db.Text, nullable=True) # Store phone/email/website etc.
    access_notes = db.Column(db.Text, nullable=True) # Gate codes, alarm info brief, etc.
    # Utility account numbers could go here if simple, or separate model later
    # electric_account = db.Column(db.String(50), nullable=True)
    # water_account = db.Column(db.String(50), nullable=True)

    # Relationship to Tenants (One Property -> Many Tenants) defined via Tenant.property backref

    # Relationship to Messages (defined via Message.property backref)

    def __repr__(self):
        return f"<Property {self.name}>"


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