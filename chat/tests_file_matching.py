"""Pure tests for chat.services.file_matching and file_commands.classify."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from chat.services import file_matching as fm
from chat.services.file_commands import Kind, classify

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class F:
    id: str
    title: str
    file_type: str
    attached_at: datetime
    readable: bool = True


def make_files(*titles):
    """Files attached in the given order: the last title is the newest."""
    return [
        F(
            id=f"e{i}",
            title=title,
            file_type=title.rsplit(".", 1)[-1],
            attached_at=_T0 + timedelta(minutes=i),
        )
        for i, title in enumerate(titles)
    ]


def titles(files):
    return [f.title for f in files]


class ChartSelectionWorkedExamplesTests(SimpleTestCase):
    """M1: every H.2 worked example."""

    def setUp(self):
        self.files = make_files("flow_country.csv", "flow_sales.csv", "education.csv")

    def target(self, message, files=None):
        return fm.select_chart_file(self.files if files is None else files, message)

    def assertFile(self, message, title, files=None):
        target = self.target(message, files)
        self.assertEqual(target.kind, "file", message)
        self.assertEqual(target.file.title, title, message)

    def test_two_flow_files_are_ambiguous(self):
        target = self.target("create a pie chart from flow csv file")
        self.assertEqual(target.kind, "ambiguous")
        self.assertEqual(
            sorted(titles(target.candidates)), ["flow_country.csv", "flow_sales.csv"]
        )

    def test_d7_example_with_one_flow_file(self):
        only = make_files("flow_country.csv")
        self.assertFile("create a pie chart from flow csv file", "flow_country.csv", only)
        self.assertFile("pie chart of flow csv", "flow_country.csv", only)

    def test_score_two_beats_score_one(self):
        self.assertFile("pie chart from flow country csv", "flow_country.csv")

    def test_exact_title(self):
        self.assertFile("chart of education.csv", "education.csv")

    def test_named_but_missing_is_not_found(self):
        for message in [
            "pie chart from budget csv file",
            "chart of budget.csv",
            "pie chart from budget file",
            "pie chart from budget csv",
            "pie chart from budget pdf",
        ]:
            target = self.target(message)
            self.assertEqual(target.kind, "not_found", message)
            self.assertEqual(target.tokens, ("budget",), message)

    def test_extension_only_picks_newest_of_that_type(self):
        files = make_files("flow_country.csv", "education.csv", "notes.pdf")
        for message in ["pie chart from csv file", "pie chart from csv"]:
            self.assertFile(message, "education.csv", files)

    def test_no_name_picks_newest(self):
        files = make_files("education.csv", "flow_country.csv", "notes.pdf")
        for message in [
            "pie chart of U.S. states population",
            "create a pie chart",
            "pie chart of students by country",
        ]:
            self.assertFile(message, "notes.pdf", files)

    def test_explicit_name_allows_inner_hits(self):
        files = make_files("2024_sales_report.csv", "education.csv")
        self.assertFile("chart from sales report file", "2024_sales_report.csv", files)

    def test_no_readable_files(self):
        self.assertEqual(self.target("create a pie chart", []).kind, "no_files")
        removed = [F("x", "a.csv", "csv", _T0, readable=False)]
        self.assertEqual(self.target("create a pie chart", removed).kind, "no_files")

    def test_extension_with_no_such_file_is_not_found(self):
        target = self.target("pie chart from pdf")
        self.assertEqual(target.kind, "not_found")
        self.assertEqual(
            fm.chart_target_reply(target, self.files),
            "There is no PDF file in this chat. Files here: "
            "flow_country.csv, flow_sales.csv, education.csv.",
        )


class ReferenceWordTests(SimpleTestCase):
    """M1, review-3 F1 table: reference words mean the newest file."""

    def setUp(self):
        self.files = make_files(
            "quarterly.xlsx", "flow_country.csv", "flow_sales.csv",
            "education.csv", "notes.pdf",
        )

    def assertFile(self, message, title):
        target = fm.select_chart_file(self.files, message)
        self.assertEqual(target.kind, "file", message)
        self.assertEqual(target.file.title, title, message)

    def test_reference_words_pick_newest_file(self):
        for message in [
            "create a pie chart from the previous file",
            "create a pie chart from that file",
            "chart the data in that file",
            "pie chart from the attached file",
            "pie chart from the last file",
            "pie chart from the same file",
            "pie chart from the current file",
            "make a pie chart of these files",
        ]:
            self.assertFile(message, "notes.pdf")

    def test_reference_words_with_extension_pick_newest_of_type(self):
        for message in [
            "pie chart from the uploaded csv",
            "create a pie chart from the above csv",
            "create a pie chart from the earlier csv",
        ]:
            self.assertFile(message, "education.csv")

    def test_excel_alias(self):
        self.assertFile("pie chart from the excel sheet", "quarterly.xlsx")

    def test_budget_csv_still_not_found(self):
        target = fm.select_chart_file(self.files, "pie chart from budget csv")
        self.assertEqual(target.kind, "not_found")


class MatchFilesTests(SimpleTestCase):
    """M2: exact/prefix passes, filters and tie-breaks."""

    def match(self, message, files, stopwords=fm.CHART_STOPWORDS):
        return fm.match_files(message, files, stopwords=stopwords)

    def test_exact_beats_prefix(self):
        files = make_files("flow.csv", "flow_country.csv")
        m = self.match("chart of flow.csv", files)
        self.assertEqual(m.kind, "exact")
        self.assertEqual(titles(m.files), ["flow.csv"])

    def test_extension_filter(self):
        files = make_files("flow_country.csv", "flow_notes.pdf")
        m = self.match("process flow pdf", files, fm.COMMAND_STOPWORDS)
        self.assertEqual(m.kind, "prefix")
        self.assertEqual(titles(m.files), ["flow_notes.pdf"])
        self.assertEqual(m.ext_filter, frozenset({"pdf"}))

    def test_score_tie_break_keeps_top_score(self):
        files = make_files("flow_country.csv", "flow_sales.csv")
        m = self.match("flow country csv", files)
        self.assertEqual(titles(m.files), ["flow_country.csv"])

    def test_same_title_collapses_to_newest_in_both_passes(self):
        files = make_files("flow.csv", "flow.csv")
        exact = self.match("chart of flow.csv", files)
        self.assertEqual(exact.kind, "exact")
        self.assertEqual([f.id for f in exact.files], ["e1"])
        prefix = self.match("chart from flow csv", files)
        self.assertEqual(prefix.kind, "prefix")
        self.assertEqual([f.id for f in prefix.files], ["e1"])

    def test_tokens_under_three_chars_are_ignored(self):
        self.assertEqual(fm.name_tokens("pie chart of ab csv", fm.CHART_STOPWORDS), [])
        files = make_files("abc.csv", "zed.csv")
        self.assertEqual(self.match("pie chart of ab csv", files).kind, "none")
        target = fm.select_chart_file(files, "pie chart of ab csv")
        self.assertEqual(target.file.title, "zed.csv")

    def test_start_hit_required_without_explicit_name(self):
        files = make_files("flow_country.csv")
        self.assertEqual(self.match("pie chart of students by country", files).kind, "none")
        m = self.match("pie chart from country file", files)
        self.assertEqual(m.kind, "prefix")
        self.assertEqual(titles(m.files), ["flow_country.csv"])

    def test_unreadable_files_are_not_candidates(self):
        files = [F("x", "flow.csv", "csv", _T0, readable=False)]
        self.assertEqual(self.match("chart of flow.csv", files).kind, "none")

    def test_prefix_hits(self):
        files = make_files("flow_country.csv", "education.csv")
        self.assertEqual(titles(fm.prefix_hits("flow", files)), ["flow_country.csv"])
        self.assertEqual(fm.prefix_hits("country", files), [])
        self.assertEqual(
            titles(fm.prefix_hits("country", files, start_only=False)),
            ["flow_country.csv"],
        )


class ExplicitNameTokensTests(SimpleTestCase):
    """M2b (V1) plus the review-3 F1 additions."""

    def test_explicit_tokens(self):
        cases = {
            "from budget csv": ["budget"],
            "flow country csv": ["flow", "country"],
            "called quarterly sales": ["quarterly", "sales"],
            "budget.csv": ["budget"],
        }
        for stopwords in (fm.CHART_STOPWORDS, fm.COMMAND_STOPWORDS):
            for text, expected in cases.items():
                self.assertEqual(fm.explicit_name_tokens(text, stopwords), expected, text)

    def test_no_explicit_tokens(self):
        for stopwords in (fm.CHART_STOPWORDS, fm.COMMAND_STOPWORDS):
            for text in [
                "explaining the document",
                "from csv file",
                "can you stop processing?",
                "from the previous file",
                "from that file",
                "from the attached file",
                "from the uploaded csv",
            ]:
                self.assertEqual(fm.explicit_name_tokens(text, stopwords), [], text)
        # A command phrase: "all" is a command stopword, not a chart stopword.
        self.assertEqual(
            fm.explicit_name_tokens(
                "status of all files in this chat", fm.COMMAND_STOPWORDS
            ),
            [],
        )

    def test_type_alias_is_never_a_token(self):
        self.assertEqual(fm.name_tokens("the excel sheet", fm.COMMAND_STOPWORDS), ["sheet"])
        self.assertEqual(fm.name_tokens("the excel sheet", fm.CHART_STOPWORDS), [])


class SegmentTests(SimpleTestCase):
    """Review-3 F6: one match per named file in a file command."""

    def setUp(self):
        self.files = make_files("flow_country.csv", "flow_sales.csv", "education.csv")

    def test_split_segments(self):
        self.assertEqual(
            fm.split_segments("process a.csv, b.csv & c.csv and d plus e"),
            ["process a.csv", "b.csv", "c.csv", "d", "e"],
        )
        self.assertEqual(fm.split_segments("process flow csv"), ["process flow csv"])

    def test_two_segments_two_files(self):
        matches = fm.match_command_segments(
            "process flow country csv and education csv",
            self.files,
            stopwords=fm.COMMAND_STOPWORDS,
        )
        self.assertEqual(len(matches), 2)
        self.assertEqual([titles(m.files) for m in matches],
                         [["flow_country.csv"], ["education.csv"]])

    def test_single_segment_is_the_whole_message(self):
        matches = fm.match_command_segments(
            "process flow csv", self.files, stopwords=fm.COMMAND_STOPWORDS
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            sorted(titles(matches[0].files)), ["flow_country.csv", "flow_sales.csv"]
        )


class ChartReplyTests(SimpleTestCase):
    """H.2 replies, with the review-3 F11 ambiguous wording."""

    def setUp(self):
        self.files = make_files("flow_country.csv", "flow_sales.csv", "education.csv")

    def test_ambiguous_reply_has_full_example_request(self):
        target = fm.select_chart_file(self.files, "create a pie chart from flow csv file")
        reply = fm.chart_target_reply(target, self.files)
        self.assertIn("for example “pie chart of", reply)
        self.assertEqual(
            reply,
            "I found several files matching “flow”: flow_country.csv, flow_sales.csv. "
            "Name the one you want, for example “pie chart of flow_country.csv”.",
        )

    def test_not_found_reply(self):
        target = fm.select_chart_file(self.files, "pie chart from budget csv")
        self.assertEqual(
            fm.chart_target_reply(target, self.files),
            "I couldn't find a file called “budget” in this chat. Files here: "
            "flow_country.csv, flow_sales.csv, education.csv.",
        )

    def test_file_target_has_no_reply(self):
        target = fm.select_chart_file(self.files, "chart of education.csv")
        self.assertEqual(fm.chart_target_reply(target, self.files), "")


class ClassifyPositiveTests(SimpleTestCase):
    """M3 plus the review-3 F2/F3/F5 positives."""

    def assertKind(self, texts, kind):
        for text in texts:
            self.assertEqual(classify(text).kind, kind, text)

    def test_process(self):
        self.assertKind([
            "process flow csv", "process education.csv", "process these",
            "please process all", "reprocess flow csv", "start processing this file",
            "can you process these files?", "process both files",
        ], Kind.PROCESS)

    def test_process_add_phrasings(self):
        self.assertKind([
            "add this file to the knowledge base", "add flow csv to kb",
            "save it to company documents", "upload these to the library",
            "put it into the knowledge base", "index this document",
            "add this document", "please add these files",
            # chat.tests_kb_selection.IntentDetectionTests phrasings
            "add the document to the company documents",
            "add documents to the knowledge base",
        ], Kind.PROCESS)

    def test_status(self):
        texts = [
            "status", "status?", "what's the status", "what is the current status",
            "current status", "status of flow csv", "show status", "status please",
        ]
        self.assertKind(texts, Kind.STATUS)
        for text in texts:
            self.assertFalse(classify(text).weak, text)

    def test_weak_status_questions(self):
        texts = [
            "is it done?", "is the processing done?", "has the upload completed?",
            "how is the processing going?", "why did it fail?", "is flow csv done?",
            "what happened to my file?",
        ]
        self.assertKind(texts, Kind.STATUS)
        for text in texts:
            self.assertTrue(classify(text).weak, text)

    def test_stop(self):
        self.assertKind(
            ["stop", "stop processing", "stop everything", "stop flow csv"], Kind.STOP
        )

    def test_resume(self):
        self.assertKind(["try again", "restart", "resume x.csv"], Kind.RESUME)

    def test_list(self):
        self.assertKind(
            ["show all files", "list files", "which files are attached?"], Kind.LIST
        )

    def test_bare(self):
        for text, kind in [
            ("stop", Kind.STOP), ("continue", Kind.RESUME), ("try again", Kind.RESUME),
            ("can you stop it?", Kind.STOP), ("could you please stop", Kind.STOP),
            ("ok stop", Kind.STOP), ("yes, resume it", Kind.RESUME),
            ("go ahead and resume", Kind.RESUME),
        ]:
            command = classify(text)
            self.assertEqual(command.kind, kind, text)
            self.assertTrue(command.bare, text)

    def test_strong_stop(self):
        for text in [
            "can you stop processing?", "I want to stop processing",
            "stop processing right now", "please stop the processing, I don't need it",
            "stop processing immediately", "stop the upload",
        ]:
            command = classify(text)
            self.assertEqual(command.kind, Kind.STOP, text)
            self.assertFalse(command.bare, text)
            self.assertTrue(command.strong, text)

    def test_strong_resume(self):
        for text in [
            "resume all stopped files", "could you try again on the failed files",
            "resume the flow file",
        ]:
            command = classify(text)
            self.assertEqual(command.kind, Kind.RESUME, text)
            self.assertTrue(command.strong, text)

    def test_short_forms_need_a_name(self):
        for text, kind in [
            ("stop flow", Kind.STOP), ("resume flow", Kind.RESUME),
            ("retry flow", Kind.RESUME),
        ]:
            command = classify(text)
            self.assertEqual(command.kind, kind, text)
            self.assertTrue(command.needs_name, text)
            self.assertFalse(command.strong, text)
            self.assertEqual(command.loose_tokens, ("flow",), text)


class ClassifyNegativeTests(SimpleTestCase):
    """M4 plus the review-3 F2/F3 negatives."""

    def test_none(self):
        for text in [
            "which documents mention revenue?",
            "what is the status of the French economy",
            "how do I process payroll in excel?",
            "how do I process a csv in pandas?",
            "try again with a pie chart of the csv",
            "retry the chart using the csv file",
            "show me the documents about revenue",
            "list the documents that mention GDP",
            "what is the progress of this project",
            "add a pie chart for the documents",
            "show me a chart of files per region",
            # F2: editing requests
            "add a summary of the document",
            "put the key points of this document in a table",
            "save the main figures from the documents as a list",
            "can you add more detail about the documents?",
            "add a conclusion section to the document",
            "store these numbers, I will compare documents later",
            # F3
            "how is it going?",
            "what is done in a merger?",
            "why did the model fail to converge?",
            "what happened in 1945?",
            "",
        ]:
            self.assertEqual(classify(text).kind, Kind.NONE, text)

    def test_verbs_without_a_file_cue_need_a_name(self):
        # F5: these reach effective_kind with needs_name, which lets them through
        # only when every loose token starts a file name, so they route as NONE.
        for text in ["stop using jargon", "continue the story", "stop the docker container"]:
            command = classify(text)
            self.assertIn(command.kind, (Kind.STOP, Kind.RESUME), text)
            self.assertTrue(command.needs_name, text)
            self.assertFalse(command.strong, text)
            self.assertTrue(command.loose_tokens, text)

    def test_non_strong_resume(self):
        for text, tokens in [
            ("continue explaining the document", ("explaining",)),
            ("restart the analysis of this file", ("analysis",)),
        ]:
            command = classify(text)
            self.assertEqual(command.kind, Kind.RESUME, text)
            self.assertFalse(command.bare, text)
            self.assertFalse(command.strong, text)
            self.assertFalse(command.needs_name, text)
            self.assertEqual(command.loose_tokens, tokens, text)
