from flask import Blueprint, request, Response
from extensions import db
from models import Contact, Message  # Message model must include local_media_paths column
from email_utils import send_email
import os
import traceback
import base64
import mimetypes
import requests
from urllib.parse import urlparse
from datetime import datetime

# Initialize Blueprint
webhook_bp = Blueprint('webhook', __name__)

@webhook_bp.route('/webhook', methods=['POST'])
def webhook():
    print("--- /webhook route accessed ---")
    try:
        print("📥 Webhook triggered")
        data = request.get_json(force=True)
        if not data:
            print("❌ Empty payload or not JSON")
            return Response("Bad Request: Payload missing or not JSON.", status=400)

        # Extract event data
        event_type = data.get("type", "")
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
        key = phone
        try:
            digits = ''.join(filter(str.isdigit, phone))
            key = digits[-10:] if len(digits) >= 10 else digits
        except Exception as e:
            print(f"⚠️ Phone normalization error: {e}")
        print(f"🔎 Normalized phone key for DB lookup: {key}")

        # Find or create contact
        contact = Contact.query.get(key)
        if not contact:
            print(f"➕ Creating contact for key: {key}")
            contact = Contact(phone_number=key, contact_name=phone)
            db.session.add(contact)
            try:
                db.session.commit()
                print(f"✅ Contact created: {contact.contact_name} ({contact.phone_number})")
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error saving contact: {e}")
                traceback.print_exc()
                return Response("Internal Server Error: Could not save contact.", 500)
        else:
            print(f"✅ Found contact: {contact.contact_name} ({contact.phone_number})")

        # Save message record
        msg = Message(
            phone_number=key,
            contact_name=contact.contact_name,
            direction=direction,
            message=text,
            media_urls=','.join(urls) if urls else None,
            timestamp=datetime.utcnow(),
            local_media_paths=None
        )
        db.session.add(msg)
        try:
            db.session.commit()
            print(f"✅ Message saved (ID: {msg.id})")
        except Exception as e:
            db.session.rollback()
            print(f"❌ Error saving message: {e}")
            traceback.print_exc()
            return Response("Internal Server Error: Could not save message.", 500)

        # Handle incoming: save media & email
        if direction == 'incoming':
            saved_paths = []
            if urls:
                print("💾 Saving media locally...")
                upload_dir = os.path.join(os.getcwd(), 'static', 'uploads')
                os.makedirs(upload_dir, exist_ok=True)
                print(f"   Upload folder: {upload_dir}")
                for idx, url in enumerate(urls):
                    try:
                        print(f"   Downloading URL: {url}")
                        resp = requests.get(url, stream=True, timeout=30)
                        resp.raise_for_status()
                        # Determine extension
                        ctype = resp.headers.get('Content-Type','').split(';')[0].strip()
                        ext = mimetypes.guess_extension(ctype) or ''
                        if ext == '.jpe': ext = '.jpg'
                        if ext.lower() not in ['.jpg','.jpeg','.png','.gif','.webp','.pdf','.txt']:
                            ext = '.jpg' if ctype.startswith('image/') else '.dat'
                        # Filename base
                        parsed = urlparse(url).path
                        base = os.path.splitext(os.path.basename(parsed))[0] or f"file{idx+1}"
                        safe = ''.join(c for c in base if c.isalnum() or c in ['_','-']) or f"file{idx+1}"
                        fname = f"msg{msg.id}_{idx+1}_{safe}{ext}"
                        local_path = os.path.join(upload_dir, fname)
                        # Write file
                        with open(local_path,'wb') as f:
                            for chunk in resp.iter_content(1024): f.write(chunk)
                        rel = f"uploads/{fname}"
                        saved_paths.append(rel)
                        print(f"   Saved: {rel}")
                    except Exception as ex:
                        print(f"⚠️ Media save error ({url}): {ex}")
                        traceback.print_exc()
                # update DB
                try:
                    msg.local_media_paths = ','.join(saved_paths)
                    db.session.commit()
                    print(f"✅ Updated media paths for message {msg.id}")
                except Exception as ex:
                    db.session.rollback()
                    print(f"❌ Error updating media paths: {ex}")

            # Prepare email
            print("📧 Preparing email...")
            attachments = []
            for url in urls:
                try:
                    rm = requests.get(url, timeout=20)
                    rm.raise_for_status()
                    # name
                    cd = rm.headers.get('Content-Disposition','')
                    fname = None
                    if 'filename=' in cd:
                        fname = cd.split('filename=')[1].strip('"')
                    if not fname:
                        p = urlparse(url).path
                        fname = os.path.basename(p) or 'attachment'
                    b64 = base64.b64encode(rm.content).decode()
                    attachments.append({
                        'content': b64,
                        'type': rm.headers.get('Content-Type',''),
                        'filename': fname,
                        'disposition':'attachment'
                    })
                    print(f"   Attachment prepared: {fname}")
                except Exception as em:
                    print(f"⚠️ Email media fetch error ({url}): {em}")
            # send
            subj = f"New message from {contact.contact_name}"
            plain = f"From: {contact.contact_name} ({phone})\n\n{text}"
            html = f"<p><b>From:</b> {contact.contact_name} ({phone})</p><hr><p>{text.replace(chr(10),'<br>')}</p>"
            to = os.getenv('SENDGRID_TO_EMAIL') or os.getenv('SMTP_TO')
            if to:
                try:
                    send_email(to_address=to, subject=subj,
                               plain_content=plain, html_content=html,
                               attachments=attachments)
                    print("✅ Email sent.")
                except Exception as se:
                    print(f"❌ Email send error: {se}")
                    traceback.print_exc()
            else:
                print("⚠️ No email recipient configured; skipped sending.")

        print("✅ Webhook processed.")
        return Response("Webhook OK", status=200)

    except Exception as e:
        print(f"❌ Unhandled webhook error: {e}")
        traceback.print_exc()
        try: db.session.rollback()
        except: pass
        return Response("Internal Server Error", status=500)

# --- End of webhook_route.py ---
