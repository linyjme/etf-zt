"""Verified implicit-TLS SMTP with redacted, delivery-aware outcomes.

SERVER_ACCEPTED means SMTP accepted DATA, not confirmed mailbox delivery. Once
DATA is attempted, an unconfirmed outcome is UNKNOWN and must not be retried.
The transport only reports retry eligibility; it never retries on its own.
"""

from __future__ import annotations

from email.message import EmailMessage
from email import policy
import errno
import math
import re
import smtplib
import socket
import ssl

from etf_rotation.notification_config import validate_config, _text


_ID_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_ID_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_MESSAGE_ID = re.compile(rf"<{_ID_ATOM}(?:\.{_ID_ATOM})*@{_ID_DOMAIN_LABEL}(?:\.{_ID_DOMAIN_LABEL})*>\Z")
_TRANSIENT_NETWORK_CODES = {
    errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.EHOSTUNREACH,
    errno.ENETUNREACH, errno.EPIPE, errno.ETIMEDOUT,
    10051, 10053, 10054, 10060, 10061, 10065,  # Windows socket error codes.
}


def _result(status: str, reason: str | None = None, retryable: bool = False) -> dict:
    return {"status": status, "reason": reason or status, "retryable": retryable}


def _transient_network_error(error: Exception) -> bool:
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return True
    # SMTPException inherits OSError, but a protocol rejection is not a network fault.
    if isinstance(error, smtplib.SMTPException):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, (TimeoutError, ConnectionError)) or (
        isinstance(error, OSError)
        and (error.errno in _TRANSIENT_NETWORK_CODES
             or getattr(error, "winerror", None) in _TRANSIENT_NETWORK_CODES)
    )


def _ready_config(config: object, secret: object) -> dict:
    validated = validate_config(config)
    # Explicit test/check actions remain usable while the notification switch is off.
    validate_config(dict(validated, enabled=True))
    if not _text(secret, limit=4096):
        raise ValueError("Notification authorization code is invalid")
    return validated


def _message(config: dict, subject: str, body: str, message_id: str) -> bytes:
    if not _text(subject, limit=998):
        raise ValueError("Notification subject is invalid")
    if type(body) is not str or "\x00" in body or len(body) > 1024 * 1024:
        raise ValueError("Notification body is invalid")
    if type(message_id) is not str or len(message_id) > 254 or not _MESSAGE_ID.fullmatch(message_id):
        raise ValueError("Notification Message-ID is invalid")
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = config["sender"]
    message["To"] = config["recipient"]
    message["Subject"] = subject
    message["Message-ID"] = message_id
    # Keep DATA ASCII-safe without requiring SMTP 8BITMIME negotiation.
    message.set_content(body, subtype="plain", charset="utf-8", cte="quoted-printable")
    return message.as_bytes()


class SmtpTransport:
    def __init__(self, *, smtp_factory=None, timeout: float = 10):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError("SMTP timeout must be greater than zero and at most 10 seconds")
        self._smtp_factory = smtp_factory if smtp_factory is not None else smtplib.SMTP_SSL
        self.timeout = timeout

    def check(self, config: object, secret: object, *, cancelled=None) -> dict:
        try:
            validated = _ready_config(config, secret)
        except Exception:
            return _result("FAILED", "INVALID_CONFIGURATION_OR_SECRET")
        return self._exchange(validated, secret, None, cancelled=cancelled)

    def send(self, config: object, secret: object, subject: str, body: str,
             message_id: str, *, cancelled=None) -> dict:
        try:
            validated = _ready_config(config, secret)
            message = _message(validated, subject, body, message_id)
        except Exception:
            return _result("FAILED", "INVALID_CONFIGURATION_SECRET_OR_MESSAGE")
        return self._exchange(validated, secret, message, cancelled=cancelled)

    def _exchange(self, config: dict, secret: str, message: bytes | None, *, cancelled=None) -> dict:
        connection = None
        data_attempted = False
        cancellation_latched = False

        def cancellation_requested():
            nonlocal cancellation_latched
            if not cancellation_latched and cancelled is not None:
                try:
                    cancellation_latched = bool(cancelled())
                except Exception:
                    # An unavailable preflight check cannot authorize submission.
                    cancellation_latched = True
            return cancellation_latched

        try:
            # create_default_context enforces CA validation and hostname checking.
            context = ssl.create_default_context()
            if cancellation_requested():
                return _result("CANCELLED", "CANCELLED_BEFORE_SUBMISSION")
            connection = self._smtp_factory(config["smtp_host"], config["smtp_port"],
                                            timeout=self.timeout, context=context)
            if cancellation_requested():
                return _result("CANCELLED", "CANCELLED_BEFORE_SUBMISSION")
            code, _ = connection.ehlo()
            if code != 250:
                return _result("FAILED", "SMTP_GREETING_REJECTED")
            if cancellation_requested():
                return _result("CANCELLED", "CANCELLED_BEFORE_SUBMISSION")
            connection.login(config["username"], secret)
            if message is None:
                return _result("CONNECTION_OK")
            if cancellation_requested():
                return _result("CANCELLED", "CANCELLED_BEFORE_SUBMISSION")
            code, _ = connection.mail(config["sender"])
            if code != 250:
                return _result("FAILED", "SENDER_REJECTED")
            if cancellation_requested():
                return _result("CANCELLED", "CANCELLED_BEFORE_SUBMISSION")
            code, _ = connection.rcpt(config["recipient"])
            if code not in (250, 251):
                return _result("FAILED", "RECIPIENT_REJECTED")
            if cancellation_requested():
                return _result("CANCELLED", "CANCELLED_BEFORE_SUBMISSION")
            # smtplib.data includes the DATA command, payload and final response.
            # Any disconnect from here is conservatively ambiguous, never retried.
            data_attempted = True
            code, _ = connection.data(message)
            if code != 250:
                return _result("FAILED", "MESSAGE_REJECTED")
            return _result("SERVER_ACCEPTED")
        except ssl.SSLCertVerificationError:
            return _result("FAILED", "CERTIFICATE_VERIFICATION_FAILED")
        except ssl.SSLError:
            return _result("UNKNOWN", "DELIVERY_OUTCOME_UNKNOWN") if data_attempted else _result("FAILED", "TLS_FAILED")
        except smtplib.SMTPAuthenticationError:
            return _result("FAILED", "AUTHENTICATION_FAILED")
        except smtplib.SMTPRecipientsRefused:
            return _result("FAILED", "RECIPIENT_REJECTED")
        except smtplib.SMTPSenderRefused:
            return _result("FAILED", "SENDER_REJECTED")
        except smtplib.SMTPDataError:
            return _result("FAILED", "MESSAGE_REJECTED")
        except (smtplib.SMTPServerDisconnected, OSError) as error:
            if data_attempted:
                return _result("UNKNOWN", "DELIVERY_OUTCOME_UNKNOWN")
            retryable = _transient_network_error(error)
            return _result("FAILED", "NETWORK_ERROR" if retryable else "SMTP_FAILED",
                           retryable=retryable)
        except Exception:
            if data_attempted:
                return _result("UNKNOWN", "DELIVERY_OUTCOME_UNKNOWN")
            return _result("FAILED", "SMTP_FAILED")
        finally:
            if connection is not None:
                # Cleanup cannot undo an already confirmed DATA acceptance.
                # Cancellation skips an extra network roundtrip during shutdown.
                if not cancellation_requested():
                    try:
                        connection.quit()
                    except Exception:
                        pass
                try:
                    connection.close()
                except Exception:
                    pass
