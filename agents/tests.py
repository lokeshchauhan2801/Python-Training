"""Tests for the flat two-table agent model (Tool + Agent), the runtime walk /
graph build, the API (activate/load + scoping), the orchestrator gate, and the
seed command."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company

from .models import Agent, Tool

User = get_user_model()


# --------------------------------------------------------------------------- #
# Model tests
# --------------------------------------------------------------------------- #
class ToolModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    def test_clean_rejects_unknown_non_langfuse_slug(self):
        tool = Tool(
            company=self.company,
            slug="not_a_real_handler",
            name="X",
            type=Tool.TYPE_GENERAL_ASSISTANT,
        )
        with self.assertRaises(ValidationError):
            tool.clean()

    def test_clean_accepts_any_slug_when_langfuse(self):
        tool = Tool(
            company=self.company,
            slug="some_prompt_pointer",
            name="X",
            type=Tool.TYPE_LANGFUSE,
        )
        tool.clean()  # no raise
        tool.save()
        self.assertTrue(Tool.objects.filter(pk=tool.pk).exists())

    def test_clean_accepts_registry_slug(self):
        tool = Tool(
            company=self.company,
            slug="general_assistant",
            name="GA",
            type=Tool.TYPE_GENERAL_ASSISTANT,
        )
        tool.save()
        self.assertTrue(Tool.objects.filter(pk=tool.pk).exists())

    def test_prompt_name_property(self):
        langfuse = Tool(
            company=self.company, slug="my_prompt", name="L", type=Tool.TYPE_LANGFUSE
        )
        self.assertEqual(langfuse.prompt_name, "my_prompt")
        builtin = Tool(
            company=self.company,
            slug="general_assistant",
            name="G",
            type=Tool.TYPE_GENERAL_ASSISTANT,
        )
        self.assertEqual(builtin.prompt_name, "")


class AgentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    def test_slug_unique_per_company_live_rows(self):
        Agent.objects.create(company=self.company, slug="a")
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            Agent.objects.create(company=self.company, slug="a")

    def test_soft_deleted_row_frees_slug(self):
        a = Agent.objects.create(company=self.company, slug="a")
        a.delete()
        # A new live row with the same slug is now allowed.
        Agent.objects.create(company=self.company, slug="a")
        self.assertEqual(Agent.objects.filter(company=self.company, slug="a").count(), 1)


# --------------------------------------------------------------------------- #
# Runtime walk + graph build tests
# --------------------------------------------------------------------------- #
def _svc():
    from chat.services.langgraph_service import LangGraphChatService

    return LangGraphChatService.__new__(LangGraphChatService)


class WalkChainTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    def _chain_slugs(self, entry):
        return [a.slug for a in _svc()._walk_chain(entry)]

    def test_linear_chain_order(self):
        n2 = Agent.objects.create(company=self.company, slug="n2", connected_to_end=True)
        n1 = Agent.objects.create(company=self.company, slug="n1", next_node=n2)
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True, next_node=n1
        )
        self.assertEqual(self._chain_slugs(entry), ["entry", "n1", "n2"])

    def test_ends_at_connected_to_end(self):
        n1 = Agent.objects.create(company=self.company, slug="n1", connected_to_end=True)
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True, next_node=n1
        )
        self.assertEqual(self._chain_slugs(entry), ["entry", "n1"])

    def test_ends_at_null_next_node(self):
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True
        )
        self.assertEqual(self._chain_slugs(entry), ["entry"])

    def test_cycle_terminates(self):
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True
        )
        n1 = Agent.objects.create(company=self.company, slug="n1", next_node=entry)
        entry.next_node = n1
        entry.save()
        slugs = self._chain_slugs(entry)
        # seen-set stops the walk; entry appears once.
        self.assertEqual(slugs.count("entry"), 1)
        self.assertLessEqual(len(slugs), 3)

    def test_soft_deleted_mid_chain_ends_at_predecessor(self):
        n2 = Agent.objects.create(company=self.company, slug="n2")
        n1 = Agent.objects.create(company=self.company, slug="n1", next_node=n2)
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True, next_node=n1
        )
        n2.delete()  # soft delete
        self.assertEqual(self._chain_slugs(entry), ["entry", "n1"])


class BuildGraphTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    def _invoke(self, chain, entry_slug):
        svc = _svc()
        ran = []

        def router_body(state):
            ran.append(state["_current"])
            return {
                "visited": (state.get("visited") or []) + [state["_current"]],
                "hops": state.get("hops", 0) + 1,
            }

        app = svc._build_graph(
            chain=chain, entry_slug=entry_slug, router_body=router_body
        )
        out = app.invoke(
            {
                "message_text": "hi",
                "history_text": "",
                "enabled_tools": [],
                "selected_tool": None,
                "route_reason": "",
                "visited": [],
                "hops": 0,
            }
        )
        return out.get("visited") or [], ran

    def test_three_node_ordered_walk(self):
        n2 = Agent.objects.create(company=self.company, slug="n2", connected_to_end=True)
        n1 = Agent.objects.create(company=self.company, slug="n1", next_node=n2)
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True, next_node=n1
        )
        chain = _svc()._walk_chain(entry)
        visited, ran = self._invoke(chain, "entry")
        self.assertEqual(visited, ["entry", "n1", "n2"])
        self.assertEqual(ran, ["entry"])  # router runs once, on entry

    def test_cycle_runs_router_once_and_terminates(self):
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True
        )
        n1 = Agent.objects.create(company=self.company, slug="n1", next_node=entry)
        entry.next_node = n1
        entry.save()
        # Build a chain manually that includes the cycle targets (walk would stop
        # at the seen-set, but the build must still terminate on invoke).
        chain = [entry, n1]
        visited, ran = self._invoke(chain, "entry")
        self.assertEqual(ran, ["entry"])
        self.assertIn("entry", visited)

    def test_next_node_outside_chain_degrades_to_end(self):
        # A lone entry whose next_node points outside the walked chain.
        other = Agent.objects.create(company=self.company, slug="other")
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True, next_node=other
        )
        chain = [entry]  # 'other' deliberately excluded
        visited, ran = self._invoke(chain, "entry")
        self.assertEqual(visited, ["entry"])
        self.assertEqual(ran, ["entry"])


# --------------------------------------------------------------------------- #
# API tests
# --------------------------------------------------------------------------- #
class ToolApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.other = User.objects.create_user(email="x@example.com", password="x")
        cls.other_co = Company.objects.create(owner=cls.other, name="O", slug="o")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_create_unknown_slug_400(self):
        resp = self.client.post(
            "/api/v1/agents/tool/",
            {"slug": "nope", "name": "N", "type": "general_assistant"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("slug", resp.json())

    def test_create_langfuse_ok(self):
        resp = self.client.post(
            "/api/v1/agents/tool/",
            {"slug": "my_prompt", "name": "L", "type": "langfuse"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)

    def test_out_of_scope_tool_404(self):
        tool = Tool.objects.create(
            company=self.other_co, slug="general_assistant", name="G",
            type="general_assistant",
        )
        resp = self.client.get(f"/api/v1/agents/tool/{tool.pk}/")
        self.assertEqual(resp.status_code, 404)


class AgentApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.other = User.objects.create_user(email="x@example.com", password="x")
        cls.other_co = Company.objects.create(owner=cls.other, name="O", slug="o")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_activate_single_active_invariant(self):
        a1 = Agent.objects.create(
            company=self.company, slug="a1", connected_from_start=True, is_active=True
        )
        a2 = Agent.objects.create(
            company=self.company, slug="a2", connected_from_start=True
        )
        resp = self.client.post(f"/api/v1/agents/agent/{a2.pk}/activate/")
        self.assertEqual(resp.status_code, 200)
        a1.refresh_from_db()
        a2.refresh_from_db()
        self.assertFalse(a1.is_active)
        self.assertTrue(a2.is_active)
        self.assertEqual(
            Agent.objects.filter(company=self.company, is_active=True).count(), 1
        )

    def test_activate_from_mid_chain_resolves_to_entry(self):
        mid = Agent.objects.create(company=self.company, slug="mid", connected_to_end=True)
        entry = Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True, next_node=mid
        )
        resp = self.client.post(f"/api/v1/agents/agent/{mid.pk}/activate/")
        self.assertEqual(resp.status_code, 200)
        entry.refresh_from_db()
        mid.refresh_from_db()
        self.assertTrue(entry.is_active)
        self.assertFalse(mid.is_active)

    def test_activate_no_start_400(self):
        orphan = Agent.objects.create(company=self.company, slug="orphan")
        resp = self.client.post(f"/api/v1/agents/agent/{orphan.pk}/activate/")
        self.assertEqual(resp.status_code, 400)

    def test_load_active_chain_and_tools(self):
        tool = Tool.objects.create(
            company=self.company, slug="general_assistant", name="G",
            type="general_assistant",
        )
        end = Agent.objects.create(
            company=self.company, slug="end", connected_to_end=True
        )
        entry = Agent.objects.create(
            company=self.company,
            slug="entry",
            connected_from_start=True,
            is_active=True,
            next_node=end,
        )
        entry.tools.add(tool)
        resp = self.client.get("/api/v1/agents/agent/load/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["entry_id"], str(entry.pk))
        self.assertTrue(data["is_active"])
        self.assertEqual([a["slug"] for a in data["agents"]], ["entry", "end"])
        self.assertEqual(len(data["tools"]), 1)

    def test_load_empty_company_returns_palette(self):
        Tool.objects.create(
            company=self.company, slug="web_search", name="W", type="web_search"
        )
        resp = self.client.get("/api/v1/agents/agent/load/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNone(data["entry_id"])
        self.assertEqual(data["agents"], [])
        self.assertEqual(len(data["tools"]), 1)

    def test_out_of_scope_agent_404(self):
        a = Agent.objects.create(company=self.other_co, slug="x")
        resp = self.client.get(f"/api/v1/agents/agent/{a.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_self_next_node_400(self):
        a = Agent.objects.create(company=self.company, slug="a")
        resp = self.client.patch(
            f"/api/v1/agents/agent/{a.pk}/",
            {"next_node": str(a.pk)},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_cross_company_next_node_400(self):
        other_agent = Agent.objects.create(company=self.other_co, slug="oa")
        resp = self.client.post(
            "/api/v1/agents/agent/",
            {"slug": "a", "next_node": str(other_agent.pk)},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_out_of_scope_tool_on_agent_400(self):
        other_tool = Tool.objects.create(
            company=self.other_co, slug="general_assistant", name="G",
            type="general_assistant",
        )
        resp = self.client.post(
            "/api/v1/agents/agent/",
            {"slug": "a", "tools": [str(other_tool.pk)]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


# --------------------------------------------------------------------------- #
# Orchestrator gate tests
# --------------------------------------------------------------------------- #
class OrchestratorForTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    def test_no_active_flow_uses_chat_service(self):
        from chat.services import ChatService
        from chat.views import _orchestrator_for

        svc = _orchestrator_for(self.company)
        self.assertIsInstance(svc, ChatService)

    def test_active_entry_uses_langgraph(self):
        from chat.services.langgraph_service import LangGraphChatService
        from chat.views import _orchestrator_for

        Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True, is_active=True
        )
        svc = _orchestrator_for(self.company)
        self.assertIsInstance(svc, LangGraphChatService)

    def test_lookup_exception_falls_back_to_chat_service(self):
        from chat.services import ChatService
        from chat.views import _orchestrator_for

        Agent.objects.create(
            company=self.company, slug="entry", connected_from_start=True, is_active=True
        )
        with mock.patch(
            "agents.models.Agent.objects.filter", side_effect=RuntimeError("boom")
        ):
            svc = _orchestrator_for(self.company)
        self.assertIsInstance(svc, ChatService)


# --------------------------------------------------------------------------- #
# Seed command tests
# --------------------------------------------------------------------------- #
class SeedAgentsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    def test_seed_is_idempotent_single_active(self):
        from django.core.management import call_command

        call_command("seed_agents", company=str(self.company.pk))
        call_command("seed_agents", company=str(self.company.pk))
        self.assertEqual(Tool.objects.filter(company=self.company).count(), 6)
        self.assertEqual(Agent.objects.filter(company=self.company).count(), 1)
        self.assertEqual(
            Agent.objects.filter(company=self.company, is_active=True).count(), 1
        )
        agent = Agent.objects.get(company=self.company, slug="chat_agent")
        self.assertTrue(agent.connected_from_start)
        self.assertTrue(agent.connected_to_end)
        self.assertEqual(agent.tools.count(), 6)
