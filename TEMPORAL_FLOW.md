# Temporal: processing flow and UI access

How a document is processed by Temporal, and how the Temporal Web UI is
protected so only staff can open it.

**In short:**

- **Processing.** The documents admin (staff) or chat (any logged-in user)
  queues a document. Django starts one Temporal workflow per document,
  `doc-<document id>`, and the `temporal_worker` runs its seven stages.
- **UI access.** Temporal UI has no login and no host port. The only way in is
  `http://localhost:8000/temporal/`. Django checks the user is logged in and
  is active staff, then forwards the request with a secret header to an nginx
  gate on `127.0.0.1:8080`. The gate passes only requests carrying that secret
  on to Temporal UI.

```
Browser ──► Django :8000 ──────────────► nginx gate 127.0.0.1:8080 ──► Temporal UI (no host port) ──► Temporal server
            /temporal/* → ui_proxy.py     403 unless secret header      view only, under /temporal      127.0.0.1:7234
            login + staff check,
            adds secret header
```

---

## 1. The pieces and where they run

| Piece | File | Runs in |
|---|---|---|
| Admin "Action → Temporal", Stop, Retry | `documents/admin.py` (`dispatch_view`, `temporal_cancel_view`, `temporal_retry_view`) | Django |
| Chat "add this file to the knowledge base" | `chat/services/file_commands.py` → `documents/controls.py` | Django |
| Queue a document | `documents/tasks.py → enqueue_document_processing` | Django |
| Start / query / terminate a workflow | `infrastructure/processing/temporal_backend.py` | Django |
| Workflow and its seven stage activities | `infrastructure/temporal/document_workflow.py` | `python manage.py temporal_worker` |
| Stage logic (extract, chunk, embed …) | `documents/services.py` | the worker |
| Staff-only UI proxy | `infrastructure/temporal/ui_proxy.py`, routed by `base/urls.py` (`temporal/`) | Django |
| nginx gate | `infrastructure/temporal/ui_gate.conf.template` | Docker `temporal-ui-gate` |
| Temporal UI | `temporalio/ui:2.34.0` | Docker `temporal-ui` |
| Temporal server | `temporalio/auto-setup:1.24.2` | Docker `temporal` (+ `temporal-postgresql`) |

All Docker services are in `docker-compose.processing.yml`.

### Ports

| Port | What | Reachable from |
|---|---|---|
| `8000` | Django | the browser |
| `127.0.0.1:8080` | nginx gate | this machine only; 403 without the secret |
| `8080` inside Docker | Temporal UI | the Docker network only (no host port) |
| `127.0.0.1:7234` | Temporal server gRPC (container port 7233) | this machine only; used by Django and the worker (`TEMPORAL_HOST`) |

---

## 2. Processing flow: from click to "Processed"

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant DJ as Django :8000
    participant DB as Database
    participant TS as Temporal server :7234
    participant W as temporal_worker

    alt Documents admin (staff)
        U->>DJ: Action → Temporal (POST <pk>/dispatch/)
    else Chat (any logged-in user)
        U->>DJ: "add this file to the knowledge base"
        DJ->>DB: create library Document from the chat file
    end
    DJ->>DB: status=queued, backend=temporal, progress 0
    DJ-->>U: 202 + temporal_url ("See workflow" shown at once, staff only)
    DJ->>TS: on commit: start DocumentProcessingWorkflow, id doc-<id>, queue document-processing
    TS->>W: run the 7 stages in order
    W->>DB: each stage updates stage, progress, status
    U->>DJ: status poll every 4 s
    DJ-->>U: progress bar, status, Retry / Stop buttons
```

### Step by step

1. **Start.**
   - **Admin:** a staff user picks **Action → Temporal**. `dispatch_view`
     resets the document and calls
     `enqueue_document_processing(pk, backend="temporal")`.
   - **Chat:** the `add_to_knowledge_base` tool (or the file panel) runs
     `file_commands.process_entries`. A chat file not yet in the library is
     copied into a new library `Document`, then `documents/controls.py`
     starts, resumes or reprocesses it, always with `backend="temporal"`.
2. **Queue.** `enqueue_document_processing` locks the row and sets
   `status=queued`, `processing_stage=queued`, `processing_progress=0`,
   `processing_backend=temporal`. The Temporal call is made only after the
   database transaction commits.
3. **Start the workflow.** `TemporalProcessingBackend.dispatch` connects to
   `TEMPORAL_HOST` and starts `DocumentProcessingWorkflow` with id
   `doc-<document id>` on task queue `document-processing`. The input is
   `{document_id, company_id, user_id, surface, backend}`.
4. **Answer the browser.** The admin gets `202` with `status_display`,
   `backend` and `temporal_url`, so the "See workflow" link appears straight
   away. Chat replies with the status and, for staff only, a workflow link.
5. **Run the stages.** `temporal_worker` runs the stages in this order:

   | # | Activity | Does |
   |---|---|---|
   | 1 | `stage_mark_processing` | sets `status=processing`, records the attempt (always runs) |
   | 2 | `stage_extract_text` | reads the file; OCR for scans and images (Tesseract, local) |
   | 3 | `stage_chunk_text` | splits the text into chunks |
   | 4 | `stage_delete_existing_chunks` | removes chunks from an earlier run |
   | 5 | `stage_embed_and_store_chunks` | embeds the chunks and stores them in the vector store |
   | 6 | `stage_mark_completed` | sets `status=ready` and the chunk count |
   | 7 | `stage_notify_completed` | final notification |

   Each activity has a 10-minute timeout and up to 3 attempts
   (`RetryPolicy(maximum_attempts=3)`). Every stage writes a `DocumentStage`
   row and updates the document's stage and progress.
6. **Follow progress.** The admin page polls the status endpoint every 4
   seconds (`status_poll.js`), updating the progress bar and status and
   showing Retry or Stop. Chat status replies read the same fields.
7. **Finish.** Success: status **Processed** at 100%. If a stage fails all 3
   attempts, the document is **Failed** and Retry appears.

### Stop and Retry (documents admin)

- **Stop** (`<pk>/temporal-cancel/`, queued or processing only). Terminates
  the workflow `doc-<id>` straight away and marks the document Failed with
  "Processing stopped by user." Completed stages and chunks are kept.
- **Retry** (`<pk>/temporal-retry/`, failed only). Starts a new run of
  `doc-<id>`. A stage that already has a completed `DocumentStage` row is
  skipped (except `stage_mark_processing`), so the run **resumes** at the
  stage that failed. A validation failure, such as a file that is too large,
  fails again unless the file or the limit changes.
- **Action → another backend.** The "See workflow" link is removed straight
  away, because the document is no longer on Temporal.

---

## 3. UI access flow: opening `/temporal/`

```mermaid
flowchart TD
    S(["Browser opens localhost:8000/temporal/…"]) --> L1{"Logged in to Django?"}
    L1 -->|"No"| L2["302 /admin/login/?next=/temporal/…<br/>Django password form"]
    L2 --> L1
    L1 -->|"Yes"| T1{"is_active and is_staff?"}
    T1 -->|"No"| T403["403 from Django"]
    T1 -->|"Yes"| T2["ui_proxy.py:<br/>drop Django cookies, Authorization<br/>and any X-Temporal-UI-Gate from the browser<br/>add X-Temporal-UI-Gate: secret"]
    T2 --> G1{"nginx gate 127.0.0.1:8080:<br/>secret header matches?"}
    G1 -->|"Yes"| G2["remove the header,<br/>forward to temporal-ui:8080<br/>(Docker network only)"]
    G2 --> TUI["Temporal UI, view only"]
    TUI --> R["response streamed back<br/>through nginx and Django"]

    X(["Anyone opening localhost:8080 directly"]) --> G1
    G1 -->|"No"| G403["403 from nginx"]
    T2 -.->|"gate or UI down"| E502["502 'Temporal UI is unavailable'"]
```

### Step by step (`infrastructure/temporal/ui_proxy.py`)

1. **Route.** `base/urls.py` sends every `/temporal/<path>` to
   `temporal_ui_proxy`.
2. **Login check.** Not logged in → redirect to `/admin/login/?next=…`, the
   normal Django admin login.
3. **Staff check.** Logged in but not `is_active and is_staff` → **403**.
   This is the check that decides who may see Temporal UI.
4. **Build the upstream URL.** `TEMPORAL_UI_URL` + `/temporal/<path>` +
   the query string, for example
   `http://localhost:8080/temporal/namespaces/default/workflows/doc-01M6…`.
5. **Clean the request.**
   - The `Cookie` header is rebuilt without Django's `sessionid`,
     `csrftoken` and `messages`, so Temporal UI never sees the Django session.
   - Not forwarded: `Authorization`, `Host`, `Content-Length`,
     `Accept-Encoding`, `Content-Encoding`, hop-by-hop headers, and any
     `X-Temporal-UI-Gate` the browser sent (a user cannot supply their own
     secret).
6. **Add the secret.** `X-Temporal-UI-Gate: <TEMPORAL_UI_PROXY_SECRET>`.
7. **Forward.** Same method and body, to the nginx gate; 5-second connect
   timeout, `TEMPORAL_UI_PROXY_TIMEOUT_SECONDS` (default 30) read timeout,
   redirects not followed.
8. **nginx gate.** Header missing or wrong → **403**. Correct → the header is
   removed and the request goes to `http://temporal-ui:8080` on the Docker
   network, with buffering off so pages stream.
9. **Temporal UI** answers. It runs under `/temporal`
   (`TEMPORAL_UI_PUBLIC_PATH`), so its paths match the proxied ones, and view
   only (`TEMPORAL_DISABLE_WRITE_ACTIONS=true`).
10. **Response.** Streamed back to the browser. A redirect pointing at the
    internal address is rewritten to stay on Django. Temporal's own
    `Set-Cookie` headers are passed through (never ones named like Django's).
11. **Errors.** Gate or UI unreachable → **502** "Temporal UI is unavailable".
    The rest of Django keeps working.

Only this view skips Django's CSRF check (`csrf_exempt`), because Temporal UI
checks CSRF itself.

### Why the nginx gate exists

Temporal UI has no login of its own, and anyone who can reach it sees every
workflow, including document, company and user IDs in the workflow inputs.
Django runs on the host, not in Docker, so it needs a host port to reach the
UI. Without the gate, any browser or program on this machine could open that
port and skip the Django login. So:

- `temporal-ui` has **no** `ports:` entry. Only containers on the Docker
  network can reach it.
- `temporal-ui-gate` (`nginx:1.27-alpine`) is the only thing published, on
  `127.0.0.1:8080`, so other machines cannot reach it either.
- At start-up, the nginx image fills `${TEMPORAL_UI_PROXY_SECRET}` into the
  config from the container environment, which Compose takes from `.env`.
  Only Django's proxy knows the secret, and it adds it only after the staff
  check.

---

## 4. Who can do what

| Django user | Start processing | See progress | Open Temporal UI |
|---|---|---|---|
| Not staff | from chat (always Temporal) | in chat | **No**: no links shown; `/temporal/…` gives 403 |
| `is_staff` | from chat or the documents admin (any backend) | chat and admin | Yes, view only |
| `is_staff` + `is_superuser` | same | same | Yes, view only |

- Give or remove access in **Admin → Users → Staff status**, or create a
  staff account with `python manage.py createsuperuser`. The proxy checks
  `is_staff` on every request, so a change applies on the next page load.
- A non-staff chat user sees the workflow id as text in status replies
  (`workflow: doc-…`). Knowing it gives no access.

### Where the Temporal links appear

| Place | Source | Shown to |
|---|---|---|
| Admin header, "Temporal workflows" | `base/templates/unfold/helpers/userlinks.html` | active staff |
| Documents admin, "See workflow" | `documents/admin.py → temporal_link` on page load; `schedule_dropdown.js` right after dispatch; `status_poll.js` as a fallback | staff (the admin is staff only) |
| Chat replies and file panel, "Workflow" | `chat/services/file_commands.py`, `session_files.py`; drawn by `chat/templates/chat/chat_page.html` | active staff (`data-can-view-temporal`) |

Every link is built by `chat/services/kb.py → temporal_ui_url` as
`/temporal/namespaces/default/workflows/doc-<document id>`. Hiding a link is
cosmetic. The staff check and the nginx gate are what block access.

---

## 5. Run it locally

```bash
# Temporal server + Postgres, Temporal UI, nginx gate (and Kafka)
docker compose -f docker-compose.processing.yml up -d

# Worker that runs the workflow stages
python manage.py temporal_worker

# Django
python manage.py runserver
```

| Open | Result |
|---|---|
| http://localhost:8000/admin/ | Django admin |
| **http://localhost:8000/temporal/** | Temporal UI (staff only) |
| http://localhost:8080/temporal/ | **403** from the gate, always, even when logged in |

After changing `.env`, fully restart `runserver` and the worker. The
auto-reloader does not pick up `.env` changes.

### Settings (`.env`)

| Variable | Local value | Meaning |
|---|---|---|
| `TEMPORAL_HOST` | `localhost:7234` | Temporal server gRPC, used by Django and the worker |
| `TEMPORAL_UI_URL` | `http://localhost:8080` | the nginx gate; used only by the proxy |
| `TEMPORAL_UI_PROXY_SECRET` | *(random)* | shared by the proxy and the gate container; without it every Temporal page is 403. Never commit it. |
| `TEMPORAL_UI_PROXY_TIMEOUT_SECONDS` | `30` (default) | how long the proxy waits for Temporal UI |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | `False` | default `True`; `False` only for plain-http local development |
| `USE_X_FORWARDED_PROTO` | unset | `True` only behind an HTTPS proxy that sets this header |

After changing `TEMPORAL_UI_PROXY_SECRET`, run
`docker compose -f docker-compose.processing.yml up -d` (so the gate gets it)
and restart `runserver`.

---

## 6. Checks

| # | Do | Expected |
|---|---|---|
| 1 | Open `/temporal/` in a private window | Django login, then Temporal UI |
| 2 | Log in as a user without staff status, open `/temporal/` | 403 |
| 3 | Log in as staff, open `/temporal/` | workflows listed |
| 4 | Admin: **Action → Temporal** on a document | "See workflow" appears straight away; progress moves; Processed at the end |
| 5 | Press **Stop** while it runs | Failed, "Processing stopped by user."; workflow terminated |
| 6 | Press **Retry** | resumes at the stage that failed |
| 7 | `curl -i http://localhost:8080/temporal/` | 403 from nginx |
| 8 | From another machine: `curl http://<server-ip>:8080/temporal/` | connection refused |
| 9 | Stop the `temporal-ui` container, open `/temporal/` | 502; Django still works |

---

## 7. Troubleshooting

| Problem | Cause / fix |
|---|---|
| Stays **Queued** forever | `temporal_worker` is not running, or `TEMPORAL_HOST` is wrong. Start the worker. |
| `/temporal/` shows a blank white page | Temporal UI older than 2.27 breaks under `/temporal` (its CSP blocks its own startup script). Use `temporalio/ui:2.34.0` or newer; hard-refresh. |
| `localhost:8080` shows 403 | Expected. Use `localhost:8000/temporal/`. |
| `localhost:8000/temporal/` gives 403 for a staff user | `TEMPORAL_UI_PROXY_SECRET` missing or different between `.env` and the gate. Restart `runserver` and run `docker compose -f docker-compose.processing.yml up -d`. |
| "See workflow" does not appear after dispatch | Hard-refresh (Ctrl+Shift+R) so the browser loads the current `schedule_dropdown.js`. |
| Login does nothing / admin forms fail on http | `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` are `True` on plain http. Set them to `False` locally. |
| 405 in the browser console on a workflow page | Normal: write actions are disabled, so Temporal UI refuses its metadata query. |
| 502 "Temporal UI is unavailable" | `docker compose -f docker-compose.processing.yml up -d temporal-ui temporal-ui-gate` |

---

## 8. Production notes

- Never publish 8080 or 7234 publicly, and never point a public nginx route
  at 8080; the public route is Django's `/temporal/`. Docker-published ports
  bypass `ufw`, so keep the `127.0.0.1:` prefix.
- Use a fresh `TEMPORAL_UI_PROXY_SECRET` per environment. If Django runs
  inside the same Docker network, drop the gate's host port and set
  `TEMPORAL_UI_URL=http://temporal-ui-gate:8080`.
- Serve the site over HTTPS only. Keep the secure-cookie settings at `True`,
  and set `USE_X_FORWARDED_PROTO=True` behind a proxy.
- Kafka's `9093` is still public in `docker-compose.processing.yml`; bind it to
  `127.0.0.1` as well.
- Review who has staff status regularly.
