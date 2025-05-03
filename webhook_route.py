# webhook_route.py

import os
import traceback
import base64
import mimetypes
import requests
import uuid # Import uuid for unique filenames

from urllib.parse import urlparse
from datetime import datetime
from flask import Blueprint, request, Response, current_app # Import current_app
from extensions import db
from models import Contact, Message
from email_utils import send_email, wrap_email_html # Assuming this utility exists

# Define the Blueprint
webhook_bp = Blueprint("webhook", __name__, url_prefix="/webhook") # Added url_prefix for clarity

@webhook_bp.route("/", methods=["POST"]) # Route is now /webhook/
def webhook():
    """Handles incoming OpenPhone webhooks for messages."""
    current_app.logger.info("\n--- /webhook route accessed ---")
    try:
        # Use force=True cautiously, check content type if possible
        data = request.get_json(force=True) or {}
        current_app.logger.debug(f"Webhook payload received: {data}") # Log the whole payload

        event_type = data.get("type", "")
        # Navigate the payload structure (adjust if OpenPhone's structure differs)
        # Assuming the relevant object is nested under 'data' -> 'object'
        payload_data = data.get("data", {})
        obj = payload_data.get("object", {}) if isinstance(payload_data, dict) else {}

        # Extract essential info
        sid = obj.get("sid") or obj.get("id") # Unique ID for the message event
        if not sid:
            current_app.logger.error("❌ Missing message SID/ID in webhook payload.")
            return Response("Bad Request: Missing unique message ID.", status=400)

        # Determine direction based on event type (adjust keywords if necessary)
        direction = "incoming" if "received" in event_type.lower() else "outgoing"

        # Extract phone number based on direction
        phone = obj.get("from") if direction == "incoming" else obj.get("to")
        current_app.logger.debug(f"Extracted raw phone number: {phone} (Direction: {direction})")

        # --- CRITICAL PHONE NUMBER CHECK ---
        if not phone:
            current_app.logger.error("❌ Webhook error: Phone number is missing or null in payload!")
            return Response("Bad Request: Missing phone number.", status=400)
        # --- END CHECK ---

        text = obj.get("body", "")
        media = obj.get("media") or [] # Ensure media is a list
        # Filter URLs robustly
        urls = [m.get("url") for m in media if isinstance(m, dict) and isinstance(m.get("url"), str)]

        current_app.logger.info(f"🔹 SID: {sid}, Direction: {direction}, Phone: {phone}, Text: '{text[:50]}...', Media URLs: {len(urls)}")

        # --- Contact Handling ---
        # Normalize phone number to create a consistent key (last 10 digits)
        key = "".join(filter(str.isdigit, phone))[-10:]
        if len(key) != 10:
             current_app.logger.warning(f"⚠️ Could not normalize phone '{phone}' to 10-digit key. Using raw: '{key}'. Check format.")
             # Consider how to handle this - maybe reject, or use the raw key if it's unique enough?
             # For now, we proceed with the potentially non-standard key.

        contact = Contact.query.get(key)
        if not contact:
            current_app.logger.info(f"ℹ️ Contact not found for key '{key}'. Creating new contact.")
            # Create contact with raw phone as default name if lookup fails
            contact = Contact(phone_number=key, contact_name=phone)
            db.session.add(contact)
            try:
                # Commit contact separately to ensure it exists before message linking
                db.session.commit()
                current_app.logger.info(f"✅ Created Contact: {contact.contact_name} (Phone Key: {key})")
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"❌ Error saving new contact (Key: {key}): {e}")
                traceback.print_exc()
                # Decide if this is fatal - maybe continue without contact association?
                return Response("Internal Server Error: Could not save contact.", 500)
        else:
            current_app.logger.info(f"✅ Found Contact: {contact.contact_name} (Phone Key: {key})")

        # --- Message Handling ---
        msg = Message.query.filter_by(sid=sid).first()

        if not msg:
            current_app.logger.info(f"ℹ️ Message with SID {sid} not found. Creating new message.")
            msg = Message(
                sid=sid,
                phone_number=key, # Link to contact via the normalized phone key
                contact_name=contact.contact_name, # Store name at time of message creation
                direction=direction,
                message=text,
                media_urls=",".join(urls) if urls else None,
                timestamp=datetime.utcnow(), # Use UTC time for consistency
                local_media_paths=None, # Initialize as None
                # property_id=None # Ensure default/handling elsewhere if used
            )
            db.session.add(msg)
            try:
                # Commit here to get the msg.id needed for unique filenames
                db.session.commit()
                current_app.logger.info(f"✅ Created Message record with DB id={msg.id} linked to key='{key}'")
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"❌ Error saving new message record (SID: {sid}): {e}")
                # This is where the IntegrityError might happen if 'key' is somehow NULL,
                # but we checked 'phone' earlier. Log the key value here for debugging.
                current_app.logger.error(f"   Message details: phone_key='{key}', direction='{direction}', sid='{sid}'")
                traceback.print_exc()
                return Response("Internal Server Error: Could not save message record.", 500)
        else:
            # Message with this SID already processed.
            current_app.logger.info(f"🔁 Found existing Message record with DB id={msg.id} (SID: {sid}). No action needed.")
            # Respond OK early to prevent re-processing (e.g., re-downloading media)
            return Response("Webhook OK (Existing Message SID)", status=200)

        # --- Media Downloading and Saving (Only for NEW incoming messages with URLs) ---
        saved_paths = []
        upload_dir = current_app.config.get('UPLOAD_FOLDER') # Get from app config
        if not upload_dir:
             current_app.logger.error("❌ UPLOAD_FOLDER is not configured in the app!")
             # Cannot save media, maybe continue without it? Or return error?
             # For now, log error and skip media processing. Email won't have attachments.
             urls = [] # Clear URLs so loop is skipped

        # Ensure the target directory exists
        if upload_dir:
             os.makedirs(upload_dir, exist_ok=True)
             current_app.logger.info(f"ℹ️ Upload directory target: {upload_dir}")

        if direction == "incoming" and urls and upload_dir: # Check upload_dir exists
            current_app.logger.info(f"⏳ Starting media download for {len(urls)} URL(s)...")
            for idx, url in enumerate(urls):
                try:
                    current_app.logger.debug(f"   Downloading media {idx+1}/{len(urls)} from: {url}")
                    resp = requests.get(url, stream=True, timeout=30)
                    resp.raise_for_status()

                    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
                    extension = mimetypes.guess_extension(content_type) or ".dat"
                    extension = ".jpg" if extension.lower() in [".jpe", ".jpeg"] else extension
                    extension = ".mp3" if content_type == "audio/mpeg" else extension

                    unique_id = str(uuid.uuid4())[:8]
                    filename = f"msg{msg.id}_{idx+1}_{unique_id}{extension}" # Unique filename
                    full_save_path = os.path.join(upload_dir, filename)
                    current_app.logger.debug(f"   Attempting to save to: {full_save_path}")

                    bytes_written = 0
                    with open(full_save_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            f.write(chunk)
                            bytes_written += len(chunk)

                    if os.path.exists(full_save_path):
                        current_app.logger.info(f"   ✅ File saved successfully: {filename} ({bytes_written} bytes)")
                        # Path relative to static folder root (e.g., "uploads/filename.jpg")
                        relative_path = os.path.join(os.path.basename(upload_dir), filename)
                        saved_paths.append(relative_path)
                    else:
                        current_app.logger.error(f"   ❌ CRITICAL ERROR: File NOT FOUND immediately after saving: {full_save_path}")

                except requests.exceptions.RequestException as req_ex:
                    current_app.logger.warning(f"   ⚠️ Network/HTTP error downloading media {idx+1} ({url}): {req_ex}")
                except IOError as io_err:
                    current_app.logger.error(f"   ⚠️ File system error saving media {idx+1} ({url}): {io_err}")
                    traceback.print_exc()
                except Exception as ex:
                    current_app.logger.error(f"   ⚠️ Unexpected error processing media {idx+1} ({url}): {ex}")
                    traceback.print_exc()

            # Update Message with Saved Paths (if any files were successfully saved)
            if saved_paths:
                current_app.logger.info(f"ℹ️ Attempting to update message {msg.id} with paths: {saved_paths}")
                msg.local_media_paths = ",".join(saved_paths)
                try:
                    current_app.logger.debug(f"   ⏳ Committing update for local_media_paths for message {msg.id}...")
                    db.session.commit()
                    current_app.logger.info(f"   ✅ Successfully committed local_media_paths update for message {msg.id}.")
                except Exception as e:
                    db.session.rollback()
                    current_app.logger.error(f"   ❌ Error committing local_media_paths update for message {msg.id}: {e}")
                    traceback.print_exc()
                    # Consider implications - message exists but paths aren't saved in DB
            else:
                current_app.logger.info(f"ℹ️ No media paths were successfully saved for message {msg.id}.")

        # --- Email Notification Logic ---
        if direction == "incoming":
            to_addr = os.getenv("SENDGRID_TO_EMAIL")
            if to_addr:
                current_app.logger.info("📧 Preparing email notification...")
                attachments = []
                if upload_dir: # Only try attaching if upload dir was valid
                    for rel_path in saved_paths:
                        # Use app context for robust path construction
                        full_path = os.path.join(upload_dir, os.path.basename(rel_path))
                        if os.path.exists(full_path):
                            try:
                                with open(full_path, "rb") as f:
                                    # Read file content directly for Base64 encoding
                                    file_content_bytes = f.read()
                                # Pass the raw bytes to send_email, let it handle encoding
                                attachments.append({
                                    "content_bytes": file_content_bytes, # Pass bytes
                                    "type": mimetypes.guess_type(full_path)[0] or "application/octet-stream",
                                    "filename": os.path.basename(full_path),
                                })
                                current_app.logger.debug(f"   📎 Added attachment data: {os.path.basename(full_path)}")
                            except Exception as read_err:
                                current_app.logger.warning(f"   ⚠️ Error reading file for email attachment ({full_path}): {read_err}")
                        else:
                            current_app.logger.warning(f"   ⚠️ File path found in saved_paths but not found on disk for email attachment: {full_path}")
                else:
                     current_app.logger.warning("   ⚠️ Skipping email attachments because UPLOAD_FOLDER is not configured.")


                email_subject = f"New message from {contact.contact_name}"
                # email_plain_content = text or f"Message from {contact.contact_name} with {len(attachments)} attachment(s)." # Removed
                # Ensure HTML is escaped properly if `text` contains HTML
                # Using a simple pre-wrap for now. Consider html.escape if needed.
                email_html_content = wrap_email_html(f"""
                    <h3>New Message from {contact.contact_name}</h3>
                    <p><strong>From:</strong> {phone} (Contact Key: {key})</p>
                    <p><strong>Message ID:</strong> {msg.id} (SID: {sid})</p>
                    <p><strong>Message:</strong></p>
                    <pre style="white-space: pre-wrap; background-color: #f0f0f0; padding: 10px; border-radius: 5px;">{text or '(No text content)'}</pre>
                    {f'<p><strong>{len(attachments)} Attachment(s) included.</strong></p>' if attachments else '<p>No attachments included or saved.</p>'}
                """)

                try:
                    current_app.logger.info(f"   Sending email to {to_addr}...")
                    # **** EDITED send_email call ****
                    send_email(
                        to_emails=[to_addr], # Changed: Pass as list
                        subject=email_subject,
                        # plain_content=email_plain_content, # Removed: Assuming not needed by send_email
                        html_content=email_html_content,
                        attachments=attachments if attachments else None # Changed: Pass the list of dicts
                    )
                    current_app.logger.info("   ✅ Email sent successfully.")
                except Exception as e:
                    current_app.logger.error(f"   ❌ Email send failed: {e}")
                    traceback.print_exc()
            else:
                current_app.logger.warning("⚠️ No SENDGRID_TO_EMAIL configured; skipping email notification.")
        else:
            current_app.logger.info("ℹ️ Skipping email notification (outgoing message).")

        # --- Final Response ---
        current_app.logger.info("✅ Webhook processed successfully.")
        return Response("Webhook OK", status=200)

    except Exception as e:
        # Catch-all for any unexpected errors during webhook processing
        current_app.logger.critical(f"❌ FATAL Unhandled webhook error: {e}")
        traceback.print_exc()
        try:
            db.session.rollback()
            current_app.logger.info("   ℹ️ Database session rolled back due to error.")
        except Exception as db_err:
            current_app.logger.error(f"   ⚠️ Error during rollback: {db_err}")
        return Response("Internal Server Error during webhook processing.", status=500)