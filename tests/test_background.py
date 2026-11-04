import threading
from types import SimpleNamespace

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.mail import send_mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from huey.contrib.djhuey import HUEY
from redis.exceptions import ConnectionError as RedisConnectionError

from names import catalog, jobs, services, tasks
from names.accounts import record_consent
from names.ai.base import AIError, AIRateLimited, DescribedIdea, RespelledName
from names.mail import QueuedEmailBackend
from names.models import AICall, Board, CatalogName, Job, Name, NameAlias, Vote

pytestmark = pytest.mark.django_db
HTMX = {"HTTP_HX_REQUEST": "true"}
LLM_HUEY = django_settings.LLM_HUEY
PARK = {"JOBS_EAGER": False}  # Work waits in the queue, as it does between a request and the worker.


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    HUEY.flush()
    LLM_HUEY.flush()


def idea(given, name=None, meaning="A meaning"):
    return DescribedIdea(
        given=given, name=name or given, native_script="", meaning=meaning, origin="Sanskrit", respelling="SAY-it",
        sounds_like=given.lower(),
        speech_lang="hi-IN", gender="any", rarity="rare", style="any", genres=["Light"], insight="Worth knowing.",
        surname_fit="",
    )  # fmt: skip


class Counting:
    def __init__(self, renames=None):
        self.batches = []
        self.renames = renames or {}

    def describe_many(self, names, genres):
        self.batches.append(list(names))
        return [idea(name, self.renames.get(name)) for name in names]


@pytest.fixture
def llm(monkeypatch):
    fake = Counting()
    monkeypatch.setattr(services, "get_ai", lambda: fake)
    return fake


@pytest.fixture
def user():
    person = get_user_model().objects.create_user(username="p@example.com", email="p@example.com", password="x")
    record_consent(person)
    return person


@pytest.fixture
def board(user):
    board = Board.objects.create(brief={"surname": "Rao"})
    board.members.add(user)
    return board


@pytest.fixture
def client(user):
    client = Client()
    client.force_login(user)
    return client


def add(board, text):
    voter = services.Voter(id="guest", name="Maa", is_parent=False)
    return services.add_name(board, text, voter, force=True).name


# Batching and caching


def approved(*texts):
    for text in texts:
        key = catalog.phonetic_key(text)
        CatalogName.objects.create(key=key, text=text, status=CatalogName.Status.APPROVED)


def test_approved_names_from_many_families_share_one_llm_call(llm, user):
    approved("Zubin", "Tanvi", "Hridaan")
    with override_settings(**PARK):
        cards = [add(Board.objects.create(), text) for text in ("Zubin", "Tanvi", "Hridaan")]
    jobs.drain(jobs.LLM)
    assert llm.batches == [["Zubin", "Tanvi", "Hridaan"]]
    assert AICall.objects.get().units == 3
    for card in cards:
        card.refresh_from_db()
        assert card.meaning == "A meaning" and card.insight == "Worth knowing." and "Light" in card.tags


@override_settings(AI_ENRICH_BATCH_SIZE=2)
def test_big_backlogs_are_split_into_batches(llm):
    approved("Zubin", "Tanvi", "Hridaan")
    with override_settings(**PARK):
        for text in ("Zubin", "Tanvi", "Hridaan"):
            add(Board.objects.create(), text)
    jobs.drain(jobs.LLM)
    assert [len(batch) for batch in llm.batches] == [2, 1]


def test_a_name_many_parents_want_is_fetched_once_and_fills_every_card(llm):
    with override_settings(**PARK):
        cards = [add(Board.objects.create(), spelling) for spelling in ("Zubin", "Zubeen", "Zubin", "ज़ुबिन")]
    jobs.drain(jobs.LLM)
    assert sum(len(batch) for batch in llm.batches) == 1
    for card in cards:
        card.refresh_from_db()
        assert card.meaning == "A meaning"


def test_a_cached_name_never_goes_back_to_the_llm(llm):
    add(Board.objects.create(), "Zubin")
    for _ in range(5):
        card = add(Board.objects.create(), "Zubin")
        assert card.meaning == "A meaning"
    assert len(llm.batches) == 1
    assert AICall.objects.count() == 1


def test_a_family_name_is_never_part_of_the_batch(llm, board):
    add(board, "Zubin")
    assert "Rao" not in str(llm.batches)


def test_batches_spare_the_familys_monthly_allowance(llm, board, user):
    add(board, "Zubin")
    assert not AICall.objects.filter(user=user).exists()


@override_settings(AI_ENRICH_NAMES_PER_HOUR=1)
def test_hourly_cap_stops_a_flood_of_new_names(llm):
    add(Board.objects.create(), "Zubin")
    add(Board.objects.create(), "Tanvi")
    assert llm.batches == [["Zubin"]]
    assert CatalogName.objects.get(text="Tanvi").enrich_state == CatalogName.Enrich.PENDING


def test_failed_batches_are_retried_then_given_up_on_for_a_while(monkeypatch, board):
    def broken():
        raise AIError("down")

    monkeypatch.setattr(services, "get_ai", broken)
    card = add(board, "Zubin")
    for _ in range(3):
        state = services.describe_name(card)
    assert state == services.Details.UNAVAILABLE
    assert CatalogName.objects.get(text="Zubin").enrich_state == CatalogName.Enrich.FAILED


def test_names_the_model_skips_are_not_lost(monkeypatch, board):
    fake = SimpleNamespace(describe_many=lambda names, genres: [])
    monkeypatch.setattr(services, "get_ai", lambda: fake)
    add(board, "Zubin")
    assert CatalogName.objects.get(text="Zubin").enrich_attempts >= 1


def test_rejected_names_are_never_sent(llm, board):
    CatalogName.objects.create(key=catalog.phonetic_key("Badword"), text="Badword", status="rejected")
    card = add(board, "Badword")
    assert services.describe_name(card) == services.Details.UNAVAILABLE
    assert llm.batches == []


def test_bare_admin_uploads_are_enriched_in_batches(llm):
    options = catalog.ImportOptions(source="admin", status="approved")
    catalog.import_rows("name\nZubin\nTanvi\n", options)
    catalog.queue_blank_entries(CatalogName.objects.all())
    assert llm.batches == [["Tanvi", "Zubin"]] or llm.batches == [["Zubin", "Tanvi"]]
    assert CatalogName.objects.get(text="Zubin").meaning == "A meaning"


def test_contributors_text_is_replaced_before_other_families_see_it(llm, board):
    options = catalog.ImportOptions(source="user", status="pending")
    catalog.import_rows("Zubin, something rude", options)
    assert add(board, "Zubin").meaning == "A meaning"


# Normalisation that learns


def test_the_llm_teaches_new_spellings_and_they_stay_free(monkeypatch, board):
    fake = Counting(renames={"Mohamad": "Muhammad"})
    monkeypatch.setattr(services, "get_ai", lambda: fake)
    first = add(board, "Mohamad")
    first.refresh_from_db()
    assert first.key == catalog.phonetic_key("Muhammad")
    assert NameAlias.objects.filter(key=catalog.phonetic_key("Mohamad")).exists()
    assert add(Board.objects.create(), "Mohamad").meaning == "A meaning"
    assert len(fake.batches) == 1


def test_the_llm_cannot_merge_a_name_into_a_different_one(monkeypatch, board):
    fake = Counting(renames={"Zubin": "Rohan"})
    monkeypatch.setattr(services, "get_ai", lambda: fake)
    add(board, "Zubin")
    assert not NameAlias.objects.exists()
    assert CatalogName.objects.filter(key=catalog.phonetic_key("Zubin")).exists()


def test_admins_can_upload_variations():
    options = catalog.ImportOptions(source="admin", status="approved")
    catalog.import_rows("name,meaning,variants\nLakshmi,Goddess,லட்சுமி|Luxmi\n", options)
    assert catalog.canonical_key("லட்சுமி") == catalog.phonetic_key("Lakshmi")


def test_a_learned_spelling_finds_the_duplicate_on_a_board(board):
    entry = CatalogName.objects.create(key="muhamad", text="Muhammad", meaning="m", status="approved")
    NameAlias.objects.create(key=catalog.phonetic_key("Mahomet"), entry=entry)
    voter = services.Voter(id="parent", name="Maa", is_parent=True)
    services.add_name(board, "Muhammad", voter)
    assert services.add_name(board, "Mahomet", voter).duplicate is not None


# Any language


@pytest.mark.parametrize(
    ("typed", "language"),
    [("ಆರವ", "kn-IN"), ("आरव", "hi-IN"), ("ஆரவ்", "ta-IN"), ("ఆరవ్", "te-IN"), ("ആരവ്", "ml-IN"), ("আরব", "bn-IN")],
)
def test_a_name_typed_in_a_script_works_from_start_to_finish(typed, language, board, client):
    guest = Client()
    response = guest.post(reverse("add", args=[board.guest_token]), {"text": typed, "by": "ಅಮ್ಮ"}, **HTMX)
    assert response.status_code == 200
    name = board.names.get()
    assert name.speech_lang == language and name.suggested_by == "ಅಮ್ಮ"
    assert typed in client.get(reverse("board", args=[board.parent_token])).content.decode()
    assert client.post(reverse("keep", args=[board.parent_token, name.pk]), **HTMX).status_code == 200
    assert Vote.objects.count() == 1
    assert client.get(reverse("account_export")).status_code == 200


def test_the_same_name_in_two_scripts_is_recognised_for_the_family(board, client):
    Client().post(reverse("add", args=[board.guest_token]), {"text": "ಆರವ"}, **HTMX)
    board.names.update(status=Name.Status.KEPT)
    response = client.post(reverse("add", args=[board.parent_token]), {"text": "आरव"}, **HTMX)
    assert "the same name as" in response.content.decode()
    assert board.names.count() == 1


def test_a_script_name_gets_a_latin_spelling_and_keeps_what_was_typed(monkeypatch, board):
    monkeypatch.setattr(services, "get_ai", lambda: Counting(renames={"ಆರವ": "Aarav"}))
    name = add(board, "ಆರವ")
    name.refresh_from_db()
    assert (name.text, name.native) == ("Aarav", "ಆರವ")


def test_syllables_and_surname_fit_work_before_any_details_arrive(board):
    with override_settings(**PARK):
        name = add(board, "ಆರವ")
    assert name.syllables == 2
    assert catalog.surname_fit("ಆರವ", "ರಾವ್")


def test_a_description_in_kannada_still_finds_genres():
    assert "Light" in catalog.themes_from_text("ಸೂರ್ಯ ಮತ್ತು ಜ್ಯೋತಿ")
    assert "Light" in catalog.themes_from_text("सूर्य की ज्योति")


def test_a_brief_in_kannada_is_saved_and_shown(board, client):
    client.post(reverse("brief", args=[board.parent_token]), {"surname": "ರಾವ್", "nickname": "ಪುಟ್ಟ", "notes": "ಸೂರ್ಯ"})
    board.refresh_from_db()
    assert "Light" in board.brief["themes"]
    assert "ಪುಟ್ಟ" in Client().get(reverse("board", args=[board.guest_token])).content.decode()


@pytest.mark.parametrize("junk", ["😀😀", "!!!", "   "])
def test_input_with_no_letters_is_refused(junk, board):
    response = Client().post(reverse("add", args=[board.guest_token]), {"text": junk}, **HTMX)
    assert response.status_code == 422
    assert not board.names.exists()


def test_csv_in_kannada_imports():
    result = catalog.import_rows("name,meaning\nಆರವ,ಶಾಂತ\n", catalog.ImportOptions(source="admin", status="approved"))
    assert result.created == 1
    assert CatalogName.objects.get().key == "arav"


# Nothing third-party on the request path


@override_settings(**PARK)
def test_asking_ai_returns_at_once_and_keeps_polling(board, client, monkeypatch):
    monkeypatch.setattr(services, "get_ai", lambda: pytest.fail("the LLM must not run in a request"))
    response = client.post(reverse("suggest", args=[board.parent_token]), **HTMX)
    job = Job.objects.get(kind="suggest")
    assert response.status_code == 200
    assert reverse("ai_job", args=[board.parent_token, job.pk]) in response.content.decode()
    assert job.status == Job.Status.QUEUED


def test_finished_ai_cards_join_the_deck(board, client):
    with override_settings(**PARK):
        client.post(reverse("suggest", args=[board.parent_token]), **HTMX)
    job = Job.objects.get(kind="suggest")
    jobs.drain(jobs.LLM)
    html = client.get(reverse("ai_job", args=[board.parent_token, job.pk]), **HTMX).content.decode()
    assert 'hx-swap-oob="beforeend:#review"' in html and "Aarav" in html


def test_double_clicking_ask_ai_runs_one_job_and_charges_once(board, client, user):
    with override_settings(**PARK):
        client.post(reverse("suggest", args=[board.parent_token]), **HTMX)
        client.post(reverse("suggest", args=[board.parent_token]), **HTMX)
    assert Job.objects.filter(kind="suggest").count() == 1
    assert AICall.objects.filter(user=user).count() == 1


def test_another_familys_job_is_out_of_reach(board, client):
    other = Board.objects.create()
    job = Job.objects.create(queue=jobs.LLM, kind="suggest", board=other)
    assert client.get(reverse("ai_job", args=[board.parent_token, job.pk])).status_code == 404


@override_settings(**PARK)
def test_family_ideas_show_the_catalogue_at_once_while_the_llm_runs_behind(board, monkeypatch):
    monkeypatch.setattr(services, "get_ai", lambda: pytest.fail("the LLM must not run in a request"))
    CatalogName.objects.create(key="oviya", text="Oviya", meaning="Painting", status="approved")
    html = Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX).content.decode()
    assert "Oviya" in html and "Finding more ideas" in html


def test_a_tapped_family_idea_cant_be_sent_twice_while_it_waits(board):
    CatalogName.objects.create(key="oviya", text="Oviya", meaning="Painting", status="approved")
    html = Client().post(reverse("family_ideas", args=[board.guest_token]), **HTMX).content.decode()
    assert 'hx-disabled-elt="this"' in html


def test_a_failed_ai_search_says_why_in_place(board, client, monkeypatch):
    def broken():
        raise AIError("Try again in a minute.")

    monkeypatch.setattr(services, "get_ai", broken)
    response = client.post(reverse("suggest", args=[board.parent_token]), **HTMX)
    assert "Try again in a minute." in response.content.decode()


# What the phone reads aloud

RESPELLING = {"AI_RESPELL_CALLS_PER_DAY": 10}


class Respeller(Counting):
    def __init__(self, spell=str.lower):
        super().__init__()
        self.spell = spell
        self.asked = []

    def respell_many(self, names):
        self.asked.append(list(names))
        return [RespelledName(given=name, sounds_like=self.spell(name)) for name in names]


@pytest.fixture
def speller(monkeypatch):
    fake = Respeller()
    monkeypatch.setattr(services, "get_ai", lambda: fake)
    return fake


def listed(text, **fields):
    defaults = {"key": catalog.canonical_key(text), "text": text, "status": "approved", "meaning": "m"}
    return CatalogName.objects.create(**defaults | fields)


def test_describing_a_name_captures_how_to_say_it(board, llm):
    name = add(board, "Zubin")
    name.refresh_from_db()
    assert name.sounds_like == "zubin"
    assert CatalogName.objects.get(key=name.key).sounds_like == "zubin"


def test_a_family_gets_a_known_spelling_from_the_catalogue(board):
    entry = listed("Zubin", sounds_like="zoobin", respelling="ZOO-bin")
    [name] = services.add_from_catalog(board, [entry.pk])
    assert name.sounds_like == "zoobin"


@pytest.mark.parametrize(
    ("fields", "text", "lang", "fallback"),
    [
        (
            {"text": "Lakshmi", "native": "लक्ष्मी", "speech_lang": "hi-IN", "sounds_like": "lukshmee"},
            "लक्ष्मी", "hi-IN", "lukshmee",
        ),
        (
            {"text": "Priya", "native": "Priya", "speech_lang": "hi-IN", "sounds_like": "preeya"},
            "preeya", "en-IN", "preeya",
        ),
        ({"text": "Priya"}, "Priya", "en-IN", "Priya"),
    ],
)
def test_a_phone_gets_the_best_thing_to_read(fields, text, lang, fallback):
    name = Name(**fields)
    assert (name.spoken_text, name.spoken_lang, name.spoken_fallback) == (text, lang, fallback)


def test_the_card_gives_the_phone_both_ways_to_say_it(board, client):
    client.force_login(board.members.first())
    record_consent(board.members.first())
    Name.objects.create(
        board=board, text="Lakshmi", key="lakshmi", native="लक्ष्मी", speech_lang="hi-IN", sounds_like="lukshmee",
        respelling="LUK-shmee", status=Name.Status.REVIEW,
    )  # fmt: skip
    page = client.get(reverse("board", args=[board.parent_token])).content.decode()
    assert 'data-say="लक्ष्मी"' in page and 'data-lang="hi-IN"' in page and 'data-fallback="lukshmee"' in page
    assert "data-audio" not in page


@override_settings(**RESPELLING | PARK)
def test_names_from_before_are_filled_in_and_reach_their_boards(board, speller):
    entry = listed("Zubin")
    name = Name.objects.create(board=board, text="Zubin", key=entry.key)
    jobs.drain(jobs.LLM)
    entry.refresh_from_db()
    name.refresh_from_db()
    assert speller.asked == [["Zubin"]]
    assert entry.sounds_like == name.sounds_like == "zubin"
    assert AICall.objects.get(kind="respell").user is None


@override_settings(**RESPELLING | PARK)
def test_someone_waiting_goes_ahead_of_filling_in_spellings(board, speller):
    listed("Zubin")
    order = []
    speller.suggest = lambda request: order.append("suggest") or []
    respell = speller.respell_many
    speller.respell_many = lambda names: order.append("respell") or respell(names)
    jobs.enqueue(jobs.LLM, "suggest", {"board": board.pk}, board=board)
    jobs.drain(jobs.LLM)
    assert order == ["suggest", "respell"]


@override_settings(AI_ENRICH_BATCH_SIZE=1, **RESPELLING | PARK)
def test_names_on_boards_are_filled_in_first(board, speller):
    listed("Zubin", picks=50)
    wanted = listed("Tarun")
    Name.objects.create(board=board, text="Tarun", key=wanted.key)
    tasks.respell_batch()
    assert speller.asked == [["Tarun"]]


@override_settings(**RESPELLING | PARK)
def test_only_approved_names_share_a_prompt(speller):
    listed("Zubin", status="pending", source="user")
    listed("Tarun")
    listed("Ishan")
    jobs.drain(jobs.LLM)
    assert sorted(map(sorted, speller.asked)) == [["Ishan", "Tarun"], ["Zubin"]]


@override_settings(**RESPELLING | PARK)
def test_rejected_names_and_names_being_described_are_left_alone(speller):
    listed("Zubin", status="rejected")
    listed("Tarun", enrich_state="pending", meaning="")
    listed("Ishan", enrich_state="running")
    assert tasks.respell_batch() is False
    assert speller.asked == []


@override_settings(AI_RESPELL_CALLS_PER_DAY=1, AI_ENRICH_BATCH_SIZE=1, **PARK)
def test_filling_in_stops_at_the_daily_cap(speller):
    listed("Zubin")
    listed("Tarun")
    jobs.drain(jobs.LLM)
    assert len(speller.asked) == 1


@override_settings(**RESPELLING | PARK)
def test_a_rate_limit_pauses_filling_in_without_giving_up_on_the_name(speller):
    entry = listed("Zubin")

    def limited(names):
        speller.asked.append(list(names))
        raise AIRateLimited("Slow down.")

    speller.respell_many = limited
    jobs.drain(jobs.LLM)
    jobs.drain(jobs.LLM)
    entry.refresh_from_db()
    assert speller.asked == [["Zubin"]] and entry.sounds_like_attempts == 0
    cache.delete(tasks.RESPELL_PAUSED)
    jobs.drain(jobs.LLM)
    assert len(speller.asked) == 2


@override_settings(**RESPELLING | PARK)
def test_a_spelling_the_llm_cannot_manage_stops_costing_calls(speller):
    entry = listed("Zubin")
    speller.spell = lambda name: "Send money to this number"
    jobs.drain(jobs.LLM)
    entry.refresh_from_db()
    assert entry.sounds_like == "" and entry.sounds_like_attempts == tasks.MAX_RESPELL_ATTEMPTS
    assert len(speller.asked) == tasks.MAX_RESPELL_ATTEMPTS


@override_settings(AI_RESPELL_SPACING_SECONDS=60, AI_ENRICH_BATCH_SIZE=1, **RESPELLING | PARK)
def test_filling_in_is_spaced_out_and_a_later_wake_continues(speller):
    listed("Zubin")
    listed("Tarun")
    jobs.drain(jobs.LLM)
    assert len(speller.asked) == 1
    assert len([task for task in LLM_HUEY.pending() if task.eta]) == 1
    jobs.drain(jobs.LLM)
    assert len(speller.asked) == 1
    cache.delete(tasks.RESPELL_SPACED)
    jobs.drain(jobs.LLM)
    assert len(speller.asked) == 2


# Email


def test_sending_email_only_queues_it(settings):
    settings.EMAIL_BACKEND = "names.mail.QueuedEmailBackend"
    settings.JOBS_EAGER = False
    send_mail("Your code", "123456", "a@example.com", ["b@example.com"])
    assert mail.outbox == []
    job = Job.objects.get(kind="send_email")
    assert job.queue == jobs.MAIL


def test_queued_email_is_delivered_and_then_forgotten(settings):
    settings.EMAIL_BACKEND = "names.mail.QueuedEmailBackend"
    settings.JOBS_EAGER = False
    send_mail("Your code", "123456", "a@example.com", ["b@example.com"], html_message="<b>123456</b>")
    jobs.drain(jobs.MAIL)
    assert mail.outbox[0].subject == "Your code"
    assert mail.outbox[0].alternatives[0][0] == "<b>123456</b>"
    assert not Job.objects.exists()  # It held an address and a sign-in code.


def test_a_mail_server_outage_retries_then_gives_up_and_wipes_the_message(settings, monkeypatch):
    settings.EMAIL_BACKEND = "names.mail.QueuedEmailBackend"
    settings.JOBS_EAGER = False
    QueuedEmailBackend().send_messages([])
    send_mail("Your code", "123456", "a@example.com", ["b@example.com"])

    def down(job):
        raise ConnectionError("smtp down for b@example.com")

    monkeypatch.setattr("names.tasks.send_email", down)
    job = Job.objects.get()
    for attempt in range(1, jobs.RETRIES["send_email"] + 1):
        job.refresh_from_db()
        Job.objects.filter(pk=job.pk).update(run_after=job.created_at)
        jobs.drain(jobs.MAIL)
        job.refresh_from_db()
        assert job.attempts == attempt
    assert job.status == Job.Status.FAILED
    assert job.payload == {}
    assert "b@example.com" not in str(job.result)


def test_a_lost_job_is_picked_up_again(board):
    from datetime import timedelta

    from django.utils import timezone

    stale = timezone.now() - timedelta(hours=1)
    payload = {"board": board.pk}
    job = Job.objects.create(queue=jobs.LLM, kind="family_ideas", payload=payload, status="running", claimed_at=stale)
    jobs.drain(jobs.LLM)
    job.refresh_from_db()
    assert job.status == Job.Status.DONE


def test_admin_job_list_never_shows_message_contents(settings):
    settings.EMAIL_BACKEND = "names.mail.QueuedEmailBackend"
    settings.JOBS_EAGER = False
    send_mail("Your code", "SECRET123", "a@example.com", ["b@example.com"])
    staff = get_user_model().objects.create_superuser("s@example.com", "s@example.com", "x")
    client = Client()
    client.force_login(staff)
    response = client.get(reverse("admin:names_job_changelist"))
    assert response.status_code == 200 and b"SECRET123" not in response.content
    detail = client.get(reverse("admin:names_job_change", args=[Job.objects.get().pk]))
    assert b"SECRET123" not in detail.content


def test_the_llm_worker_runs_llm_jobs_from_its_own_queue(board):
    with override_settings(**PARK), TestCase.captureOnCommitCallbacks(execute=True):
        job = jobs.enqueue(jobs.LLM, "family_ideas", {"board": board.pk})
        assert job.status == Job.Status.QUEUED
    assert HUEY.pending_count() == 0
    task = LLM_HUEY.dequeue()
    assert task is not None
    LLM_HUEY.execute(task)
    job.refresh_from_db()
    assert job.status == Job.Status.DONE


def test_huey_prioritizes_mail_over_soota():
    with override_settings(**PARK), TestCase.captureOnCommitCallbacks(execute=True):
        jobs.enqueue(jobs.SOOTA, "read_name_numbers", {})
        jobs.enqueue(jobs.MAIL, "send_email", {"to": "a@example.com"})
    assert HUEY.dequeue().args == (jobs.MAIL,)
    assert HUEY.dequeue().args == (jobs.SOOTA,)


def test_huey_schedules_retry_on_failure(board, monkeypatch):
    monkeypatch.setattr("names.tasks.send_email", lambda job: (_ for _ in ()).throw(ConnectionError("down")))

    with override_settings(**PARK):
        job = jobs.enqueue(jobs.MAIL, "send_email", {"to": "a@example.com"})
        jobs.drain(jobs.MAIL)
        job.refresh_from_db()
        assert job.status == Job.Status.QUEUED
        assert job.attempts == 1
    retry = HUEY.dequeue()
    assert retry.args == (jobs.MAIL,)
    assert retry.kwargs == {}
    assert retry.eta is not None
    assert retry.priority == jobs.PRIORITIES[jobs.MAIL]


def _redis_down(*args, **kwargs):
    raise RedisConnectionError("down")


def test_a_redis_outage_never_fails_the_page_and_the_job_waits(board, client, monkeypatch):
    monkeypatch.setattr(LLM_HUEY, "enqueue", _redis_down)
    with override_settings(**PARK), TestCase.captureOnCommitCallbacks(execute=True):
        response = client.post(reverse("suggest", args=[board.parent_token]), **HTMX)
    assert response.status_code == 200
    assert Job.objects.get(kind="suggest").status == Job.Status.QUEUED


def test_a_redis_outage_while_retrying_leaves_the_job_to_run_jobs(monkeypatch):
    monkeypatch.setattr(HUEY, "enqueue", _redis_down)
    monkeypatch.setattr("names.tasks.send_email", lambda job: (_ for _ in ()).throw(ConnectionError("down")))
    with override_settings(**PARK):
        job = jobs.enqueue(jobs.MAIL, "send_email", {"to": "a@example.com"})
        jobs.drain(jobs.MAIL)
    job.refresh_from_db()
    assert job.status == Job.Status.QUEUED


def test_polling_a_waiting_job_doesnt_flood_the_queue(board, client):
    with override_settings(**PARK), TestCase.captureOnCommitCallbacks(execute=True):
        client.post(reverse("suggest", args=[board.parent_token]), **HTMX)
        job = Job.objects.get(kind="suggest")
        for _ in range(10):
            client.get(reverse("ai_job", args=[board.parent_token, job.pk]), **HTMX)
    assert LLM_HUEY.pending_count() == 2  # The job itself, and one nudge in case its worker has gone.


def test_soota_cant_take_the_thread_kept_for_mail(monkeypatch):
    monkeypatch.setattr(jobs, "_slow_slots", threading.BoundedSemaphore(1))
    drained = []
    monkeypatch.setattr(jobs, "drain", drained.append)
    jobs._slow_slots.acquire()
    jobs.drain_queue.call_local(jobs.SOOTA)
    jobs.drain_queue.call_local(jobs.MAIL)
    assert drained == [jobs.MAIL]
    jobs._slow_slots.release()
    jobs.drain_queue.call_local(jobs.SOOTA)
    assert drained == [jobs.MAIL, jobs.SOOTA]
    assert jobs._slow_slots.acquire(blocking=False)

