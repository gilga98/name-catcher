"""All deploy-specific configuration comes from the environment (12-factor)."""

from pathlib import Path

import environ
from csp.constants import NONE, SELF, UNSAFE_INLINE

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env()
if (BASE_DIR / ".env").exists():
    env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = [*env.list("ALLOWED_HOSTS", default=[]), "localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django_htmx",
    "names",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "csp.middleware.CSPMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django_ratelimit.middleware.RatelimitMiddleware",
    "names.middleware.AIErrorMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]

DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
CACHES = {"default": env.cache("CACHE_URL", default="dbcache://app_cache")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

LANGUAGE_CODE = "en"
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}

# TLS terminates at Coolify's proxy, which also handles the HTTP -> HTTPS redirect.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SECURE_REFERRER_POLICY = "same-origin"  # Share links are secrets: never leak them in Referer.

CDN = "https://cdn.jsdelivr.net"
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": [SELF],
        "script-src": [SELF, CDN],
        "style-src": [SELF, UNSAFE_INLINE, CDN],  # 0build and htmx set inline styles.
        "font-src": [SELF, CDN],
        "img-src": [SELF, "data:"],
        "connect-src": [SELF],
        "frame-ancestors": [NONE],
        "base-uri": [SELF],
        "form-action": [SELF],
    }
}

AI_BACKEND = env("AI_BACKEND", default="names.ai.gemini.GeminiAI")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_MODEL = env("GEMINI_MODEL", default="gemini-flash-latest")
GEMINI_TTS_MODEL = env("GEMINI_TTS_MODEL", default="gemini-2.5-flash-preview-tts")
GEMINI_TTS_VOICE = env("GEMINI_TTS_VOICE", default="Kore")
GEMINI_TIMEOUT_MS = env.int("GEMINI_TIMEOUT_MS", default=45000)

AI_RATE_PER_BOARD = env("AI_RATE_PER_BOARD", default="30/h")
AI_RATE_GLOBAL = env("AI_RATE_GLOBAL", default="200/h")
BOARD_CREATE_RATE = env("BOARD_CREATE_RATE", default="60/h")
RATELIMIT_VIEW = "names.views.rate_limited"
# The database cache is shared across workers; its small increment race is fine for a budget cap.
SILENCED_SYSTEM_CHECKS = ["django_ratelimit.E003", "django_ratelimit.W001"]
