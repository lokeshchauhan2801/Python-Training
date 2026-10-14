"""Chat orchestration service (the LLM dependency-inversion seam).

``ChatService`` depends on the ``LLMClient`` *contract* (one ``complete``
method), injected via its constructor. The default is a real ``LLMClient`` whose
backend/key is only touched on the first ``complete`` call, so constructing a
``ChatService`` never requires a key. Tests inject a fake client.
"""

from basics.llm import LLMClient

from .models import Conversation

_SUMMARY_SYSTEM_PROMPT = (
    "You are a helpful assistant. Summarize the following conversation "
    "concisely, capturing the key questions and conclusions."
)


class ChatService:
    def __init__(self, llm_client: LLMClient | None = None):
        # Default to a real client, but build no backend/key at construct time.
        self._llm = llm_client or LLMClient()

    def _session_messages(self, session):
        """Build provider-neutral messages from the session's prior turns."""
        messages = []
        for turn in session.turns.order_by("created_at"):
            messages.append({"role": "user", "content": turn.question})
            if turn.response:
                messages.append({"role": "assistant", "content": turn.response})
        return messages

    def ask(self, *, session, user, question, actor=None, **context) -> Conversation:
        """Ask a question in a session; persist and return the created turn."""
        messages = self._session_messages(session)
        messages.append({"role": "user", "content": question})

        answer = self._llm.complete(messages)

        turn = Conversation(
            session=session,
            user=user,
            company=session.company,
            model_name=self._llm.model,
            question=question,
            response=answer,
            context=context or {},
            metadata={"provider": self._llm.provider, "model": self._llm.model},
        )
        # full_clean enforces the company-match invariant (and field validation)
        # before the save-time backstop re-checks it.
        turn.full_clean()
        turn.save(actor=actor or user)
        return turn

    def summarize(self, *, session, actor=None) -> str:
        """Summarize a session; persist the summary onto the session."""
        messages = [{"role": "system", "content": _SUMMARY_SYSTEM_PROMPT}]
        messages.extend(self._session_messages(session))

        summary = self._llm.complete(messages)

        session.summary = summary
        session.model_name = self._llm.model
        session.save(
            actor=actor,
            update_fields=["summary", "model_name", "updated_at", "updated_by"],
        )
        return summary
