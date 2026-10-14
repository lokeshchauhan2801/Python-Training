# agentic-flow

A small, self-contained **Django 5.2** learning application. It is **PostgreSQL-only**,
driven almost entirely through the **Django admin** (via [django-unfold](https://unfoldadmin.com/))
with a thin **Django REST Framework** API behind it. The project demonstrates clean SOLID
boundaries: a reusable `basics` kernel (soft-delete managers, an audit base model, a
`contextvars` actor middleware, a ULID utility, and a lazy, injectable LLM adapter) plus four
domain apps (`accounts`, `company`, `chat`, `scrapper`).

Deferred by design (no apps, models, or dependencies yet): LangGraph, Langfuse, vector
embeddings / Chroma, document upload, Celery, Kafka, Temporal.

---

## Install & run

Prerequisites: Python 3.10+, a reachable PostgreSQL, and a database named `agentic-db`
owned by the app user.

```bash
cd /home/auriga/learnings/agentic-flow

# 1. Virtualenv + dependencies (single, pinned requirements.txt)
python -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Configure the single .env (already present in this repo). It holds
#    DJANGO_SECRET_KEY, DEBUG, the POSTGRES_* connection, and the LLM_* settings.
#    There is intentionally NO .env.example / sample / template file.

# 3. Create the database once (run by a Postgres superuser):
#    CREATE DATABASE "agentic-db" OWNER aiplatform;

# 4. Migrate and create an admin user
venv/bin/python manage.py migrate
venv/bin/python manage.py createsuperuser   # prompts for email, then username

# 5. Run the dev server; the admin is the primary UI
venv/bin/python manage.py runserver
# -> http://127.0.0.1:8000/admin/
```

The app imports and runs `check` / `migrate` with **no LLM key present** — the `LLMClient`
is lazy and never builds a backend at import time.

---

## Why exactly five models

The project keeps a deliberately small surface: **five** concrete models, each with one clear
reason to exist. Everything else is an abstract base or a service.

| Model | App | Role / rationale |
|-------|-----|------------------|
| `User` | `accounts` | Email-login user (`AbstractUser` + UUID pk). Re-declares the audit/soft-delete columns explicitly because it cannot inherit the abstract audit base. |
| `Company` | `company` | The **tenant root**. Scoping (`company.owner == request.user`) is the single authorization axis for the whole API and admin. Has no company FK of its own. |
| `ConversationSession` | `chat` | A chat session grouping turns, keyed by a sortable **ULID** `session_id`. Demonstrates the admin learning goals. |
| `Conversation` | `chat` | One question/response turn. Owns the cross-table invariant `company == session.company`, enforced in both `clean()` and `save()`. |
| `ScrapeJob` | `scrapper` | A single URL fetch + result, run through the **SSRF-safe** `ScrapeService`. |

`Company` and `User` build on the company-free `AuditModel`; `ConversationSession`,
`Conversation`, and `ScrapeJob` build on `BaseModel` (audit + a required `company` FK). No
message/version/media child tables, and no models for the deferred tech.

---

## Admin learning map

`chat.ConversationSession`'s admin is the teaching centrepiece. It demonstrates five Unfold /
Django-admin extension goals, each wired to a concrete feature:

| Goal | Where it shows up |
|------|-------------------|
| 1. Override admin templates per app | App-local `change_list.html` / `change_form.html` under `chat/templates/admin/chat/conversationsession/` that extend the Unfold templates and add buttons. |
| 2. Custom admin URLs + AJAX | `get_urls()` adds `detail-json/` (GET → JSON), `summarize/` (POST, CSRF-validated), and `learning/`, all wrapped in `admin_site.admin_view` and resolving objects from the permission-scoped queryset only. |
| 3. A custom admin page | `learning_view` returns a `TemplateResponse` rendered with the admin context (`learning_view.html`). |
| 4. Rich changelist/change-form config | `list_display` / `list_filter` / `search_fields` / `date_hierarchy`, a `ConversationInline`, and display methods (turn count, short summary). |
| 5. Row/detail action buttons + bulk actions | The overridden templates render per-session **Detail JSON** / **Summarize** buttons that call the AJAX endpoints without a full page refresh (rendered with `textContent`/`createElement`, never `innerHTML`); bulk **soft-delete** / **restore** come from `BaseModelAdmin`. |

`scrapper.ScrapeJob`'s admin adds a **CSRF-protected `run/` confirmation page** that invokes
the SSRF-safe scrape service.

> Note on template layout: templates live **app-locally** under each app's
> `templates/admin/<app>/<model>/` with `APP_DIRS=True` and no central `DIRS`. This
> deliberately diverges from the sibling `django-unfold` project's central `templates/` tree.

---

## API endpoints (`api/v1/`)

DRF, mounted under `api/v1/<app>/`. Auth is **SessionAuthentication + BasicAuthentication**
with `IsAuthenticated` (JWT is deferred). Every queryset and every submitted relation is
scoped to the request user by company ownership (superusers see all); server-controlled
fields are read-only.

| Method | Path | Behavior |
|--------|------|----------|
| GET | `api/v1/auth/user/me/` | The authenticated user (password never exposed). |
| GET | `api/v1/company/company/` | List companies owned by the user (superuser: all). |
| POST | `api/v1/company/company/` | Create a company; `owner` / `created_by` forced to the request user. |
| GET | `api/v1/chat/conversation-session/` | List the user's sessions (scoped). |
| POST | `api/v1/chat/conversation-session/` | Create a session; `user`/audit forced; `company` resolved by the required-company rule; `session_id` auto. |
| POST | `api/v1/chat/conversation/ask/` | Body `{session, question}`; resolves the session by company ownership (**404** if out of scope); runs `ChatService.ask`; returns the created turn. |
| GET | `api/v1/scrapper/scrape-job/` | List the user's scrape jobs (scoped). |
| POST | `api/v1/scrapper/scrape-job/` | Create a job (`url` + optional `title`); audit forced; `company` per the required-company rule; status defaults to `pending`. |
| POST | `api/v1/scrapper/scrape-job/<uuid>/run/` | Run the SSRF-safe scrape; returns the job with its resulting status (expected failures surface as an `error` job, HTTP 200). |

**Required-`company` resolution** on create (`ConversationSession`, `ScrapeJob`): a supplied
`company` must be one the user owns (else **400**); if omitted it defaults to the user's single
owned company; owning **zero** or **more than one** company without specifying is a **400**.

---

## LLM provider: OpenAI vs DeepSeek (pure env swap)

The LLM is reached through a single provider-agnostic `LLMClient` wrapping an
OpenAI-compatible chat client. Switching providers is **config only** — no code change — by
editing `.env`:

```ini
# OpenAI (default)
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_BASE_URL=
# the key is read from LLM_API_KEY, else OPENAI_API_KEY, else AI_API_KEY
LLM_API_KEY=sk-...
```

```ini
# DeepSeek (OpenAI-compatible endpoint)
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-...
```

Because the backend is only constructed on the first `complete()` call, the app runs
`check` / `migrate` and the whole test suite with **no key set** (tests inject a fake client).

---

## Verification

```bash
cd /home/auriga/learnings/agentic-flow

# No-key gate: check + migrate succeed with every LLM key unset
env -u OPENAI_API_KEY -u AI_API_KEY -u LLM_API_KEY venv/bin/python manage.py check
env -u OPENAI_API_KEY -u AI_API_KEY -u LLM_API_KEY venv/bin/python manage.py migrate

# Targeted tests (reuse the test DB with --keepdb)
venv/bin/python manage.py test basics auth company --keepdb
venv/bin/python manage.py test chat scrapper --keepdb

# Full suite (run once before hand-off)
venv/bin/python manage.py test --keepdb

# DB reachable
PGPASSWORD=aiplatform123 psql -h localhost -p 5432 -U aiplatform -d "agentic-db" -tc "SELECT 1"
```
