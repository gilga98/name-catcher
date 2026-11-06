"""Django refuses to start on a failed system check, which the rest of the suite never runs."""

from django.core.management import call_command


def test_the_app_passes_the_checks_it_runs_at_startup():
    call_command("check", fail_level="ERROR")
