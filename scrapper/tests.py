"""Targeted tests for the scrapper domain (FEAT-003).

Covers the Status.PENDING default (NIT 5) and the SSRF policy: scheme
allow-list, embedded credentials, disallowed ports, and private/loopback IP
rejection across all resolved addresses (getaddrinfo mocked).
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company

from .models import ScrapeJob
from .services import ScrapeService, ScrapeValidationError, _validate_url

User = get_user_model()


def _addrinfo(ip):
    # (family, type, proto, canonname, sockaddr) with sockaddr[0] == ip
    return [(2, 1, 6, "", (ip, 443))]


class ScrapeJobModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=owner, name="C", slug="c")

    def test_status_defaults_to_pending(self):
        job = ScrapeJob.objects.create(company=self.company, url="https://example.com")
        self.assertEqual(job.status, ScrapeJob.Status.PENDING)
        self.assertEqual(job.status, "pending")


class SSRFValidationTests(TestCase):
    def test_rejects_non_http_scheme(self):
        with self.assertRaises(ScrapeValidationError):
            _validate_url("file:///etc/passwd")

    def test_rejects_embedded_credentials(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(ScrapeValidationError):
                _validate_url("https://user:pass@example.com/")

    def test_rejects_disallowed_port(self):
        with self.assertRaises(ScrapeValidationError):
            _validate_url("http://example.com:8080/")

    def test_rejects_loopback_address(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            with self.assertRaises(ScrapeValidationError):
                _validate_url("http://localhost.evil.test/")

    def test_rejects_private_address(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("10.0.0.5")):
            with self.assertRaises(ScrapeValidationError):
                _validate_url("http://internal.evil.test/")

    def test_rejects_if_any_resolved_address_is_private(self):
        infos = _addrinfo("93.184.216.34") + _addrinfo("169.254.169.254")
        with mock.patch("socket.getaddrinfo", return_value=infos):
            with self.assertRaises(ScrapeValidationError):
                _validate_url("http://rebind.evil.test/")

    def test_accepts_public_address(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            host, port = _validate_url("https://example.com/")
        self.assertEqual(host, "example.com")
        self.assertEqual(port, 443)


class ScrapeServiceRunTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=owner, name="C", slug="c")

    def test_run_records_error_on_blocked_url_without_raising(self):
        job = ScrapeJob.objects.create(company=self.company, url="file:///etc/passwd")
        result = ScrapeService().run(job)
        self.assertEqual(result.status, ScrapeJob.Status.ERROR)
        self.assertEqual(result.error, "URL not allowed")
