from django.apps import AppConfig


class NamesConfig(AppConfig):
    name = "names"

    def ready(self) -> None:
        from . import checks  # noqa: F401
