"""Exact limits and error cases of forms, commands, validators, cron jobs and settings."""

import ast
import datetime
import io
from collections.abc import Callable
from unittest import mock

import pytest
from dateutil.relativedelta import relativedelta
from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils import timezone
from django.utils.module_loading import import_string
from PIL import Image

from aktionen import services
from aktionen.models import Aktion, AktionenListe, TrackedActions
from aktionen.views import WEEKLY_TRACKING_INTERVAL_DAYS, add_weekly_tracking_action
from artikel.models import Artikel
from artikel.models import Category as ArtikelCategory
from core.all_messages import all_messages
from core.middleware.generate_csp_nonce_middleware import GenerateCSPNonceMiddleware
from core.validators import validate_image
from core.views import create_reminder
from events.models import Event
from home.models import Benachrichtigung
from home.views import total_klimapunkte_of_all_users
from personals.models import IOSDevice, TreeCodes, UserErweitert
from personals.tokens import email_verification_token
from petitions.models import Category as PetitionCategory
from petitions.models import Petition
from petitions.views import MAX_GOAL, MIN_GOAL
from tests.conftest import LangClient, flash

pytestmark = pytest.mark.usefixtures("no_push")

TODAY = timezone.localdate


def msg(key: str) -> str:
    return str(all_messages[key])


# ---- actions ---------------------------------------------------------------------------------------------


def action_post(action_type: AktionenListe, **overrides: str) -> dict[str, str]:
    return {
        "is_truth": "on",
        "action_type_id": str(action_type.id),
        "action_date": TODAY().isoformat(),
        "action_quantity": "1",
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"action_quantity": "50"},  # exactly the daily maximum
        {"action_description": "x" * services.MAX_DESCRIPTION_LENGTH},
        {"action_date": (TODAY() - relativedelta(months=1)).isoformat()},  # oldest allowed day
    ],
)
def test_actions_at_the_limit_are_accepted(user: User, action_type: AktionenListe, overrides: dict) -> None:
    assert services.validate_action_input(user, action_post(action_type, **overrides)).action_type == action_type


@pytest.mark.parametrize(
    "overrides, key",
    [
        ({"action_quantity": "50.0001"}, "max_action_quantity"),
        ({"action_quantity": "0.00001"}, "invalid_quantity"),  # rounds to zero
        ({"action_date": (TODAY() - relativedelta(months=1, days=1)).isoformat()}, "action_too_past"),
    ],
)
def test_actions_beyond_the_limit_are_rejected(
    user: User, action_type: AktionenListe, overrides: dict, key: str
) -> None:
    with pytest.raises(services.ActionInputError) as error:
        services.validate_action_input(user, action_post(action_type, **overrides))
    assert error.value.message == all_messages[key]


def test_actions_of_other_users_never_conflict(
    user: User, other: User, action_type: AktionenListe, make_aktion: Callable[..., Aktion]
) -> None:
    make_aktion(other, action_type, 50, date=TODAY())
    services.validate_action_input(user, action_post(action_type, action_quantity="50"))


@pytest.mark.parametrize(
    "days_ago, added", [(WEEKLY_TRACKING_INTERVAL_DAYS - 1, False), (WEEKLY_TRACKING_INTERVAL_DAYS, True)]
)
def test_weekly_tracking_waits_for_the_interval(
    user: User, action_type: AktionenListe, days_ago: int, added: bool
) -> None:
    TrackedActions.objects.create(action=action_type, user=user, since=TODAY() - datetime.timedelta(days=days_ago))
    add_weekly_tracking_action()
    assert Aktion.objects.exists() is added


def test_weekly_reminder_stops_exactly_at_the_goal(
    make_user: Callable[..., User], action_type: AktionenListe, make_aktion: Callable[..., Aktion]
) -> None:
    reached = make_user("reached")
    make_aktion(reached, action_type, 10, date=TODAY())  # exactly 100 % of the goal
    make_user("english", lang="en")
    create_reminder()
    assert [n.user.username for n in Benachrichtigung.objects.all()] == ["english"]


# ---- content limits -------------------------------------------------------------------------------------


@pytest.mark.parametrize("goal", [MIN_GOAL, MAX_GOAL])
def test_petition_goals_at_the_limit_are_accepted(logged_in: LangClient, db, goal: int) -> None:
    PetitionCategory.objects.create(title="Umwelt")
    data = {name: "x" * 100 for name in ("title_de", "title_en", "content_de", "content_en")}
    logged_in.post("/petitions/add_petition/", {"is_truth": "on", "category": "Umwelt", "goal": str(goal), **data})
    assert Petition.objects.get().goal == goal


def test_article_titles_of_exactly_one_hundred_characters_are_accepted(logged_in: LangClient, db) -> None:
    ArtikelCategory.objects.create(title="Klima")
    data = {"name_de": "x" * 100, "name_en": "y" * 100, "content_de": "c", "content_en": "c"}
    logged_in.post("/artikel/add_artikel/", {"is_truth": "on", "category": "Klima", **data})
    assert Artikel.objects.get().name_en == "y" * 100


def event_post(**overrides: str) -> dict[str, str]:
    date_time = (timezone.localtime() + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    return {
        "is_truth": "on",
        "name": "n",
        "content": "c",
        "date_time": date_time,
        "duration": "0",
        "adress": "a" * 100,
        **overrides,
    }


def test_events_may_have_no_duration_and_a_full_length_address(logged_in: LangClient) -> None:
    logged_in.post("/events/add/", event_post())
    assert Event.objects.get().duration == 0


@pytest.mark.parametrize(
    "overrides, key",
    [
        ({"adress": "a" * 101}, "too_long_input"),
        ({"duration": "inf"}, "invalid_duration"),
        ({"duration": ""}, "missing_required_inputs"),
        (
            {"date_time": (timezone.localtime() + datetime.timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M")},
            "date_must_be_tomorrow",
        ),
    ],
)
def test_events_beyond_the_limit_are_rejected(logged_in: LangClient, overrides: dict, key: str) -> None:
    assert flash(logged_in.post("/events/add/", event_post(**overrides))) == [msg(key)]
    assert not Event.objects.exists()


@pytest.mark.parametrize("length, accepted", [(500, True), (501, False)])
def test_admin_notification_length(client: LangClient, staff: User, length: int, accepted: bool) -> None:
    client.force_login(staff)
    client.post("/admin/", {"receiver": "user", "name": staff.username, "msg_de": "d" * length, "msg_en": "e"})
    assert Benachrichtigung.objects.exists() is accepted


def test_total_points_of_an_empty_database(db) -> None:
    assert total_klimapunkte_of_all_users() == 0


# ---- accounts -----------------------------------------------------------------------------------------------


def settings_post(client: LangClient, **data: str):
    return client.post("/personals/settings/", data)


@pytest.mark.parametrize("goal", ["1", "100000"])
def test_weekly_goals_at_the_limit_are_accepted(logged_in: LangClient, user: User, goal: str) -> None:
    settings_post(logged_in, change_weekly_goal="1", weekly_goal=goal)
    assert UserErweitert.objects.get(user=user).weekly_goal == int(goal)


def test_usernames_of_maximum_length_are_accepted(logged_in: LangClient, user: User) -> None:
    settings_post(logged_in, change_username="1", new_username="n" * 150)
    user.refresh_from_db()
    assert user.username == "n" * 150


def test_settings_stay_open_for_two_days_after_the_login(logged_in: LangClient, user: User) -> None:
    User.objects.filter(id=user.id).update(last_login=timezone.now() - datetime.timedelta(days=2))
    assert logged_in.get("/personals/settings/").status_code == 200


def test_activation_link_stops_working_after_an_email_change(user: User) -> None:
    user.email = "first@x.de"
    token = email_verification_token.make_token(user)
    assert email_verification_token.check_token(user, token)
    user.email = "second@x.de"
    assert not email_verification_token.check_token(user, token)


@pytest.mark.parametrize("days, expected", [("1", 1), ("3650", 3650), ("3651", 30), ("", 30), ("1.5", 30)])
def test_statistics_day_limits(logged_in: LangClient, days: str, expected: int) -> None:
    assert logged_in.get(f"/personals/statistics/?days={days}").context["days"] == expected


def test_statistics_cannot_break_out_of_the_script_tag(
    logged_in: LangClient, user: User, action_type: AktionenListe, make_aktion: Callable[..., Aktion]
) -> None:
    action_type.category.name = "</script><script>alert(1)</script>"
    action_type.category.save()
    make_aktion(user, action_type, 1, date=TODAY())
    chart_data = logged_in.get("/personals/statistics/").context["chart_data"]
    assert "<" not in chart_data and "\\u003c/script>" in chart_data


def test_credits_never_exceed_the_earned_points_after_deleting_actions(
    logged_in: LangClient, user: User, action_type: AktionenListe, make_aktion: Callable[..., Aktion]
) -> None:
    for code in ("a", "b"):
        TreeCodes.objects.create(code=code, redeemURL="https://x.de", sponsor="s")
    aktion = make_aktion(user, action_type, 250)  # exactly one credit
    logged_in.post("/personals/credits/")
    aktion.delete()
    logged_in.post("/personals/credits/")
    assert TreeCodes.objects.filter(planted=True).count() == 1


@pytest.mark.parametrize("user_id", ["9" * 30, -1, 1.5, True, [1]])
def test_ios_registration_with_strange_user_ids_finds_nobody(client: LangClient, db, user_id: object) -> None:
    payload = {"user_id": user_id, "apns_token": "t"}
    response = client.post("/personals/api/devices/ios/register/", payload, content_type="application/json")
    assert response.status_code == 404
    assert not IOSDevice.objects.exists()


# ---- middleware, validators and commands ----------------------------------------------------------------------


def test_csp_header_allows_exactly_the_nonces_of_the_request() -> None:
    request = RequestFactory().get("/")
    response = GenerateCSPNonceMiddleware(lambda _request: HttpResponse())(request)
    policy = response["Content-Security-Policy"]
    assert f"script-src 'self' 'nonce-{request.csp_script_nonce}'" in policy
    assert f"style-src 'self' 'nonce-{request.csp_style_nonce}'" in policy
    assert request.csp_script_nonce != request.csp_style_nonce
    assert "unsafe-inline" not in policy


def image_upload(name: str, image_format: str, size: int | None = None) -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, image_format)
    content = buffer.getvalue()
    if size:
        content += b"\0" * (size - len(content))
    return SimpleUploadedFile(name, content)


@pytest.mark.parametrize(
    "name, image_format", [("a.jpg", "JPEG"), ("a.JPEG", "JPEG"), ("a.b.webp", "WEBP"), ("a.avif", "AVIF")]
)
def test_allowed_images_stay_readable(name: str, image_format: str) -> None:
    upload = image_upload(name, image_format)
    validate_image(upload)
    assert upload.tell() == 0


def test_image_of_exactly_five_megabytes_is_allowed() -> None:
    validate_image(image_upload("a.png", "PNG", size=5 * 1024 * 1024))
    with pytest.raises(ValidationError, match="Maximal 5 MB"):
        validate_image(image_upload("a.png", "PNG", size=5 * 1024 * 1024 + 1))


@pytest.mark.parametrize(
    "name, image_format, message",
    [("noextension", "PNG", "Dateiendung"), ("a.png", "BMP", "Bildtyp")],
)
def test_rejected_images_say_why(name: str, image_format: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_image(image_upload(name, image_format))


@pytest.mark.parametrize(
    "content, error",
    [
        ('{ members: ["a"] }', KeyError),
        ('{ short_name: "pw" }', KeyError),
        ("not json5 {", ValueError),
    ],
)
def test_company_import_of_a_broken_file_creates_nobody(db, worldwide, tmp_path, content: str, error: type) -> None:
    source = tmp_path / "company.json5"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(error):
        call_command("add_company_without_user_family_relationship", str(source))
    assert not User.objects.exists()


def test_company_import_is_all_or_nothing(worldwide, tmp_path) -> None:
    source = tmp_path / "company.json5"
    source.write_text('{ short_name: "pw", members: ["anna", "ben"] }', encoding="utf-8")
    with (
        mock.patch.object(UserErweitert.objects, "create", side_effect=[mock.DEFAULT, RuntimeError("db down")]),
        pytest.raises(RuntimeError),
    ):
        call_command("add_company_without_user_family_relationship", str(source))
    assert not User.objects.exists()


@pytest.mark.parametrize("content", ["code\nA1\n", "redeemURL\nhttps://x.de\n"])
def test_tree_code_import_needs_both_columns(db, tmp_path, content: str) -> None:
    from django.core.management.base import CommandError

    source = tmp_path / "codes.csv"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(CommandError, match="code und redeemURL"):
        call_command("import_tree_codes_from_csv", str(source), "s")
    assert not TreeCodes.objects.exists()


# ---- production settings --------------------------------------------------------------------------------------------


def production_value(name: str) -> object:
    """A literal setting of ``settingsprod``, read without importing it (that would change the test settings)."""
    source = (django_settings.BASE_DIR / "ClimateQuest" / "settingsprod.py").read_text(encoding="utf-8")
    assignments = (node for node in ast.parse(source).body if isinstance(node, ast.Assign))
    return next(ast.literal_eval(node.value) for node in assignments if node.targets[0].id == name)


def test_every_cron_job_points_to_an_existing_function() -> None:
    jobs = production_value("CRONJOBS")
    assert len(jobs) == 3
    for _schedule, dotted_path, _redirect in jobs:
        assert callable(import_string(dotted_path))
