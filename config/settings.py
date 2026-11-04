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
# Never localhost in production: a request sent straight to the server must not pass as this site.
# Health checks don't need it, because config.health answers them before any host check.
ALLOWED_HOSTS = [*env.list("ALLOWED_HOSTS", default=[]), *(["localhost", "127.0.0.1"] if DEBUG else [])]
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "names",  # First, so its templates restyle allauth's pages.
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.mfa",
    "django_htmx",
]
MIDDLEWARE = [
    "config.health.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "names.middleware.CloudflareAccessMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "csp.middleware.CSPMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django_ratelimit.middleware.RatelimitMiddleware",
    "names.middleware.ConsentGateMiddleware",
    "names.middleware.AIErrorMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "names.context.legal",
            ]
        },
    }
]

DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
DATABASES["default"] |= {"CONN_MAX_AGE": 60, "CONN_HEALTH_CHECKS": True}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env("REDIS_URL", default="")
if REDIS_URL:
    # A Redis outage must not take the site down: reads miss, sessions fall back to the database,
    # and rate-limited actions pause because their counters can't be trusted.
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "IGNORE_EXCEPTIONS": True,
                "SOCKET_CONNECT_TIMEOUT": 2,
                "SOCKET_TIMEOUT": 2,
            },
        }
    }
    DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    SILENCED_SYSTEM_CHECKS = ["django_ratelimit.E003", "django_ratelimit.W001"]
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

AUTHENTICATION_BACKENDS = ["allauth.account.auth_backends.AuthenticationBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{validator}"}
    for validator in ("UserAttributeSimilarityValidator", "MinimumLengthValidator", "CommonPasswordValidator")
]
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = ACCOUNT_LOGOUT_REDIRECT_URL = "home"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED = True
ACCOUNT_PASSWORD_RESET_BY_CODE_ENABLED = True
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_EMAIL_SUBJECT_PREFIX = "Naamadeva: "
ACCOUNT_SIGNUP_FORM_CLASS = "names.forms.SignupConsentForm"
# Google sign-up still stops at our consent form. An existing account with the same verified email is reused.
SOCIALACCOUNT_AUTO_SIGNUP = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", default="")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", default="")
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APPS": [{"client_id": GOOGLE_CLIENT_ID, "secret": GOOGLE_CLIENT_SECRET}] if GOOGLE_CLIENT_ID else [],
        "SCOPE": ["openid", "email"],  # Ask for the least we need. We have no use for the Google profile.
        "OAUTH_PKCE_ENABLED": True,
    }
}
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
ADMIN_PATH = env("ADMIN_PATH", default="manage/")
REQUIRE_ADMIN_MFA = env.bool("REQUIRE_ADMIN_MFA", default=not DEBUG)
# Set to CF-Connecting-IP behind Cloudflare, so every per-visitor limit sees the real visitor rather than the proxy.
# Only safe once the server can't be reached except through Cloudflare, since anyone else could send this header.
ALLAUTH_TRUSTED_CLIENT_IP_HEADER = env("TRUSTED_CLIENT_IP_HEADER", default=None)
# Both set: the admin panel also checks the token Cloudflare Access signs, so reaching the server directly gets nowhere.
CF_ACCESS_TEAM_DOMAIN = env("CF_ACCESS_TEAM_DOMAIN", default="")
CF_ACCESS_AUD = env("CF_ACCESS_AUD", default="")
# The admin panel stays shut in production until those are set, so forgetting them can't leave it open to the internet.
ADMIN_REQUIRES_ACCESS = env.bool("ADMIN_REQUIRES_ACCESS", default=not DEBUG)
# Both set: sign-up asks Cloudflare Turnstile whether this is a person.
TURNSTILE_SITE_KEY = env("TURNSTILE_SITE_KEY", default="")
TURNSTILE_SECRET_KEY = env("TURNSTILE_SECRET_KEY", default="")
TURNSTILE_ORIGIN = "https://challenges.cloudflare.com"

# Everything goes through the job queue. EMAIL_DELIVERY_BACKEND is what finally sends it.
EMAIL_BACKEND = "names.mail.QueuedEmailBackend"
EMAIL_DELIVERY_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Naamadeva <no-reply@localhost>")

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
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=not DEBUG)
# A day to begin with, because browsers can't be told to forget it. Raise to a year once HTTPS is proven.
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0 if DEBUG else 86400)
# Share links are secrets: a Referer never holds a path, even between our own pages, where analytics could read it.
SECURE_REFERRER_POLICY = "strict-origin"

# Only the paths we use. jsDelivr serves anyone's code, so allowing the whole host lets injected HTML load any of it.
CDN_KIT = "https://cdn.jsdelivr.net/gh/0builddotdev/0build@0.6.13/"
CDN_FONTS = "https://cdn.jsdelivr.net/npm/@fontsource-variable/"
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": [SELF],
        "script-src": [SELF, CDN_KIT],
        "style-src": [SELF, UNSAFE_INLINE, CDN_KIT, CDN_FONTS],  # 0build and htmx set inline styles.
        "font-src": [SELF, CDN_KIT, CDN_FONTS],
        "img-src": [SELF, "data:"],
        "connect-src": [SELF],
        "frame-ancestors": [NONE],
        "base-uri": [SELF],
        # Chrome also applies this to redirects after a submit, which is how the Google button leaves the site.
        "form-action": [SELF, "https://accounts.google.com"],
    }
}
if TURNSTILE_SITE_KEY:
    CONTENT_SECURITY_POLICY["DIRECTIVES"]["script-src"].append(TURNSTILE_ORIGIN)
    CONTENT_SECURITY_POLICY["DIRECTIVES"]["frame-src"] = [TURNSTILE_ORIGIN]

AI_BACKEND = env("AI_BACKEND", default="names.ai.gemini.GeminiAI")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_MODEL = env("GEMINI_MODEL", default="gemini-3.5-flash-lite")
GEMINI_TTS_MODEL = env("GEMINI_TTS_MODEL", default="gemini-3.8-flash-lite-tts")
GEMINI_TTS_VOICE = env("GEMINI_TTS_VOICE", default="Kore")
GEMINI_TIMEOUT_MS = env.int("GEMINI_TIMEOUT_MS", default=20000)
GEMINI_ATTEMPTS = env.int("GEMINI_ATTEMPTS", default=2)

JOBS_EAGER = False  # Tests run background work inline.
AI_ENRICH_BATCH_SIZE = env.int("AI_ENRICH_BATCH_SIZE", default=10)
AI_ENRICH_BATCH_WINDOW_SECONDS = env.float("AI_ENRICH_BATCH_WINDOW_SECONDS", default=1.0)
AI_ENRICH_NAMES_PER_HOUR = env.int("AI_ENRICH_NAMES_PER_HOUR", default=2000)
AI_ENRICH_CALLS_PER_HOUR = env.int("AI_ENRICH_CALLS_PER_HOUR", default=200)
AI_FREE_CALLS_PER_MONTH = env.int("AI_FREE_CALLS_PER_MONTH", default=50)
CATALOG_SHARD_LIMIT = env.int("CATALOG_SHARD_LIMIT", default=3000)
MAX_PENDING_CONTRIBUTIONS = env.int("MAX_PENDING_CONTRIBUTIONS", default=2000)
ACCOUNT_RETENTION_DAYS = env.int("ACCOUNT_RETENTION_DAYS", default=730)

# Published in the legal pages. Change POLICY_VERSION when the policies change: everyone accepts again.
POLICY_VERSION = "2026-11-04.5"
LEGAL_ENTITY_NAME = env("LEGAL_ENTITY_NAME", default="")
LEGAL_ADDRESS = env("LEGAL_ADDRESS", default="")
LEGAL_JURISDICTION_CITY = env("LEGAL_JURISDICTION_CITY", default="")
GRIEVANCE_OFFICER_NAME = env("GRIEVANCE_OFFICER_NAME", default="")
GRIEVANCE_EMAIL = env("GRIEVANCE_EMAIL", default="")

AI_RATE_PER_BOARD = env("AI_RATE_PER_BOARD", default="30/h")
AI_RATE_GLOBAL = env("AI_RATE_GLOBAL", default="200/h")
ADD_RATE_PER_BOARD = env("ADD_RATE_PER_BOARD", default="60/h")
# Mobile networks in India put many households behind one address, so per-visitor limits stay generous.
AI_RATE_PER_IP = env("AI_RATE_PER_IP", default="60/h")
ADD_RATE_PER_IP = env("ADD_RATE_PER_IP", default="60/h")
BOARD_CREATE_RATE = env("BOARD_CREATE_RATE", default="60/h")
BOARD_CREATE_PER_USER = env("BOARD_CREATE_PER_USER", default="20/h")
BOARD_RATE_PER_IP = env("BOARD_RATE_PER_IP", default="300/m")
GUEST_VOTERS_PER_IP = env("GUEST_VOTERS_PER_IP", default="30/h")
RATELIMIT_VIEW = "names.views.rate_limited"
