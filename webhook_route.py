from flask import Blueprint, request, Response, current_app # Import current_app
from extensions import db
from models import Contact, Message
from email_utils import send_email, wrap_email_html
import os
import traceback
import base64
import mimetypes
import requests
from urllib.parse import urlparse
from datetime import datetime
import uuid # Import uuid for unique filenames

webhook_bp = Blueprint("webhook", __name__)

@webhook_bp.route("/webhook", methods=["POST"])
def webhook():
    print("\n--- /webhook route accessed ---")
    try:
        # Use force=True cautiously, ensure content type is appropriate
        data = request.get_json(force=True) or {}
        event_type = data.get("type", "")
        obj = data.get("data", {}).get("object", {})

        # Extract essential info
        # Prefer 'id' from the event object if 'sid' isn't present (more general)
        sid = obj.get("sid") or obj.get("id")
        if not sid:
            print("❌ Missing message SID/ID in webhook payload.")
            return Response("Bad Request: Missing unique message ID.", status=400)

        direction = "incoming" if "received" in event_type.lower() else "outgoing"
        phone = obj.get("from") if direction == "incoming" else obj.get("to")
        text = obj.get("body", "")
        media = obj.get("media") or [] # Ensure media is a list
        # Filter URLs robustly
        urls = [m.get("url") for m in media if isinstance(m, dict) and isinstance(m.get("url"), str)]

        print(f"🔹 SID: {sid}, Direction: {direction}, Phone: {phone}, Text: '{text[:50]}...', Media URLs: {urls}")

        if not phone:
            print("❌ Missing phone number in webhook payload.")
            return Response("Bad Request: Missing phone number.", status=400)

        # --- Contact Handling ---
        # Normalize phone number for key (e.g., last 10 digits)
        # Consider adding country code handling if needed
        key = "".join(filter(str.isdigit, phone))[-10:]
        contact = Contact.query.get(key)
        if not contact:
            print(f"ℹ️ Contact not found for key '{key}'. Creating new contact.")
            contact = Contact(phone_number=key, contact_name=phone) # Default name to phone
            db.session.add(contact)
            try:
                db.session.commit()
                print(f"✅ Created Contact: {contact.contact_name} (Phone: {key})")
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error saving new contact: {e}")
                traceback.print_exc()
                # Decide if this is fatal - maybe continue without contact?
                return Response("Internal Server Error: Could not save contact.", 500)
        else:
            print(f"✅ Found Contact: {contact.contact_name} (Phone: {key})")

        # --- Message Handling ---
        # Check if message already exists by SID
        msg = Message.query.filter_by(sid=sid).first()

        if not msg:
            print(f"ℹ️ Message with SID {sid} not found. Creating new message.")
            msg = Message(
                sid=sid,
                phone_number=key, # Link to contact via phone key
                contact_name=contact.contact_name, # Store name at time of message
                direction=direction,
                message=text,
                media_urls=",".join(urls) if urls else None,
                timestamp=datetime.utcnow(), # Use UTC time
                local_media_paths=None, # Initialize as None
                # property_id=None # Ensure property_id is handled if needed elsewhere
            )
            db.session.add(msg)
            try:
                # Commit here to get the msg.id for filenames
                db.session.commit()
                print(f"✅ Created Message record with DB id={msg.id}")
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error saving new message record: {e}")
                traceback.print_exc()
                return Response("Internal Server Error: Could not save message record.", 500)
        else:
            # Message already exists, maybe update timestamp or content?
            # For now, just log it. Prevents duplicate processing.
            print(f"🔁 Found existing Message record with DB id={msg.id}. No media download needed for existing SID.")
            # If reprocessing is needed, logic would go here, potentially clearing old local_media_paths
            # For now, we assume if SID exists, we don't re-download.
            return Response("Webhook OK (Existing Message SID)", status=200) # Respond OK early

        # --- Media Downloading and Saving (Only for NEW incoming messages with URLs) ---
        saved_paths = []
        # Use current_app's configured static folder and upload subfolder
        # This is more robust than os.getcwd()
        upload_dir = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, 'uploads'))
        # Ensure the target directory exists (redundant if done at app start, but safe)
        os.makedirs(upload_dir, exist_ok=True)
        print(f"ℹ️ Upload directory target: {upload_dir}")


        if direction == "incoming" and urls:
            print(f"⏳ Starting media download for {len(urls)} URL(s)...")
            for idx, url in enumerate(urls):
                try:
                    print(f"   Downloading media {idx+1}/{len(urls)} from: {url}")
                    # Make the request to download the media
                    resp = requests.get(url, stream=True, timeout=30) # Use stream=True for large files
                    resp.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

                    # Guess file extension from Content-Type header
                    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
                    extension = mimetypes.guess_extension(content_type) or ".dat" # Default to .dat
                    # Normalize common image extensions
                    extension = ".jpg" if extension.lower() in [".jpe", ".jpeg"] else extension
                    extension = ".mp3" if content_type == "audio/mpeg" else extension # Add specific cases if needed

                    # Create a more robust and unique filename
                    # Using message ID, index, and a UUID part to avoid collisions
                    unique_id = str(uuid.uuid4())[:8] # Short unique ID part
                    # Basic sanitization of original filename if available
                    # parsed_path = urlparse(url).path
                    # safe_base = "".join(c for c in os.path.basename(parsed_path) if c.isalnum() or c in {"_", "-"}).strip() or f"media"
                    # filename = f"msg{msg.id}_{idx+1}_{safe_base}_{unique_id}{extension}"
                    filename = f"msg{msg.id}_{idx+1}_{unique_id}{extension}" # Simpler unique name

                    full_save_path = os.path.join(upload_dir, filename)
                    print(f"   Attempting to save to: {full_save_path}")

                    # Write the content to the file
                    with open(full_save_path, "wb") as f:
                        bytes_written = 0
                        for chunk in resp.iter_content(chunk_size=8192): # Use larger chunks
                            f.write(chunk)
                            bytes_written += len(chunk)

                    # *** Add check: Verify file exists immediately after saving ***
                    if os.path.exists(full_save_path):
                         print(f"   ✅ File saved successfully: {filename} ({bytes_written} bytes)")
                         # Construct relative path for DB (e.g., "uploads/filename.jpg")
                         relative_path = os.path.join(os.path.basename(upload_dir), filename)
                         saved_paths.append(relative_path)
                    else:
                         print(f"   ❌ CRITICAL ERROR: File NOT FOUND immediately after saving: {full_save_path}")
                         # Decide how to handle this - skip adding path? Raise error?

                except requests.exceptions.RequestException as req_ex:
                    # Handle network/HTTP errors specifically
                    print(f"   ⚠️ Network/HTTP error downloading media {idx+1} ({url}): {req_ex}")
                    # Optionally add this URL to an 'error_urls' list in the message?
                except IOError as io_err:
                    # Handle file system errors during open/write
                    print(f"   ⚠️ File system error saving media {idx+1} ({url}): {io_err}")
                    traceback.print_exc()
                except Exception as ex:
                    # Catch any other unexpected errors during download/save
                    print(f"   ⚠️ Unexpected error processing media {idx+1} ({url}): {ex}")
                    traceback.print_exc()

            # --- Update Message with Saved Paths (if any files were saved) ---
            if saved_paths:
                print(f"ℹ️ Attempting to update message {msg.id} with paths: {saved_paths}")
                msg.local_media_paths = ",".join(saved_paths)
                try:
                    # *** Add Log: Before commit ***
                    print(f"   ⏳ Committing update for local_media_paths for message {msg.id}...")
                    db.session.commit()
                    # *** Add Log: After commit ***
                    print(f"   ✅ Successfully committed local_media_paths update for message {msg.id}.")
                except Exception as e:
                    db.session.rollback()
                    print(f"   ❌ Error committing local_media_paths update for message {msg.id}: {e}")
                    # Consider if this error should prevent email sending
            else:
                print(f"ℹ️ No media paths were successfully saved for message {msg.id}.")

        # --- Email Notification Logic ---
        # Send email only for incoming messages and if configured
        if direction == "incoming":
            to_addr = os.getenv("SENDGRID_TO_EMAIL")
            if to_addr:
                print("📧 Preparing email notification...")
                attachments = []
                # Use the paths confirmed to be saved
                for rel_path in saved_paths:
                    # Construct full path using current_app context again for safety
                    full_path = os.path.join(current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.static_folder, 'uploads')), os.path.basename(rel_path))
                    # Check if file exists *again* before attaching (paranoid check)
                    if os.path.exists(full_path):
                        try:
                            with open(full_path, "rb") as f:
                                content_b64 = base64.b64encode(f.read()).decode()
                            attachments.append({
                                "content": content_b64,
                                "type": mimetypes.guess_type(full_path)[0] or "application/octet-stream",
                                "filename": os.path.basename(full_path),
                                "disposition": "attachment",
                            })
                            print(f"   📎 Added attachment: {os.path.basename(full_path)}")
                        except Exception as read_err:
                            print(f"   ⚠️ Error reading file for email attachment ({full_path}): {read_err}")
                    else:
                        print(f"   ⚠️ File path found in saved_paths but not found on disk for email attachment: {full_path}")


                # Prepare email content
                email_subject = f"New message from {contact.contact_name}"
                email_plain_content = text or f"Message from {contact.contact_name} with {len(attachments)} attachment(s)."
                email_html_content = wrap_email_html(f"""
                    <h3>New Message from {contact.contact_name}</h3>
                    <p><strong>From:</strong> {phone} (Contact Key: {key})</p>
                    <p><strong>Message ID:</strong> {msg.id} (SID: {sid})</p>
                    <p><strong>Message:</strong></p>
                    <pre style="white-space: pre-wrap; background-color: #f0f0f0; padding: 10px; border-radius: 5px;">{text or '(No text content)'}</pre>
                    {f'<p><strong>{len(attachments)} Attachment(s) included.</strong></p>' if attachments else '<p>No attachments included or saved.</p>'}
                """)

                try:
                    print(f"   Sending email to {to_addr}...")
                    send_email(
                        to_address=to_addr,
                        subject=email_subject,
                        plain_content=email_plain_content,
                        html_content=email_html_content,
                        # Send attachments only if list is not empty
                        attachments=attachments if attachments else None,
                    )
                    print("   ✅ Email sent successfully.")
                except Exception as e:
                    print(f"   ❌ Email send failed: {e}")
                    # Log but don't necessarily fail the webhook response
                    traceback.print_exc()
            else:
                print("⚠️ No SENDGRID_TO_EMAIL configured; skipping email notification.")
        else:
            print("ℹ️ Skipping email notification (outgoing message).")

        # --- Final Response ---
        print("✅ Webhook processed successfully.")
        return Response("Webhook OK", status=200)

    except Exception as e:
        # Catch-all for any unexpected errors during webhook processing
        print(f"❌ FATAL Unhandled webhook error: {e}")
        traceback.print_exc()
        # Attempt to rollback DB session in case of error
        try:
            db.session.rollback()
            print("   ℹ️ Database session rolled back due to error.")
        except Exception as db_err:
            print(f"   ⚠️ Error during rollback: {db_err}")
        # Return a generic server error response
        return Response("Internal Server Error during webhook processing.", status=500)
