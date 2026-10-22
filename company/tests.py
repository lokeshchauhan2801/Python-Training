"""Targeted tests for the company domain (FEAT-004).

Covers default soft-delete filtering + ``all_objects`` recovery + restore,
automatic/explicit audit-user population on ``Company``, company isolation
(owner scoping), and the company API (owner forced to the request user; a client
cannot create or see another owner's company).
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from basics.context import reset_current_user, set_current_user

from .models import Company

User = get_user_model()


class CompanySoftDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")

    def test_default_manager_hides_soft_deleted(self):
        c = Company.objects.create(owner=self.owner, name="A", slug="a")
        c.delete()
        self.assertFalse(Company.objects.filter(pk=c.pk).exists())
        self.assertTrue(Company.all_objects.filter(pk=c.pk).exists())

    def test_restore_recovers_soft_deleted(self):
        c = Company.objects.create(owner=self.owner, name="B", slug="b")
        c.delete()
        Company.all_objects.get(pk=c.pk).restore()
        self.assertTrue(Company.objects.filter(pk=c.pk).exists())

    def test_queryset_bulk_soft_delete_and_restore(self):
        Company.objects.create(owner=self.owner, name="C", slug="c")
        Company.objects.create(owner=self.owner, name="D", slug="d")
        Company.objects.all().delete()
        self.assertEqual(Company.objects.count(), 0)
        self.assertEqual(Company.all_objects.count(), 2)
        Company.all_objects.all().restore()
        self.assertEqual(Company.objects.count(), 2)


class CompanyAuditPopulationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.actor = User.objects.create_user(email="actor@example.com", password="x")

    def test_explicit_actor(self):
        c = Company(owner=self.owner, name="E", slug="e")
        c.save(actor=self.actor)
        self.assertEqual(c.created_by_id, self.actor.id)
        self.assertEqual(c.updated_by_id, self.actor.id)

    def test_contextvar_actor(self):
        token = set_current_user(self.actor)
        try:
            c = Company(owner=self.owner, name="F", slug="f")
            c.save()
        finally:
            reset_current_user(token)
        self.assertEqual(c.created_by_id, self.actor.id)


class CompanyApiScopingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.alice = User.objects.create_user(email="alice@example.com", password="x")
        self.bob = User.objects.create_user(email="bob@example.com", password="x")
        self.alice_co = Company.objects.create(owner=self.alice, name="AC", slug="ac")
        self.bob_co = Company.objects.create(owner=self.bob, name="BC", slug="bc")

    def test_list_scoped_to_owner(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.get("/api/v1/company/company/")
        self.assertEqual(resp.status_code, 200)
        slugs = {row["slug"] for row in resp.data}
        self.assertEqual(slugs, {"ac"})

    def test_create_forces_owner_to_request_user(self):
        self.client.force_authenticate(self.alice)

        resp = self.client.post(
            "/api/v1/company/company/",
            {"name": "New", "slug": "new", "owner": str(self.bob.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        created = Company.objects.get(slug="new")
        self.assertEqual(created.owner_id, self.alice.id)
        self.assertEqual(created.created_by_id, self.alice.id)

    def test_cannot_see_another_owners_company_detail(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.get(f"/api/v1/company/company/{self.bob_co.id}/")
        self.assertEqual(resp.status_code, 404)
