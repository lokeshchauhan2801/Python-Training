"""Tests for the LangGraph agent runtime (FEAT-002).

Follows ``chat/tests.py`` patterns: a fake ``LLMClient`` injected into the
services (no network, no key) and agents-side seams patched. Covers:

* the LLM router's unknown-tool coercion to ``__direct__``;
* the ``visited`` / ``MAX_HOPS`` cycle guard terminating on a synthetic
  multi-node graph;
* ``chat.views._orchestrator_for`` selecting ``ChatService`` when no active
  graph and ``LangGraphChatService`` when a runnable one exists;
* end-to-end ``send`` through a seeded active graph recording
  ``metadata.tool_used`` for ``document_search`` / ``__direct__`` / ``web_search``;
* the no-graph fallback reproducing an existing ``process_message`` assertion;
* ``Tool.clean()`` slug validation and the model company / start-end / edge
  invariants via ``full_clean()``.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from company.models import Company

from agents.models import AgentEdge, AgentGraph, AgentNode, Tool
from agents.services.graph_service import (
    DIRECT,
    LangGraphChatService,
    RouterDecision,
)

User = get_user_model()


# --------------------------------------------------------------------------- #
# Fake LLM client — supports both the router (generate_structured ->           #
# RouterDecision) and the tool handlers (complete / generate_structured ->     #
# ChatCompletion).                                                             #
# --------------------------------------------------------------------------- #
class _FakeRouterLLM:
    """Returns a canned router slug and a canned completion for handlers.

    ``router_tool`` is the slug the router will "pick"; ``reply`` is the text a
    ``complete``/``ChatCompletion`` handler returns.
    """

    provider = "fake"
    model = "fake-model"

    def __init__(self, router_tool=DIRECT, router_reason="because", reply="fake answer"):
        self.router_tool = router_tool
        self.router_reason = router_reason
        self.reply = reply
        self.structured_calls = []
        self.complete_calls = []

    def complete(self, messages, *, temperature=None, max_tokens=None):
        self.complete_calls.append(messages)
        return self.reply

    def generate_structured(self, prompt, schema):
        self.structured_calls.append((prompt, schema))
        if schema is RouterDecision:
            return RouterDecision(tool=self.router_tool, reason=self.router_reason)
        # Any other schema is a ChatCompletion (document_search).
        return schema(response=self.reply, chart=None, title=None)

    def embed_query(self, text):
        return [0.0] * 8


def _langgraph_service(llm):
    from chat.services import ChatService

    class _NoopLangfuse:
        def get_prompt(self, name, *, fallback):
            return fallback

        def trace(self, name, **kw):
            import contextlib

            @contextlib.contextmanager
            def _cm():
                yield None

            return _cm()

        def flush(self):
            pass

    return LangGraphChatService(
        chat_service=ChatService(llm_client=llm),
        langfuse=_NoopLangfuse(),
    )


def _seed_tools(company):
    """Create the four built-in Tool rows for ``company``."""
    specs = [
        ("general_assistant", "General assistant"),
        ("document_search", "Document search"),
        ("chat_summarise", "Chat summary"),
        ("web_search", "Web search"),
    ]
    tools = {}
    for slug, name in specs:
        tool = Tool(company=company, slug=slug, name=name, is_enabled=True)
        tool.save()
        tools[slug] = tool
    return tools


def _seed_active_graph(company, tools, *, step_tools=None):
    """Seed START -> chat_agent(STEP) -> END and activate the graph."""
    graph = AgentGraph(company=company, name="Default agent", is_active=True)
    graph.save()
    start = AgentNode(company=company, graph=graph, slug="start", kind=AgentNode.KIND_START)
    start.save()
    step = AgentNode(company=company, graph=graph, slug="chat_agent", kind=AgentNode.KIND_STEP)
    step.save()
    end = AgentNode(company=company, graph=graph, slug="end", kind=AgentNode.KIND_END)
    end.save()
    for slug in step_tools if step_tools is not None else tools.keys():
        step.tools.add(tools[slug])
    for src, tgt in ((start, step), (step, end)):
        edge = AgentEdge(company=company, graph=graph, source=src, target=tgt)
        edge.save()
    return graph, start, step, end


# --------------------------------------------------------------------------- #
# Model invariants (no LLM).                                                   #
# --------------------------------------------------------------------------- #
class AgentModelInvariantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company_a = Company.objects.create(owner=cls.owner, name="A", slug="a")
        cls.company_b = Company.objects.create(owner=cls.owner, name="B", slug="b")

    def test_tool_clean_rejects_unknown_slug(self):
        tool = Tool(company=self.company_a, slug="not_a_real_tool", name="Nope")
        with self.assertRaises(ValidationError):
            tool.full_clean()

    def test_tool_clean_accepts_registry_slug(self):
        tool = Tool(company=self.company_a, slug="general_assistant", name="GA")
        # Should not raise.
        tool.full_clean()

    def test_node_company_must_match_graph(self):
        graph = AgentGraph(company=self.company_a, name="G")
        graph.save()
        node = AgentNode(
            company=self.company_b,  # mismatch
            graph=graph,
            slug="start",
            kind=AgentNode.KIND_START,
        )
        with self.assertRaises(ValidationError):
            node.full_clean()

    def test_second_start_node_rejected(self):
        graph = AgentGraph(company=self.company_a, name="G")
        graph.save()
        first = AgentNode(
            company=self.company_a, graph=graph, slug="s1", kind=AgentNode.KIND_START
        )
        first.save()
        second = AgentNode(
            company=self.company_a, graph=graph, slug="s2", kind=AgentNode.KIND_START
        )
        with self.assertRaises(ValidationError):
            second.full_clean()

    def test_edge_self_loop_rejected(self):
        graph = AgentGraph(company=self.company_a, name="G")
        graph.save()
        node = AgentNode(
            company=self.company_a, graph=graph, slug="n", kind=AgentNode.KIND_STEP
        )
        node.save()
        edge = AgentEdge(
            company=self.company_a, graph=graph, source=node, target=node
        )
        with self.assertRaises(ValidationError):
            edge.full_clean()

    def test_cross_graph_edge_rejected(self):
        g1 = AgentGraph(company=self.company_a, name="G1")
        g1.save()
        g2 = AgentGraph(company=self.company_a, name="G2")
        g2.save()
        n1 = AgentNode(company=self.company_a, graph=g1, slug="n1", kind=AgentNode.KIND_STEP)
        n1.save()
        n2 = AgentNode(company=self.company_a, graph=g2, slug="n2", kind=AgentNode.KIND_STEP)
        n2.save()
        edge = AgentEdge(company=self.company_a, graph=g1, source=n1, target=n2)
        with self.assertRaises(ValidationError):
            edge.full_clean()


# --------------------------------------------------------------------------- #
# Router unit behaviour (no DB graph traversal).                               #
# --------------------------------------------------------------------------- #
class RouterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="r@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="RC", slug="rc")
        cls.tools = _seed_tools(cls.company)
        cls.graph, cls.start, cls.step, cls.end = _seed_active_graph(
            cls.company, cls.tools
        )

    def test_unknown_tool_coerced_to_direct(self):
        llm = _FakeRouterLLM(router_tool="totally_made_up")
        service = _langgraph_service(llm)
        tool_rows = {t.slug: t for t in self.step.tools.all()}
        selected, reason, _trace = service._route(
            message_text="hello",
            history_text="",
            enabled_tools=["document_search", "web_search"],
            step_node=self.step,
            tool_rows=tool_rows,
            llm=llm,
        )
        self.assertEqual(selected, DIRECT)
        self.assertIn("unknown tool", reason)

    def test_no_enabled_tools_forces_direct_without_llm(self):
        llm = _FakeRouterLLM(router_tool="document_search")
        service = _langgraph_service(llm)
        selected, reason, _trace = service._route(
            message_text="hello",
            history_text="",
            enabled_tools=[],
            step_node=self.step,
            tool_rows={},
            llm=llm,
        )
        self.assertEqual(selected, DIRECT)
        # The LLM must NOT be consulted when there are no tools.
        self.assertEqual(llm.structured_calls, [])

    def test_known_tool_is_selected(self):
        llm = _FakeRouterLLM(router_tool="document_search", router_reason="rag")
        service = _langgraph_service(llm)
        tool_rows = {t.slug: t for t in self.step.tools.all()}
        selected, reason, _trace = service._route(
            message_text="hello",
            history_text="",
            enabled_tools=["document_search", "web_search"],
            step_node=self.step,
            tool_rows=tool_rows,
            llm=llm,
        )
        self.assertEqual(selected, "document_search")
        self.assertEqual(reason, "rag")


# --------------------------------------------------------------------------- #
# Cycle guard on a synthetic multi-node graph.                                 #
# --------------------------------------------------------------------------- #
class CycleGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="c@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="CC", slug="cc")

    def test_cycle_terminates(self):
        llm = _FakeRouterLLM(router_tool=DIRECT, reply="done")
        service = _langgraph_service(llm)

        # Build a graph wired with a cycle: start -> step, step -> step (self
        # cycle via a sibling), step -> end. The visited guard must terminate.
        graph = AgentGraph(company=self.company, name="Cyclic")
        graph.save()
        start = AgentNode(company=self.company, graph=graph, slug="start", kind=AgentNode.KIND_START)
        start.save()
        step = AgentNode(company=self.company, graph=graph, slug="step", kind=AgentNode.KIND_STEP)
        step.save()
        loop = AgentNode(company=self.company, graph=graph, slug="loop", kind=AgentNode.KIND_STEP)
        loop.save()
        end = AgentNode(company=self.company, graph=graph, slug="end", kind=AgentNode.KIND_END)
        end.save()
        for src, tgt in ((start, step), (step, loop), (loop, step), (step, end)):
            AgentEdge(company=self.company, graph=graph, source=src, target=tgt).save()

        router_calls = {"n": 0}

        def _router_body(state):
            router_calls["n"] += 1
            from agents.tools.base import ToolResult

            # Record a result so the outer run has an outcome.
            _router_body.outcome = ToolResult(content="done", tool_used="general_assistant")
            return {
                "visited": (state.get("visited") or []) + [state["_current"]],
                "hops": state.get("hops", 0) + 1,
            }

        nodes = list(AgentNode.objects.filter(graph=graph).order_by("slug"))
        edges = list(
            AgentEdge.objects.filter(graph=graph)
            .select_related("source", "target")
            .order_by("source__slug", "target__slug")
        )
        app = service._build_graph(
            nodes=nodes, edges=edges, step_slug="step", router_body=_router_body
        )
        out = app.invoke(
            {
                "message_text": "x",
                "history_text": "",
                "enabled_tools": [],
                "visited": [],
                "hops": 0,
            }
        )
        # Terminated (did not loop forever) and respected MAX_HOPS.
        max_hops = 2 * len(nodes) + 1
        self.assertLessEqual(out.get("hops", 0), max_hops + 1)
        # The 'step' router node is only entered once (second entry is blocked by
        # the visited guard), so routing never diverges into an infinite loop.
        self.assertLessEqual(router_calls["n"], 1)


# --------------------------------------------------------------------------- #
# _orchestrator_for selection.                                                 #
# --------------------------------------------------------------------------- #
class OrchestratorSelectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="orc@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="OC", slug="oc")

    def test_no_active_graph_returns_chat_service(self):
        from chat.services import ChatService
        from chat.views import _orchestrator_for

        service = _orchestrator_for(self.company)
        self.assertIsInstance(service, ChatService)

    def test_active_runnable_graph_returns_langgraph_service(self):
        from chat.views import _orchestrator_for

        tools = _seed_tools(self.company)
        _seed_active_graph(self.company, tools)
        service = _orchestrator_for(self.company)
        self.assertIsInstance(service, LangGraphChatService)

    def test_active_graph_without_step_falls_back(self):
        from chat.services import ChatService
        from chat.views import _orchestrator_for

        graph = AgentGraph(company=self.company, name="G", is_active=True)
        graph.save()
        AgentNode(
            company=self.company, graph=graph, slug="start", kind=AgentNode.KIND_START
        ).save()
        # No STEP node -> not runnable.
        service = _orchestrator_for(self.company)
        self.assertIsInstance(service, ChatService)


# --------------------------------------------------------------------------- #
# End-to-end send through a seeded active graph (HTTP layer).                  #
# --------------------------------------------------------------------------- #
class SendThroughGraphTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(email="ga@example.com", password="pw")
        cls.company = Company.objects.create(
            owner=cls.alice, name="GA Co", slug="ga-co"
        )

    def setUp(self):
        self.client = Client()
        from chat.models import ConversationSession

        self.session = ConversationSession.objects.create(
            company=self.company, user=self.alice, title="New session"
        )
        self.tools = _seed_tools(self.company)
        _seed_active_graph(self.company, self.tools)

    def _send(self, llm, question="hello there"):
        self.client.force_login(self.alice)
        service = _langgraph_service(llm)
        with mock.patch(
            "chat.views._orchestrator_for", return_value=service
        ), mock.patch(
            "chat.services.document_services.find_relevant_chunks",
            return_value=[],
        ):
            return self.client.post(
                reverse("chat:send", args=[self.session.pk]),
                {"question": question},
            )

    def test_direct_route_records_general_assistant(self):
        llm = _FakeRouterLLM(router_tool=DIRECT, reply="a direct answer")
        resp = self._send(llm)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["assistant"]["content"], "a direct answer")
        self.assertEqual(
            data["assistant"]["metadata"]["tool_used"], "general_assistant"
        )
        self.assertEqual(data["assistant"]["metadata"]["source"], "langgraph")

    def test_document_search_route_records_tool_used(self):
        llm = _FakeRouterLLM(router_tool="document_search", reply="a cited answer")
        resp = self._send(llm)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["assistant"]["content"], "a cited answer")
        self.assertEqual(
            data["assistant"]["metadata"]["tool_used"], "document_search"
        )

    def test_web_search_route_records_not_configured(self):
        llm = _FakeRouterLLM(router_tool="web_search")
        resp = self._send(llm)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(
            data["assistant"]["metadata"]["tool_used"], "web_search"
        )
        self.assertIn("not configured", data["assistant"]["content"].lower())

    def test_graph_send_persists_pair_and_retitles(self):
        llm = _FakeRouterLLM(router_tool=DIRECT, reply="hi back")
        resp = self._send(llm, question="what is up")
        self.assertEqual(resp.status_code, 201)
        from chat.models import Message

        rows = Message.objects.filter(conversation=self.session)
        self.assertEqual(rows.count(), 2)
        self.session.refresh_from_db()
        self.assertEqual(self.session.title, "what is up"[:50])


# --------------------------------------------------------------------------- #
# Fallback with no graph reproduces the existing process_message behaviour.    #
# --------------------------------------------------------------------------- #
class NoGraphFallbackTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="nf@example.com", password="x")
        cls.company = Company.objects.create(
            owner=cls.user, name="NF Co", slug="nf-co"
        )

    def setUp(self):
        from chat.models import ConversationSession

        self.session = ConversationSession.objects.create(
            company=self.company, user=self.user, title="New session"
        )

    def test_no_graph_uses_chat_service_path(self):
        # No AgentGraph seeded for this company: _orchestrator_for returns
        # ChatService and process_message persists one user + one assistant
        # Message with NO langgraph metadata (reproduces the pinned
        # process_message assertion).
        from chat.models import Message
        from chat.services import ChatService
        from chat.views import _orchestrator_for

        service = _orchestrator_for(self.company)
        self.assertIsInstance(service, ChatService)

        fake = _FakeRouterLLM(reply="plain answer")
        with mock.patch(
            "chat.services.document_services.find_relevant_chunks",
            return_value=[],
        ):
            assistant = ChatService(llm_client=fake).process_message(
                user=self.user,
                message_text="hi there",
                conversation=self.session,
                company=self.company,
            )
        self.assertEqual(assistant.role, Message.ROLE_ASSISTANT)
        self.assertEqual(assistant.content, "plain answer")
        self.assertEqual(assistant.metadata, {})
        roles = list(
            self.session.messages.order_by("created_at").values_list(
                "role", flat=True
            )
        )
        self.assertEqual(roles, [Message.ROLE_USER, Message.ROLE_ASSISTANT])
