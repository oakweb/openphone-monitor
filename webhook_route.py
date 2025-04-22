from flask import Blueprint, request, Response
from extensions import db
from models import Contact, Message
from email_utils import send_email # Import the updated send_email
import requests
import traceback
import base64
from datetime import datetime
import os # <<< ADD THIS LINE >>>

# <<< CHANGE >>> Use __name__ for Blueprint initialization
webhook_bp = Blueprint('webhook', __name__)

@webhook_bp.route('/webhook', methods=['POST'])
def webhook():
    try:
        print("📥 Webhook triggered")

        # Use force=True cautiously, but okay if you trust the source
        # Consider adding more robust JSON validation if needed
        data = request.get_json(force=True)
        if not data:
             print("❌ Empty payload or not JSON")
             return Response("Bad Request: Payload missing or not JSON.", status=400)

        print(f"🔹 Payload received: {data}") # Be careful logging full payload in production

        # Safely get nested data
        event_type = data.get("type", "unknown_event")
        obj = data.get("data", {}).get("object", {})

        # Determine direction and extract common fields
        direction = "incoming" if "received" in event_type else "outgoing"
        phone = obj.get("from") if direction == "incoming" else obj.get("to")
        text = obj.get("body", "")
        media_items = obj.get("media") or [] # Ensure media_items is always a list

        print(f"🔹 Direction: {direction}, Phone: {phone}")
        print(f"🔹 Text: '{text}'") # Log text content
        print(f"🔹 Media Items Count: {len(media_items)}") # Log count instead of full list initially

        if not phone:
            print("❌ Missing phone number in payload object")
            return Response("Bad Request: Missing phone number.", status=400)

        # Normalize phone number to last 10 digits for use as key
        try:
            key = ''.join(filter(str.isdigit, phone))[-10:]
            if len(key) < 10:
                 print(f"⚠️ Phone number '{phone}' resulted in short key '{key}'. Using original.")
                 key = phone # Fallback or handle as needed
        except Exception as e:
             print(f"⚠️ Error normalizing phone number '{phone}': {e}. Using original.")
             key = phone # Fallback

        print(f"🔎 Normalized phone key for DB lookup: {key}")

        # Find or create contact
        contact = Contact.query.get(key)
        if not contact:
            print(f"➕ Creating new contact for key: {key}")
            # Use the original phone number for display name initially
            contact = Contact(phone_number=key, contact_name=phone)
            db.session.add(contact)
            # Commit here to ensure contact exists before message is added,
            # especially if message needs contact_id (though not directly used here)
            try:
                db.session.commit()
                print(f"✅ Contact '{contact.contact_name}' ({contact.phone_number}) created.")
            except Exception as e:
                 db.session.rollback()
                 print(f"❌ Error saving new contact: {e}")
                 traceback.print_exc()
                 return Response("Internal Server Error: Could not save contact", status=500)
        else:
            print(f"✅ Found existing contact: {contact.contact_name} ({contact.phone_number})")


        # Extract media URLs
        urls = [m.get('url') for m in media_items if isinstance(m, dict) and m.get('url')]
        print(f"📎 Media URLs found: {urls}")

        # Create and save the message
        msg = Message(
            phone_number=key, # Store the normalized key
            contact_name=contact.contact_name, # Use name from contact record
            direction=direction,
            message=text,
            media_urls=','.join(urls) if urls else None, # Store URLs comma-separated, or None
            timestamp=datetime.utcnow()
        )
        db.session.add(msg)
        try:
            db.session.commit()
            print("✅ Message saved to DB")
        except Exception as e:
            db.session.rollback()
            print(f"❌ Error saving message: {e}")
            traceback.print_exc()
            return Response("Internal Server Error: Could not save message", status=500)

        # If incoming, fetch media and send email notification
        if direction == "incoming":
            attachments_for_email = []
            for url in urls:
                try:
                    print(f"⬇️ Fetching media: {url}")
                    r = requests.get(url, timeout=20) # Increased timeout slightly
                    r.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

                    # Get filename more robustly
                    content_disp = r.headers.get('Content-Disposition')
                    filename = None
                    if content_disp:
                        # Simple parse, might need refinement for complex headers
                        parts = content_disp.split('filename=')
                        if len(parts) > 1:
                           filename = parts[1].strip('"\' ')
                    if not filename:
                         # Fallback to URL parsing
                         filename = url.split("/")[-1].split("?")[0] or "attachment" # Ensure filename exists

                    content_type = r.headers.get("Content-Type", "application/octet-stream")
                    content_b64 = base64.b64encode(r.content).decode('utf-8') # Ensure utf-8 decoding

                    attachments_for_email.append({
                        "content": content_b64,
                        "type": content_type,
                        "filename": filename,
                        "disposition": "attachment", # For SendGrid
                        "content_id": None # For SendGrid inline images if needed later
                    })
                    print(f"📎 Attachment prepared: {filename} ({content_type})")
                except requests.exceptions.RequestException as err:
                    print(f"⚠️ Media fetch failed for {url}: {err}")
                except Exception as err:
                    print(f"⚠️ Unexpected error processing media {url}: {err}")
                    traceback.print_exc() # Log unexpected errors during attachment prep

            # Prepare email content
            subject = f"New message from {contact.contact_name}"
            plain_text_content = f"From: {contact.contact_name} ({phone})\n\n{text}"
            html_content = f"<p><b>From:</b> {contact.contact_name} ({phone})</p><hr><p>{text.replace(chr(10), '<br>')}</p>" # Basic HTML formatting

            try:
                print(f"📧 Attempting to send email notification to {os.getenv('SMTP_TO') or os.getenv('SENDGRID_TO_EMAIL', 'default_recipient@example.com')}") # Log recipient address source
                # <<< CHANGE >>> Call the updated send_email function
                # Make sure you have a recipient email address configured in your env vars
                # (e.g., SMTP_TO or SENDGRID_TO_EMAIL)
                recipient = os.getenv('SMTP_TO') or os.getenv('SENDGRID_TO_EMAIL')
                if recipient:
                     send_email(
                         to_address=recipient,
                         subject=subject,
                         html_content=html_content,
                         plain_content=plain_text_content, # Pass plain text too
                         attachments=attachments_for_email # Pass prepared attachments
                     )
                     print("✅ Email notification sent successfully.")
                else:
                     print("⚠️ Email notification skipped: Recipient address not configured (SMTP_TO or SENDGRID_TO_EMAIL missing).")

            except Exception as e:
                 print(f"❌ Failed to send email notification: {e}")
                 traceback.print_exc() # Log email sending error but don't fail the webhook

        return Response("✅ Webhook processed successfully.", status=200)

    except Exception as e:
        # Catch-all for any unexpected error during webhook processing
        print(f"❌ Unhandled error in webhook: {e}")
        traceback.print_exc()
        # Return 500 for internal errors
        return Response("Internal Server Error", status=500)