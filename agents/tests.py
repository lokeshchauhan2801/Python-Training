"""Tests for the two-level agent model (Agent container + Step node), the
runtime step-walk / graph build, the per-step sequential pipeline (with a stub
LLM), the API (activate/load + CRUD scoping), the orchestrator gate, and the
seed command."""

from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company

from .models import Agent, Step, Tool

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
        Agent.objects.create(company=self.company, slug="a")
        self.assertEqual(
            Agent.objects.filter(company=self.company, slug="a").count(), 1
        )


class StepModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.other_co = Company.objects.create(
            owner=cls.owner, name="O", slug="o"
        )
        cls.agent = Agent.objects.create(company=cls.company, slug="a")

    def test_clean_rejects_company_mismatch(self):
        step = Step(
            company=self.other_co, agent=self.agent, slug="s1"
        )
        with self.assertRaises(ValidationError) as ctx:
            step.clean()
        self.assertIn("company", ctx.exception.message_dict)

    def test_clean_rejects_cross_agent_next_step(self):
        other_agent = Agent.objects.create(company=self.company, slug="b")
        target = Step.objects.create(
            company=self.company, agent=other_agent, slug="t"
        )
        step = Step(
            company=self.company, agent=self.agent, slug="s1", next_step=target
        )
        with self.assertRaises(ValidationError) as ctx:
            step.clean()
        self.assertIn("next_step", ctx.exception.message_dict)

    def test_clean_rejects_self_next_step(self):
        step = Step.objects.create(
            company=self.company, agent=self.agent, slug="s1"
        )
        step.next_step = step
        with self.assertRaises(ValidationError) as ctx:
            step.clean()
        self.assertIn("next_step", ctx.exception.message_dict)

    def test_slug_unique_per_agent_live_rows(self):
        Step.objects.create(company=self.company, agent=self.agent, slug="s1")
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            Step.objects.create(company=self.company, agent=self.agent, slug="s1")

    def test_soft_deleted_step_frees_slug(self):
        s = Step.objects.create(company=self.company, agent=self.agent, slug="s1")
        s.delete()
        Step.objects.create(company=self.company, agent=self.agent, slug="s1")
        self.assertEqual(
            Step.objects.filter(agent=self.agent, slug="s1").count(), 1
        )

    def test_two_agents_may_share_step_slug(self):
        b = Agent.objects.create(company=self.company, slug="b")
        Step.objects.create(company=self.company, agent=self.agent, slug="main")
        # Same slug under a DIFFERENT agent is allowed.
        Step.objects.create(company=self.company, agent=b, slug="main")
        self.assertEqual(Step.objects.filter(slug="main").count(), 2)


# --------------------------------------------------------------------------- #
# Runtime step-walk + graph build tests
# --------------------------------------------------------------------------- #
def _svc():
    from chat.services.langgraph_service import LangGraphChatService

    return LangGraphChatService.__new__(LangGraphChatService)


class WalkStepsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.agent = Agent.objects.create(company=cls.company, slug="a")

    def _chain_slugs(self, agent):
        return [s.slug for s in _svc()._walk_steps(agent)]

    def _step(self, slug, **kw):
        return Step.objects.create(
            company=self.company, agent=self.agent, slug=slug, **kw
        )

    def test_linear_chain_order(self):
        s3 = self._step("s3", connected_to_end=True)
        s2 = self._step("s2", next_step=s3)
        self._step("s1", connected_from_start=True, next_step=s2)
        self.assertEqual(self._chain_slugs(self.agent), ["s1", "s2", "s3"])

    def test_ends_at_connected_to_end(self):
        s2 = self._step("s2", connected_to_end=True)
        self._step("s1", connected_from_start=True, next_step=s2)
        self.assertEqual(self._chain_slugs(self.agent), ["s1", "s2"])

    def test_ends_at_null_next_step(self):
        self._step("s1", connected_from_start=True)
        self.assertEqual(self._chain_slugs(self.agent), ["s1"])

    def test_no_entry_returns_empty(self):
        self._step("orphan")
        self.assertEqual(self._chain_slugs(self.agent), [])

    def test_cycle_terminates(self):
        s1 = self._step("s1", connected_from_start=True)
        s2 = self._step("s2", next_step=s1)
        s1.next_step = s2
        s1.save()
        slugs = self._chain_slugs(self.agent)
        self.assertEqual(slugs.count("s1"), 1)
        self.assertLessEqual(len(slugs), 3)

    def test_soft_deleted_mid_chain_ends_at_predecessor(self):
        s3 = self._step("s3")
        s2 = self._step("s2", next_step=s3)
        self._step("s1", connected_from_start=True, next_step=s2)
        s3.delete()  # soft delete
        self.assertEqual(self._chain_slugs(self.agent), ["s1", "s2"])


class BuildGraphTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.agent = Agent.objects.create(company=cls.company, slug="a")

    def _step(self, slug, **kw):
        return Step.objects.create(
            company=self.company, agent=self.agent, slug=slug, **kw
        )

    def _invoke(self, chain, entry_slug):
        svc = _svc()
        ran = []

        def make_node(step):
            def _node(state):
                ran.append(step.slug)
                return {
                    "visited": (state.get("visited") or []) + [step.slug],
                    "hops": state.get("hops", 0) + 1,
                }

            return _node

        app = svc._build_graph(
            chain=chain, entry_slug=entry_slug, make_node=make_node
        )
        out = app.invoke(
            {
                "message_text": "hi",
                "history_text": "",
                "pipeline_context": "",
                "selected_tool": None,
                "route_reason": "",
                "visited": [],
                "hops": 0,
            }
        )
        return out.get("visited") or [], ran

    def test_three_node_ordered_walk(self):
        s3 = self._step("s3", connected_to_end=True)
        s2 = self._step("s2", next_step=s3)
        self._step("s1", connected_from_start=True, next_step=s2)
        chain = _svc()._walk_steps(self.agent)
        visited, ran = self._invoke(chain, "s1")
        # EVERY step runs (no pass-through nodes) in order.
        self.assertEqual(visited, ["s1", "s2", "s3"])
        self.assertEqual(ran, ["s1", "s2", "s3"])

    def test_cycle_runs_each_node_once_and_terminates(self):
        s1 = self._step("s1", connected_from_start=True)
        s2 = self._step("s2", next_step=s1)
        s1.next_step = s2
        s1.save()
        chain = [s1, s2]
        visited, ran = self._invoke(chain, "s1")
        # seen-set drops the cyclic edge; each node runs at most once.
        self.assertEqual(ran.count("s1"), 1)
        self.assertEqual(ran.count("s2"), 1)

    def test_next_step_outside_chain_degrades_to_end(self):
        other = self._step("other")
        self._step("s1", connected_from_start=True, next_step=other)
        chain = [Step.objects.get(slug="s1")]  # 'other' excluded
        visited, ran = self._invoke(chain, "s1")
        self.assertEqual(visited, ["s1"])
        self.assertEqual(ran, ["s1"])


# --------------------------------------------------------------------------- #
# Pipeline ordering tests (the multi-step proof with a stub LLM)
# --------------------------------------------------------------------------- #
class _FakeSpan:
    id = "trace-x"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _NoopLangfuse:
    """No-op langfuse: get_prompt returns the fallback, trace a dummy span."""

    def get_prompt(self, name, fallback=""):
        return fallback

    @contextmanager
    def trace(self, name):
        yield _FakeSpan()


class _StubLLM:
    """Records every prompt and returns a deterministic per-call value.

    ``generate_structured`` returns a RouterDecision picking ``tool_for(prompt)``;
    ``complete`` returns ``text_for(prompt)``. Prompts are captured so a test can
    assert step 2 saw step 1's output marker.
    """

    provider = "fake"
    model = "fake-model"

    def __init__(self, tool_for, text_for):
        self._tool_for = tool_for
        self._text_for = text_for
        self.router_prompts = []
        self.handler_prompts = []

    def generate_structured(self, prompt, schema):
        from chat.services.langgraph_service import RouterDecision

        # RouterDecision is the router call; anything else is a handler
        # (document_search / chart) call.
        if schema is RouterDecision:
            self.router_prompts.append(prompt)
            return RouterDecision(tool=self._tool_for(prompt), reason="stub")
        self.handler_prompts.append(prompt)
        # document_search returns a ChatCompletion-shaped object.
        return schema(response=self._text_for(prompt), chart=None)

    def complete(self, messages):
        # messages[0] is the system prompt; capture it + the user text.
        prompt = "\n".join(m.get("content", "") for m in messages)
        self.handler_prompts.append(prompt)
        return self._text_for(prompt)


class _FakeChatService:
    """Minimal ChatService stand-in for the pipeline (only _llm + history)."""

    def __init__(self, llm):
        self._llm = llm

    def _build_conversation_context(self, turns):
        return ""


class PipelineOrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    def _tool(self, slug):
        return Tool.objects.create(
            company=self.company, slug=slug, name=slug, type=slug
        )

    def _build_two_step(self, s1_tool="general_assistant", s2_tool="general_assistant"):
        agent = Agent.objects.create(
            company=self.company, slug="pipeline", is_active=True
        )
        t1 = self._tool(s1_tool)
        t2 = self._tool(s2_tool) if s2_tool != s1_tool else t1
        s2 = Step.objects.create(
            company=self.company, agent=agent, slug="s2",
            connected_to_end=True, order=1,
        )
        s2.tools.add(t2)
        s1 = Step.objects.create(
            company=self.company, agent=agent, slug="s1",
            connected_from_start=True, next_step=s2, order=0,
        )
        s1.tools.add(t1)
        return agent

    def _service(self, llm):
        from chat.services.langgraph_service import LangGraphChatService

        return LangGraphChatService(
            chat_service=_FakeChatService(llm), langfuse=_NoopLangfuse()
        )

    def _fake_conversation(self):
        fake_turns = mock.Mock()
        fake_turns.order_by.return_value = []
        return mock.Mock(turns=fake_turns, summary="", metadata={})

    def _run(self, agent, llm, message="hello", conversation=None):
        svc = self._service(llm)
        return svc._run_graph(
            user=self.owner,
            message_text=message,
            conversation=conversation or self._fake_conversation(),
            company=self.company,
            attachment_ids=None,
            model_name=None,
            history_text="",
            agent=agent,
            llm=llm,
        )

    def test_two_steps_run_in_order_with_handoff(self):
        # Step 1 uses general_assistant, step 2 uses web_search — both plain
        # LLM-complete handlers (no RAG), so the stub LLM alone drives them and
        # the handoff is visible in each handler's system prompt.
        agent = self._build_two_step(
            s1_tool="general_assistant", s2_tool="web_search"
        )

        def tool_for(prompt):
            # Each step has exactly one tool; the router picks that step's tool.
            # The router prompt lists the step's enabled tools as "slug: desc".
            return "web_search" if "web_search:" in prompt else "general_assistant"

        def text_for(prompt):
            # Step 2's handler prompt carries step 1's output marker.
            return "OUTPUT_B" if "[Pipeline context" in prompt else "OUTPUT_A"

        llm = _StubLLM(tool_for, text_for)
        outcome = self._run(agent, llm)

        # (a) both steps executed in order
        self.assertEqual(
            outcome["steps"],
            [
                {"step": "s1", "tool": "general_assistant"},
                {"step": "s2", "tool": "web_search"},
            ],
        )
        # final reply is step 2's output
        self.assertEqual(outcome["tool_result"].content, "OUTPUT_B")

        # (c) exactly one router call PER step
        self.assertEqual(len(llm.router_prompts), 2)

        # (b) step 2's ROUTER prompt saw step 1's output marker (via history)
        step2_router = llm.router_prompts[1]
        self.assertIn("[Step 's1' used tool 'general_assistant']", step2_router)
        self.assertIn("OUTPUT_A", step2_router)

        # (b) step 2's HANDLER prompt saw step 1's output (via pipeline_context)
        step2_handlers = [p for p in llm.handler_prompts if "OUTPUT_A" in p]
        self.assertTrue(
            step2_handlers,
            "step 2 handler prompt must contain step 1's output",
        )
        self.assertIn(
            "[Pipeline context from previous steps]", step2_handlers[0]
        )

    def test_chat_summarise_step_receives_handoff(self):
        agent = self._build_two_step(
            s1_tool="general_assistant", s2_tool="chat_summarise"
        )

        def tool_for(prompt):
            return (
                "chat_summarise"
                if "chat_summarise" in prompt and "general_assistant" not in prompt
                else "general_assistant"
            )

        def text_for(prompt):
            return "SUMMARY" if "[Pipeline context" in prompt else "OUTPUT_A"

        llm = _StubLLM(tool_for, text_for)
        # chat_summarise reads conversation.turns -> use a fake conversation.
        fake_turns = mock.Mock()
        fake_turns.order_by.return_value = []
        conversation = mock.Mock(turns=fake_turns, summary="", metadata={})
        svc = self._service(llm)
        outcome = svc._run_graph(
            user=self.owner,
            message_text="summarise",
            conversation=conversation,
            company=self.company,
            attachment_ids=None,
            model_name=None,
            history_text="",
            agent=agent,
            llm=llm,
        )
        self.assertEqual(
            [s["step"] for s in outcome["steps"]], ["s1", "s2"]
        )
        # the summary handler's conversation_context carried step 1's marker
        summarise_prompts = [
            p for p in llm.handler_prompts if "OUTPUT_A" in p
        ]
        self.assertTrue(summarise_prompts)
        self.assertIn(
            "[Step 's1' used tool 'general_assistant']", summarise_prompts[0]
        )

    def test_single_step_backward_compatible(self):
        agent = Agent.objects.create(
            company=self.company, slug="single", is_active=True
        )
        t = self._tool("general_assistant")
        s = Step.objects.create(
            company=self.company, agent=agent, slug="main",
            connected_from_start=True, connected_to_end=True,
        )
        s.tools.add(t)

        llm = _StubLLM(lambda p: "general_assistant", lambda p: "ONLY_OUTPUT")
        outcome = self._run(agent, llm)

        self.assertEqual(
            outcome["steps"], [{"step": "main", "tool": "general_assistant"}]
        )
        self.assertEqual(outcome["tool_result"].content, "ONLY_OUTPUT")
        # exactly one router call
        self.assertEqual(len(llm.router_prompts), 1)
        # NO pipeline-context block in the single-step router or handler prompt
        self.assertNotIn(
            "[Pipeline context from previous steps]", llm.router_prompts[0]
        )
        for p in llm.handler_prompts:
            self.assertNotIn("[Pipeline context from previous steps]", p)


class WithPipelineContextTests(TestCase):
    def test_empty_context_unchanged(self):
        from chat.services.llm_tools import ToolContext, _with_pipeline_context

        ctx = ToolContext(
            user=None, company=None, conversation=None, message_text="m"
        )
        self.assertEqual(_with_pipeline_context("BASE", ctx), "BASE")

    def test_non_empty_context_prepended(self):
        from chat.services.llm_tools import ToolContext, _with_pipeline_context

        ctx = ToolContext(
            user=None,
            company=None,
            conversation=None,
            message_text="m",
            pipeline_context="PRIOR",
        )
        out = _with_pipeline_context("BASE", ctx)
        self.assertIn("[Pipeline context from previous steps]", out)
        self.assertIn("PRIOR", out)
        self.assertIn("BASE", out)
        self.assertTrue(out.index("PRIOR") < out.index("BASE"))

    def test_tool_context_default_pipeline_context(self):
        from chat.services.llm_tools import ToolContext

        ctx = ToolContext(
            user=None, company=None, conversation=None, message_text="m"
        )
        self.assertEqual(ctx.pipeline_context, "")


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

    def test_attach_to_step(self):
        agent = Agent.objects.create(company=self.company, slug="a")
        step = Step.objects.create(
            company=self.company, agent=agent, slug="s1"
        )
        resp = self.client.post(
            "/api/v1/agents/tool/attach/",
            {"slug": "general_assistant", "step": str(step.pk)},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [t.slug for t in step.tools.all()], ["general_assistant"]
        )


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
            company=self.company, slug="a1", is_active=True
        )
        a2 = Agent.objects.create(company=self.company, slug="a2")
        resp = self.client.post(f"/api/v1/agents/agent/{a2.pk}/activate/")
        self.assertEqual(resp.status_code, 200)
        a1.refresh_from_db()
        a2.refresh_from_db()
        self.assertFalse(a1.is_active)
        self.assertTrue(a2.is_active)
        self.assertEqual(
            Agent.objects.filter(company=self.company, is_active=True).count(), 1
        )

    def test_activate_half_built_agent_no_400(self):
        # A half-built agent (no entry step) MAY be activated (degrades to RAG).
        orphan = Agent.objects.create(company=self.company, slug="orphan")
        resp = self.client.post(f"/api/v1/agents/agent/{orphan.pk}/activate/")
        self.assertEqual(resp.status_code, 200)
        orphan.refresh_from_db()
        self.assertTrue(orphan.is_active)

    def test_load_lists_all_agents_with_selected_steps_and_tools(self):
        tool = Tool.objects.create(
            company=self.company, slug="general_assistant", name="G",
            type="general_assistant",
        )
        agent = Agent.objects.create(
            company=self.company, slug="a1", is_active=True
        )
        other_agent = Agent.objects.create(company=self.company, slug="a2")
        s2 = Step.objects.create(
            company=self.company, agent=agent, slug="s2",
            connected_to_end=True, order=1,
        )
        s1 = Step.objects.create(
            company=self.company, agent=agent, slug="s1",
            connected_from_start=True, next_step=s2, order=0,
        )
        s1.tools.add(tool)
        # An orphan step (unwired) must still be visible.
        Step.objects.create(
            company=self.company, agent=agent, slug="orphan", order=2
        )
        resp = self.client.get("/api/v1/agents/agent/load/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["active_agent_id"], str(agent.pk))
        self.assertEqual(data["selected_agent_id"], str(agent.pk))
        self.assertEqual(
            sorted(a["slug"] for a in data["agents"]), ["a1", "a2"]
        )
        self.assertEqual(
            [s["slug"] for s in data["steps"]], ["s1", "s2", "orphan"]
        )
        self.assertEqual(len(data["tools"]), 1)
        self.assertIn(str(other_agent.pk), [a["id"] for a in data["agents"]])

    def test_load_empty_company_total_payload(self):
        Tool.objects.create(
            company=self.company, slug="web_search", name="W", type="web_search"
        )
        resp = self.client.get("/api/v1/agents/agent/load/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNone(data["active_agent_id"])
        self.assertIsNone(data["selected_agent_id"])
        self.assertEqual(data["agents"], [])
        self.assertEqual(data["steps"], [])
        self.assertEqual(len(data["tools"]), 1)

    def test_load_agent_selector(self):
        a1 = Agent.objects.create(company=self.company, slug="a1", is_active=True)
        a2 = Agent.objects.create(company=self.company, slug="a2")
        Step.objects.create(
            company=self.company, agent=a2, slug="only",
            connected_from_start=True, connected_to_end=True,
        )
        resp = self.client.get(f"/api/v1/agents/agent/load/?agent={a2.pk}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["selected_agent_id"], str(a2.pk))
        self.assertEqual([s["slug"] for s in data["steps"]], ["only"])

    def test_load_stale_agent_selector_falls_back_never_404(self):
        a1 = Agent.objects.create(company=self.company, slug="a1", is_active=True)
        # A foreign agent id must not 404 the bootstrap; it falls back.
        foreign = Agent.objects.create(company=self.other_co, slug="x")
        resp = self.client.get(f"/api/v1/agents/agent/load/?agent={foreign.pk}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["selected_agent_id"], str(a1.pk))

    def test_out_of_scope_agent_404(self):
        a = Agent.objects.create(company=self.other_co, slug="x")
        resp = self.client.get(f"/api/v1/agents/agent/{a.pk}/")
        self.assertEqual(resp.status_code, 404)


class StepApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.other = User.objects.create_user(email="x@example.com", password="x")
        cls.other_co = Company.objects.create(owner=cls.other, name="O", slug="o")
        cls.agent = Agent.objects.create(company=cls.company, slug="a")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_create_requires_agent_400(self):
        resp = self.client.post(
            "/api/v1/agents/step/", {"slug": "s1"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("agent", resp.json())

    def test_create_defaults_company_from_agent(self):
        resp = self.client.post(
            "/api/v1/agents/step/",
            {"agent": str(self.agent.pk), "slug": "s1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.json())
        step = Step.objects.get(pk=resp.json()["id"])
        self.assertEqual(step.company_id, self.company.pk)

    def test_out_of_scope_step_404(self):
        other_agent = Agent.objects.create(company=self.other_co, slug="oa")
        step = Step.objects.create(
            company=self.other_co, agent=other_agent, slug="s1"
        )
        resp = self.client.get(f"/api/v1/agents/step/{step.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_cross_agent_next_step_400(self):
        other_agent = Agent.objects.create(company=self.company, slug="b")
        target = Step.objects.create(
            company=self.company, agent=other_agent, slug="t"
        )
        resp = self.client.post(
            "/api/v1/agents/step/",
            {
                "agent": str(self.agent.pk),
                "slug": "s1",
                "next_step": str(target.pk),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("next_step", resp.json())

    def test_other_company_tool_400(self):
        other_tool = Tool.objects.create(
            company=self.other_co, slug="general_assistant", name="G",
            type="general_assistant",
        )
        resp = self.client.post(
            "/api/v1/agents/step/",
            {
                "agent": str(self.agent.pk),
                "slug": "s1",
                "tools": [str(other_tool.pk)],
            },
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

    def test_no_active_agent_uses_chat_service(self):
        from chat.services import ChatService
        from chat.views import _orchestrator_for

        svc = _orchestrator_for(self.company)
        self.assertIsInstance(svc, ChatService)

    def test_active_but_no_entry_step_uses_chat_service(self):
        from chat.services import ChatService
        from chat.views import _orchestrator_for

        Agent.objects.create(company=self.company, slug="a", is_active=True)
        svc = _orchestrator_for(self.company)
        self.assertIsInstance(svc, ChatService)

    def test_active_with_entry_step_uses_langgraph(self):
        from chat.services.langgraph_service import LangGraphChatService
        from chat.views import _orchestrator_for

        agent = Agent.objects.create(
            company=self.company, slug="a", is_active=True
        )
        Step.objects.create(
            company=self.company, agent=agent, slug="s1",
            connected_from_start=True,
        )
        svc = _orchestrator_for(self.company)
        self.assertIsInstance(svc, LangGraphChatService)

    def test_lookup_exception_falls_back_to_chat_service(self):
        from chat.services import ChatService
        from chat.views import _orchestrator_for

        agent = Agent.objects.create(
            company=self.company, slug="a", is_active=True
        )
        Step.objects.create(
            company=self.company, agent=agent, slug="s1",
            connected_from_start=True,
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

    def test_seed_is_idempotent_single_active_step(self):
        from django.core.management import call_command

        call_command("seed_agents", company=str(self.company.pk))
        call_command("seed_agents", company=str(self.company.pk))
        self.assertEqual(Tool.objects.filter(company=self.company).count(), 6)
        self.assertEqual(Agent.objects.filter(company=self.company).count(), 1)
        self.assertEqual(
            Agent.objects.filter(company=self.company, is_active=True).count(), 1
        )
        agent = Agent.objects.get(company=self.company, slug="chat_agent")
        steps = Step.objects.filter(agent=agent)
        self.assertEqual(steps.count(), 1)
        step = steps.first()
        self.assertTrue(step.connected_from_start)
        self.assertTrue(step.connected_to_end)
        self.assertEqual(step.tools.count(), 6)
