import os

os.environ.setdefault("SECRET_KEY", "test-only")
os.environ.setdefault("DATABASE_URL", "sqlite://:memory:")
os.environ["DEBUG"] = "True"
os.environ["REDIS_URL"] = ""
os.environ["AI_BACKEND"] = "names.ai.stub.StubAI"
os.environ["SOOTA_API_URL"] = ""
os.environ["SOOTA_API_KEY"] = ""

from huey import MemoryHuey  # noqa: E402

from .settings import *  # noqa: E402, F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_DELIVERY_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
JOBS_EAGER = True
LLM_HUEY = MemoryHuey("name_catcher_llm", immediate=False)
AI_ENRICH_BATCH_WINDOW_SECONDS = 0
AI_VOICE_PER_DAY = 0  # Off, except in the tests about it.
AI_VOICE_SPACING_SECONDS = 0
HUEY = {
    "huey_class": "huey.MemoryHuey",
    "name": "name_catcher",
    "immediate": False,
}
