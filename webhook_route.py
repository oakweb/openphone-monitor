from flask import Blueprint, request, Response
from extensions import db
from models import Contact, Message        # Message model must include a unique 'sid' column
from email_utils import send_email
import os
import traceback
import base64
import mimetypes
import requests
from urllib.parse import urlparse
from datetime import datetime

# Initialize Blueprint
webhook_bp = Blueprint("webhook", __name__)

@webhook_bp.route("/webhook", methods=["POST"])
def webhook():
    print("--- /webhook route accessed ---")
    try:
        # 1️⃣ Parse JSON payload
        data = request.get_json(force=True) or {}
        event_type = data.get("type", "")
        obj = data.get("data", {}).get("object", {})

        # 2️⃣ Determine unique SID for dedupe
        sid = obj.get("sid") or obj.get("id")
        if not sid:
            print("❌ Missing message SID/ID for deduplication")
            return Response("Bad Request: Missing unique message ID.", status=400)

        # 3️⃣ Determine direction, phone, text, media URLs
        direction = "incoming" if "received" in event_type else "outgoing"
        phone     = obj.get("from") if direction == "incoming" else obj.get("to")
        text      = obj.get("body", "")
        media     = obj.get("media") or []
        urls      = [m.get("url") for m in media if isinstance(m, dict) and m.get("url")]

        print(f"🔹 SID: {sid}")
        print(f"🔹 Direction: {direction}, Phone: {phone}")
        print(f"🔹 Text: '{text}'")
        print(f"🔹 Media URLs: {urls}")

        if not phone:
            print("❌ Missing phone number")
            return Response("Bad Request: Missing phone number.", status=400)

        # 4️⃣ Normalize phone for DB lookup
        digits = "".join(filter(str.isdigit, phone))
        key    = digits[-10:] if len(digits) >= 10 else digits
        print(f"🔎 Normalized phone key: {key}")

        # 5️⃣ Find or create Contact
        contact = Contact.query.get(key)
        if not contact:
            contact = Contact(phone_number=key, contact_name=phone)
            db.session.add(contact)
            try:
                db.session.commit()
                print(f"✅ Created Contact: {contact.contact_name} ({contact.phone_number})")
            except Exception as e:
                db.session.rollback()
                print(f"❌ Error saving contact: {e}")
                traceback.print_exc()
                return Response("Internal Server Error: Could not save contact.", 500)
        else:
            print(f"✅ Found Contact: {contact.contact_name} ({contact.phone_number})")

        # 6️⃣ Dedupe: look up existing Message by SID
        msg = Message.query.filter_by(sid=sid).first()

        # 7️⃣ If not found, create a new Message (no id=… field!)
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
            print(f"🔁 Reusing existing Message id={msg.id}")

        # 8️⃣ If incoming, download & save media files
        if direction == "incoming" and urls:
            saved_paths = []
            upload_dir = os.path.join(os.getcwd(), "static", "uploads")
            os.makedirs(upload_dir, exist_ok=True)

            for idx, url in enumerate(urls):
                try:
                    resp = requests.get(url, stream=True, timeout=30)
                    resp.raise_for_status()
                    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
                    ext   = mimetypes.guess_extension(ctype) or ""
                    if ext == ".jpe":
                        ext = ".jpg"
                    if ext.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                        ext = ".jpg" if ctype.startswith("image/") else ".dat"
                    base = os.path.splitext(os.path.basename(urlparse(url).path))[0] or f"file{idx+1}"
                    safe = "".join(c for c in base if c.isalnum() or c in {"_","-"})
                    fname = f"msg{msg.id}_{idx+1}_{safe}{ext}"
                    full  = os.path.join(upload_dir, fname)
                    with open(full, "wb") as f:
                        for chunk in resp.iter_content(1024):
                            f.write(chunk)
                    rel = f"uploads/{fname}"
                    saved_paths.append(rel)
                    print(f"💾 Saved media: {rel}")
                except Exception as ex:
                    print(f"⚠️ Error saving media {url}: {ex}")
                    traceback.print_exc()

            # Update the DB with local paths
            if saved_paths:
                msg.local_media_paths = ",".join(saved_paths)
                try:
                    db.session.commit()
                    print(f"✅ Updated media paths for Message id={msg.id}")
                except Exception as e:
                    db.session.rollback()
                    print(f"❌ Error updating media paths: {e}")

                # 9️⃣ Send email notification if configured
                to_addr = os.getenv("SENDGRID_TO_EMAIL")
                if to_addr and saved_paths:
                    print("📧 Sending notification email…")
                    attachments = []
                    for rel in saved_paths:
                        full = os.path.join(os.getcwd(), "static", rel)
                        with open(full, "rb") as f:
                            content_b64 = base64.b64encode(f.read()).decode()
                        attachments.append({
                            "content": content_b64,
                            "type": mimetypes.guess_type(full)[0] or "application/octet-stream",
                            "filename": os.path.basename(full),
                            "disposition": "attachment",
                        })
                    try:
                        send_email(
                            to_address=to_addr,
                            subject=f"New image message from {contact.contact_name}",
                            plain_content=text,
                            html_content=f"<p>{text}</p>",
                            attachments=attachments,
                        )
                        print("✅ Email sent.")
                    except Exception as e:
                        print(f"❌ Email send failed: {e}")
                else:
                    print("⚠️ No SENDGRID_TO_EMAIL set or no media; skipping email.")

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
