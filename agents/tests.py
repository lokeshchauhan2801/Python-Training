"""Integration tests for the top-level agent-builder pages."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class AgentBuilderV2PageTests(TestCase):
    """The /agent-builder-v2/ page (Svelte canvas) inside the Unfold shell."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="builder@example.com", password="pw", is_staff=True
        )

    def test_page_renders_for_logged_in_user(self):
        self.client.force_login(self.user)

        resp = self.client.get(reverse("agents_v2:agent_builder_v2"))

        self.assertEqual(resp.status_code, 200)

    def test_page_redirects_anonymous_to_login(self):
        resp = self.client.get(reverse("agents_v2:agent_builder_v2"))

        self.assertEqual(resp.status_code, 302)
