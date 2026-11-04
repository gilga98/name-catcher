from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.urls import include, path


def ready(request):
    try:
        connection.ensure_connection()
    except DatabaseError:
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz", lambda request: HttpResponse("ok", content_type="text/plain")),
    path("readyz", ready),
    path("accounts/", include("allauth.urls")),
    path("", include("names.urls")),
]
