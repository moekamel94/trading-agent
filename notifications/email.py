import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config


def send(subject: str, body: str):
    if not config.EMAIL_APP_PASSWORD:
        print("  [Email] No app password set — skipping email.")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = config.EMAIL_ADDRESS
        msg["To"]      = config.EMAIL_ADDRESS

        # Plain text part
        msg.attach(MIMEText(body, "plain"))

        # HTML part — monospace pre block for clean formatting
        html = f"""
        <html><body>
        <pre style="font-family:monospace;font-size:13px;line-height:1.5;color:#1a1a1a">
{body}
        </pre>
        </body></html>
        """
        msg.attach(MIMEText(html, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
            server.sendmail(config.EMAIL_ADDRESS, config.EMAIL_ADDRESS, msg.as_string())

        print(f"  [Email] Sent: {subject}")

    except Exception as e:
        print(f"  [Email] Failed to send '{subject}': {e}")
