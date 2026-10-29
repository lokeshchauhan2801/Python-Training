"""Intent detection in ``chat.services.kb`` (status / add-to-KB phrasings).

The numbered pending-selection flow these tests used to cover is retired; the
chat file commands that replaced it are tested in ``chat.tests_file_commands``.
"""

from __future__ import annotations

from django.test import TestCase

from .services import kb


class IntentDetectionTests(TestCase):
    """is_status_request / is_add_to_kb_request classification."""

    def test_status_phrasings_true(self):
        for text in [
            "show me the status",
            "show status",
            "processing status",
            "what's processing",
            "what is processing",
            "is my file done",
            "is my document ready",
            "check the status of my upload",
        ]:
            self.assertTrue(kb.is_status_request(text), text)

    def test_ordinary_question_not_status(self):
        self.assertFalse(
            kb.is_status_request("what is the capital of France")
        )
        self.assertFalse(
            kb.is_status_request("what is the status of the French economy")
        )

    def test_add_to_kb_phrasings_true(self):
        self.assertTrue(
            kb.is_add_to_kb_request("add the document to the company documents")
        )
        self.assertTrue(
            kb.is_add_to_kb_request("add documents to the knowledge base")
        )
