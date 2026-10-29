"""Chart file selection (D7) on both orchestration paths (design I C14–C17).

Covers ``views.send`` step 6 (ambiguous / not-found chart targets), the plain
``ChatService`` chart-with-file-target branch, the LangGraph router rule (R6),
``llm_tools._file_chart_reply`` (R7, H.3) and the D8 no-blocking regression.
"""
from __future__ import annotations

import contextlib
from unittest import mock

from .tests_file_commands import _CSV, LangGraphEntryAgentMixin, _Base

_ROUTE = "chat.services.langgraph_service.LangGraphChatService._route"
_R6_REASON = "chart request with a file target -> chart that file"
_PIE = {
    "type": "pie",
    "title": "Regions",
    "x_axis": "Region",
    "y_axis": "Share",
    "series": ["Share"],
    "data": [{"label": "North", "values": [40]}, {"label": "South", "values": [60]}],
}
_FORBIDDEN = (
    "unsettled/",
    "storage_key",
    "unprocessed_docs",
    "processed_docs",
    '"attachments"',
    "documents/20",
)


class _RecordingLLM:
    """Records every prompt; answers structured calls with an optional chart."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, response="llm answer", chart=None, route="general_assistant"):
        self.response = response
        self.chart = chart
        self.route = route
        self.prompts = []
        self.structured_calls = 0
        self.complete_calls = 0
        self.router_calls = 0

    def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        if schema.__name__ == "RouterDecision":
            self.router_calls += 1
            return schema(tool=self.route, reason="fake router")
        self.structured_calls += 1
        return schema(response=self.response, chart=self.chart, title=None)

    def complete(self, messages, *, temperature=None, max_tokens=None):
        self.complete_calls += 1
        self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
        return self.response

    def embed_query(self, text):
        return [0.0] * 8

    @property
    def calls(self):
        return self.structured_calls + self.complete_calls + self.router_calls


class _FakeLangfuse:
    def get_prompt(self, name, fallback=""):
        return fallback

    def trace(self, name):
        return contextlib.nullcontext(None)


class _ChartBase(LangGraphEntryAgentMixin, _Base):
    def setUp(self):
        super().setUp()
        # _Base's _chat_service patch reads self.llm at call time.
        self.llm = _RecordingLLM()

    def chart_llm(self):
        self.llm = _RecordingLLM(chart=_PIE)
        return self.llm

    def langgraph(self):
        if not hasattr(self, "_agent"):
            self._agent = self.enable_langgraph()

    def assert_chart_turn(self, msg, title):
        self.assertIsNotNone(msg["chart_data"])
        self.assertIn(title, msg["content"])
        self.assertNotIn("file_panel", msg["metadata"])


# --------------------------------------------------------------------------
# C14: no blocking (D8)
# --------------------------------------------------------------------------


class NoBlockingRegressionTests(_ChartBase):
    """D8: a picker / list / ambiguous reply never captures the next message."""

    def test_chart_after_a_picker_reply(self):
        self.attach("county_uk.csv")
        self.attach("education.csv")
        picker = self.assistant(self.send("process this file to company kb"))
        self.assertEqual(picker["metadata"]["file_panel"]["mode"], "picker")

        resp = self.send("create a pie chart")
        self.assertFalse(resp.json()["selection_consumed"])
        msg = self.assistant(resp)
        self.assert_chart_turn(msg, "education.csv")
        self.assertNotEqual(msg["model_name"], "knowledge_base")
        self.enqueue.assert_not_called()
        self.assertEqual(self.company_docs().count(), 0)

    def test_chart_after_a_list_reply(self):
        self.attach("county_uk.csv")
        listing = self.assistant(self.send("show all files"))
        self.assertIn("file_panel", listing["metadata"])

        msg = self.assistant(self.send("create a pie chart"))
        self.assert_chart_turn(msg, "county_uk.csv")
        self.enqueue.assert_not_called()

    def test_chart_after_an_ambiguous_chart_reply(self):
        self.attach("flow_country.csv")
        self.attach("flow_sales.csv")
        ask = self.assistant(self.send("create a pie chart from flow csv file"))
        self.assertIsNone(ask["chart_data"])

        msg = self.assistant(self.send("create a pie chart"))
        self.assert_chart_turn(msg, "flow_sales.csv")
        self.enqueue.assert_not_called()

    def test_bare_continue_with_nothing_stopped_reaches_the_orchestrator(self):
        self.attach("county_uk.csv")
        msg = self.assistant(self.send("continue"))
        self.assertEqual(msg["content"], "llm answer")
        self.assertEqual(self.llm.structured_calls, 1)
        self.enqueue.assert_not_called()

    def test_chart_after_a_picker_reply_on_langgraph(self):
        self.langgraph()
        self.attach("county_uk.csv")
        self.attach("education.csv")
        picker = self.assistant(self.send("process this file to company kb"))
        self.assertEqual(picker["metadata"]["file_panel"]["mode"], "picker")

        with mock.patch(_ROUTE) as route:
            msg = self.assistant(self.send("create a pie chart"))
        route.assert_not_called()
        self.assertEqual(msg["metadata"]["tool_used"], "document_search")
        self.assert_chart_turn(msg, "education.csv")
        self.enqueue.assert_not_called()


# --------------------------------------------------------------------------
# C15: chart selection
# --------------------------------------------------------------------------


class ChartSelectionTests(_ChartBase):
    def test_no_name_uses_the_latest_file(self):
        self.attach("flow_country.csv")
        self.attach("education.csv")
        msg = self.assistant(self.send("create a pie chart"))
        self.assert_chart_turn(msg, "education.csv")

    def test_flow_csv_file_with_one_flow_file(self):
        self.attach("flow_country.csv")
        self.attach("education.csv")
        msg = self.assistant(self.send("pie chart from flow csv file"))
        self.assert_chart_turn(msg, "flow_country.csv")

    def test_two_flow_files_ask_with_a_full_example(self):
        self.attach("flow_country.csv")
        self.attach("flow_sales.csv")
        resp = self.send("create a pie chart from flow csv file")
        self.assertFalse(resp.json()["selection_consumed"])
        msg = self.assistant(resp)
        self.assertIn("for example “pie chart of", msg["content"])
        self.assertIn("flow_country.csv", msg["content"])
        self.assertIn("flow_sales.csv", msg["content"])
        self.assertEqual(msg["model_name"], "knowledge_base")
        self.assertNotIn("file_panel", msg["metadata"])
        self.assertIsNone(msg["chart_data"])
        self.assertEqual(self.llm.calls, 0)

        # The next message routes normally.
        nxt = self.assistant(self.send("what is the capital of France?"))
        self.assertEqual(nxt["content"], "llm answer")
        self.assertEqual(self.llm.structured_calls, 1)

    def test_named_missing_file_is_not_found_without_the_llm(self):
        self.attach("flow_country.csv")
        msg = self.assistant(self.send("pie chart from budget csv file"))
        self.assertEqual(
            msg["content"],
            "I couldn't find a file called “budget” in this chat. "
            "Files here: flow_country.csv.",
        )
        self.assertNotIn("file_panel", msg["metadata"])
        self.assertEqual(self.llm.calls, 0)

    def test_missing_extension_is_not_found(self):
        self.attach("notes.txt", "alpha")
        msg = self.assistant(self.send("pie chart from csv"))
        self.assertEqual(
            msg["content"], "There is no CSV file in this chat. Files here: notes.txt."
        )
        self.assertEqual(self.llm.calls, 0)

    def test_previous_file_on_both_paths_has_the_source_line(self):
        self.chart_llm()

        def setup(session):
            self.attach("data.csv", session=session)
            self.attach("notes.txt", "North 40, South 60", session=session)

        expected = "llm answer\n\nChart built from “notes.txt”."
        text = "create a pie chart from the previous file"

        plain = self.new_session()
        setup(plain)
        msg = self.assistant(self.send(text, session=plain))
        self.assertEqual(msg["content"], expected)
        self.assertIsNotNone(msg["chart_data"])

        self.langgraph()
        graph = self.new_session()
        setup(graph)
        with mock.patch(_ROUTE) as route:
            msg = self.assistant(self.send(text, session=graph))
        route.assert_not_called()
        self.assertEqual(msg["metadata"]["tool_used"], "document_search")
        self.assertEqual(msg["content"], expected)
        self.assertIsNotNone(msg["chart_data"])

    def test_source_line_is_not_repeated_when_the_title_is_named(self):
        self.llm = _RecordingLLM(response="Shares from notes.txt", chart=_PIE)
        self.attach("notes.txt", "North 40, South 60")
        msg = self.assistant(self.send("create a pie chart"))
        self.assertEqual(msg["content"], "Shares from notes.txt")

    def test_non_csv_target_prompt_holds_only_that_file(self):
        self.chart_llm()
        self.attach("notes.txt", "alpha facts 1")
        self.attach("report.txt", "beta facts 2")
        with mock.patch("documents.services.find_relevant_chunks") as chunks:
            msg = self.assistant(self.send("pie chart from notes file"))
        chunks.assert_not_called()
        self.assertIn("“notes.txt”", msg["content"])
        prompt = self.llm.prompts[-1]
        self.assertIn("alpha facts 1", prompt)
        self.assertNotIn("beta facts 2", prompt)
        self.assertIn("retrieved file context supports the values", prompt)

    def test_unreadable_pdf_target_on_both_paths(self):
        expected = "I couldn't read any usable data from “letter.pdf” to build a chart."
        with mock.patch("documents.services.extract_document_text", return_value=""):
            self.attach("letter.pdf", b"%PDF-1.4 not really")
            msg = self.assistant(self.send("create a pie chart"))
            self.assertEqual(msg["content"], expected)
            self.assertIsNone(msg["chart_data"])

            self.langgraph()
            graph = self.new_session()
            self.attach("letter.pdf", b"%PDF-1.4 not really", session=graph)
            with mock.patch(_ROUTE) as route:
                msg = self.assistant(self.send("create a pie chart", session=graph))
            route.assert_not_called()
            self.assertEqual(msg["content"], expected)
        self.assertEqual(self.llm.structured_calls, 0)

    def test_no_files_plain_path_uses_general_knowledge(self):
        self.chart_llm()
        msg = self.assistant(self.send("pie chart of India's GDP growth"))
        self.assertIsNotNone(msg["chart_data"])
        self.assertNotIn("Chart built from", msg["content"])
        self.assertIn("when no file context is available", self.llm.prompts[-1])

    def test_no_files_langgraph_uses_the_knowledge_chart_reply(self):
        self.chart_llm()
        self.langgraph()
        with self.route_to("general_assistant") as route:
            msg = self.assistant(self.send("pie chart of India's GDP growth"))
        route.assert_called_once()
        self.assertEqual(msg["metadata"]["tool_used"], "general_assistant")
        self.assertIsNotNone(msg["chart_data"])
        self.assertIn("Important: decide whether THIS user message", self.llm.prompts[-1])
        self.assertIn("when no file context is available", self.llm.prompts[-1])


# --------------------------------------------------------------------------
# C16: LangGraph routing (R6) and general-tool delegation
# --------------------------------------------------------------------------


class LangGraphChartRoutingTests(_ChartBase):
    def test_session_csv_forces_document_search_without_the_router(self):
        self.langgraph()
        self.attach("county_uk.csv")
        with mock.patch(_ROUTE) as route:
            msg = self.assistant(self.send("create a pie chart"))
        route.assert_not_called()
        self.assertEqual(self.llm.router_calls, 0)
        self.assertEqual(msg["metadata"]["tool_used"], "document_search")
        self.assertEqual(msg["metadata"]["route_reason"], _R6_REASON)
        self.assert_chart_turn(msg, "county_uk.csv")

    def test_ambiguous_target_is_answered_before_the_graph(self):
        self.langgraph()
        self.attach("flow_country.csv")
        self.attach("flow_sales.csv")
        with mock.patch(_ROUTE) as route:
            msg = self.assistant(self.send("pie chart of flow csv"))
        route.assert_not_called()
        self.assertEqual(msg["model_name"], "knowledge_base")
        self.assertIn("for example “pie chart of", msg["content"])
        self.assertEqual(self.llm.calls, 0)

    def test_non_chart_message_still_uses_the_router(self):
        self.langgraph()
        self.attach("county_uk.csv")
        with self.route_to("general_assistant") as route:
            self.assistant(self.send("hello there"))
        route.assert_called_once()


class GeneralToolDelegationTests(_ChartBase):
    """An agent without ``document_search``: the general tool charts the file."""

    langgraph_tools = ("general_assistant",)

    def test_general_assistant_charts_the_session_csv(self):
        self.langgraph()
        self.attach("county_uk.csv")
        with self.route_to("general_assistant"):
            msg = self.assistant(self.send("create a pie chart"))
        self.assertEqual(msg["metadata"]["tool_used"], "general_assistant")
        self.assert_chart_turn(msg, "county_uk.csv")
        self.assertEqual(self.llm.structured_calls, 0)

    def test_general_assistant_charts_a_text_file_with_the_source_line(self):
        self.chart_llm()
        self.langgraph()
        self.attach("notes.txt", "North 40, South 60")
        with self.route_to("general_assistant"):
            msg = self.assistant(self.send("create a pie chart"))
        self.assertEqual(msg["metadata"]["tool_used"], "general_assistant")
        self.assertEqual(msg["content"], "llm answer\n\nChart built from “notes.txt”.")
        self.assertIn("North 40, South 60", self.llm.prompts[-1])
        self.assertNotIn("Important: decide whether", self.llm.prompts[-1])


# --------------------------------------------------------------------------
# C17: no storage details in any prompt
# --------------------------------------------------------------------------


class PromptLeakTests(_ChartBase):
    def setup_files(self, session):
        notes = self.attach("notes.txt", "North 40, South 60", session=session)
        data = self.attach("data.csv", _CSV, session=session)
        self.process(notes, data, session=session)

    def assert_no_leak(self):
        self.assertTrue(self.llm.prompts)
        for prompt in self.llm.prompts:
            for needle in _FORBIDDEN:
                self.assertNotIn(needle, prompt)

    def test_plain_path_prompts(self):
        self.chart_llm()
        self.setup_files(self.session)
        self.assistant(self.send("create a pie chart"))
        self.assistant(self.send("pie chart from notes file"))
        self.assistant(self.send("what do these files say?"))
        self.assertGreaterEqual(self.llm.structured_calls, 3)
        self.assert_no_leak()

    def test_langgraph_router_and_tool_prompts(self):
        self.chart_llm()
        self.langgraph()
        patcher = mock.patch("basics.langfuse.get_langfuse_client", return_value=_FakeLangfuse())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.setup_files(self.session)
        self.assistant(self.send("pie chart from notes file"))
        self.assistant(self.send("what do these files say?"))
        self.assertEqual(self.llm.router_calls, 1)
        self.assertGreaterEqual(self.llm.structured_calls, 1)
        self.assertEqual(self.llm.complete_calls, 1)
        self.assert_no_leak()
