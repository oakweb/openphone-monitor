# --- Imports ---
from flask import Blueprint, request, Response
from extensions import db
from models import Contact, Message # Make sure Message model has local_media_paths column
from email_utils import send_email
import requests
import traceback
import base64
from datetime import datetime
import os                      # Needed for path joining, makedirs, remove, basename
from urllib.parse import urlparse # To help get original filename

# Initialize Blueprint
webhook_bp = Blueprint('webhook', __name__)

@webhook_bp.route('/webhook', methods=['POST'])
def webhook():
    # ADDED PRINT STATEMENT BELOW TO CHECK IF ROUTE IS ACCESSED AT ALL
    print("--- /webhook route accessed ---") # <<< Check logs for this FIRST
    # Using 4 standard spaces for indentation consistently
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

        # Create and save the message (Initialize local_media_paths)
        msg = Message(
            phone_number=key, contact_name=contact.contact_name, direction=direction,
            message=text, media_urls=','.join(urls) if urls else None, timestamp=datetime.utcnow(),
            local_media_paths=None
        )
        db.session.add(msg)
        try:
            db.session.commit()
            print(f"✅ Webhook saved Message ID {msg.id} from {msg.phone_number}")
            print(f"✅ Message saved to DB (ID: {msg.id})") # Log ID
        except Exception as e:
            db.session.rollback()
            print(f"❌ Webhook error saving message: {e}")


        # --- Handle Incoming Messages (Local Save & Email) ---
        if direction == "incoming":
            saved_file_paths = [] # Keep track of successfully saved local paths

            # --- Local File Saving Section (Now Active) ---
            if urls:
                print("💾 Attempting to save media locally...")
                # Using the hardcoded path for consistency with logs, but relative (os.getcwd()) is often better
                upload_folder_name = 'uploads'
                upload_folder = '/home/runner/workspace/static/uploads' # Your specified path
                # Alt: upload_folder = os.path.join(os.getcwd(), 'static', upload_folder_name)
                print(f"   Target absolute upload folder path: {upload_folder}")
                try:
                    os.makedirs(upload_folder, exist_ok=True)
                    print(f"   Ensured upload folder exists: {upload_folder}")

                    for idx, url in enumerate(urls):
                        local_file_path = None # Define for potential cleanup
                        try:
                            print(f"   [Save Attempt {idx+1}] Processing URL: {url}")
                            response = requests.get(url, timeout=30)
                            print(f"   [Save Attempt {idx+1}] Download status code: {response.status_code}")
                            response.raise_for_status() # Check for download errors

                            # Determine filename
                            content_disp = response.headers.get('Content-Disposition')
                            filename = None
                            if content_disp:
                                parts = content_disp.split('filename=')
                                if len(parts) > 1: filename = parts[1].strip('"\' ')
                            # Fallback using URL path
                            if not filename:
                                try:
                                    url_path = urlparse(url).path
                                    filename = os.path.basename(url_path) if url_path else None
                                except Exception as parse_err:
                                    print(f"    - Error parsing URL path for filename: {parse_err}")
                                    filename = None
                            # Final fallback with extension guessing
                            if not filename:
                                content_type = response.headers.get("Content-Type", "")
                                ext = ".dat" # Default extension
                                if 'image/jpeg' in content_type: ext = ".jpg"
                                elif 'image/png' in content_type: ext = ".png"
                                elif 'image/gif' in content_type: ext = ".gif"
                                elif 'image/webp' in content_type: ext = ".webp"
                                filename = f"attachment_{idx+1}{ext}"
                                print(f"    - Warning: Could not determine filename, using fallback: {filename}")


                            # Sanitize filename (Remove potentially unsafe characters)
                            safe_base = "".join(c for c in os.path.splitext(filename)[0] if c.isalnum() or c in ['_', '-']).strip() or f"file_{idx+1}"
                            safe_ext = os.path.splitext(filename)[1].lower() if os.path.splitext(filename)[1] else '.dat'
                            # Optional: Ensure extension is allowed (adjust as needed)
                            allowed_exts = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.txt']
                            if safe_ext not in allowed_exts:
                                print(f"    - Warning: File extension '{safe_ext}' not in allowed list, using '.dat'")
                                safe_ext = '.dat'
                            safe_filename = f"{safe_base}{safe_ext}"


                            # Create unique filename
                            msg_id_prefix = f"msg{msg.id}_" if msg.id else f"msgUNK_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_"
                            timestamp_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                            unique_filename = f"{msg_id_prefix}{timestamp_str}_{idx+1}_{safe_filename}"
                            local_file_path = os.path.join(upload_folder, unique_filename)

                            print(f"   [Save Attempt {idx+1}] Attempting to save locally to: {local_file_path}")
                            with open(local_file_path, 'wb') as f:
                                f.write(response.content)
                            print(f"   [Save Attempt {idx+1}] Save successful: {unique_filename}")

                            # Store RELATIVE path for DB/Template (use forward slash)
                            relative_path = os.path.join(upload_folder_name, unique_filename).replace(os.path.sep, '/')
                            saved_file_paths.append(relative_path)
                            print(f"   Stored relative path: {relative_path}")

                        except requests.exceptions.RequestException as req_err:
                            print(f"⚠️ [Save Attempt {idx+1}] FAILED TO DOWNLOAD {url}: {req_err}")
                        except IOError as io_err: # More specific for file write errors
                             print(f"⚠️ [Save Attempt {idx+1}] IO ERROR saving file {local_file_path}: {io_err}")
                             traceback.print_exc()
                        except Exception as save_err:
                            print(f"⚠️ [Save Attempt {idx+1}] FAILED TO PROCESS/SAVE media from {url}: {save_err}")
                            traceback.print_exc()
                    # --- End for loop ---

                    # --- Database Update for Local Paths ---
                    if saved_file_paths:
                        try:
                            # Re-fetch msg object to ensure it's attached to session
                            msg_to_update = Message.query.get(msg.id)
                            if msg_to_update:
                                msg_to_update.local_media_paths = ",".join(saved_file_paths)
                                db.session.commit()
                                print(f"✅ Updated message {msg.id} record with local file paths: {msg_to_update.local_media_paths}")
                            else:
                                print(f"❌ Could not find message {msg.id} to update with local paths.")
                        except Exception as db_err:
                            db.session.rollback()
                            print(f"❌ Error updating message {msg.id} with local paths: {db_err}")
                            traceback.print_exc()
                    # --- End Database Update ---

                except OSError as os_err: # Catch error creating directory
                    print(f"❌ OS Error during local file saving setup (check permissions/path: {upload_folder}): {os_err}")
                    traceback.print_exc()
                except Exception as general_err:
                    print(f"❌ General Error during local file saving setup: {general_err}")
                    traceback.print_exc()
            # --- End Local File Saving Section ---


            # --- Email Notification Section ---
            # Ensure this block is correctly indented inside 'if direction == "incoming":'
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
                        filename = parts[1].strip('"\' ') if len(parts) > 1 else None
                    # Fallback using URL path
                    if not filename:
                        try:
                            url_path=urlparse(url).path
                            filename=os.path.basename(url_path) if url_path else None
                        except Exception as parse_err:
                            print(f"    - Error parsing URL path for email filename: {parse_err}")
                            filename = None
                    # Final fallback
                    if not filename:
                        filename = "attachment"

                    content_type = r.headers.get("Content-Type", "application/octet-stream")
                    content_b64 = base64.b64encode(r.content).decode('utf-8')
                    attachments_for_email.append({
                        "content": content_b64,
                        "type": content_type,
                        "filename": filename,
                        "disposition": "attachment", # Ensure this is set for SendGrid
                        "content_id": None # Usually not needed for standard attachments
                    })
                    print(f"   Attachment prepared for email: {filename}")
                except requests.exceptions.RequestException as err:
                    print(f"⚠️ Email media fetch failed for {url}: {err}")
                except Exception as err:
                    print(f"⚠️ Unexpected error processing media for email {url}: {err}")
                    traceback.print_exc()

            # Send the email
            subject = f"New message from {contact.contact_name}"
            plain_text_content = f"From: {contact.contact_name} ({phone})\n\n{text}"
            html_content = f"<p><b>From:</b> {contact.contact_name} ({phone})</p><hr><p>{text.replace(chr(10), '<br>')}</p>" # Handle newlines in HTML
            try:
                recipient = os.getenv('SMTP_TO') or os.getenv('SENDGRID_TO_EMAIL')
                if recipient:
                    print(f"   Attempting to send email notification to {recipient}")
                    # Ensure email_utils.send_email function can handle the attachments format correctly
                    send_email(
                        to_address=recipient,
                        subject=subject,
                        html_content=html_content,
                        plain_content=plain_text_content,
                        attachments=attachments_for_email
                    )
                    print("✅ Email notification sent successfully.")
                else:
                    print("⚠️ Email notification skipped: Recipient address not configured.")
            except Exception as e:
                print(f"❌ Failed to send email notification: {e}")
                traceback.print_exc()
            # --- End Email Notification Section ---

        # --- Return success response ---
        # Correctly indented: outside 'if direction == "incoming":' but inside 'try'
        print("✅ Webhook processing finished.")
        return Response("✅ Webhook processed.", status=200)

    # --- Main exception handler ---
    except Exception as e:
        print(f"❌ Unhandled error in webhook: {e}")
        traceback.print_exc()
        # Ensure rollback in case of failure before response
        try:
            db.session.rollback()
        except Exception as rb_err:
            print(f"⚠️ Error during rollback: {rb_err}")
        return Response("Internal Server Error", status=500)

# --- End of file ---