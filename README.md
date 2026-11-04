# Naamadeva

Helps parents choose a baby name: suggestions from a human-reviewed catalogue, ranked in the browser, with Gemini as a last resort. Pronunciation, a shortlist, family suggestions, and duplicate detection across spellings and Indian scripts. Django, HTMX and [0build](https://0build.dev). See [the architecture](docs/ARCHITECTURE.md).

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
| `ALLOWED_HOSTS` | Yes | Your domain. `localhost` is only allowed with `DEBUG`, so a request sent straight to the server is refused. Health checks still work. |
| `CSRF_TRUSTED_ORIGINS` | Yes | `https://` plus your domain. |
| `DATABASE_URL` | Recommended | A Coolify Postgres URL. Otherwise SQLite at `/data`, which needs a persistent volume. |
| `REDIS_URL` | Yes | Shared cache, session cache, rate-limit counters, and Huey task queue. |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | Yes | SMTP for sign-up and password-reset codes. Nobody can finish signing up without it. |
| `LEGAL_ENTITY_NAME`, `LEGAL_ADDRESS`, `LEGAL_JURISDICTION_CITY`, `GRIEVANCE_OFFICER_NAME`, `GRIEVANCE_EMAIL` | Yes | Published in the legal pages, which show a visible placeholder until set. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | No | Shows "Continue with Google". The redirect URI is `https://<domain>/accounts/google/login/callback/`. |
| `ADMIN_PATH` | No | Where the admin panel lives. Defaults to `manage/`. |
| `REQUIRE_ADMIN_MFA` | No | Staff need two-factor sign-in for the admin panel. On unless `DEBUG`. |
| `AI_ENRICH_BATCH_SIZE`, `AI_ENRICH_BATCH_WINDOW_SECONDS`, `AI_ENRICH_NAMES_PER_HOUR`, `AI_ENRICH_CALLS_PER_HOUR` | No | Approved names per LLM call, how long to wait to fill a batch, and the hourly caps on names and calls for tagging. Unreviewed names always get a call of their own. |
| `AI_FREE_CALLS_PER_MONTH`, `CATALOG_SHARD_LIMIT`, `ACCOUNT_RETENTION_DAYS` | No | LLM allowance per parent, names per browser shard, days before an idle account is deleted. |
| `TRUSTED_CLIENT_IP_HEADER` | Yes, behind Cloudflare | `CF-Connecting-IP`, so limits apply per visitor. Only safe once the server is reachable solely through Cloudflare, because anyone else could send this header. |
| `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | Yes | Your `<team>.cloudflareaccess.com` and the Access application's AUD tag. The admin panel then refuses requests without a valid Access token, even if someone reaches the server directly. Until both are set, it answers 404 in production. |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` | Yes | Asks Cloudflare whether someone signing up is a person. If Cloudflare can't answer, sign-up pauses. |
| `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` | No | On unless `DEBUG`. HSTS starts at a day: raise it to `31536000` once HTTPS is proven, because browsers can't be told to forget it. |
| `WEB_CONCURRENCY` | No | Gunicorn workers. Defaults to 2. |
| `AI_RATE_PER_IP`, `AI_RATE_PER_BOARD`, `AI_RATE_GLOBAL` | No | Limits on LLM spend, narrowest first. A request stopped by a narrow one doesn't count against the wider ones. |
| `BOARD_CREATE_RATE`, `BOARD_CREATE_PER_USER`, `BOARD_RATE_PER_IP`, `ADD_RATE_PER_IP`, `GUEST_VOTERS_PER_IP` | No | Per-visitor and per-account limits. Generous, because mobile networks in India put many households behind one address. |
| `GEMINI_MODEL`, `GEMINI_TTS_MODEL`, `GEMINI_TTS_VOICE` | No | Change models without touching code. |
| `SOOTA_API_URL`, `SOOTA_API_KEY` | No | `http://soota-engine-gateway:8000`, and the key for the `naamadeva` client (`python -m engine_gateway.keys naamadeva` in Soota's repo). Soota isn't public, so the gateway must run on the same Coolify server, on the `coolify` network. Parents see the birth star panel once the URL is set. Mark the key as a secret. |
| `SOOTA_TIMEOUT_SECONDS`, `SOOTA_AYANAMSHA`, `SOOTA_NUMEROLOGY_SYSTEM`, `BIRTH_STAR_RATE`, `PLACE_SEARCH_RATE` | No | How long to wait for an answer, Soota's ayanamsha index (2 is Lahiri), `chaldean` or `pythagorean`, and readings and place searches per board. |

Emails, every LLM call and Soota requests run off the request path, queued to Redis and drained by Huey in a separate worker container (`python manage.py run_huey`). Retries use Huey's task scheduler. Schedule `python manage.py run_jobs` every few minutes to tidy finished jobs and pick up anything stranded by a restart. Schedule `python manage.py purge_inactive` daily: the Privacy Policy promises it. Bump `POLICY_VERSION` in settings when the policies change, and everyone is asked to accept again.

Before launch, `python manage.py check --deploy` should list no `names.W` warnings. Also, outside this code: make the server reachable only through Cloudflare (a Tunnel, or a firewall allowing only Cloudflare's addresses), point a Zero Trust application at your `ADMIN_PATH`, and set a billing cap in Google Cloud.

## Admin

Create a staff user with `python manage.py createsuperuser`, then open `/manage/`. Staff set up two-factor sign-in on first visit. Upload names as a CSV at **Catalog names > Upload names**. Family boards show counts only, by design.

## Access

- Parents sign up with email and password and verify a code by email (django-allauth). Their boards are private to members.
- A partner joins through a single-use invite link, using their own account.
- Relatives use the family link with no account: they can only suggest, and can't see any names on the board. Parents can replace the link at any time.

## Failure behaviour

- **Gemini down or over its limit:** a clear message and a retry button. Catalogue suggestions, adding names and spotting duplicates don't use Gemini, so they keep working.
- **Soota down:** one retry, then a message on the birth star panel, and the parent can try again. Cards simply go without a number. Nothing else depends on it.
- **Open-Meteo down:** the place of birth box says the search isn't working, and a reading can't be started until it returns. Nothing else uses it.
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
- `names/places.py`: place of birth search, and the time zone offset for a birth.
- `names/soota.py`: asks Soota for birth stars and numerology, and the only code that reads its answers.
