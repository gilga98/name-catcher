FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_URL=sqlite:////data/db.sqlite3
WORKDIR /app
# Coolify's health check runs curl inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN SECRET_KEY=build-only python manage.py collectstatic --noinput \
    && useradd --create-home app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD if [ -n "$CONTAINER_ROLE" ]; then exit 0; else python -c "import urllib.request as u; u.urlopen('http://localhost:8000/healthz')"; fi
# Every app deploys at once on a push, so workers hold off until the web app has migrated the database.
CMD ["sh", "-c", "if [ -n \"$CONTAINER_ROLE\" ]; then until python manage.py migrate --check; do echo 'Waiting for migrations'; sleep 5; done; fi; case \"$CONTAINER_ROLE\" in worker) exec python manage.py run_huey ;; llm-worker) exec python manage.py run_llm_worker ;; *) python manage.py migrate --noinput && exec gunicorn config.wsgi --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-2} --threads 4 --timeout 60 --graceful-timeout 30 --max-requests 1000 --max-requests-jitter 100 --worker-tmp-dir /dev/shm --access-logfile - ;; esac"]
