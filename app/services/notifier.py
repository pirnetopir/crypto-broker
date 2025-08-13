import smtplib, os
from email.mime.text import MIMEText
from email.utils import formataddr

def send_email(subject: str, html: str) -> None:
    host = os.getenv("EMAIL_HOST")
    port = int(os.getenv("EMAIL_PORT", "587"))
    user = os.getenv("EMAIL_USER")
    pwd  = os.getenv("EMAIL_PASS")
    to   = os.getenv("EMAIL_TO")

    if not all([host, port, user, pwd, to]):
        raise RuntimeError("Chýbajú EMAIL_* premenné v prostredí.")

    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = formataddr(("Krypto Broker", user))
    msg["To"] = to
    msg["Subject"] = subject

    s = smtplib.SMTP(host, port)
    s.starttls()
    s.login(user, pwd)
    s.sendmail(user, [to], msg.as_string())
    s.quit()
