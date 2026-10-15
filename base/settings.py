"""
Django settings for the agentic-flow project.

Single settings module (no local/prod split). PostgreSQL only. Admin-driven via
django-unfold with a thin DRF API layer. The project is import-safe with no LLM
key present (LLMClient is lazy and never built at import time).
"""

import os
from pathlib import Path

from django.urls import reverse_lazy
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load the single .env file from the project root BEFORE reading any env vars.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-change-me")
DEBUG = os.getenv("DEBUG", "False").lower() in ("1", "true", "yes", "on")

ALLOWED_HOSTS = [h for h in os.getenv("ALLOWED_HOSTS", "*").split(",") if h]

# Application definition.
# Unfold and the contrib apps it needs must come BEFORE django.contrib.admin.
INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "basics",
    "auth",  # label: accounts
    "company",
    "chat",
    "scrapper",
    "documents",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Capture the acting user into a contextvar AFTER AuthenticationMiddleware
    # so request.user is populated.
    "basics.middleware.CurrentUserMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "base.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # App-local templates only (APP_DIRS=True, NO central DIRS). This
        # deliberately diverges from the sibling django-unfold project, which
        # uses a central templates/ tree.
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "base.wsgi.application"
ASGI_APPLICATION = "base.asgi.application"

# Database — PostgreSQL only. NAME is forced to agentic-db (default). An
# optional DATABASE_URL may override the discrete settings. NO SQLite anywhere.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "agentic-db"),
        "USER": os.getenv("POSTGRES_USER", "aiplatform"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "aiplatform123"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

_database_url = os.getenv("DATABASE_URL")
if _database_url:
    from urllib.parse import unquote, urlparse

    _parsed = urlparse(_database_url)
    DATABASES["default"].update(
        {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _parsed.path.lstrip("/") or DATABASES["default"]["NAME"],
            "USER": unquote(_parsed.username) if _parsed.username else DATABASES["default"]["USER"],
            "PASSWORD": unquote(_parsed.password) if _parsed.password else DATABASES["default"]["PASSWORD"],
            "HOST": _parsed.hostname or DATABASES["default"]["HOST"],
            "PORT": str(_parsed.port) if _parsed.port else DATABASES["default"]["PORT"],
        }
    )

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# LLM configuration. The key may be absent entirely; the app must import and run
# check/migrate with no key, so nothing here constructs a backend client.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")  # "" -> default OpenAI endpoint
LLM_API_KEY = (
    os.getenv("LLM_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or os.getenv("AI_API_KEY")
    or ""
)

# Vector store (ChromaDB) + embedding configuration. These are plain values
# only; no Chroma or embedding client is constructed at import. ChromaVectorStore
# (basics/vectorstore.py) and LLMClient read these lazily on first use. When
# CHROMA_HOST is set the store talks to a remote Chroma server, otherwise it uses
# a local PersistentClient rooted at CHROMA_PATH (gitignored chroma_data/).
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", BASE_DIR / "chroma_data"))
CHROMA_HOST = os.getenv("CHROMA_HOST", "")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# Document-processing backends (Phase 4). All values are plain strings read from
# the environment; NOTHING here opens a broker/server connection at import. The
# selected backend is resolved lazily when a document is dispatched. The DEBUG
# "celery" path runs inline on a background thread so no Redis/worker is needed
# in local development.
#   celery   — Celery/Redis (inline ThreadPool in DEBUG, apply_async otherwise)
#   kafka    — publish an event consumed by the kafka_worker
#   temporal — start a Temporal workflow run by the temporal_worker
DOCUMENT_PROCESSING_BACKEND = os.getenv("DOCUMENT_PROCESSING_BACKEND", "celery")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
TEMPORAL_UI_URL = os.getenv("TEMPORAL_UI_URL", "http://localhost:8080")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# django-unfold configuration. Branding (SITE_TITLE/SITE_HEADER/SITE_SYMBOL),
# sidebar search, and a grouped, collapsible, icon'd navigation give the admin
# the full Unfold look instead of the plain Django-admin fallback. Every link
# uses reverse_lazy and points ONLY at routes/models that exist in this project,
# so importing settings never resolves URLs (import-safe check/migrate) and the
# admin never 500s on a missing reverse. Material Symbols names supply the icons.
UNFOLD = {
    "SITE_TITLE": "Agentic Demo",
    "SITE_HEADER": "Agentic Demo",
    "SITE_URL": "/",
    "SITE_SYMBOL": "dashboard",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "ENVIRONMENT": os.getenv("DJANGO_ENVIRONMENT", "Development"),
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Dashboard",
                "collapsible": True,
                "items": [
                    {
                        "title": "Home",
                        "icon": "home",
                        "link": reverse_lazy("admin:index"),
                    },
                    {
                        "title": "Chat",
                        "icon": "chat",
                        "link": reverse_lazy("admin:chat_conversation_sessions"),
                    },
                    {
                        "title": "New chat",
                        "icon": "add_comment",
                        "link": reverse_lazy("admin:chat_conversation_new"),
                    },
                ],
            },
            {
                "title": "Company",
                "collapsible": True,
                "items": [
                    {
                        "title": "Companies",
                        "icon": "business",
                        "link": reverse_lazy("admin:company_company_changelist"),
                    },
                ],
            },
            {
                "title": "Auth",
                "collapsible": True,
                "items": [
                    {
                        "title": "Users",
                        "icon": "person",
                        "link": reverse_lazy("admin:accounts_user_changelist"),
                    },
                ],
            },
            {
                "title": "Scrapper",
                "collapsible": True,
                "items": [
                    {
                        "title": "Documents",
                        "icon": "description",
                        "link": reverse_lazy("admin:documents_document_changelist"),
                    },
                    {
                        "title": "Document chunks",
                        "icon": "auto_stories",
                        "link": reverse_lazy("admin:documents_documentchunk_changelist"),
                    },
                    {
                        "title": "Queue messages",
                        "icon": "queue",
                        "link": "#",  # placeholder — model lands in Phase 3/4
                    },
                ],
            },
            {
                "title": "Agentic",
                "collapsible": True,
                "items": [
                    {
                        "title": "LangGraph",
                        "icon": "account_tree",
                        "link": "#",  # placeholder — wired when LangGraph is added
                    },
                ],
            },
            {
                "title": "Chat",
                "collapsible": True,
                "items": [
                    {
                        "title": "Conversation sessions",
                        "icon": "forum",
                        "link": reverse_lazy("admin:chat_conversationsession_changelist"),
                    },
                    {
                        "title": "Conversations",
                        "icon": "chat_bubble",
                        "link": reverse_lazy("admin:chat_conversation_changelist"),
                    },
                    {
                        "title": "Tickets",
                        "icon": "confirmation_number",
                        "link": "#",  # placeholder — model lands later
                    },
                ],
            },
        ],
    },
}
