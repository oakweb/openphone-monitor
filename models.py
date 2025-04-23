# models.py
# Corrected and Consolidated Version

from extensions import db
from datetime import datetime

# Defines the 'contacts' table
class Contact(db.Model):
    __tablename__ = 'contacts'
    # Use the normalized 10-digit phone number as the unique ID
    phone_number = db.Column(db.String(20), primary_key=True)
    # Name assigned by the user
    contact_name = db.Column(db.String(100), nullable=True) # Allow null if added before name known
    # Optional: Track when contact was created
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship to easily get all messages for this contact
    # 'messages' is the attribute name on the Contact object
    # 'contact' is the attribute name created on the Message object
    messages = db.relationship('Message', backref='contact', lazy=True, foreign_keys='Message.phone_number')

    def __repr__(self):
        return f"<Contact {self.contact_name} ({self.phone_number})>"

# Defines the 'properties' table
class Property(db.Model):
    __tablename__ = 'properties'
    id = db.Column(db.Integer, primary_key=True)
    # Name of the property (e.g., address) - must be unique
    name = db.Column(db.String(200), nullable=False, unique=True)
    # Optional: Track when property was added
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship defined via backref in Message model ('property.messages')

    def __repr__(self):
        return f"<Property {self.name}>"

# Defines the 'messages' table
class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    phone_number = db.Column(db.String(20), db.ForeignKey('contacts.phone_number'), nullable=False, index=True)
    contact_name = db.Column(db.String(100), nullable=True)
    direction = db.Column(db.String(10), nullable=False, index=True)
    message = db.Column(db.Text, nullable=True)
    media_urls = db.Column(db.Text, nullable=True) # Original external URLs
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    property_id = db.Column(db.Integer, db.ForeignKey('properties.id'), nullable=True, index=True)
    property = db.relationship('Property', backref=db.backref('messages', lazy='dynamic'))

    # --- <<< ADD THIS LINE >>> ---
    local_media_paths = db.Column(db.Text, nullable=True) # Comma-separated relative paths (e.g., uploads/img1.jpg,uploads/img2.png)
    # --- <<< END OF ADDITION >>> ---

    def __repr__(self):
        return f"<Message {self.id} from {self.contact_name or self.phone_number}>"
