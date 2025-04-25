import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
import mimetypes  # To guess MIME type for SMTP attachments

# SendGrid imports
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail,
    Attachment,
    FileContent,
    FileName,
    FileType,
    Disposition,
)


# <<< NOTE >>> This function is simple. Consider using libraries like 'bleach'
# if you need to sanitize HTML content before sending.
def wrap_email_html(content: str) -> str:
    """Basic HTML wrapper (Consider if needed - often email clients handle basic text/HTML)"""
    # return f"<html><body>{content}</body></html>" # Example wrapper
    return content  # Keep it simple for now


def _send_via_smtp(
    to_address: str,
    subject: str,
    html_content: str,
    plain_content: str = None,
    attachments: list = None,
):
    """Send an email via SMTP using environment-based credentials and handle attachments."""
    smtp_server = os.getenv("SMTP_SERVER") or os.getenv("SMTP_HOST")
    smtp_port_str = os.getenv("SMTP_PORT", "587")
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    from_address = os.getenv("SMTP_FROM") or smtp_username

    if not all(
        [smtp_server, smtp_port_str, smtp_username, smtp_password, from_address]
    ):
        raise RuntimeError(
            "SMTP configuration (SERVER, PORT, USERNAME, PASSWORD, FROM) is incomplete."
        )

    # Create the email message object
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg["Message-ID"] = make_msgid()  # Add a unique message ID

    # Set content: prefer HTML, provide plain text as alternative
    # If no plain_content is provided, EmailMessage can often generate a basic one from HTML,
    # but providing it explicitly is better for compatibility.
    if plain_content:
        msg.set_content(plain_content, subtype="plain")
        msg.add_alternative(html_content, subtype="html")
    else:
        # If only HTML is available
        msg.set_content("Please enable HTML to view this message.")  # Basic fallback
        msg.add_alternative(html_content, subtype="html")

    # <<< CHANGE >>> Add attachment handling for SMTP
    if attachments:
        for attachment_data in attachments:
            filename = attachment_data.get("filename", "attachment")
            content_b64 = attachment_data.get("content")
            content_type = attachment_data.get("type", "application/octet-stream")

            if not content_b64:
                print(f"⚠️ Skipping attachment '{filename}': Missing content.")
                continue

            try:
                # Decode base64 content
                attachment_bytes = base64.b64decode(content_b64)

                # Guess maintype/subtype if not fully provided
                if "/" not in content_type:
                    ctype, encoding = mimetypes.guess_type(filename)
                    if ctype:
                        content_type = ctype
                    else:
                        content_type = (
                            "application/octet-stream"  # Default if guess fails
                        )

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

    # Send the email
    try:
        port = int(smtp_port_str)
        context = ssl.create_default_context()
        if port == 465:
            # Use SSL
            print(f"📧 Connecting to SMTP server {smtp_server}:{port} using SSL...")
            with smtplib.SMTP_SSL(smtp_server, port, context=context) as server:
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
        else:
            # Use STARTTLS (commonly port 587 or 25)
            print(
                f"📧 Connecting to SMTP server {smtp_server}:{port} using STARTTLS..."
            )
            with smtplib.SMTP(smtp_server, port) as server:
                server.starttls(context=context)
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
        print(f"✅ Email sent successfully via SMTP to {to_address}")
    except smtplib.SMTPException as e:
        print(f"❌ SMTP Error: {e}")
        raise  # Re-raise SMTP specific errors
    except Exception as e:
        print(f"❌ Unexpected Error during SMTP send: {e}")
        raise  # Re-raise other errors


def _send_via_sendgrid(
    to_address: str,
    subject: str,
    html_content: str,
    plain_content: str = None,
    attachments: list = None,
):
    """Send an email via SendGrid API using the SendGrid Python client."""
    api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("SENDGRID_FROM_EMAIL")
    if not api_key or not from_email:
        raise RuntimeError(
            "SendGrid API key or sender email is not configured (SENDGRID_API_KEY, SENDGRID_FROM_EMAIL)."
        )

    # <<< CHANGE >>> Add plain_content and attachment handling for SendGrid
    message = Mail(
        from_email=from_email,
        to_emails=to_address,
        subject=subject,
        html_content=html_content,
        plain_text_content=plain_content,  # Add plain text version
    )

    if attachments:
        for attachment_data in attachments:
            filename = attachment_data.get("filename", "attachment")
            content_b64 = attachment_data.get("content")
            content_type = attachment_data.get("type", "application/octet-stream")
            disposition = attachment_data.get(
                "disposition", "attachment"
            )  # 'attachment' or 'inline'
            content_id = attachment_data.get("content_id")  # For inline images

            if not content_b64:
                print(f"⚠️ Skipping SendGrid attachment '{filename}': Missing content.")
                continue

            try:
                attachment = Attachment(
                    FileContent(content_b64),
                    FileName(filename),
                    FileType(content_type),
                    Disposition(disposition),
                    # ContentId is optional, add if needed: , ContentId(content_id)
                )
                message.add_attachment(attachment)
                print(f"📎 Added attachment '{filename}' for SendGrid.")
            except Exception as e:
                print(f"⚠️ Error adding attachment '{filename}' for SendGrid: {e}")

    try:
        print(f"📧 Sending email via SendGrid to {to_address}...")
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(
            f"✅ Email sent successfully via SendGrid. Status: {response.status_code}"
        )
        # Optional: Log response body for debugging if needed (can be large)
        # print(f"SendGrid Response Body: {response.body}")
        return response
    except Exception as e:
        print(f"❌ SendGrid API Error: {e}")
        # Attempt to print response body if available on error
        if hasattr(e, "body"):
            print(f"SendGrid Error Body: {e.body}")
        raise  # Re-raise the exception


# <<< CHANGE >>> Updated function signature to accept html, plain text, and attachments
def send_email(
    to_address: str,
    subject: str,
    html_content: str,
    plain_content: str = None,
    attachments: list = None,
):
    """
    High-level utility to send an email with optional attachments.
    Uses SendGrid if API key is set, otherwise falls back to SMTP.
    Includes basic fallback if the primary method fails.

    Args:
        to_address: Recipient email address.
        subject: Email subject line.
        html_content: The HTML version of the email body.
        plain_content: The plain text version of the email body (optional but recommended).
        attachments: A list of dictionaries, where each dictionary represents an attachment
                     and should contain keys like 'content' (base64 string), 'filename', 'type' (MIME type).
                     Example: [{'filename': 'invoice.pdf', 'content': '...', 'type': 'application/pdf'}]
    """
    use_sendgrid = bool(os.getenv("SENDGRID_API_KEY"))
    primary_method = _send_via_sendgrid if use_sendgrid else _send_via_smtp
    fallback_method = (
        _send_via_smtp if use_sendgrid else _send_via_sendgrid
    )  # Fallback is the other one

    # Prepare arguments dictionary (makes calling both methods consistent)
    email_args = {
        "to_address": to_address,
        "subject": subject,
        "html_content": html_content,
        "plain_content": plain_content,
        "attachments": attachments,
    }

    try:
        print(f"Attempting email send via {'SendGrid' if use_sendgrid else 'SMTP'}...")
        primary_method(**email_args)
    except Exception as e:
        print(
            f"⚠️ Email send failed via primary method ({'SendGrid' if use_sendgrid else 'SMTP'}): {e}"
        )
        print("Attempting fallback method...")
        # Check if fallback is viable (e.g., are SMTP creds set if falling back to SMTP?)
        # This simple version just tries the fallback regardless. Add checks if needed.
        try:
            # Check if fallback is even configured before trying
            can_fallback = False
            if primary_method == _send_via_sendgrid:  # Trying SMTP as fallback
                if all(
                    [
                        os.getenv("SMTP_SERVER"),
                        os.getenv("SMTP_PORT"),
                        os.getenv("SMTP_USERNAME"),
                        os.getenv("SMTP_PASSWORD"),
                        os.getenv("SMTP_FROM"),
                    ]
                ):
                    can_fallback = True
                else:
                    print("Fallback to SMTP skipped: SMTP configuration incomplete.")
            else:  # Trying SendGrid as fallback
                if all(
                    [os.getenv("SENDGRID_API_KEY"), os.getenv("SENDGRID_FROM_EMAIL")]
                ):
                    can_fallback = True
                else:
                    print(
                        "Fallback to SendGrid skipped: SendGrid configuration incomplete."
                    )

            if can_fallback:
                fallback_method(**email_args)
            else:
                # Re-raise the original error if fallback is not configured
                raise e from None

        except Exception as inner_e:
            print(f"❌ Email send failed via fallback method as well: {inner_e}")
            # Decide whether to raise the original error (e) or the fallback error (inner_e)
            # Raising the original might be more informative about the primary failure.
            raise e  # Re-raise the original exception after fallback fails
