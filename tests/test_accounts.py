import json
from urllib.parse import parse_qs, urlparse

import pytest
from allauth.socialaccount.forms import SignupForm as SocialSignupForm
from allauth.socialaccount.models import SocialLogin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from names.accounts import profile_of, record_consent, voter_id
from names.models import Board, CatalogName, ConsentRecord, Name, Vote

pytestmark = pytest.mark.django_db
SIGNUP = {"email": "n@example.com", "password1": "a-long-passphrase-9", "password2": "a-long-passphrase-9"}


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


def make_user(email="p@example.com", consent=True):
    user = get_user_model().objects.create_user(username=email, email=email, password="x")
    if consent:
        record_consent(user)
    return user


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.parametrize("route", ["privacy", "terms", "cookies", "disclaimer", "grievance"])
def test_legal_pages_are_public_and_linked_from_home(route):
    assert Client().get(reverse(route)).status_code == 200
    assert reverse(route).encode() in Client().get(reverse("home")).content


def test_unset_legal_details_show_an_obvious_placeholder():
    assert b"[set GRIEVANCE_OFFICER_NAME]" in Client().get(reverse("grievance")).content


@override_settings(GRIEVANCE_OFFICER_NAME="A Person", GRIEVANCE_EMAIL="g@example.com")
def test_set_legal_details_are_published():
    html = Client().get(reverse("grievance")).content
    assert b"A Person" in html and b"g@example.com" in html


@pytest.mark.parametrize("missing", ["accept_terms", "confirm_adult"])
def test_signup_needs_both_consents(missing):
    data = {**SIGNUP, "accept_terms": "on", "confirm_adult": "on"}
    del data[missing]
    Client().post(reverse("account_signup"), data)
    assert not get_user_model().objects.exists()


def test_signup_records_which_policy_version_was_accepted(settings):
    Client().post(reverse("account_signup"), {**SIGNUP, "accept_terms": "on", "confirm_adult": "on"})
    record = ConsentRecord.objects.get()
    assert (record.user.email, record.policy_version) == ("n@example.com", settings.POLICY_VERSION)


def test_people_without_current_consent_must_accept_before_carrying_on():
    client = logged_in(make_user(consent=False))
    response = client.get(reverse("dashboard"))
    assert response.url.startswith(reverse("accept_policies"))
    response = client.post(reverse("accept_policies"), {"accept": "1", "next": reverse("dashboard")})
    assert response.url == reverse("dashboard")
    assert client.get(reverse("dashboard")).status_code == 200


def test_new_policy_version_asks_again():
    client = logged_in(make_user())
    assert client.get(reverse("dashboard")).status_code == 200
    with override_settings(POLICY_VERSION="next"):
        assert client.get(reverse("dashboard")).status_code == 302


def test_refusing_new_policies_never_blocks_export_or_deletion():
    client = logged_in(make_user(consent=False))
    assert client.get(reverse("account_settings")).status_code == 200
    assert client.get(reverse("account_export")).status_code == 200
    assert client.post(reverse("account_delete"), {"confirm": "DELETE"}).status_code == 302
    assert not get_user_model().objects.exists()


def test_accept_page_refuses_to_redirect_off_site():
    client = logged_in(make_user(consent=False))
    response = client.post(reverse("accept_policies"), {"accept": "1", "next": "https://evil.example"})
    assert response.url == reverse("dashboard")


def test_dashboard_and_settings_need_a_login():
    for route in ("dashboard", "account_settings", "account_export", "contribute"):
        assert Client().get(reverse(route)).status_code == 302


def test_display_name_shows_on_votes_and_ai_toggle_saves():
    user = make_user()
    client = logged_in(user)
    client.post(reverse("account_settings"), {"display_name": "Ganesh"})
    profile = profile_of(user)
    assert (profile.display_name, profile.ai_enabled) == ("Ganesh", False)
    board = Board.objects.create()
    board.members.add(user)
    name = Name.objects.create(board=board, text="Tara", key="tara", status="kept")
    client.post(reverse("vote", args=[board.parent_token, name.pk]), HTTP_HX_REQUEST="true")
    assert Vote.objects.get().voter_name == "Ganesh"


def test_export_holds_my_data_and_nothing_secret():
    user = make_user()
    board = Board.objects.create(brief={"surname": "Rao"})
    board.members.add(user)
    Name.objects.create(board=board, text="Tara", key="tara", audio=b"RIFF")
    response = logged_in(user).get(reverse("account_export"))
    assert "attachment" in response["Content-Disposition"]
    data = json.loads(response.content)
    assert data["account"]["email"] == user.email
    assert data["boards"][0]["names"][0]["text"] == "Tara"
    assert data["consents"]
    assert board.parent_token not in response.content.decode()
    assert board.guest_token not in response.content.decode()


def test_deleting_needs_the_exact_word():
    client = logged_in(make_user())
    client.post(reverse("account_delete"), {"confirm": "delete"})
    assert get_user_model().objects.exists()


def test_deleting_erases_my_boards_keeps_shared_ones_and_unlinks_the_rest():
    me, partner = make_user(), make_user("q@example.com")
    mine = Board.objects.create()
    mine.members.add(me)
    shared = Board.objects.create()
    shared.members.add(me, partner)
    name = Name.objects.create(board=shared, text="Tara", key="tara", status="kept")
    Vote.objects.create(name=name, voter=voter_id(me), is_parent=True)
    contribution = CatalogName.objects.create(key="tejas", text="Tejas", submitted_by=me)

    logged_in(me).post(reverse("account_delete"), {"confirm": "DELETE"})

    assert list(Board.objects.all()) == [shared]
    assert list(shared.members.all()) == [partner]
    assert not Vote.objects.exists()
    contribution.refresh_from_db()
    assert contribution.submitted_by is None
    trail = ConsentRecord.objects.filter(action="withdrawn").get()
    assert trail.user is None


def test_idle_accounts_are_purged_but_not_active_or_staff_ones(settings):
    long_ago = timezone.now() - timezone.timedelta(days=settings.ACCOUNT_RETENTION_DAYS + 1)
    idle = make_user("idle@example.com")
    idle.last_login = long_ago
    idle.save()
    make_user("active@example.com")
    staff = make_user("staff@example.com")
    staff.is_staff, staff.last_login = True, long_ago
    staff.save()
    call_command("purge_inactive")
    assert set(get_user_model().objects.values_list("email", flat=True)) == {"active@example.com", "staff@example.com"}


GOOGLE_APP = {
    "google": {"APPS": [{"client_id": "x", "secret": "y"}], "SCOPE": ["openid", "email"], "OAUTH_PKCE_ENABLED": True}
}


def test_google_button_only_appears_once_configured():
    assert b"Continue with Google" not in Client().get(reverse("account_login")).content
    with override_settings(SOCIALACCOUNT_PROVIDERS=GOOGLE_APP):
        assert b"Continue with Google" in Client().get(reverse("account_signup")).content


def test_google_button_goes_to_google_with_minimal_scope_and_csp_allows_it():
    with override_settings(SOCIALACCOUNT_PROVIDERS=GOOGLE_APP):
        response = Client().post(reverse("google_login"))
    assert response.url.startswith("https://accounts.google.com/")
    query = parse_qs(urlparse(response.url).query)
    assert set(query["scope"][0].split()) == {"openid", "email"}
    assert "code_challenge" in query
    assert "https://accounts.google.com" in Client().get(reverse("account_login"))["Content-Security-Policy"]


def test_google_sign_up_still_has_to_pass_the_consent_form():
    form = SocialSignupForm(sociallogin=SocialLogin(user=get_user_model()()))
    assert {"accept_terms", "confirm_adult"} <= set(form.fields)
