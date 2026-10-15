# Schedule → in-row inline dropdown

Changed the Documents admin "Schedule" backend chooser from a **separate full
page** into an **in-row inline dropdown** on the changelist. No backend, model,
migration, or dispatcher logic was touched — this is a changelist-UI + AJAX
change only.

## Approach

### Column + button + dropdown
- Removed `schedule` from `actions_row` (kept `show_chunks`).
- Added a `schedule_column` `list_display` method that renders, per row:
  - an **"Action"** button (`.schedule-toggle`, purple Unfold/Tailwind
    `bg-primary-600` with a `material-symbols-outlined` `expand_more` icon), and
  - a **hidden dropdown** (`.schedule-menu`, `hidden` by default) holding the
    four items — **Process now / Celery / Kafka / Temporal** — each a `<button
    role="menuitem">` carrying `data-doc-id` and `data-backend`.
  - Non-library rows (which cannot be processed) render a `—` placeholder.
  - Markup is built with `format_html` / `format_html_join` — only server-
    controlled values (pk, backend keys, static labels) are interpolated.

### Vanilla JS (`documents/static/documents/js/schedule_dropdown.js`)
- Toggles one dropdown open at a time; clicking elsewhere closes it;
  `aria-expanded` kept in sync.
- On an item click, POSTs `{backend}` to the AJAX dispatch endpoint with the
  CSRF token from the `csrftoken` cookie — **no navigation, no full reload**.
- Updates that row's status cell (`td.field-status_badge`) from the JSON
  response using `textContent`/`createElement` (never `innerHTML`).
- Wired via the changelist override `change_list.html`, which passes the
  reverse()d dispatch URL as a `__PK__` placeholder template through a data
  attribute (no URL built from server text). The existing `status_poll.js`
  still runs and shows the completion toast on the terminal transition.

### AJAX dispatch endpoint
- `admin:documents_document_dispatch` at `<pk>/dispatch/`, registered in
  `get_urls()` wrapped in `self.admin_site.admin_view` (CSRF-protected).
- POST-only (GET → 405); resolves the document from
  `self.get_queryset(request)` (out-of-scope → 404); rejects non-library docs
  (400) and invalid backend (400).
- `"now"` → `dispatch_document_processing(..., backend=None)` (default/current
  backend = DEBUG celery inline / synchronous "Process now"); `celery` / `kafka`
  / `temporal` set `processing_backend` and dispatch via that backend — all
  through the **existing** `dispatch_document_processing` seam.
- Returns JSON `{status, status_display, backend, document_id, title}`.

### Retired the standalone page
- Deleted `templates/admin/documents/document/schedule.html` and the `schedule`
  row-action; `admin:documents_document_schedule` no longer resolves. No row
  navigates to a dead `/schedule/` page.

## Verification

- `manage.py check` — clean (0 issues).
- Key-free `check` (`env -u OPENAI_API_KEY`) — clean.
- `makemigrations --check --dry-run` — **No changes detected** (admin/template/
  url/JS only, as expected).
- Targeted `documents.tests_phase4.ScheduleAdminTests` — **8/8 OK**. Rewrote the
  class for the new behavior:
  - changelist GET 200 with Unfold sidebar; asserts the `schedule-toggle` /
    `schedule-menu` / "Action" button + the four items (Process now / Celery /
    Kafka / Temporal) present **inline** with `data-doc-id`/`data-backend`, the
    `__PK__` dispatch template present, and **no** `/schedule/` navigation link;
  - old `/schedule/` page retired (`NoReverseMatch`);
  - dispatch POST: `now` → `backend=None`; `celery`/`kafka`/`temporal` set
    `processing_backend` and dispatch with the expected backend (dispatcher
    patched — no broker); invalid → 400; GET → 405; CSRF enforced → 403;
    out-of-scope pk → 404 for a non-superuser owner.
- Documents app suite — 66 tests OK (1 skipped: tesseract OCR absent).
- **Full suite** (`manage.py test --keepdb`) — **152 tests OK (1 skipped)**.
  This was the single final pre-merge full-suite run.

### Live smoke — SKIPPED
Docker is usable without sudo, but a live "Process now" run embeds via the
OpenAI key and would mutate the shared **dev** Postgres database (not the test
DB) with real document/chunk rows. Since the inline/QUEUED dispatch path is
already covered by unit tests (DEBUG celery → inline executor, QUEUED
transition, backend routing), the live run was skipped to avoid mutating shared
state and depending on a real key.

## Human glance needed
Pixel-level dropdown look (exact purple shade, menu offset/shadow, icon
rendering) needs a human glance in the browser. Structural assertions used:
presence of `.schedule-toggle` / `.schedule-menu` / `material-symbols-outlined`,
the four `data-backend` values, inline placement in the row, and no
navigation link to the retired page.
