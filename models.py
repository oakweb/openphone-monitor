from extensions import db
from datetime import datetime

class Contact(db.Model):
    # <<< CHANGE >>> Use __tablename__ (double underscore)
    __tablename__ = 'contacts'
    # Phone number (normalized 10 digits) used as primary key
    phone_number = db.Column(db.String(20), primary_key=True) # Increased length just in case
    # Display name for the contact (can be updated)
    contact_name = db.Column(db.String(100), nullable=False)

    # Optional: Add a relationship to messages
    # messages = db.relationship('Message', backref='contact', lazy=True)

    def __repr__(self):
        return f"<Contact {self.contact_name} ({self.phone_number})>"


class Message(db.Model):
    # <<< CHANGE >>> Use __tablename__ (double underscore)
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True) # Index for sorting
    # Store normalized phone number, consider adding a foreign key to contacts
    phone_number = db.Column(db.String(20), nullable=False, index=True) # Index for lookups
    # Store contact name at the time of message (denormalized) or use relationship
    contact_name = db.Column(db.String(100), nullable=True)
    # Direction ('incoming' or 'outgoing')
    direction = db.Column(db.String(10), nullable=False, index=True) # Index for filtering
    message = db.Column(db.Text, nullable=True) # Message body can be empty (e.g., media only)
    # Store media URLs as comma-separated text, or use a separate related table for media
    media_urls = db.Column(db.Text, nullable=True)

    # Optional: Foreign Key to Contact model
    # contact_phone_number = db.Column(db.String(20), db.ForeignKey('contacts.phone_number'), nullable=False)

    def __repr__(self):
        return f"<Message ID: {self.id} From: {self.phone_number} At: {self.timestamp}>"