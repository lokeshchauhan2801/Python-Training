"""Celery workflow for document processing (Redis broker + result backend).

The same seven stages as the Temporal ``DocumentProcessingWorkflow`` run as a
Celery chain, one task per stage (``documents.workflow.<stage>``), on the
``document-processing`` queue. The stage functions are the ones Temporal uses
(``documents.services.stage_*``), so completed stages short-circuit and a
resumed run continues where it stopped.

A run is identified by ``Document.processing_task_id`` (the run id). Each
chain link has the task id ``<run id>.<n>.<stage>``; the run's live state is a
Redis hash ``celery-docflow:<run id>``:

* ``task:<stage>``  latest task id of that stage (for status lookups)
* ``active``        task id that is running or queued next ("" when idle)
* ``stage``         stage that is running
* ``parked``        stage the run stopped before because the user paused it
* ``failed``        stage that failed after its retries
* ``seq``           chain counter (a resumed run builds a new chain)

Controls (``documents.controls``):

* Pause: the document becomes PAUSED. The running stage finishes (embedding
  stops at its next batch) and the run parks before the next stage.
* Resume: the document is PROCESSING again and a chain starts from the parked
  stage under the same run id. With no parked or live run, a new run starts
  and skips the completed stages.
* Cancel: the work is discarded at once and the run's tasks are revoked. The
  stage that is already running finishes, then the run discards again.

Each stage gets three attempts (two retries with backoff) unless the error is
a validation error. Every task checks, under the document's row lock, that
the run is still the document's current run before and after its stage.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from celery import Task, chain, shared_task
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

QUEUE = "document-processing"
TASK_PREFIX = "documents.workflow."
STATE_TTL_SECONDS = 7 * 24 * 3600

STAGES = (
    "mark_processing",
    "extract_text",
    "chunk_text",
    "delete_existing_chunks",
    "embed_and_store_chunks",
    "mark_completed",
    "notify_completed",
)
# Once mark_completed ran the document is READY: never park before this.
_NO_PAUSE_BEFORE = {"notify_completed"}

# Outcomes a stage task returns (and the chain stops on all but CONTINUE).
CONTINUE = "continue"
COMPLETED = "completed"
PAUSED = "paused"
RERUN = "rerun"
CANCELLED = "cancelled"
SUPERSEDED = "superseded"
STOPPED = "stopped"
MISSING = "missing"


# --------------------------------------------------------------------------- #
# Redis run state
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _redis():
    import redis

    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _key(run_id: str) -> str:
    return f"celery-docflow:{run_id}"


def _state_set(run_id: str, **fields) -> None:
    key = _key(run_id)
    pipe = _redis().pipeline()
    pipe.hset(key, mapping={k: "" if v is None else str(v) for k, v in fields.items()})
    pipe.expire(key, STATE_TTL_SECONDS)
    pipe.execute()


def run_state(run_id: str) -> dict:
    if not run_id:
        return {}
    return _redis().hgetall(_key(run_id))


# --------------------------------------------------------------------------- #
# Starting a run
# --------------------------------------------------------------------------- #
def start(document_id: str, run_id: str, from_stage: str | None = None) -> str:
    """Publish the stage chain for ``run_id`` from ``from_stage`` (default:
    the first stage). Returns the first link's task id."""
    stages = STAGES[STAGES.index(from_stage):] if from_stage else STAGES
    seq = _redis().hincrby(_key(run_id), "seq", 1)
    links, ids = [], {}
    for stage in stages:
        task_id = f"{run_id}.{seq}.{stage}"
        ids[f"task:{stage}"] = task_id
        links.append(
            STAGE_TASKS[stage].si(str(document_id), run_id).set(
                task_id=task_id, queue=QUEUE
            )
        )
    first = ids[f"task:{stages[0]}"]
    _state_set(
        run_id,
        document_id=document_id,
        active=first,
        parked="",
        failed="",
        ended="",
        stage="",
        **ids,
    )
    chain(*links).apply_async(
        retry=True,
        retry_policy={"max_retries": 2, "interval_start": 0.5, "interval_step": 1},
    )
    return first


def revoke_run(run_id: str, reason: str = "cancelled") -> None:
    """Revoke every task of the run that has not started (queued links and
    scheduled retries). A stage that is already running finishes.
    ``reason`` ("cancelled", "stopped" or "replaced") is kept for the status."""
    if not run_id:
        return
    try:
        state = run_state(run_id)
        ids = [v for k, v in state.items() if k.startswith("task:") and v]
        if ids:
            from base.celery import app

            app.control.revoke(ids)
        _state_set(run_id, active="", parked="", ended=reason)
    except Exception:  # noqa: BLE001 - the DB state already decides the run
        logger.exception("Could not revoke Celery run %s", run_id)


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
def _alive(state: dict) -> bool:
    """The run has a task running or queued (not finished/failed/revoked)."""
    from celery.result import AsyncResult

    active = state.get("active") or ""
    if not active:
        return False
    return AsyncResult(active).state not in ("SUCCESS", "FAILURE", "REVOKED")


def resume(document_id: str) -> str:
    """Continue a resumed (now PROCESSING) document's run.

    Returns ``"parked"`` (chain started from the parked stage), ``"running"``
    (the run never stopped; it carries on by itself) or ``"restarted"`` (no
    run was left: a new run starts and skips the completed stages).
    """
    from documents.models import Document

    with transaction.atomic():
        document = (
            Document.objects.select_for_update().filter(pk=document_id).first()
        )
        if document is None or document.status != Document.STATUS_PROCESSING:
            return "skipped"
        run_id = document.processing_task_id
        state = run_state(run_id)
        parked = state.get("parked") or ""
        if parked:
            _state_set(run_id, parked="")
            transaction.on_commit(lambda: start(document_id, run_id, parked))
            return "parked"
        if _alive(state):
            return "running"
    from documents.tasks import enqueue_document_processing

    enqueue_document_processing(document_id, backend="celery")
    return "restarted"


# --------------------------------------------------------------------------- #
# Run guards (under the document row lock)
# --------------------------------------------------------------------------- #
def _cancelled(document) -> bool:
    from documents.models import Document

    return document.status == Document.STATUS_UPLOADED and not document.processing_task_id


def _discard_after_commit(document_id: str) -> None:
    def _discard():
        from documents.models import Document
        from documents.services import discard_processing

        document = Document.objects.filter(pk=document_id).first()
        if document is not None and _cancelled(document):
            discard_processing(document)

    transaction.on_commit(_discard)


def _enter(document_id: str, run_id: str, stage: str, task_id: str, retrying: bool) -> str:
    """Decide whether this stage may run. ``"run"`` or a stop outcome."""
    from documents.controls import STOPPED_BY_USER_MESSAGE
    from documents.models import Document

    with transaction.atomic():
        document = (
            Document.objects.select_for_update().filter(pk=document_id).first()
        )
        if document is None:
            return MISSING
        if document.processing_task_id != run_id:
            if _cancelled(document):
                _discard_after_commit(document_id)
                return CANCELLED
            return SUPERSEDED
        status = document.status
        if status == Document.STATUS_PAUSED and stage not in _NO_PAUSE_BEFORE:
            _state_set(run_id, parked=stage, active="", stage="")
            return PAUSED
        if status == Document.STATUS_FAILED:
            stopped = (document.processing_error or "").strip() == STOPPED_BY_USER_MESSAGE
            if not retrying or stopped:
                _state_set(run_id, active="", stage="")
                return STOPPED
            # A retry after this stage's own failure: processing again.
            Document.objects.filter(pk=document.pk).update(
                status=Document.STATUS_PROCESSING, processing_error=""
            )
        elif status == Document.STATUS_READY and stage not in _NO_PAUSE_BEFORE:
            return SUPERSEDED
        elif status == Document.STATUS_UPLOADED:
            return CANCELLED
        _state_set(run_id, active=task_id, stage=stage)
    return "run"


def _after(document_id: str, run_id: str, stage: str, result) -> str:
    """After a stage: continue, park on a pause, rerun a yielded stage that
    was resumed meanwhile, or stop on a cancel."""
    from documents.models import Document

    yielded = isinstance(result, dict) and bool(result.get("paused"))
    index = STAGES.index(stage)
    next_stage = stage if yielded else (STAGES[index + 1] if index + 1 < len(STAGES) else None)

    with transaction.atomic():
        document = (
            Document.objects.select_for_update().filter(pk=document_id).first()
        )
        if document is None:
            return MISSING
        if document.processing_task_id != run_id:
            if _cancelled(document):
                _discard_after_commit(document_id)
                return CANCELLED
            return SUPERSEDED
        if (
            document.status == Document.STATUS_PAUSED
            and next_stage
            and next_stage not in _NO_PAUSE_BEFORE
        ):
            _state_set(run_id, parked=next_stage, active="", stage="")
            return PAUSED
        if yielded:
            if document.status == Document.STATUS_PROCESSING:
                # Paused and resumed while the stage was stopping: run it again.
                _state_set(run_id, active="", stage="")
                transaction.on_commit(lambda: start(document_id, run_id, stage))
                return RERUN
            _state_set(run_id, active="", stage="")
            return STOPPED
        if next_stage is None:
            _state_set(run_id, active="", stage="")
            return COMPLETED
        state = run_state(run_id)
        _state_set(run_id, active=state.get(f"task:{next_stage}", ""), stage="")
    return CONTINUE


def _final_failure(document_id: str, run_id: str, stage: str, exc) -> None:
    """A stage failed after its retries: the document is FAILED at it."""
    from documents.models import Document
    from documents.services import _mark_document_failed

    try:
        _state_set(run_id, active="", stage="", failed=stage)
    except Exception:  # noqa: BLE001
        logger.exception("Could not record failure of Celery run %s", run_id)
    document = Document.objects.filter(
        pk=document_id,
        processing_task_id=run_id,
        status__in=(Document.STATUS_QUEUED, Document.STATUS_PROCESSING),
    ).first()
    if document is not None:
        _mark_document_failed(document, f"{stage.replace('_', ' ')} failed: {exc}")


# --------------------------------------------------------------------------- #
# Stage tasks
# --------------------------------------------------------------------------- #
def _permanent_errors() -> tuple:
    from documents.services import DocumentValidationError

    return (DocumentValidationError,)


class StageTask(Task):
    """One workflow stage: retried (3 attempts in all) with backoff, acked
    after it finishes so a worker crash re-delivers it."""

    autoretry_for = (Exception,)
    max_retries = 2
    retry_backoff = 5
    retry_backoff_max = 60
    retry_jitter = True
    acks_late = True
    reject_on_worker_lost = True
    track_started = True

    @property
    def stage(self) -> str:
        return self.name[len(TASK_PREFIX):]

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        document_id, run_id = args[0], args[1]
        _final_failure(document_id, run_id, self.stage, exc)


def _stop_chain(task) -> None:
    task.request.chain = None
    task.request.callbacks = None


def _run_stage(task, document_id: str, run_id: str) -> dict:
    from documents import services
    from infrastructure.processing.handlers import _record_attempt

    stage = task.stage
    attempt = task.request.retries + 1
    verdict = _enter(document_id, run_id, stage, task.request.id, attempt > 1)
    if verdict != "run":
        _stop_chain(task)
        logger.info("Celery run %s stops before %s: %s", run_id, stage, verdict)
        return {"stage": stage, "outcome": verdict}

    _record_attempt("documents", document_id, attempt)
    try:
        result = getattr(services, f"stage_{stage}")(document_id)
    except _permanent_errors() as exc:
        # Not retried: the stage already marked the document failed.
        _final_failure(document_id, run_id, stage, exc)
        _stop_chain(task)
        return {"stage": stage, "outcome": "failed", "error": str(exc)}

    outcome = _after(document_id, run_id, stage, result)
    if outcome != CONTINUE:
        _stop_chain(task)
    payload = {"stage": stage, "outcome": outcome, "attempt": attempt}
    if isinstance(result, dict):
        payload.update({k: v for k, v in result.items() if k not in payload})
    return payload


def _make_stage_task(stage: str):
    def run(self, document_id: str, run_id: str) -> dict:
        return _run_stage(self, document_id, run_id)

    run.__name__ = f"stage_{stage}"
    run.__qualname__ = run.__name__
    return shared_task(bind=True, base=StageTask, name=f"{TASK_PREFIX}{stage}")(run)


STAGE_TASKS = {stage: _make_stage_task(stage) for stage in STAGES}


# --------------------------------------------------------------------------- #
# Status (from Celery's Redis result backend)
# --------------------------------------------------------------------------- #
def describe(run_id: str) -> dict:
    """The run as Celery sees it: overall status, current/parked stage and the
    Celery state of every stage task. Never raises."""
    from celery.result import AsyncResult

    try:
        state = run_state(run_id)
    except Exception as exc:  # noqa: BLE001 - Redis down: report it
        return {"reachable": False, "found": False, "error": str(exc)}
    if not state:
        return {"reachable": True, "found": False, "run_id": run_id}

    stages = []
    for stage in STAGES:
        task_id = state.get(f"task:{stage}") or ""
        row = {"name": stage, "task_id": task_id, "state": "NOT_SCHEDULED"}
        if task_id:
            result = AsyncResult(task_id)
            row["state"] = result.state
            info = result.info
            if isinstance(info, dict):
                row["outcome"] = info.get("outcome")
                row["attempt"] = info.get("attempt")
            elif info is not None and result.state in ("FAILURE", "RETRY"):
                row["error"] = str(info)[:300]
            if result.date_done:
                row["finished_at"] = result.date_done.isoformat()
        stages.append(row)

    if state.get("ended"):
        status = state["ended"].upper()
    elif state.get("parked"):
        status = "PAUSED"
    elif state.get("failed"):
        status = "FAILED"
    elif any(r.get("outcome") == CANCELLED for r in stages):
        status = "CANCELLED"
    elif state.get("active"):
        status = "RUNNING"
    elif any(r.get("outcome") == COMPLETED for r in stages):
        status = "COMPLETED"
    else:
        status = "STOPPED"
    return {
        "reachable": True,
        "found": True,
        "run_id": run_id,
        "status": status,
        "stage": state.get("stage") or "",
        "parked": state.get("parked") or "",
        "failed": state.get("failed") or "",
        "active_task": state.get("active") or "",
        "chains": int(state.get("seq") or 0),
        "stages": stages,
    }


def workers_online(timeout: float = 1.0) -> list:
    """Names of the Celery workers that answer a ping (empty when none)."""
    try:
        from base.celery import app

        replies = app.control.ping(timeout=timeout) or []
        return sorted(name for reply in replies for name in reply)
    except Exception:  # noqa: BLE001
        return []
