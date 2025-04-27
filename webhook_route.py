from flask import Blueprint, request, Response
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

webhook_bp = Blueprint("webhook", __name__)

@webhook_bp.route("/webhook", methods=["POST"])
def webhook():
    print("--- /webhook route accessed ---")
    try:
        data = request.get_json(force=True) or {}
        event_type = data.get("type", "")
        obj = data.get("data", {}).get("object", {})

        sid = obj.get("sid") or obj.get("id")
        if not sid:
            print("❌ Missing message SID/ID")
            return Response("Bad Request: Missing unique message ID.", status=400)

        direction = "incoming" if "received" in event_type else "outgoing"
        phone = obj.get("from") if direction == "incoming" else obj.get("to")
        text = obj.get("body", "")
        media = obj.get("media") or []
        urls = [m.get("url") for m in media if isinstance(m, dict) and m.get("url")]

        print(f"🔹 SID: {sid}, Direction: {direction}, Phone: {phone}, Text: '{text}', Media URLs: {urls}")

        if not phone:
            print("❌ Missing phone number")
            return Response("Bad Request: Missing phone number.", status=400)

        key = "".join(filter(str.isdigit, phone))[-10:]

        contact = Contact.query.get(key)
        if not contact:
            contact = Contact(phone_number=key, contact_name=phone)
            db.session.add(contact)
            try:
                db.session.commit()
                print(f"✅ Created Contact: {contact.contact_name}")
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error saving contact: {e}")
                traceback.print_exc()
                return Response("Internal Server Error: Could not save contact.", 500)
        else:
            print(f"✅ Found Contact: {contact.contact_name}")

        msg = Message.query.filter_by(sid=sid).first()

        if not msg:
            msg = Message(
                sid=sid,
                phone_number=key,
                contact_name=contact.contact_name,
                direction=direction,
                message=text,
                media_urls=",".join(urls) if urls else None,
                timestamp=datetime.utcnow(),
                local_media_paths=None,
            )
            db.session.add(msg)
            try:
                db.session.commit()
                print(f"✅ Created Message id={msg.id}")
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error saving message: {e}")
                traceback.print_exc()
                return Response("Internal Server Error: Could not save message.", 500)
        else:
            print(f"🔁 Existing Message id={msg.id}")

        saved_paths = []
        upload_dir = os.path.join(os.getcwd(), "static", "uploads")
        os.makedirs(upload_dir, exist_ok=True)

        if direction == "incoming" and urls:
            for idx, url in enumerate(urls):
                try:
                    resp = requests.get(url, stream=True, timeout=30)
                    resp.raise_for_status()
                    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
                    ext = mimetypes.guess_extension(ctype) or ".dat"
                    ext = ".jpg" if ext.lower() in [".jpe", ".jpeg"] else ext
                    safe_base = "".join(c for c in os.path.basename(urlparse(url).path) if c.isalnum() or c in {"_", "-"}).strip() or f"file{idx+1}"
                    fname = f"msg{msg.id}_{idx+1}_{safe_base}{ext}"
                    full = os.path.join(upload_dir, fname)

                    with open(full, "wb") as f:
                        for chunk in resp.iter_content(1024):
                            f.write(chunk)

                    rel = f"uploads/{fname}"
                    saved_paths.append(rel)
                    print(f"💾 Saved media: {rel}")
                except Exception as ex:
                    print(f"⚠️ Error saving media {url}: {ex}")
                    traceback.print_exc()

            if saved_paths:
                msg.local_media_paths = ",".join(saved_paths)
                try:
                    db.session.commit()
                    print(f"✅ Updated media paths for Message id={msg.id}")
                except Exception as e:
                    db.session.rollback()
                    print(f"❌ Error updating media paths: {e}")

        # Email notification for every incoming message (media or text)
        to_addr = os.getenv("SENDGRID_TO_EMAIL")
        if to_addr:
            attachments = []
            for rel in saved_paths:
                full_path = os.path.join(os.getcwd(), "static", rel)
                with open(full_path, "rb") as f:
                    content_b64 = base64.b64encode(f.read()).decode()
                attachments.append({
                    "content": content_b64,
                    "type": mimetypes.guess_type(full_path)[0] or "application/octet-stream",
                    "filename": os.path.basename(full_path),
                    "disposition": "attachment",
                })

            email_html_content = wrap_email_html(f"""
                <h3>New Message from {contact.contact_name}</h3>
                <p><strong>From:</strong> {phone}</p>
                <p><strong>Message:</strong> {text or '(No text content)'}</p>
                {'<p>Attachments included.</p>' if attachments else ''}
            """)

            try:
                send_email(
                    to_address=to_addr,
                    subject=f"New message from {contact.contact_name}",
                    plain_content=text or "You have received a message with attachments.",
                    html_content=email_html_content,
                    attachments=attachments if attachments else None,
                )
                print("✅ Email sent.")
            except Exception as e:
                print(f"❌ Email send failed: {e}")
        else:
            print("⚠️ No SENDGRID_TO_EMAIL set; skipping email.")

        print("✅ Webhook processed successfully.")
        return Response("Webhook OK", status=200)

    except Exception as e:
        print(f"❌ Unhandled webhook error: {e}")
        traceback.print_exc()
        try:
            db.session.rollback()
        except:
            pass
        return Response("Internal Server Error", status=500)
