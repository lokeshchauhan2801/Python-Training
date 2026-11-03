import os
from pathlib import Path

from django.urls import reverse_lazy
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

def _env_flag(name, default):
    """Read a boolean env var ("1", "true", "yes", "on" mean True)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-change-me")
DEBUG = os.getenv("DEBUG", "False").lower() in ("1", "true", "yes", "on")

ALLOWED_HOSTS = [h for h in os.getenv("ALLOWED_HOSTS", "*").split(",") if h]

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
    "mozilla_django_oidc",
    "basics",
    "auth",
    "company",
    "chat",
    "scrapper",
    "documents",
    "agents",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",


    "basics.middleware.CurrentUserMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# --- Keycloak (OIDC) login -------------------------------------------------
# Off by default: the normal Django admin login works exactly as before. When
# on, /admin/login/ hands off to Keycloak. Users are matched ONLY by the
# Keycloak ``sub`` (auth.OIDCIdentity), never by email; roles are synced from
# the ``groups`` claim on every login (see auth/oidc.py).
KEYCLOAK_LOGIN_ENABLED = _env_flag("KEYCLOAK_LOGIN_ENABLED", False)
# Separate local-password login at /admin/local-login/ for when Keycloak is
# down. Only for accounts NOT linked to Keycloak. Keep off in normal operation.
BREAK_GLASS_LOGIN_ENABLED = _env_flag("BREAK_GLASS_LOGIN_ENABLED", False)

KEYCLOAK_REALM_URL = os.getenv(
    "KEYCLOAK_REALM_URL", "http://localhost:8180/realms/agentic-flow"
).rstrip("/")
KEYCLOAK_ADMIN_GROUP = "/django-admins"
KEYCLOAK_SUPERUSER_GROUP = "/django-superusers"

OIDC_RP_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "django-app")
OIDC_RP_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
if KEYCLOAK_LOGIN_ENABLED and not OIDC_RP_CLIENT_SECRET:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "KEYCLOAK_LOGIN_ENABLED is on but KEYCLOAK_CLIENT_SECRET is not set."
    )
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_RP_SCOPES = "openid email profile"
OIDC_OP_AUTHORIZATION_ENDPOINT = f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/auth"
OIDC_OP_TOKEN_ENDPOINT = f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/token"
OIDC_OP_USER_ENDPOINT = f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/userinfo"
OIDC_OP_JWKS_ENDPOINT = f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/certs"
OIDC_OP_LOGOUT_ENDPOINT = f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/logout"
OIDC_OP_LOGOUT_URL_METHOD = "auth.oidc.provider_logout"
# The id_token is kept (DB session) only as the logout hint for Keycloak.
OIDC_STORE_ID_TOKEN = True
# SessionRefresh re-checks the user with Keycloak every 15 minutes, so a user
# removed from a group or disabled in Keycloak loses access within 15 minutes.
OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS = 15 * 60
OIDC_TIMEOUT = 10

AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
if KEYCLOAK_LOGIN_ENABLED:
    AUTHENTICATION_BACKENDS.insert(0, "auth.oidc.KeycloakOIDCBackend")
    MIDDLEWARE.append("mozilla_django_oidc.middleware.SessionRefresh")
    LOGIN_URL = "admin:login"
    LOGIN_REDIRECT_URL = "/admin/"
    LOGOUT_REDIRECT_URL = "/admin/"
# Must NOT be /admin/login/: with Keycloak on that redirects straight back to
# Keycloak, which would loop on a refused login.
LOGIN_REDIRECT_URL_FAILURE = "/auth/login-failed/"

# --- Session / cookie security ----------------------------------------------
# Secure cookies are on by default; a plain-http local setup turns them off in
# .env (SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False).
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", True)
CSRF_COOKIE_SECURE = _env_flag("CSRF_COOKIE_SECURE", True)
if _env_flag("USE_X_FORWARDED_PROTO", False):
    # Only behind a proxy that sets this header itself.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        # Login, role-change and break-glass audit lines (never tokens).
        "auth": {"handlers": ["console"], "level": "INFO"},
        "infrastructure.temporal.ui_proxy": {"handlers": ["console"], "level": "INFO"},
        "mozilla_django_oidc": {"handlers": ["console"], "level": "WARNING"},
    },
}

X_FRAME_OPTIONS = "SAMEORIGIN"

ROOT_URLCONF = "base.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",


        "DIRS": [BASE_DIR / "base" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",

                "chat.context_processors.admin_chat_history",


                "company.context_processors.active_company",
            ],
        },
    },
]

WSGI_APPLICATION = "base.wsgi.application"
ASGI_APPLICATION = "base.asgi.application"

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

# Password validators disabled: the add-user form no longer shows the four
# password rule help-text lines.
AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = (
    os.getenv("LLM_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or os.getenv("AI_API_KEY")
    or ""
)

# Per-request timeout (seconds) and retry budget for every chat/embedding call.
# Defaults match the openai SDK (600s, 2 retries): a non-streamed call waits
# for the whole generation, so long answers must not be cut off. Connecting is
# bounded separately so an unreachable endpoint fails fast.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "600"))
LLM_CONNECT_TIMEOUT_SECONDS = float(
    os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "10")
)
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
# with_structured_output method used for EVERY provider/model. "function_calling"
# (tool calls) works on both OpenAI and DeepSeek; DeepSeek rejects "json_schema".
LLM_STRUCTURED_METHOD = os.getenv("LLM_STRUCTURED_METHOD", "function_calling")

# Chat history sent to the model: the last CHAT_RECENT_TURNS turns verbatim
# (older turns are condensed into a digest), and only the last
# CHAT_ROUTER_TURNS turns for the Agent router's tool pick.
CHAT_RECENT_TURNS = int(os.getenv("CHAT_RECENT_TURNS", "12"))
CHAT_ROUTER_TURNS = int(os.getenv("CHAT_ROUTER_TURNS", "4"))
# Show the response-time line ("⏱ 3.2s · ...") under each assistant reply.
# Timing is always recorded in turn metadata; this only hides the line.
CHAT_SHOW_TIMING = os.getenv("CHAT_SHOW_TIMING", "True").strip().lower() in (
    "1", "true", "yes", "on",
)

def _csv(env_value, default_list):
    """Parse a comma-separated env list, falling back to default_list."""
    raw = (env_value or "").strip()
    if not raw:
        return list(default_list)
    return [item.strip() for item in raw.split(",") if item.strip()]


LLM_PROVIDERS = {
    "openai": {
        "chat_model": os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini"),


        "models": _csv(os.getenv("OPENAI_MODELS"), ["gpt-4o-mini", "gpt-4o"]),
        "embedding_model": os.getenv(
            "OPENAI_EMBEDDING_MODEL", os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        ),
        "base_url": os.getenv("OPENAI_BASE_URL", ""),
        "api_key": (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("LLM_API_KEY")
            or os.getenv("AI_API_KEY")
            or ""
        ),
        "embedding_base_url": os.getenv("OPENAI_EMBEDDING_BASE_URL", ""),
        "embedding_api_key": os.getenv("OPENAI_EMBEDDING_API_KEY", ""),
    },
    "deepseek": {
        "chat_model": os.getenv("DEEPSEEK_LLM_MODEL", "deepseek-chat"),
        "models": _csv(os.getenv("DEEPSEEK_MODELS"), ["deepseek-chat", "deepseek-reasoner"]),


        "embedding_model": os.getenv(
            "DEEPSEEK_EMBEDDING_MODEL", os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        ),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),


        "embedding_base_url": os.getenv("DEEPSEEK_EMBEDDING_BASE_URL", ""),
        "embedding_api_key": (
            os.getenv("DEEPSEEK_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        ),
    },
}

LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_BASE_URL = os.getenv("LANGFUSE_BASE_URL", "")

CHROMA_PATH = Path(os.getenv("CHROMA_PATH", BASE_DIR / "chroma_data"))
CHROMA_HOST = os.getenv("CHROMA_HOST", "")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

DOCUMENT_PROCESSING_BACKEND = os.getenv("DOCUMENT_PROCESSING_BACKEND", "celery")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
# Temporal Web UI is never opened directly: Django proxies /temporal/ to it for
# active staff only (infrastructure/temporal/ui_proxy.py). TEMPORAL_UI_URL is
# the INTERNAL upstream (bound to 127.0.0.1); the UI runs with
# TEMPORAL_UI_PUBLIC_PATH=/temporal so its paths match the proxied ones.
TEMPORAL_UI_URL = os.getenv("TEMPORAL_UI_URL", "http://localhost:8080").rstrip("/")
TEMPORAL_UI_PUBLIC_PATH = "/temporal"
# Shared with the temporal-ui-gate container, which refuses any request
# without it, so :8080 cannot be opened directly. Never commit it.
TEMPORAL_UI_PROXY_SECRET = os.getenv("TEMPORAL_UI_PROXY_SECRET", "")
TEMPORAL_UI_PROXY_CONNECT_TIMEOUT_SECONDS = 5
TEMPORAL_UI_PROXY_TIMEOUT_SECONDS = float(
    os.getenv("TEMPORAL_UI_PROXY_TIMEOUT_SECONDS", "30")
)

# Scanned PDFs are rendered one page at a time at 300 DPI. These limits bound
# OCR memory/CPU while preserving recognition quality for normal documents.
DOCUMENT_MAX_PDF_BYTES = 25 * 1024 * 1024
DOCUMENT_MAX_PDF_PAGES = 100
DOCUMENT_OCR_PDF_DPI = 300
DOCUMENT_OCR_BATCH_PAGES = 1
DOCUMENT_OCR_RENDER_TIMEOUT_SECONDS = 120
DOCUMENT_OCR_PAGE_TIMEOUT_SECONDS = 120
# A page whose plain OCR yields fewer characters than this is retried on an
# adaptive black/white copy (phone photos of paper); the longer text wins.
DOCUMENT_OCR_RETRY_BELOW_CHARS = int(os.getenv("DOCUMENT_OCR_RETRY_BELOW_CHARS", "300"))
DOCUMENT_OCR_CLEANUP_BLOCK = 31
DOCUMENT_OCR_CLEANUP_OFFSET = 12

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

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


                "title": "Chat",
                "collapsible": True,
                "items": [
                    {
                        "title": "Chat",
                        "icon": "chat",
                        "link": reverse_lazy("chat:home"),
                    },
                    {
                        "title": "Conversation sessions",
                        "icon": "forum",
                        "link": reverse_lazy("admin:chat_conversationsession_changelist"),
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
                        "title": "Queue messages",
                        "icon": "queue",
                        "link": reverse_lazy(
                            "admin:documents_document_queue"
                        ),
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
                        "link": reverse_lazy("agents:agent_builder"),
                    },
                ],
            },
        ],
    },
}
