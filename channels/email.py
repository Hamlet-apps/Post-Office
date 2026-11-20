"""SMTP for email senders (Gmail and generic SMTP).

open_connection(sender)  -> a logged-in smtplib.SMTP (caller closes it)
test_connection(sender)  -> (ok, error_text): connect and authenticate only
"""
import smtplib
import ssl

TIMEOUT = 20


def open_connection(sender):
    context = ssl.create_default_context()
    if sender.tls_mode == "ssl":
        smtp = smtplib.SMTP_SSL(sender.host, sender.port, timeout=TIMEOUT, context=context)
    else:
        smtp = smtplib.SMTP(sender.host, sender.port, timeout=TIMEOUT)
        smtp.ehlo()
        if sender.tls_mode == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
    if sender.username:
        # Gmail app passwords are shown with spaces; SMTP wants them without.
        password = sender.password.replace(" ", "") if sender.kind == "gmail" else sender.password
        smtp.login(sender.username, password)
    return smtp


def describe_error(exc):
    """The server's error in words staff can act on."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"The username or password was refused ({exc.smtp_code} {detail.strip()})"
    if isinstance(exc, smtplib.SMTPResponseException):
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"{exc.smtp_code} {detail.strip()}"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return f"Could not reach the mail server ({exc})"
    return str(exc) or exc.__class__.__name__


def test_connection(sender):
    if not sender.host:
        return False, "No mail server address was given"
    try:
        smtp = open_connection(sender)
    except Exception as exc:  # noqa: BLE001 - every failure is reported to staff
        return False, describe_error(exc)
    try:
        smtp.quit()
    except Exception:  # noqa: BLE001
        pass
    return True, ""


# --------------------------------------------------------------------------- building and sending one email

from email.message import EmailMessage  # noqa: E402
from email.utils import formataddr, formatdate, make_msgid  # noqa: E402


class SendFailure(Exception):
    """Why one email did not go out, in words for staff.

    temporary: worth retrying later (connection trouble, SMTP 4xx)
    bounced:   the server refused this recipient for good (SMTP 5xx)
    """

    def __init__(self, reason, temporary=False, bounced=False, connection_lost=False):
        super().__init__(reason)
        self.reason = reason
        self.temporary = temporary
        self.bounced = bounced
        self.connection_lost = connection_lost


def build_message(sender, to_email, subject, html, text="", *, reply_to="", headers=None):
    """A MIME email with a plain-text part and an HTML part."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender.from_name, sender.from_email))
    msg["To"] = to_email
    msg["Subject"] = " ".join((subject or "").split())
    msg["Date"] = formatdate(localtime=False)
    domain = sender.from_email.rsplit("@", 1)[-1] if "@" in sender.from_email else None
    msg["Message-ID"] = make_msgid(domain=domain)
    if reply_to and reply_to != sender.from_email:
        msg["Reply-To"] = reply_to
    for name, value in (headers or {}).items():
        if value:
            msg[name] = value
    msg.set_content(text or " ")
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _detail(value):
    return (value.decode(errors="replace") if isinstance(value, bytes) else str(value)).strip()


def classify(exc):
    """Turn an smtplib or socket error into a SendFailure."""
    if isinstance(exc, SendFailure):
        return exc
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        code, detail = next(iter(exc.recipients.values()), (550, b""))
        if 500 <= int(code) < 600:
            return SendFailure(f"The mail server refused this address ({code} {_detail(detail)})", bounced=True)
        return SendFailure(f"The mail server asked us to try this address later ({code} {_detail(detail)})", temporary=True)
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return SendFailure("The mail server closed the connection", temporary=True, connection_lost=True)
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(exc.smtp_code or 0)
        text = f"{code} {_detail(exc.smtp_error)}"
        if 400 <= code < 500:
            return SendFailure(f"The mail server asked us to try again later ({text})", temporary=True)
        return SendFailure(f"The mail server refused the message ({text})")
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, smtplib.SMTPException)):
        return SendFailure(describe_error(exc), temporary=True, connection_lost=True)
    return SendFailure(describe_error(exc))


def send_mime(smtp, sender, to_email, mime):
    """Send one built message over an open connection. Raises SendFailure."""
    try:
        smtp.send_message(mime, from_addr=sender.from_email, to_addrs=[to_email])
    except Exception as exc:  # noqa: BLE001 - every failure becomes a reason for staff
        raise classify(exc) from exc


def close_quietly(smtp):
    if smtp is None:
        return
    try:
        smtp.quit()
    except Exception:  # noqa: BLE001
        pass
