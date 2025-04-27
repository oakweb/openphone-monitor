# models.py
# Corrected and Consolidated Version

from extensions import db
from datetime import datetime


# Defines the 'contacts' table
class Contact(db.Model):
    __tablename__ = "contacts"
    # Use the normalized 10-digit phone number as the unique ID
    phone_number = db.Column(db.String(20), primary_key=True)
    # Name assigned by the user
    contact_name = db.Column(db.String(100), nullable=True)
    # Optional: Track when contact was created
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship to easily get all messages for this contact
    # 'messages' is the attribute name on the Contact object
    # 'contact' is the attribute name created on the Message object
    messages = db.relationship(
        "Message",
        backref="contact",
        lazy=True,
        foreign_keys="Message.phone_number",
    )

    def __repr__(self):
        return f"<Contact {self.contact_name} ({self.phone_number})>"


# Defines the 'properties' table
class Property(db.Model):
    __tablename__ = "properties"
    id = db.Column(db.Integer, primary_key=True)
    # Unique SID for deduplication of incoming webhooks
    sid = db.Column(db.String, unique=True, nullable=False, index=True)

    # Name of the property (e.g., address) - must be unique
    name = db.Column(db.String(200), nullable=False, unique=True)
    # Optional: Track when property was added
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship defined via backref in Message model ('property.messages')

    def __repr__(self):
        return f"<Property {self.name}>"


# Defines the 'messages' table
class Message(db.Model):
    __tablename__ = "messages"

    # Primary key; let Postgres auto-increment
    id = db.Column(db.Integer, primary_key=True)

    # Unique SID for deduplication of incoming webhooks
    sid = db.Column(db.String, unique=True, nullable=False, index=True)

    # Link back to Contact by normalized phone number
    phone_number = db.Column(
        db.String(20),
        db.ForeignKey("contacts.phone_number"),
        nullable=False,
        index=True,
    )
    contact_name = db.Column(db.String(100), nullable=True)

    # incoming / outgoing
    direction = db.Column(db.String(10), nullable=False, index=True)

    # Message body and any media URLs from the provider
    message = db.Column(db.Text, nullable=True)
    media_urls = db.Column(db.Text, nullable=True)  # Comma-separated external URLs

    # When we received/sent it
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Optional link to a Property
    property_id = db.Column(
        db.Integer, db.ForeignKey("properties.id"), nullable=True, index=True
    )
    property = db.relationship(
        "Property", backref=db.backref("messages", lazy="dynamic")
    )

    # Local paths of downloaded media (relative to `static/uploads`)
    local_media_paths = db.Column(
        db.Text, nullable=True
    )  # Comma-separated local file paths

    def __repr__(self):
        return f"<Message {self.id} from {self.contact_name or self.phone_number}>"
