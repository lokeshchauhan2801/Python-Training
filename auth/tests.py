"""Targeted tests for the auth (``accounts``) domain (FEAT-004).

Covers the composed User manager (email required/normalized, username defaults
to the full email, soft-deleted users hidden by ``objects`` but visible via
``all_objects`` with restore), automatic (contextvar/middleware) AND explicit
(``actor=``) audit-user population, and the ``api/v1/auth/user/me/`` endpoint.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from basics.context import reset_current_user, set_current_user

User = get_user_model()


class UserManagerTests(TestCase):
    def test_create_user_requires_email(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email="", password="x")

    def test_create_user_normalizes_email_domain(self):
        user = User.objects.create_user(email="Person@EXAMPLE.COM", password="x")

        self.assertEqual(user.email, "Person@example.com")

    def test_username_defaults_to_full_email(self):
        user = User.objects.create_user(email="a@example.com", password="x")
        self.assertEqual(user.username, "a@example.com")

    def test_explicit_username_is_kept(self):
        user = User.objects.create_user(
            email="b@example.com", username="custom", password="x"
        )
        self.assertEqual(user.username, "custom")

    def test_soft_deleted_user_hidden_by_objects_visible_via_all_objects(self):
        user = User.objects.create_user(email="c@example.com", password="x")
        user.delete()
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertTrue(User.all_objects.filter(pk=user.pk).exists())

    def test_restore_brings_user_back(self):
        user = User.objects.create_user(email="d@example.com", password="x")
        user.delete()
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        User.all_objects.get(pk=user.pk).restore()
        self.assertTrue(User.objects.filter(pk=user.pk).exists())


class UserAuditPopulationTests(TestCase):
    def test_explicit_actor_populates_audit(self):
        actor = User.objects.create_user(email="actor@example.com", password="x")
        target = User(email="t1@example.com", username="t1@example.com")
        target.set_password("x")
        target.save(actor=actor)
        self.assertEqual(target.created_by_id, actor.id)
        self.assertEqual(target.updated_by_id, actor.id)

    def test_contextvar_actor_populates_audit(self):
        actor = User.objects.create_user(email="actor2@example.com", password="x")
        token = set_current_user(actor)
        try:
            target = User(email="t2@example.com", username="t2@example.com")
            target.set_password("x")
            target.save()
        finally:
            reset_current_user(token)
        self.assertEqual(target.created_by_id, actor.id)
        self.assertEqual(target.updated_by_id, actor.id)


class CurrentUserApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="me@example.com", password="pw")

    def test_me_returns_authenticated_user_without_password(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get("/api/v1/auth/user/me/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["email"], "me@example.com")
        self.assertNotIn("password", resp.data)

    def test_me_requires_authentication(self):
        resp = self.client.get("/api/v1/auth/user/me/")
        self.assertIn(resp.status_code, (401, 403))
