from .interfaces import ProcessingBackend, ProcessingRequest


class TemporalProcessingBackend(ProcessingBackend):
    TASK_QUEUE = "document-processing"

    def dispatch(self, request: ProcessingRequest) -> str:


        import asyncio

        return asyncio.run(self._dispatch_async(request))

    async def _dispatch_async(self, request: ProcessingRequest) -> str:
        import temporalio.client as tc
        from django.conf import settings

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        surface = (request.extra or {}).get("surface", "documents")
        handle = await client.start_workflow(
            "DocumentProcessingWorkflow",
            {
                "document_id": request.document_id,
                "company_id": request.company_id,
                "user_id": request.user_id,
                "surface": surface,
                "backend": request.backend,
            },
            id=f"doc-{request.document_id}",
            task_queue=self.TASK_QUEUE,
        )
        return handle.id

    def get_status(self, job_id: str) -> dict:


        import asyncio

        try:
            return asyncio.run(self._status_async(job_id))
        except Exception:
            return {
                "status": "unknown",
                "degraded": True,
                "run_id": None,
                "started": None,
                "closed": None,
                "result": None,
            }

    async def _status_async(self, job_id: str) -> dict:
        import temporalio.client as tc
        from django.conf import settings

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        handle = client.get_workflow_handle(job_id)


        desc = await handle.describe()
        return {
            "status": desc.status.name if desc.status else "unknown",
            "degraded": False,
            "run_id": desc.run_id,
            "started": desc.start_time.isoformat() if desc.start_time else None,
            "closed": desc.close_time.isoformat() if desc.close_time else None,
            "result": None,
        }

    def terminate(self, job_id: str) -> dict:
        """Hard-stop a running workflow by id.

        ``terminate`` is used instead of ``cancel`` on purpose:
        ``terminate`` is an immediate, server-side hard stop of a running
        workflow, whereas ``cancel`` only *requests* cooperative cancellation
        that the workflow code must observe between activities.
        ``DocumentProcessingWorkflow.run`` has no cancellation-aware points
        between its activities, so a ``cancel`` would not reliably halt it —
        ``terminate`` guarantees the run stops now.

        Tolerant like ``get_status``: a missing/already-closed workflow or an
        unreachable Temporal server never raises out of here; it returns a
        degraded result dict instead so the caller can still flip the document.
        """
        import asyncio

        try:
            return asyncio.run(self._terminate_async(job_id))
        except Exception:
            return {"terminated": False, "degraded": True}

    async def _terminate_async(self, job_id: str) -> dict:
        import temporalio.client as tc
        from django.conf import settings

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        handle = client.get_workflow_handle(job_id)
        try:
            await handle.terminate(reason="Stopped by user")
        except Exception:
            # Not found / already closed — nothing left to stop; stay tolerant
            # so the caller still marks the document stopped.
            return {"terminated": False, "already_closed": True}
        return {"terminated": True}

    def signal(self, job_id: str, name: str) -> bool:
        """Send signal ``name`` (``pause`` / ``resume``) to the running workflow.

        False when it could not be delivered: no running workflow under that id
        (never started, or already closed) or Temporal is unreachable.
        """
        import asyncio

        try:
            return asyncio.run(self._signal_async(job_id, name))
        except Exception:
            return False

    async def _signal_async(self, job_id: str, name: str) -> bool:
        import temporalio.client as tc
        from django.conf import settings

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        await client.get_workflow_handle(job_id).signal(name)
        return True

    def cancel(self, job_id: str) -> bool:
        """Request cancellation; the workflow cleans up and closes as Cancelled.

        False when there is no running workflow or Temporal is unreachable.
        """
        import asyncio

        try:
            return asyncio.run(self._cancel_async(job_id))
        except Exception:
            return False

    async def _cancel_async(self, job_id: str) -> bool:
        import temporalio.client as tc
        from django.conf import settings

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        handle = client.get_workflow_handle(job_id)
        desc = await handle.describe()
        if desc.status is None or desc.status.name != "RUNNING":
            return False
        await handle.cancel()
        return True

    # ------------------------------------------------------------------ #
    # Generic helpers (chat-only file processing uses its own workflow)
    # ------------------------------------------------------------------ #
    def start(self, workflow: str, params: dict, workflow_id: str) -> str:
        """Start ``workflow`` under ``workflow_id`` on this task queue."""
        import asyncio

        return asyncio.run(self._start_async(workflow, params, workflow_id))

    async def _start_async(self, workflow: str, params: dict, workflow_id: str) -> str:
        import temporalio.client as tc
        from django.conf import settings

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        handle = await client.start_workflow(
            workflow, params, id=workflow_id, task_queue=self.TASK_QUEUE
        )
        return handle.id

    def describe(self, job_id: str) -> dict:
        """The workflow's status from the Temporal API, plus its ``get_state``
        query while it is running.

        ``{"reachable": False}`` when Temporal cannot be reached, and
        ``{"reachable": True, "found": False}`` when no workflow has that id.
        """
        import asyncio

        try:
            return asyncio.run(self._describe_async(job_id))
        except Exception:
            return {"reachable": False}

    async def _describe_async(self, job_id: str) -> dict:
        import temporalio.client as tc
        from django.conf import settings
        from temporalio.service import RPCError, RPCStatusCode

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        handle = client.get_workflow_handle(job_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return {"reachable": True, "found": False}
            raise
        status = desc.status.name if desc.status else "UNKNOWN"
        state = {}
        if status == "RUNNING":
            try:
                state = await handle.query("get_state")
            except Exception:  # noqa: BLE001 - a busy worker must not hide the status
                state = {}
        return {
            "reachable": True,
            "found": True,
            "status": status,
            "run_id": desc.run_id,
            "started": desc.start_time.isoformat() if desc.start_time else None,
            "closed": desc.close_time.isoformat() if desc.close_time else None,
            "state": state if isinstance(state, dict) else {},
        }

    # ------------------------------------------------------------------ #
    # Full inspection for the admin Info modal: everything from Temporal
    # ------------------------------------------------------------------ #
    def inspect(self, job_id: str, stage_order: tuple = ()) -> dict:
        """Status, live state and per-stage timings of ``job_id``, read only
        from the Temporal API (no Django DB).

        * ``DescribeWorkflowExecution`` -> status, run id, times, pending activities
        * ``QueryWorkflow("get_state")`` -> paused flag + current stage (running only)
        * ``GetWorkflowExecutionHistory`` -> one row per stage from the
          ActivityTask* events, the signals received and the workflow result

        ``stage_order`` lists the stage slugs so stages not reached yet show
        as ``pending``. Never raises: an unreachable server returns
        ``{"reachable": False, ...}`` and an unknown id ``{"found": False}``.
        """
        import asyncio

        try:
            return asyncio.run(self._inspect_async(job_id, stage_order))
        except Exception as exc:  # noqa: BLE001 - the modal must still render
            return {"reachable": False, "found": False, "error": str(exc)}

    async def _inspect_async(self, job_id: str, stage_order: tuple) -> dict:
        import temporalio.client as tc
        from django.conf import settings
        from temporalio.api.enums.v1 import EventType
        from temporalio.service import RPCError, RPCStatusCode

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        handle = client.get_workflow_handle(job_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return {"reachable": True, "found": False}
            raise
        status = desc.status.name if desc.status else "UNKNOWN"

        state: dict = {}
        if status == "RUNNING":
            try:
                state = await handle.query("get_state")
            except Exception:  # noqa: BLE001 - no worker: status still shows
                state = {}

        converter = client.data_converter

        async def decode(payloads):
            if not payloads or not payloads.payloads:
                return None
            try:
                values = await converter.decode(list(payloads.payloads))
                return values[0] if values else None
            except Exception:  # noqa: BLE001
                return None

        def iso(ts):
            return ts.ToDatetime().replace(tzinfo=_utc()).isoformat() if ts and ts.seconds else None

        # scheduled event id -> stage slug (completion events point back to it)
        scheduled: dict[int, str] = {}
        stages: dict[str, dict] = {}
        signals: list[dict] = []
        result = None
        failure = ""

        async for event in handle.fetch_history_events():
            et = event.event_type
            when = iso(event.event_time)
            if et == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                attrs = event.activity_task_scheduled_event_attributes
                slug = attrs.activity_type.name.removeprefix("stage_")
                scheduled[event.event_id] = slug
                row = stages.setdefault(slug, {
                    "name": slug, "status": "scheduled", "detail": "",
                    "scheduled_at": when, "started_at": None, "finished_at": None,
                    "attempt": 0, "runs": 0,
                })
                row["runs"] += 1
                row["status"] = "scheduled"
                row["finished_at"] = None
            elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
                attrs = event.activity_task_started_event_attributes
                row = stages.get(scheduled.get(attrs.scheduled_event_id, ""))
                if row:
                    row["started_at"] = row["started_at"] or when
                    row["attempt"] = attrs.attempt
                    row["status"] = "running"
                    if attrs.last_failure.message:
                        row["detail"] = attrs.last_failure.message
            elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
                attrs = event.activity_task_completed_event_attributes
                row = stages.get(scheduled.get(attrs.scheduled_event_id, ""))
                if row:
                    value = await decode(attrs.result)
                    row["finished_at"] = when
                    if isinstance(value, dict) and value.get("paused"):
                        row["status"] = "paused"
                        row["detail"] = "Stopped at a checkpoint (pause signal)"
                    else:
                        row["status"] = "completed"
                        row["detail"] = ""
            elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED:
                attrs = event.activity_task_failed_event_attributes
                row = stages.get(scheduled.get(attrs.scheduled_event_id, ""))
                if row:
                    row["status"] = "failed"
                    row["finished_at"] = when
                    row["detail"] = attrs.failure.message
            elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
                attrs = event.activity_task_timed_out_event_attributes
                row = stages.get(scheduled.get(attrs.scheduled_event_id, ""))
                if row:
                    row["status"] = "timed_out"
                    row["finished_at"] = when
                    row["detail"] = attrs.failure.message
            elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_CANCELED:
                attrs = event.activity_task_canceled_event_attributes
                row = stages.get(scheduled.get(attrs.scheduled_event_id, ""))
                if row:
                    row["status"] = "canceled"
                    row["finished_at"] = when
            elif et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
                signals.append({
                    "name": event.workflow_execution_signaled_event_attributes.signal_name,
                    "at": when,
                })
            elif et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
                result = await decode(event.workflow_execution_completed_event_attributes.result)
            elif et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED:
                failure = event.workflow_execution_failed_event_attributes.failure.message
            elif et == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED:
                failure = event.workflow_execution_terminated_event_attributes.reason

        # A paused run that is waiting shows its current stage as paused.
        if state.get("paused") and state.get("stage") in stages:
            row = stages[state["stage"]]
            if row["status"] in ("completed", "paused"):
                row["status"] = "paused"
                row["detail"] = row["detail"] or "Waiting for resume signal"

        pending = []
        for pa in desc.raw_description.pending_activities:
            pending.append({
                "name": pa.activity_type.name.removeprefix("stage_"),
                "state": _pending_state_name(pa.state),
                "attempt": pa.attempt,
                "last_failure": pa.last_failure.message or "",
                "last_heartbeat": iso(pa.last_heartbeat_time),
            })

        ordered = [stages.pop(slug) for slug in stage_order if slug in stages]
        ordered += list(stages.values())  # any activity outside the known order
        reached = {row["name"] for row in ordered}
        for slug in stage_order:
            if slug not in reached:
                ordered.append({
                    "name": slug, "status": "pending", "detail": "",
                    "scheduled_at": None, "started_at": None, "finished_at": None,
                    "attempt": 0, "runs": 0,
                })
        if stage_order:
            position = {slug: i for i, slug in enumerate(stage_order)}
            ordered.sort(key=lambda r: position.get(r["name"], len(position)))

        return {
            "reachable": True,
            "found": True,
            "status": status,
            "run_id": desc.run_id,
            "workflow_type": desc.workflow_type,
            "task_queue": desc.task_queue,
            "started": desc.start_time.isoformat() if desc.start_time else None,
            "closed": desc.close_time.isoformat() if desc.close_time else None,
            "history_length": desc.history_length,
            "paused": bool(state.get("paused")),
            "current_stage": state.get("stage") or "",
            "query_ok": bool(state) or status != "RUNNING",
            "pending_activities": pending,
            "signals": signals,
            "result": result,
            "failure": failure,
            "stages": ordered,
        }


def _utc():
    from datetime import timezone

    return timezone.utc


def _pending_state_name(value) -> str:
    from temporalio.api.enums.v1 import PendingActivityState

    try:
        return PendingActivityState.Name(value).removeprefix("PENDING_ACTIVITY_STATE_")
    except ValueError:
        return str(value)
