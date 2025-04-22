# --- Imports ---
from flask import Blueprint, request, Response
from extensions import db
from models import Contact, Message
from email_utils import send_email # Assuming this handles email sending
import requests
import traceback
import base64
from datetime import datetime
import os                      # Needed for path joining, makedirs, remove, basename
from urllib.parse import urlparse # To help get original filename
# --- Make sure 'from mega import Mega' is DELETED ---

# Initialize Blueprint
webhook_bp = Blueprint('webhook', __name__)

@webhook_bp.route('/webhook', methods=['POST'])
def webhook():
    # Use 4-space indentation consistently
    try:
        print("📥 Webhook triggered")

        data = request.get_json(force=True)
        if not data:
            print("❌ Empty payload or not JSON")
            return Response("Bad Request: Payload missing or not JSON.", status=400)

        # Extract data
        event_type = data.get("type", "unknown_event")
        obj = data.get("data", {}).get("object", {})
        direction = "incoming" if "received" in event_type else "outgoing"
        phone = obj.get("from") if direction == "incoming" else obj.get("to")
        text = obj.get("body", "")
        media_items = obj.get("media") or []
        urls = [m.get('url') for m in media_items if isinstance(m, dict) and m.get('url')]

        print(f"🔹 Direction: {direction}, Phone: {phone}")
        print(f"🔹 Text: '{text}'")
        print(f"🔹 Media Items Count: {len(media_items)}")
        print(f"📎 Media URLs found: {urls}")

        if not phone:
            print("❌ Missing phone number")
            return Response("Bad Request: Missing phone number.", status=400)

        # Normalize phone number
        try:
            key = ''.join(filter(str.isdigit, phone))[-10:]
            if len(key) < 10: key = phone # Fallback
        except Exception as e:
            print(f"⚠️ Error normalizing phone number '{phone}': {e}. Using original.")
            key = phone
        print(f"🔎 Normalized phone key for DB lookup: {key}")

        # Find or create contact
        contact = Contact.query.get(key)
        if not contact:
            print(f"➕ Creating new contact for key: {key}")
            contact = Contact(phone_number=key, contact_name=phone)
            db.session.add(contact)
            try:
                db.session.commit()
                print(f"✅ Contact '{contact.contact_name}' ({contact.phone_number}) created.")
            except Exception as e:
                db.session.rollback(); print(f"❌ Error saving new contact: {e}"); traceback.print_exc()
                return Response("Internal Server Error: Could not save contact", status=500)
        else:
            print(f"✅ Found existing contact: {contact.contact_name} ({contact.phone_number})")

        # Create and save the message
        msg = Message(
            phone_number=key, contact_name=contact.contact_name, direction=direction,
            message=text, media_urls=','.join(urls) if urls else None, timestamp=datetime.utcnow()
            # Add 'local_media_paths=None' here if you add that column to the model
        )
        db.session.add(msg)
        try:
            db.session.commit()
            print("✅ Message saved to DB")
            # msg.id is now available if needed below
        except Exception as e:
            db.session.rollback(); print(f"❌ Error saving message: {e}"); traceback.print_exc()
            return Response("Internal Server Error: Could not save message", status=500)


        # --- Handle Incoming Messages (Local Save & Email) ---
        if direction == "incoming":

            # --- Local File Saving Section ---
            if urls:
                print("💾 Attempting to save media locally...")
                saved_file_paths = []
                upload_folder = 'static/uploads' # Relative path to save folder
                try:
                    os.makedirs(upload_folder, exist_ok=True)
                    print(f"   Ensured upload folder exists: {upload_folder}")

                    for idx, url in enumerate(urls):
                        local_file_path = None # Define for finally block
                        try:
                            print(f"   Downloading media from: {url}")
                            response = requests.get(url, timeout=30)
                            response.raise_for_status()

                            # Determine filename
                            content_disp = response.headers.get('Content-Disposition')
                            filename = None
                            if content_disp:
                                parts = content_disp.split('filename=')
                                if len(parts) > 1: filename = parts[1].strip('"\' ')
                            if not filename:
                                try:
                                    url_path = urlparse(url).path
                                    filename = os.path.basename(url_path) if url_path else None
                                except Exception as parse_err:
                                    print(f"    - Error parsing URL path for filename: {parse_err}")
                                    filename = None
                            if not filename: filename = f"attachment_{idx+1}"

                            # Sanitize filename
                            safe_filename = "".join(c for c in filename if c.isalnum() or c in ['.', '_', '-']).strip()
                            if not safe_filename: safe_filename = f"attachment_{idx+1}.dat"

                            # Create unique filename
                            msg_id_prefix = f"msg{msg.id}_" if msg.id else "msg_"
                            timestamp_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                            unique_filename = f"{msg_id_prefix}{timestamp_str}_{idx+1}_{safe_filename}"
                            local_file_path = os.path.join(upload_folder, unique_filename)

                            print(f"   Saving locally to: {local_file_path}")
                            with open(local_file_path, 'wb') as f:
                                f.write(response.content)
                            print(f"   Save successful: {unique_filename}")
                            relative_path = os.path.join('uploads', unique_filename).replace(os.path.sep, '/')
                            saved_file_paths.append(relative_path)

                        except requests.exceptions.RequestException as req_err:
                            print(f"⚠️ Failed to download {url}: {req_err}")
                        except Exception as save_err:
                            print(f"⚠️ Failed to process/save media from {url}: {save_err}")
                            traceback.print_exc()
                        # No finally block needed here as 'local_file_path' used within try

                    # --- Optional: Update DB with Local Paths ---
                    # (Requires adding a 'local_media_paths' column to Message model)
                    # if saved_file_paths:
                    #    try:
                    #        msg_to_update = Message.query.get(msg.id)
                    #        if msg_to_update:
                    #             current_paths = msg_to_update.local_media_paths.split(',') if msg_to_update.local_media_paths else []
                    #             new_paths = current_paths + saved_file_paths
                    #             msg_to_update.local_media_paths = ",".join(new_paths)
                    #             db.session.commit()
                    #             print("✅ Updated message record with local file paths.")
                    #    except Exception as db_err:
                    #        db.session.rollback()
                    #        print(f"❌ Error updating message with local paths: {db_err}")
                    # --- End Optional DB Update ---

                except Exception as general_err:
                    print(f"❌ Error during local file saving setup: {general_err}")
                    traceback.print_exc()
            # --- End Local File Saving Section ---


            # --- Email Notification Section ---
            print("📧 Preparing email notification...")
            attachments_for_email = []
            for url in urls: # Loop through original URLs again for email
                try:
                    print(f"   Fetching media for email: {url}")
                    r = requests.get(url, timeout=20)
                    r.raise_for_status()

                    # Determine filename for email attachment
                    content_disp = r.headers.get('Content-Disposition')
                    filename = None
                    if content_disp:
                        parts = content_disp.split('filename=')
                        if len(parts) > 1: filename = parts[1].strip('"\' ')

                    # <<< CORRECTED Filename Logic >>>
                    if not filename:
                        try:
                            url_path = urlparse(url).path
                            filename = os.path.basename(url_path) if url_path else None
                        except Exception as parse_err:
                            print(f"    - Error parsing URL path for email filename: {parse_err}")
                            filename = None
                    # Fallback if still no filename
                    if not filename:
                         filename = "attachment" # Generic fallback for email
                    # <<< End Corrected Filename Logic >>>

                    content_type = r.headers.get("Content-Type", "application/octet-stream")
                    content_b64 = base64.b64encode(r.content).decode('utf-8')
                    attachments_for_email.append({
                        "content": content_b64, "type": content_type, "filename": filename,
                        "disposition": "attachment", "content_id": None
                    })
                    print(f"   Attachment prepared for email: {filename}")
                except requests.exceptions.RequestException as err:
                    print(f"⚠️ Email media fetch failed for {url}: {err}")
                except Exception as err:
                    print(f"⚠️ Unexpected error processing media for email {url}: {err}")

            # Prepare and send email (same as before)
            subject = f"New message from {contact.contact_name}"
            plain_text_content = f"From: {contact.contact_name} ({phone})\n\n{text}"
            html_content = f"<p><b>From:</b> {contact.contact_name} ({phone})</p><hr><p>{text.replace(chr(10), '<br>')}</p>"
            try:
                recipient = os.getenv('SMTP_TO') or os.getenv('SENDGRID_TO_EMAIL')
                if recipient:
                    print(f"   Attempting to send email notification to {recipient}")
                    send_email(to_address=recipient, subject=subject, html_content=html_content,
                               plain_content=plain_text_content, attachments=attachments_for_email)
                    print("✅ Email notification sent successfully.")
                else:
                    print("⚠️ Email notification skipped: Recipient address not configured.")
            except Exception as e:
                print(f"❌ Failed to send email notification: {e}")
                traceback.print_exc()
            # --- End Email Notification Section ---

        # --- Return success response ---
        # Return success even if local save or email failed, as message was saved
        return Response("✅ Webhook processed.", status=200)

    except Exception as e:
        # Catch-all for unexpected errors during core webhook processing
        print(f"❌ Unhandled error in webhook: {e}")
        traceback.print_exc()
        return Response("Internal Server Error", status=500)