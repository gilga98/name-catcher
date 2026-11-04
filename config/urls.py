from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path(settings.ADMIN_PATH, admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", include("names.urls")),
]
