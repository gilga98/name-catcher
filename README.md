# Naamadeva

Helps parents choose a baby name: guided suggestions from Gemini, pronunciation, a shortlist, family voting, and duplicate detection across spellings and Indian scripts. Django, HTMX and [0build](https://0build.dev).

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
| `WEB_CONCURRENCY` | No | Gunicorn workers. Defaults to 2. |
| `AI_RATE_PER_BOARD`, `AI_RATE_GLOBAL`, `BOARD_CREATE_RATE` | No | Limits on LLM spend and new boards. |
| `GEMINI_MODEL`, `GEMINI_TTS_MODEL`, `GEMINI_TTS_VOICE` | No | Change models without touching code. |

## Layout

- `names/ai/`: small interfaces (`base.py`) with Gemini and offline implementations. `AI_BACKEND` picks one.
- `names/dedup.py`: phonetic keys, fuzzy matching, and LLM judgement for uncertain pairs.
- `names/services.py`: what families can do. `names/views.py` only translates HTTP.
