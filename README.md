# Naamadeva

Helps parents choose a baby name: suggestions from a human-reviewed catalogue, ranked in the browser, with Gemini as a last resort. Pronunciation, a shortlist, family voting, and duplicate detection across spellings and Indian scripts. Django, HTMX and [0build](https://0build.dev). See [the architecture](docs/ARCHITECTURE.md).

## Run locally

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # set SECRET_KEY and GEMINI_API_KEY
make run
```

To work without calling Gemini, set `AI_BACKEND=names.ai.stub.StubAI` in `.env`.

`make check` runs ruff (including the C901 complexity limit of 10), xenon and the tests.

## Deploy on Coolify

Use the Dockerfile build pack with port 8000 and health check path `/healthz`. Set these as environment variables:

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | Yes | Long random value. Mark it as a secret. |
| `GEMINI_API_KEY` | Yes | Mark it as a secret. Used only on the server. |
| `ALLOWED_HOSTS` | Yes | Your domain. |
| `CSRF_TRUSTED_ORIGINS` | Yes | `https://` plus your domain. |
| `DATABASE_URL` | Recommended | A Coolify Postgres URL. Otherwise SQLite at `/data`, which needs a persistent volume. |
| `REDIS_URL` | Recommended | Shared cache, session cache and rate-limit counters. Without it, each worker keeps its own, so limits are looser. |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | Yes | SMTP for sign-up and password-reset codes. Nobody can finish signing up without it. |
| `LEGAL_ENTITY_NAME`, `LEGAL_ADDRESS`, `LEGAL_JURISDICTION_CITY`, `GRIEVANCE_OFFICER_NAME`, `GRIEVANCE_EMAIL` | Yes | Published in the legal pages, which show a visible placeholder until set. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | No | Shows "Continue with Google". The redirect URI is `https://<domain>/accounts/google/login/callback/`. |
| `ADMIN_PATH` | No | Where the admin panel lives. Defaults to `manage/`. |
| `REQUIRE_ADMIN_MFA` | No | Staff need two-factor sign-in for the admin panel. On unless `DEBUG`. |
| `AI_FREE_CALLS_PER_MONTH`, `CATALOG_SHARD_LIMIT`, `ACCOUNT_RETENTION_DAYS` | No | LLM allowance per parent, names per browser shard, days before an idle account is deleted. |
| `TRUSTED_CLIENT_IP_HEADER` | Behind Cloudflare | `CF-Connecting-IP`, so login limits apply per visitor. |
| `WEB_CONCURRENCY` | No | Gunicorn workers. Defaults to 2. |
| `AI_RATE_PER_BOARD`, `AI_RATE_GLOBAL`, `BOARD_CREATE_RATE` | No | Limits on LLM spend and new boards. |
| `GEMINI_MODEL`, `GEMINI_TTS_MODEL`, `GEMINI_TTS_VOICE` | No | Change models without touching code. |

Schedule `python manage.py purge_inactive` daily: the Privacy Policy promises it. Bump `POLICY_VERSION` in settings when the policies change, and everyone is asked to accept again.

## Admin

Create a staff user with `python manage.py createsuperuser`, then open `/manage/`. Staff set up two-factor sign-in on first visit. Upload names as a CSV at **Catalog names > Upload names**. Family boards show counts only, by design.

## Access

- Parents sign up with email and password and verify a code by email (django-allauth). Their boards are private to members.
- A partner joins through a single-use invite link, using their own account.
- Relatives use the family link with no account: they can only vote and suggest. Parents can replace the link at any time.

## Failure behaviour

- **Gemini down or over its limit:** a clear message and a retry button. Catalogue suggestions, adding names and spotting duplicates don't use Gemini, so they keep working.
- **Redis down:** the site keeps working, and sessions fall back to the database. Actions under a rate limit pause until it returns.
- **Database down:** `/readyz` returns 503, while `/healthz` stays up so the container isn't restarted in a loop.

## Layout

- `names/catalog.py`: the shared name pool, bulk import, and everything that avoids an LLM call.
- `names/static/names/engine.js`: ranks a shard of the catalogue in the browser. `tests/engine.test.mjs` covers it.
- `names/accounts.py`: consent, AI allowance, data export and erasure.
- `names/admin.py`: the operator's panel.
- `names/ai/`: small interfaces (`base.py`) with Gemini and offline implementations. `AI_BACKEND` picks one.
- `names/dedup.py`: phonetic keys and fuzzy matching. Close matches are always confirmed by the person.
- `names/services.py`: what families can do. `names/views.py` only translates HTTP.
