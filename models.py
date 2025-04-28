# models.py
# Corrected and Consolidated Version - WITH suggested edit

from extensions import db
from datetime import datetime


# Defines the 'contacts' table
class Contact(db.Model):
    __tablename__ = "contacts"
    # Use the normalized 10-digit phone number as the unique ID
    # Increased length slightly just in case, but 10 should be primary target.
    phone_number = db.Column(db.String(20), primary_key=True)
    # Name assigned by the user
    contact_name = db.Column(db.String(100), nullable=True)
    # Optional: Track when contact was created
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship to easily get all messages for this contact
    # 'messages' is the attribute name on the Contact object
    # 'contact' is the attribute name created on the Message object via backref
    messages = db.relationship(
        "Message",
        backref="contact", # Creates Message.contact
        lazy=True,
        foreign_keys="Message.phone_number",
        # --- SUGGESTED EDIT ---
        # This tells SQLAlchemy NOT to try and NULL out Message.phone_number
        # during the flush before deleting a Contact. It relies on the DB's
        # foreign key constraint behavior OR our manual update logic.
        passive_deletes=True
        # --- END SUGGESTED EDIT ---
    )

    def __repr__(self):
        return f"<Contact {self.contact_name} ({self.phone_number})>"


# Defines the 'properties' table
class Property(db.Model):
    __tablename__ = "properties"
    id = db.Column(db.Integer, primary_key=True)
    # Name of the property (e.g., address) - must be unique
    name = db.Column(db.String(200), nullable=False, unique=True)
    # Optional: Track when property was added
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship defined via backref in Message model ('property.messages')
    # No changes needed here based on current errors.

    def __repr__(self):
        return f"<Property {self.name}>"


# Defines the 'messages' table
class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    # Unique SID from provider (e.g., Twilio, OpenPhone)
    sid = db.Column(db.String, unique=True, nullable=True, index=True) # Allow NULL if not always present

    # Foreign key link back to Contact by normalized phone number
    # Type matches Contact.phone_number (String)
    phone_number = db.Column(
        db.String(20), # Matches Contact key type/length
        db.ForeignKey("contacts.phone_number"), # Correct FK definition
        nullable=False, # Correctly NOT NULL (matches DB constraint)
        index=True,
    )
    # Contact name stored at the time of message (denormalized for history)
    contact_name = db.Column(db.String(100), nullable=True)

    # incoming / outgoing
    direction = db.Column(db.String(10), nullable=False, index=True)

    # Message content
    message = db.Column(db.Text, nullable=True)
    # Comma-separated list of URLs provided by the messaging service
    media_urls = db.Column(db.Text, nullable=True)

    # Timestamp (ideally UTC)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Optional link to a Property
    property_id = db.Column(
        db.Integer, db.ForeignKey("properties.id"), nullable=True, index=True
    )
    # Relationship back to Property
    property = db.relationship(
        "Property", backref=db.backref("messages", lazy="dynamic")
    )

    # Comma-separated list of relative paths to downloaded media files
    # Stored relative to the app's static folder (e.g., 'uploads/filename.jpg')
    local_media_paths = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<Message {self.id} from {self.contact_name or self.phone_number}>"