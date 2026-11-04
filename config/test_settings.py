import os

os.environ.setdefault("SECRET_KEY", "test-only")
os.environ.setdefault("DATABASE_URL", "sqlite://:memory:")
os.environ["DEBUG"] = "True"
os.environ["REDIS_URL"] = ""
os.environ["AI_BACKEND"] = "names.ai.stub.StubAI"

from .settings import *  # noqa: E402, F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
