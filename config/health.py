from django.db import DatabaseError, connection
from django.http import HttpResponse


def _ready() -> HttpResponse:
    try:
        connection.ensure_connection()
    except DatabaseError:
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


class HealthCheckMiddleware:
    """Runs first. Health checks come over plain HTTP from inside the container, so they skip host and HTTPS checks."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz":
            return HttpResponse("ok", content_type="text/plain")
        if request.path == "/readyz":
            return _ready()
        return self.get_response(request)
