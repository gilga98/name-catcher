import os

os.environ.setdefault("SECRET_KEY", "test-only")
os.environ.setdefault("DATABASE_URL", "sqlite://:memory:")
os.environ["DEBUG"] = "True"
os.environ["AI_BACKEND"] = "names.ai.stub.StubAI"

from .settings import *  # noqa: E402, F403

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
