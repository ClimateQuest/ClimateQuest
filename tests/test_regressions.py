"""Regression tests for bugs found while auditing the test suite.

Several of them only crash on PostgreSQL, which enforces ``max_length`` while SQLite does not. These tests
therefore check the stored values and the validation messages, so that they fail on both databases.
"""

import html
from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, RequestFactory
from django.utils import timezone

from aktionen.models import Aktion, AktionenListe
from artikel.models import Artikel
from artikel.models import Category as ArtikelCategory
from core.all_messages import all_messages
from core.middleware import language_middleware
from core.views import create_reminder
from events.models import Event
from family.models import Family
from home.models import Benachrichtigung
from personals.models import IOSDevice, UserErweitert
from petitions.models import Category as PetitionCategory
from petitions.models import Petition, Update
from tests.conftest import LangClient, flash
from users.sitemaps import LevelViewSitemap
from utils import functions as fn
from utils.functions import create_internal_error, create_notification
from utils.groups import MAX_FIELD_LENGTH

pytestmark = pytest.mark.usefixtures("no_push")

HOSTILE_TEXT = '<a href="https://evil.example">click</a>'


def msg(key: str) -> str:
    return str(all_messages[key])


@pytest.fixture
def petition(user: User) -> Petition:
    return Petition.objects.create(
        title_de="Rettet",
        title_en="Save",
        content_de="c",
        content_en="c",
        goal=10,
        creator=user,
        category=PetitionCategory.objects.create(title="Umwelt"),
    )


def assert_no_hostile_markup(notification: Benachrichtigung) -> None:
    for text in (notification.benachrichtigung_de, notification.benachrichtigung_en):
        assert "<a href" not in text
        assert "&lt;a href" in text


# ---- sitemaps --------------------------------------------------------------------------------------


def test_level_sitemap_links_to_the_level_page(user: User) -> None:
    assert LevelViewSitemap().location(user) == f"/users/level/{user.id}/"


# ---- secrets and malformed input ---------------------------------------------------------------------


def test_error_log_never_contains_credentials_or_ip_addresses(user: User, settings, tmp_path) -> None:
    settings.LOGS_DIR = tmp_path
    request = RequestFactory().get(
        "/",
        HTTP_COOKIE="sessionid=top-secret-session",
        HTTP_AUTHORIZATION="Bearer top-secret-token",
        HTTP_X_CSRFTOKEN="top-secret-csrf",
        HTTP_X_FORWARDED_FOR="203.0.113.7",
        HTTP_USER_AGENT="kept-agent",
    )
    request.user = user
    create_internal_error(request, "boom", throw_exception=False)
    log = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "top-secret" not in log and "203.0.113.7" not in log
    assert "kept-agent" in log


@pytest.mark.parametrize("header", ["de;q=.", "de;q=1.2.3", "en;q=..,de;q=0.5"])
def test_malformed_accept_language_header_does_not_crash(db, header: str) -> None:
    response = Client().get("/support/", HTTP_ACCEPT_LANGUAGE=header)
    assert response.status_code == 302


def test_malformed_quality_ranks_last() -> None:
    assert language_middleware.parse_accept_language_header("fr;q=.,de;q=0.5") == [("de", 0.5), ("fr", 0.0)]


def test_event_receiver_id_with_unicode_digit_is_rejected(client: LangClient, staff: User) -> None:
    client.force_login(staff)
    response = client.post("/admin/", {"receiver": "event-participants", "name": "²", "msg_de": "d", "msg_en": "e"})
    assert flash(response) == [msg("admin__event_not_found")]


def test_astronomically_large_tracked_distance_is_rejected(logged_in: LangClient, action_type: AktionenListe) -> None:
    action_type.track_kilometerly = True
    action_type.save()
    response = logged_in.post(
        "/aktionen/track/", {"kilometerly_track": "1", "distance": "9" * 400, "action_id": action_type.id}
    )
    assert flash(response) == [msg("internal_error")]
    assert not Aktion.objects.exists()


# ---- database column limits ------------------------------------------------------------------------


def test_long_notifications_fit_into_the_database_but_are_mailed_completely(make_user: Callable[..., User]) -> None:
    receiver = make_user("receiver", email="r@x.de", mail_verified=True)
    request = RequestFactory().get("/")
    request.user = receiver
    create_notification(request, "d" * 900, "e" * 900, user=receiver)
    stored = Benachrichtigung.objects.get()
    assert len(stored.benachrichtigung_de) == 500
    assert len(stored.benachrichtigung_en) == 500
    assert "d" * 900 in mail.outbox[0].body


def test_long_family_chat_message_creates_storable_notifications(
    logged_in: LangClient, user: User, other: User
) -> None:
    family = Family.objects.create(name="Fam", password="p", admin_password="a")
    family.members.add(user, other)
    logged_in.post(f"/family/family/{family.id}/chat/", {"message": "x" * 2000})
    assert len(Benachrichtigung.objects.get(user=other).benachrichtigung_de) <= 500


@pytest.mark.parametrize("field, length", [("app_version", 51), ("bundle_id", 201), ("apns_token", 201)])
def test_ios_device_values_longer_than_their_column_are_rejected(
    client: LangClient, user: User, field: str, length: int
) -> None:
    payload = {"user_id": user.id, "apns_token": "tok", field: "x" * length}
    response = client.post("/personals/api/devices/ios/register/", payload, content_type="application/json")
    assert response.status_code == 400
    assert not IOSDevice.objects.exists()


@pytest.mark.parametrize("field", ["title_de", "title_en"])
def test_new_petition_with_too_long_title_is_rejected(logged_in: LangClient, db, field: str) -> None:
    PetitionCategory.objects.create(title="Umwelt")
    data = {
        "is_truth": "on",
        "title_de": "t",
        "title_en": "t",
        "content_de": "c",
        "content_en": "c",
        "goal": "20",
        "category": "Umwelt",
        field: "x" * (MAX_FIELD_LENGTH + 1),
    }
    response = logged_in.post("/petitions/add_petition/", data)
    assert flash(response) == [msg("too_long_input")]
    assert not Petition.objects.exists()


@pytest.mark.parametrize("field", ["title_de", "title_en"])
def test_petition_update_with_too_long_title_is_rejected(logged_in: LangClient, petition: Petition, field: str) -> None:
    data = {
        "update": "1",
        "is_truth": "on",
        "title_de": "t",
        "title_en": "t",
        "content_de": "c",
        "content_en": "c",
        field: "x" * (MAX_FIELD_LENGTH + 1),
    }
    response = logged_in.post(f"/petitions/edit_petition/{petition.id}/", data)
    assert flash(response) == [msg("too_long_input")]
    assert not Update.objects.exists()


def test_too_long_block_message_is_rejected(client: LangClient, staff: User, user: User) -> None:
    artikel = Artikel.objects.create(
        name_de="a",
        name_en="a",
        content_de="c",
        content_en="c",
        creator=user,
        category=ArtikelCategory.objects.create(title="c"),
    )
    client.force_login(staff)
    response = client.post(f"/artikel/artikel_detail/{artikel.id}", {"block": "1", "block_msg": "x" * 101})
    artikel.refresh_from_db()
    assert flash(response) == [msg("too_long_input")]
    assert not artikel.blocked


def test_email_address_longer_than_its_column_is_invalid() -> None:
    domain = ".".join(["d" * 60] * 4)
    assert not fn.ist_email_gueltig(f"{'a' * 20}@{domain}.de")


# ---- account state ------------------------------------------------------------------------------------


def test_failed_email_change_keeps_an_unverified_address_unverified(
    logged_in: LangClient, user: User, smtp_down: None
) -> None:
    user.email = "old@x.de"
    user.save()
    logged_in.post("/personals/settings/", {"change_email": "1", "email": "new@x.de"})
    user.refresh_from_db()
    assert user.email == "old@x.de"
    assert UserErweitert.objects.get(user=user).mail_verified is False


def test_new_password_is_readable_in_the_html_mail(
    client: LangClient, make_user: Callable[..., User], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_user("target", email="t@x.de")
    password = "ab<cd&amp;ef"
    monkeypatch.setattr("personals.views.generate_random_password", lambda: password)
    client.post("/personals/reset_password/", {"email": "t@x.de"})
    html_body = mail.outbox[0].alternatives[0][0]
    shown = html_body.split("<b>")[1].split("</b>")[0]
    assert "<" not in shown
    assert html.unescape(shown) == password
    target.refresh_from_db()
    assert target.check_password(password)


# ---- user input inside notification HTML --------------------------------------------------------------


def test_event_name_is_escaped_in_edit_and_delete_notifications(logged_in: LangClient, user: User, other: User) -> None:
    event = Event.objects.create(
        name=HOSTILE_TEXT, description="d", adress="a", date_time=timezone.now(), duration=1, creator=user
    )
    event.participants.add(other)
    tomorrow = (timezone.now() + timezone.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    form = {"is_truth": "on", "name": HOSTILE_TEXT, "content": "d", "date_time": tomorrow, "duration": "1"}
    logged_in.post(f"/events/edit/{event.id}/", {"edit": "1", "adress": "a", **form})
    logged_in.post(f"/events/edit/{event.id}/", {"delete": "1"})
    notifications = Benachrichtigung.objects.filter(user=other)
    assert notifications.count() == 2
    for notification in notifications:
        assert_no_hostile_markup(notification)


@pytest.mark.parametrize("action", ["update", "success", "success_removed"])
def test_petition_title_is_escaped_in_notifications(
    logged_in: LangClient, other: User, petition: Petition, action: str
) -> None:
    Petition.objects.filter(id=petition.id).update(
        title_de=HOSTILE_TEXT, title_en=HOSTILE_TEXT, success=action == "success_removed"
    )
    petition.signs.add(other)
    button = "update" if action == "update" else "success"
    form = {"is_truth": "on", "title_de": "u", "title_en": "u", "content_de": "c", "content_en": "c"}
    logged_in.post(f"/petitions/edit_petition/{petition.id}/", {button: "1", **form})
    assert_no_hostile_markup(Benachrichtigung.objects.get(user=other))


def test_username_is_escaped_when_an_account_is_deleted(logged_in: LangClient, user: User, other: User) -> None:
    User.objects.filter(id=user.id).update(username=HOSTILE_TEXT)
    family = Family.objects.create(name="Fam", password="p", admin_password="a")
    family.members.add(user, other)
    logged_in.post("/personals/settings/", {"delete_account": "1"})
    assert_no_hostile_markup(Benachrichtigung.objects.get(user=other))


def test_username_is_escaped_in_the_weekly_reminder(make_user: Callable[..., User]) -> None:
    make_user(HOSTILE_TEXT)
    create_reminder()
    assert_no_hostile_markup(Benachrichtigung.objects.get())


def test_family_cannot_be_renamed_to_the_reserved_ranking_name(
    logged_in: LangClient, user: User, worldwide: Family
) -> None:
    worldwide.delete()
    family = Family.objects.create(name="Fam", password="p", admin_password="admin")
    family.members.add(user)
    response = logged_in.post(
        f"/family/family/{family.id}/edit/",
        {"change_familyname": "1", "admin_password_familyname": "admin", "new_familyname": "worldwide ranking"},
    )
    family.refresh_from_db()
    assert flash(response) == [msg("family_name_forbidden")]
    assert family.name == "Fam"
