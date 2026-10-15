"""Tests for the GLOBAL session-based active-company switcher.

Covers the owner-based active-company resolution module (``company.active``),
the ``set_active_company`` setter view, the global header switcher rendering on
admin + chat pages, admin scoping following the active company, new chat
sessions created under the active company, the no-company empty state (no 500),
and the guard against switching to an inaccessible company.

The fixture creates a SUPERUSER who OWNS TWO companies (mirroring the real
account ``lokeshchauhan058@gmail.com`` owning ``auriga`` plus a second) so
``can_switch_company`` is true and switching is actually exercised. Everything
runs against the test database (``--keepdb``) so ``agentic-db`` is never touched.
"""

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from company.active import (
    active_company_for_request,
    allowed_companies_for_user,
    can_switch_company,
)
from company.models import Company

User = get_user_model()

_MEDIA = tempfile.mkdtemp(prefix="company-active-media-")


def _request_with_session(user, session=None):
    """A GET request carrying a real DB-backed session, for the resolver."""
    request = RequestFactory().get("/")
    request.user = user
    request.session = session if session is not None else SessionStore()
    return request


class ActiveCompanyResolutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_owner = User.objects.create_superuser(
            email="lokeshchauhan058@gmail.com", password="adminpass123"
        )
        cls.company_a = Company.objects.create(
            owner=cls.super_owner, name="auriga", slug="auriga"
        )
        cls.company_b = Company.objects.create(
            owner=cls.super_owner, name="second", slug="second"
        )
        cls.single_owner = User.objects.create_user(
            email="single@example.com", password="x"
        )
        cls.single_co = Company.objects.create(
            owner=cls.single_owner, name="Solo", slug="solo"
        )
        cls.no_company_user = User.objects.create_user(
            email="nocompany@example.com", password="x"
        )
        cls.other_owner = User.objects.create_user(
            email="other@example.com", password="x"
        )
        cls.other_co = Company.objects.create(
            owner=cls.other_owner, name="Other", slug="other"
        )

    def test_allowed_companies_owner_scoped(self):
        allowed = allowed_companies_for_user(self.single_owner)
        self.assertEqual(set(allowed), {self.single_co})
        # Another owner's company is never visible.
        self.assertNotIn(self.other_co, allowed)

    def test_allowed_companies_superuser_sees_all_active(self):
        allowed = set(allowed_companies_for_user(self.super_owner))
        self.assertEqual(
            allowed,
            {self.company_a, self.company_b, self.single_co, self.other_co},
        )

    def test_allowed_companies_excludes_inactive(self):
        inactive = Company.objects.create(
            owner=self.single_owner, name="Dead", slug="dead", is_active=False
        )
        self.assertNotIn(inactive, allowed_companies_for_user(self.single_owner))

    def test_can_switch_company_true_with_two_owned(self):
        self.assertTrue(can_switch_company(self.super_owner))

    def test_can_switch_company_false_with_one(self):
        self.assertFalse(can_switch_company(self.single_owner))
        self.assertFalse(can_switch_company(self.no_company_user))

    def test_resolves_valid_session_selection(self):
        session = SessionStore()
        session["active_company_id"] = str(self.company_b.id)
        request = _request_with_session(self.super_owner, session)
        self.assertEqual(active_company_for_request(request), self.company_b)

    def test_pops_and_redefaults_invalid_session_id(self):
        session = SessionStore()
        # A company this user cannot access.
        session["active_company_id"] = str(self.other_co.id)
        request = _request_with_session(self.single_owner, session)
        resolved = active_company_for_request(request)
        # Falls back to the user's own company and rewrites the session.
        self.assertEqual(resolved, self.single_co)
        self.assertEqual(
            request.session["active_company_id"], str(self.single_co.id)
        )

    def test_auto_selects_single_company(self):
        request = _request_with_session(self.single_owner)
        self.assertEqual(active_company_for_request(request), self.single_co)
        self.assertEqual(
            request.session["active_company_id"], str(self.single_co.id)
        )

    def test_returns_none_for_user_without_company(self):
        request = _request_with_session(self.no_company_user)
        self.assertIsNone(active_company_for_request(request))
        self.assertNotIn("active_company_id", request.session)


class SetActiveCompanyViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_owner = User.objects.create_superuser(
            email="lokeshchauhan058@gmail.com", password="adminpass123"
        )
        cls.company_a = Company.objects.create(
            owner=cls.super_owner, name="auriga", slug="auriga"
        )
        cls.company_b = Company.objects.create(
            owner=cls.super_owner, name="second", slug="second"
        )
        cls.other_owner = User.objects.create_user(
            email="other@example.com", password="x"
        )
        cls.other_co = Company.objects.create(
            owner=cls.other_owner, name="Other", slug="other"
        )

    def setUp(self):
        self.url = reverse("company:set_active_company")

    def test_switch_stores_company_in_session(self):
        self.client.force_login(self.super_owner)
        resp = self.client.post(
            self.url, {"company": str(self.company_b.id), "next": "/admin/"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            self.client.session["active_company_id"], str(self.company_b.id)
        )

    def test_ajax_switch_returns_json(self):
        self.client.force_login(self.super_owner)
        resp = self.client.post(
            self.url,
            {"company": str(self.company_a.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["active_company_id"], str(self.company_a.id))

    def test_cannot_switch_to_inaccessible_company(self):
        """A non-superuser cannot switch to another owner's company."""
        self.client.force_login(self.other_owner)
        # Seed a valid active company first.
        self.client.post(self.url, {"company": str(self.other_co.id)})
        resp = self.client.post(self.url, {"company": str(self.company_a.id)})
        self.assertEqual(resp.status_code, 403)
        # The session key is unchanged (still the user's own company).
        self.assertEqual(
            self.client.session["active_company_id"], str(self.other_co.id)
        )

    def test_get_rejected(self):
        self.client.force_login(self.super_owner)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 405)

    def test_unsafe_next_falls_back_to_root(self):
        self.client.force_login(self.super_owner)
        resp = self.client.post(
            self.url,
            {"company": str(self.company_a.id), "next": "https://evil.example.com/x"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/")


class SwitcherRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_owner = User.objects.create_superuser(
            email="lokeshchauhan058@gmail.com", password="adminpass123"
        )
        cls.company_a = Company.objects.create(
            owner=cls.super_owner, name="auriga", slug="auriga"
        )
        cls.company_b = Company.objects.create(
            owner=cls.super_owner, name="second", slug="second"
        )
        cls.single_owner = User.objects.create_user(
            email="single@example.com", password="x", is_staff=True
        )
        cls.single_co = Company.objects.create(
            owner=cls.single_owner, name="Solo", slug="solo"
        )

    def test_switcher_on_admin_index_for_two_company_user(self):
        self.client.force_login(self.super_owner)
        resp = self.client.get(reverse("admin:index"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/company/set-active-company/")

    def test_switcher_on_documents_changelist(self):
        self.client.force_login(self.super_owner)
        resp = self.client.get(reverse("admin:documents_document_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/company/set-active-company/")

    def test_switcher_on_chat_page(self):
        self.client.force_login(self.super_owner)
        resp = self.client.get(reverse("chat:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/company/set-active-company/")

    def test_no_switcher_form_for_single_company_user(self):
        self.client.force_login(self.single_owner)
        resp = self.client.get(reverse("admin:index"))
        self.assertEqual(resp.status_code, 200)
        # The auto-submit switcher form is absent; the single company name shows.
        self.assertNotContains(resp, 'name="company" aria-label')
        self.assertContains(resp, "Solo")


@override_settings(MEDIA_ROOT=_MEDIA)
class AdminScopingFollowsActiveCompanyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.super_owner = User.objects.create_superuser(
            email="lokeshchauhan058@gmail.com", password="adminpass123"
        )
        cls.company_a = Company.objects.create(
            owner=cls.super_owner, name="auriga", slug="auriga"
        )
        cls.company_b = Company.objects.create(
            owner=cls.super_owner, name="second", slug="second"
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def _make_doc(self, company, title):
        from documents.models import Document

        doc = Document(
            company=company,
            uploaded_by=self.super_owner,
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
            title=title,
            file=SimpleUploadedFile(f"{title}.txt", b"hello there world"),
            file_type="txt",
        )
        doc.save(actor=self.super_owner)
        return doc

    def _set_active(self, company):
        session = self.client.session
        session["active_company_id"] = str(company.id)
        session.save()

    def test_changelist_shows_only_active_company_rows(self):
        doc_a = self._make_doc(self.company_a, "doc-a")
        doc_b = self._make_doc(self.company_b, "doc-b")
        self.client.force_login(self.super_owner)

        self._set_active(self.company_a)
        resp = self.client.get(reverse("admin:documents_document_changelist"))
        self.assertContains(resp, "doc-a")
        self.assertNotContains(resp, "doc-b")

        self._set_active(self.company_b)
        resp = self.client.get(reverse("admin:documents_document_changelist"))
        self.assertContains(resp, "doc-b")
        self.assertNotContains(resp, "doc-a")

    def test_set_active_switches_changelist_scope_and_chat_sessions(self):
        from chat.models import ConversationSession

        doc_a = self._make_doc(self.company_a, "docA")
        doc_b = self._make_doc(self.company_b, "docB")
        sess_a = ConversationSession(company=self.company_a, user=self.super_owner)
        sess_a.save(actor=self.super_owner)
        sess_b = ConversationSession(company=self.company_b, user=self.super_owner)
        sess_b.save(actor=self.super_owner)

        self.client.force_login(self.super_owner)
        self.client.post(
            reverse("company:set_active_company"),
            {"company": str(self.company_b.id)},
        )
        self.assertEqual(
            self.client.session["active_company_id"], str(self.company_b.id)
        )
        # Documents changelist follows company B.
        resp = self.client.get(reverse("admin:documents_document_changelist"))
        self.assertContains(resp, "docB")
        self.assertNotContains(resp, "docA")
        # Chat session list (filtered by the active company) follows company B.
        resp = self.client.get(
            reverse("chat:sessions_json") + "?company=" + str(self.company_b.id)
        )
        ids = {row["id"] for row in resp.json()["sessions"]}
        self.assertEqual(ids, {str(sess_b.id)})

    def test_add_document_uses_active_company(self):
        from documents import services
        from documents.models import Document

        self.client.force_login(self.super_owner)
        self._set_active(self.company_b)

        def _inline_dispatch(document_id, backend=None):
            return "job", "local-background"

        with mock.patch(
            "documents.admin.dispatch_document_processing",
            side_effect=_inline_dispatch,
        ), mock.patch.object(
            services, "get_llm_client", lambda: mock.Mock()
        ):
            resp = self.client.post(
                "/admin/documents/document/add/",
                {
                    "file": SimpleUploadedFile("new.txt", b"hello there world"),
                    "source_url": "",
                },
            )
        self.assertEqual(resp.status_code, 302)
        created = Document.objects.order_by("-created_at").first()
        self.assertEqual(created.company_id, self.company_b.id)


@override_settings(MEDIA_ROOT=_MEDIA)
class NoCompanyEmptyStateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Permission

        cls.user = User.objects.create_user(
            email="empty@example.com", password="x", is_staff=True
        )
        perms = Permission.objects.filter(
            content_type__app_label="documents",
            content_type__model="document",
        )
        cls.user.user_permissions.add(*perms)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def test_changelist_200_empty_for_no_company_user(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("admin:documents_document_changelist"))
        self.assertEqual(resp.status_code, 200)

    def test_add_post_without_company_is_clean_403_not_500(self):
        from documents.models import Document

        self.client.force_login(self.user)
        with mock.patch(
            "documents.admin.dispatch_document_processing",
            side_effect=lambda *a, **k: ("job", "local-background"),
        ):
            resp = self.client.post(
                "/admin/documents/document/add/",
                {
                    "file": SimpleUploadedFile("x.txt", b"hello there world"),
                    "source_url": "",
                },
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Document.all_objects.count(), 0)

    def test_chat_home_200_with_empty_state(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("chat:home"))
        self.assertEqual(resp.status_code, 200)
