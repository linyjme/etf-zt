from __future__ import annotations

from email import policy
from email.parser import BytesParser
import errno
import importlib
import importlib.util
import inspect
import smtplib
import socket
import ssl
import unittest


class FakeSmtp:
    """Only the network boundary is replaced; message/config logic stays real."""

    def __init__(self, failure_stage=None, failure=None, responses=None):
        self.failure_stage = failure_stage
        self.failure = failure
        self.responses = responses or {}
        self.calls = []
        self.message = None
        self.closed = False

    def step(self, name, *args):
        self.calls.append((name, args))
        if self.failure_stage == name:
            raise self.failure
        return self.responses.get(name, (250, b"synthetic success"))

    def ehlo(self):
        return self.step("ehlo")

    def login(self, username, secret):
        return self.step("login", username, secret)

    def mail(self, sender):
        return self.step("mail", sender)

    def rcpt(self, recipient):
        return self.step("rcpt", recipient)

    def data(self, message):
        self.message = message
        return self.step("data", message)

    def quit(self):
        return self.step("quit")

    def close(self):
        self.closed = True


class NotificationMailTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("etf_rotation.notification_mail"),
                             "SMTP transport module must exist")
        self.api = importlib.import_module("etf_rotation.notification_mail")
        config_api = importlib.import_module("etf_rotation.notification_config")
        self.config = config_api.default_config()
        self.config.update(sender="sender@163.com", recipient="recipient@example.com",
                           username="sender@163.com")
        self.secret = "synthetic-secret-do-not-print"
        self.message_id = "<notification-123@local.invalid>"
        self.connection_args = None

    def transport(self, fake=None, connection_error=None, **kwargs):
        self.connection_args = None
        self.fake = fake or FakeSmtp()
        def factory(host, port, **options):
            self.connection_args = (host, port, options)
            if connection_error is not None:
                raise connection_error
            return self.fake
        return self.api.SmtpTransport(smtp_factory=factory, **kwargs)

    def send(self, transport, **kwargs):
        return transport.send(self.config, self.secret, kwargs.get("subject", "行情提醒"),
                              kwargs.get("body", "价格上涨 1%。\n仅作提醒。"),
                              kwargs.get("message_id", self.message_id))

    def test_connection_check_logs_in_using_verified_implicit_tls_without_sending(self):
        result = self.transport().check(self.config, self.secret)
        self.assertEqual({"status": "CONNECTION_OK", "reason": "CONNECTION_OK", "retryable": False}, result)
        host, port, options = self.connection_args
        self.assertEqual(("smtp.163.com", 465), (host, port))
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 10)
        self.assertEqual(ssl.CERT_REQUIRED, options["context"].verify_mode)
        self.assertTrue(options["context"].check_hostname)
        self.assertEqual(["ehlo", "login", "quit"], [name for name, _ in self.fake.calls])
        self.assertTrue(self.fake.closed)

    def test_send_builds_plain_utf8_message_with_stable_message_id_and_envelope(self):
        result = self.send(self.transport())
        self.assertEqual({"status": "SERVER_ACCEPTED", "reason": "SERVER_ACCEPTED", "retryable": False}, result)
        message = BytesParser(policy=policy.default).parsebytes(self.fake.message)
        self.assertEqual("行情提醒", message["Subject"])
        self.assertEqual(self.message_id, message["Message-ID"])
        self.assertEqual("sender@163.com", message["From"])
        self.assertEqual("recipient@example.com", message["To"])
        self.assertEqual("text/plain", message.get_content_type())
        self.assertEqual("utf-8", message.get_content_charset())
        self.assertIn("价格上涨 1%", message.get_content())
        self.assertNotIn(self.secret.encode(), self.fake.message)
        self.assertIn(("mail", ("sender@163.com",)), self.fake.calls)
        self.assertIn(("rcpt", ("recipient@example.com",)), self.fake.calls)
        self.assertTrue(self.fake.closed)

    def test_accepted_message_stays_accepted_when_quit_disconnects(self):
        result = self.send(self.transport(FakeSmtp("quit", OSError(self.secret))))
        self.assertEqual("SERVER_ACCEPTED", result["status"])
        self.assertFalse(result["retryable"])
        self.assertTrue(self.fake.closed)

    def test_chinese_mail_uses_ascii_safe_data_without_8bitmime_negotiation(self):
        body = "价格上涨 1%。\n仅作观察，请核验最新行情。"
        result = self.send(self.transport(), body=body)
        self.assertEqual("SERVER_ACCEPTED", result["status"])
        self.assertTrue(self.fake.message.isascii())
        message = BytesParser(policy=policy.default).parsebytes(self.fake.message)
        self.assertEqual("行情提醒", message["Subject"])
        self.assertEqual(body + "\n", message.get_content().replace("\r\n", "\n"))

    def test_cancellation_stops_before_each_pre_data_stage_without_quit(self):
        for completed_stage in (None, "connect", "ehlo", "login", "mail", "rcpt"):
            with self.subTest(completed_stage=completed_stage):
                transport = self.transport()
                self.assertIn("cancelled", inspect.signature(transport.send).parameters)

                def cancelled():
                    return (completed_stage is None
                            or completed_stage == "connect" and self.connection_args is not None
                            or any(name == completed_stage for name, _ in self.fake.calls))

                result = transport.send(self.config, self.secret, "观察", "合成正文",
                                        self.message_id, cancelled=cancelled)
                self.assertEqual({"status": "CANCELLED", "reason": "CANCELLED_BEFORE_SUBMISSION",
                                  "retryable": False}, result)
                self.assertIsNone(self.fake.message)
                self.assertNotIn("quit", [name for name, _ in self.fake.calls])
                self.assertEqual(self.fake.closed, completed_stage is not None)

    def test_connection_check_obeys_cancellation_before_each_network_stage(self):
        for completed_stage in (None, "connect", "ehlo"):
            with self.subTest(completed_stage=completed_stage):
                transport = self.transport()
                self.assertIn("cancelled", inspect.signature(transport.check).parameters)

                def cancelled():
                    return (completed_stage is None
                            or completed_stage == "connect" and self.connection_args is not None
                            or any(name == completed_stage for name, _ in self.fake.calls))

                result = transport.check(self.config, self.secret, cancelled=cancelled)
                self.assertEqual("CANCELLED", result["status"])
                self.assertFalse(result["retryable"])
                self.assertIsNone(self.fake.message)
                self.assertNotIn("quit", [name for name, _ in self.fake.calls])

    def test_cancellation_during_data_preserves_accepted_or_unknown_without_quit(self):
        for failure, expected in ((None, "SERVER_ACCEPTED"),
                                  (TimeoutError(self.secret), "UNKNOWN")):
            with self.subTest(expected=expected):
                fake = FakeSmtp("data", failure) if failure else FakeSmtp()
                transport = self.transport(fake)
                self.assertIn("cancelled", inspect.signature(transport.send).parameters)
                result = transport.send(self.config, self.secret, "观察", "合成正文",
                                        self.message_id, cancelled=lambda: fake.message is not None)
                self.assertEqual(expected, result["status"])
                self.assertFalse(result["retryable"])
                self.assertNotIn("quit", [name for name, _ in fake.calls])
                self.assertTrue(fake.closed)

    def test_failed_cancellation_check_blocks_data_without_exposing_error(self):
        transport = self.transport()
        self.assertIn("cancelled", inspect.signature(transport.send).parameters)

        def cancelled():
            if any(name == "rcpt" for name, _ in self.fake.calls):
                raise ValueError(self.secret)
            return False

        result = transport.send(self.config, self.secret, "观察", "合成正文",
                                self.message_id, cancelled=cancelled)
        self.assertEqual("CANCELLED", result["status"])
        self.assertFalse(result["retryable"])
        self.assertNotIn(self.secret, str(result))
        self.assertIsNone(self.fake.message)
        self.assertNotIn("quit", [name for name, _ in self.fake.calls])
        self.assertTrue(self.fake.closed)

    def test_false_cancellation_callback_keeps_normal_delivery_and_cleanup(self):
        transport = self.transport()
        self.assertIn("cancelled", inspect.signature(transport.send).parameters)
        result = transport.send(self.config, self.secret, "观察", "合成正文",
                                self.message_id, cancelled=lambda: False)
        self.assertEqual("SERVER_ACCEPTED", result["status"])
        self.assertIn("quit", [name for name, _ in self.fake.calls])
        self.assertTrue(self.fake.closed)

    def test_successful_check_stays_successful_when_quit_disconnects(self):
        result = self.transport(FakeSmtp("quit", OSError(self.secret))).check(self.config, self.secret)
        self.assertEqual("CONNECTION_OK", result["status"])
        self.assertTrue(self.fake.closed)

    def test_timeout_is_bounded_and_invalid_timeouts_are_rejected(self):
        self.send(self.transport(timeout=3))
        self.assertEqual(3, self.connection_args[2]["timeout"])
        for timeout in (True, 0, -1, 11, float("nan"), float("inf"), "10"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.transport(timeout=timeout)

    def test_pre_data_network_failure_is_retryable_and_sanitized(self):
        for stage in ("connect", "ehlo", "login", "mail", "rcpt"):
            with self.subTest(stage=stage):
                error = TimeoutError(self.secret)
                transport = self.transport(connection_error=error) if stage == "connect" else self.transport(FakeSmtp(stage, error))
                result = self.send(transport)
                self.assertEqual("FAILED", result["status"])
                self.assertTrue(result["retryable"])
                self.assertNotIn(self.secret, str(result))
                self.assertIsNone(self.fake.message)

    def test_data_disconnect_or_timeout_is_unknown_and_never_retryable(self):
        for error in (TimeoutError(self.secret), smtplib.SMTPServerDisconnected(self.secret), OSError(self.secret)):
            with self.subTest(error=type(error)):
                result = self.send(self.transport(FakeSmtp("data", error)))
                self.assertEqual("UNKNOWN", result["status"])
                self.assertFalse(result["retryable"])
                self.assertNotIn(self.secret, str(result))
                self.assertTrue(self.fake.closed)

    def test_explicit_data_rejection_is_failed_and_not_retried(self):
        for fake in (FakeSmtp(responses={"data": (451, self.secret.encode())}),
                     FakeSmtp("data", smtplib.SMTPDataError(554, self.secret.encode()))):
            result = self.send(self.transport(fake))
            self.assertEqual("FAILED", result["status"])
            self.assertFalse(result["retryable"])
            self.assertNotIn(self.secret, str(result))

    def test_authentication_recipient_sender_and_certificate_failures_never_retry(self):
        failures = (
            ("login", smtplib.SMTPAuthenticationError(535, self.secret.encode())),
            ("rcpt", smtplib.SMTPRecipientsRefused({"recipient@example.com": (550, self.secret)})),
            ("mail", smtplib.SMTPSenderRefused(550, self.secret.encode(), "sender@163.com")),
            ("connect", ssl.SSLCertVerificationError(self.secret)),
            ("connect", ssl.SSLError(self.secret)),
        )
        for stage, error in failures:
            with self.subTest(stage=stage, error=type(error)):
                transport = self.transport(connection_error=error) if stage == "connect" else self.transport(FakeSmtp(stage, error))
                result = self.send(transport)
                self.assertEqual("FAILED", result["status"])
                self.assertFalse(result["retryable"])
                self.assertNotIn(self.secret, str(result))

    def test_explicit_pre_data_rejections_do_not_send_or_retry(self):
        for stage, code in (("ehlo", 500), ("mail", 451), ("rcpt", 450), ("rcpt", 550)):
            with self.subTest(stage=stage):
                result = self.send(self.transport(FakeSmtp(responses={stage: (code, self.secret.encode())})))
                self.assertEqual("FAILED", result["status"])
                self.assertFalse(result["retryable"])
                self.assertIsNone(self.fake.message)

    def test_only_transient_network_errors_can_be_retried(self):
        permanent = (
            smtplib.SMTPNotSupportedError(self.secret),
            smtplib.SMTPResponseException(550, self.secret.encode()),
            smtplib.SMTPException(self.secret),
            socket.gaierror(socket.EAI_NONAME, self.secret),
            PermissionError(errno.EACCES, self.secret),
            OSError(errno.EBADF, self.secret),
        )
        for error in permanent:
            with self.subTest(error=type(error), code=getattr(error, "errno", None)):
                result = self.send(self.transport(connection_error=error))
                self.assertEqual("FAILED", result["status"])
                self.assertFalse(result["retryable"])
                self.assertNotIn(self.secret, str(result))
        for error in (socket.gaierror(socket.EAI_AGAIN, self.secret),
                      ConnectionRefusedError(errno.ECONNREFUSED, self.secret),
                      OSError(errno.ENETUNREACH, self.secret)):
            result = self.send(self.transport(connection_error=error))
            self.assertTrue(result["retryable"])

    def test_invalid_boundary_inputs_never_open_a_connection(self):
        for field, value in (("recipient", "bad"), ("sender", "x@x.com\r\nBcc: x@evil.com"),
                             ("smtp_host", "evil.example"), ("username", "")):
            with self.subTest(field=field):
                transport = self.transport()
                invalid = dict(self.config, **{field: value})
                result = transport.send(invalid, self.secret, "Subject", "Body", self.message_id)
                self.assertEqual("FAILED", result["status"])
                self.assertFalse(result["retryable"])
                self.assertIsNone(self.connection_args)
        self.config["recipient"] = ""
        result = self.transport().check(self.config, self.secret)
        self.assertEqual("FAILED", result["status"])
        self.assertIsNone(self.connection_args)

    def test_header_injection_and_invalid_message_body_are_rejected_before_network(self):
        for overrides in ({"subject": "hello\r\nBcc: x@evil.com"}, {"subject": "hello\x00"},
                          {"message_id": "<valid@local>\nX: y"}, {"message_id": "not-a-message-id"},
                          {"message_id": "<..@local>"}, {"message_id": "<good@...>"},
                          {"message_id": "<one@local> <two@local>"}, {"body": None}, {"body": "x\x00y"}):
            with self.subTest(overrides=overrides):
                result = self.send(self.transport(), **overrides)
                self.assertEqual("FAILED", result["status"])
                self.assertFalse(result["retryable"])
                self.assertIsNone(self.connection_args)

    def test_missing_or_malformed_secret_never_connects(self):
        for secret in (None, "", b"bytes", "x\r\ny", "x\x00y"):
            with self.subTest(secret=secret):
                result = self.transport().check(self.config, secret)
                self.assertEqual("FAILED", result["status"])
                self.assertFalse(result["retryable"])
                self.assertIsNone(self.connection_args)


if __name__ == "__main__":
    unittest.main()
