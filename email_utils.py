import os
import smtplib
import ssl
import base64
from email.message import EmailMessage
from email.utils import make_msgid
import mimetypes

# SendGrid imports
from sendgrid import SendGridAPIClient
# In email_utils.py
from sendgrid.helpers.mail import (
    Mail,
    Attachment,
    FileContent,
    FileName,
    FileType,
    Disposition,
    From,             # Add this
    To,               # Add this
    Subject,          # Add this
    HtmlContent       # Add this
)

def wrap_email_html(content: str) -> str:
    """Enhanced HTML wrapper with conversation-style design."""
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0;
                padding: 0;
                background-color: #f5f5f5;
            }
            .email-container {
                max-width: 600px;
                margin: 20px auto;
                background-color: #ffffff;
                border-radius: 8px;
                overflow: hidden;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            }
            .header {
                background: linear-gradient(135deg, #3a8bab 0%, #6cb9d3 100%);
                color: white;
                padding: 20px;
                text-align: center;
            }
            .header h2 {
                margin: 0;
                font-size: 24px;
                font-weight: 600;
            }
            .header p {
                margin: 5px 0 0 0;
                font-size: 14px;
                opacity: 0.9;
            }
            .content {
                padding: 30px 20px;
            }
            .message-box {
                background-color: #f8f9fa;
                border-left: 4px solid #6cb9d3;
                padding: 15px;
                margin: 20px 0;
                border-radius: 4px;
            }
            .message-text {
                white-space: pre-wrap;
                word-wrap: break-word;
                margin: 0;
                color: #333;
                line-height: 1.6;
            }
            .info-row {
                display: flex;
                justify-content: space-between;
                margin-bottom: 10px;
                padding-bottom: 10px;
                border-bottom: 1px solid #e9ecef;
            }
            .info-label {
                font-weight: 600;
                color: #495057;
            }
            .info-value {
                color: #212529;
            }
            .attachment-notice {
                background-color: #e7f3ff;
                border: 1px solid #b3d9ff;
                padding: 12px;
                border-radius: 4px;
                margin-top: 20px;
                color: #0056b3;
            }
            .footer {
                background-color: #f8f9fa;
                padding: 20px;
                text-align: center;
                font-size: 12px;
                color: #6c757d;
                border-top: 1px solid #e9ecef;
            }
            .footer a {
                color: #3a8bab;
                text-decoration: none;
            }
            pre {
                margin: 0;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            }
        </style>
    </head>
    <body>
        <div class="email-container">
            <div class="header">
                <h2>Sin City Rentals</h2>
                <p>New Message Notification</p>
            </div>
            <div class="content">
                {content}
            </div>
            <div class="footer">
                <p>This is an automated message from Sin City Rentals OpenPhone system.</p>
                <p>© 2025 Sin City Rentals. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html_template.replace("{content}", content) if content.strip() else html_template.replace("{content}", "<p>(No text content)</p>")

def _send_via_smtp(
    to_address: str,
    subject: str,
    html_content: str,
    plain_content: str = None,
    attachments: list = None,
):
    smtp_server = os.getenv("SMTP_SERVER") or os.getenv("SMTP_HOST")
    smtp_port_str = os.getenv("SMTP_PORT", "587")
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    from_address = os.getenv("SMTP_FROM") or smtp_username

    if not all([smtp_server, smtp_port_str, smtp_username, smtp_password, from_address]):
        raise RuntimeError("SMTP configuration is incomplete.")

    html_content = wrap_email_html(html_content)
    plain_content = plain_content or "(No text content)"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg["Message-ID"] = make_msgid()

    msg.set_content(plain_content, subtype="plain")
    msg.add_alternative(html_content, subtype="html")

    if attachments:
        for attachment_data in attachments:
            filename = attachment_data.get("filename", "attachment")
            content_b64 = attachment_data.get("content")
            content_type = attachment_data.get("type", "application/octet-stream")

            if not content_b64:
                print(f"⚠️ Skipping attachment '{filename}': Missing content.")
                continue

            try:
                attachment_bytes = base64.b64decode(content_b64)
                ctype, _ = mimetypes.guess_type(filename)
                content_type = ctype or content_type
                maintype, subtype = content_type.split("/", 1)

                msg.add_attachment(
                    attachment_bytes,
                    maintype=maintype,
                    subtype=subtype,
                    filename=filename,
                )
                print(f"📎 Added attachment '{filename}' for SMTP.")
            except Exception as e:
                print(f"⚠️ Error adding attachment '{filename}' for SMTP: {e}")

    port = int(smtp_port_str)
    context = ssl.create_default_context()
    try:
        if port == 465:
            print(f"📧 Connecting to SMTP server {smtp_server}:{port} using SSL...")
            with smtplib.SMTP_SSL(smtp_server, port, context=context) as server:
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
        else:
            print(f"📧 Connecting to SMTP server {smtp_server}:{port} using STARTTLS...")
            with smtplib.SMTP(smtp_server, port) as server:
                server.starttls(context=context)
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
        print(f"✅ Email sent successfully via SMTP to {to_address}")
    except Exception as e:
        print(f"❌ SMTP Error: {e}")
        raise

def _send_via_sendgrid(
    to_address: str,
    subject: str,
    html_content: str,
    plain_content: str = None,
    attachments: list = None,
):
    api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("SENDGRID_FROM_EMAIL")
    if not api_key or not from_email:
        raise RuntimeError("SendGrid API key or sender email not configured.")

    html_content = wrap_email_html(html_content)
    plain_content = plain_content or "(No text content)"

    message = Mail(
        from_email=from_email,
        to_emails=to_address,
        subject=subject,
        html_content=html_content,
        plain_text_content=plain_content,
    )

    if attachments:
        for attachment_data in attachments:
            filename = attachment_data.get("filename", "attachment")
            content_b64 = attachment_data.get("content")
            content_type = attachment_data.get("type", "application/octet-stream")
            disposition = attachment_data.get("disposition", "attachment")

            if not content_b64:
                print(f"⚠️ Skipping SendGrid attachment '{filename}': Missing content.")
                continue

            try:
                attachment = Attachment(
                    FileContent(content_b64),
                    FileName(filename),
                    FileType(content_type),
                    Disposition(disposition),
                )
                message.add_attachment(attachment)
                print(f"📎 Added attachment '{filename}' for SendGrid.")
            except Exception as e:
                print(f"⚠️ Error adding attachment '{filename}' for SendGrid: {e}")

    try:
        print(f"📧 Sending email via SendGrid to {to_address}...")
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(f"✅ Email sent via SendGrid. Status: {response.status_code}")
        return response
    except Exception as e:
        print(f"❌ SendGrid Error: {e}")
        if hasattr(e, "body"):
            print(f"SendGrid Error Body: {e.body}")
        raise

def send_email(to_emails, subject, html_content, attachments=None):
    message = Mail(
        from_email=From('phil@sincityrentals.com', 'Sin City Rentals'),
        to_emails=to_emails, # Can be a list of emails or To objects    
        subject=Subject(subject),
        html_content=HtmlContent(html_content)
    )

    if attachments:
        processed_attachments = []
        for attachment_data in attachments:
            # Ensure content is Base64 encoded string
            encoded_content = base64.b64encode(attachment_data['content_bytes']).decode()

            attachment = Attachment(
                FileContent(encoded_content),
                FileName(attachment_data['filename']),
                FileType(attachment_data['type']), # Use the content type from Flask
                Disposition('attachment') # 'inline' is also an option for images
                # ContentId('...') # Optional: Used for inline images
            )
            processed_attachments.append(attachment)

        message.attachment = processed_attachments # Assign the list of attachments

    try:
        sg = SendGridAPIClient(os.environ.get('SENDGRID_API_KEY'))
        response = sg.send(message)
        print(f"Email sent to {to_emails}, Status Code: {response.status_code}")
        # Handle response status, body, headers as needed
        return True
    except Exception as e:
        print(f"Error sending email to {to_emails}: {e}")
        # Log the error details (e.g., e.body if available)
        return False
