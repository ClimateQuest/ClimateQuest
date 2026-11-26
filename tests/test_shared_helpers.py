"""Direct tests of the helpers shared by several apps: time periods, groups, action feedback and templates."""

import datetime
from collections.abc import Callable, Iterator
from unittest import mock

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpRequest
from django.test import RequestFactory
from django.urls import reverse

from aktionen.models import Aktion, AktionenListe
from core.all_messages import all_messages
from core.context_processors import notification_count
from core.templatetags.html_math import percent
from core.templatetags.safe_html import safe_html
from forum.templatetags.request_user_has_answered_post import request_user_has_answered_post
from home.models import Benachrichtigung, ReportedUser
from personals.models import IOSDevice, Level
from utils import functions as fn
from utils.action_feedback import report_action_change
from utils.groups import MAX_FIELD_LENGTH, escaped, notify, require_admin_password, too_long
from utils.periods import InvalidPeriod, Period, default_period, format_date, parse_period

# A Wednesday, so that "the running week" has days before and after it.
TODAY = datetime.date(2026, 3, 18)
MONDAY = datetime.date(2026, 3, 16)


def make_request(user: User | AnonymousUser | None = None, language: str = "de") -> HttpRequest:
    request = RequestFactory().get("/")
    request.user = user or AnonymousUser()
    request.LANGUAGE_CODE = language
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def flashed(request: HttpRequest) -> list[str]:
    return [str(message) for message in get_messages(request)]


# ---- periods ---------------------------------------------------------------------------------------------


def custom(start: str, end: str) -> dict[str, str]:
    return {"zeitraum": "Benutzerdefiniert", "start_date": start, "end_date": end}


def test_period_without_selection_is_the_total_period() -> None:
    assert parse_period({}, "de", TODAY) == Period(text="Gesamt") == default_period()


@pytest.mark.parametrize(
    "zeitraum, days",
    [("Heute", 0), ("Sieben Tage", 7), ("Dreißig Tage", 30), ("Dreihundertfünfundsechzig Tage", 365)],
)
def test_relative_periods_end_today(zeitraum: str, days: int) -> None:
    period = parse_period({"zeitraum": zeitraum}, "de", TODAY)
    assert (period.start, period.end, period.is_total) == (TODAY - datetime.timedelta(days=days), TODAY, False)


def test_custom_period_may_be_a_single_day_ending_today() -> None:
    period = parse_period(custom(TODAY.isoformat(), TODAY.isoformat()), "de", TODAY)
    assert (period.start, period.end, period.is_total) == (TODAY, TODAY, False)
    assert period.text == "18.03.2026 bis 18.03.2026"


@pytest.mark.parametrize(
    "post, key",
    [
        (custom("", "2026-03-01"), "missing_required_inputs"),
        (custom("2026-03-01", ""), "missing_required_inputs"),
        (custom("2026-03-01", "01.03.2026"), "invalid_date"),
        (custom("2026-02-29", "2026-03-01"), "invalid_date"),
        (custom("2026-03-02", "2026-03-01"), "invalid_date_range"),
        (custom("2026-03-01", "2026-03-19"), "date_in_future"),
        ({"zeitraum": ""}, "invalid_time_period"),
    ],
)
def test_invalid_periods_explain_the_problem(post: dict[str, str], key: str) -> None:
    with pytest.raises(InvalidPeriod) as error:
        parse_period(post, "de", TODAY)
    assert error.value.message == all_messages[key]


@pytest.mark.parametrize("language, expected", [("de", "05.04.2026"), ("en", "04/05/2026")])
def test_format_date_follows_the_language(language: str, expected: str) -> None:
    assert format_date(datetime.date(2026, 4, 5), language) == expected


@pytest.mark.parametrize("zeitraum, days", [("Dreißig Tage", 30), ("Dreihundertfünfundsechzig Tage", 365)])
def test_long_date_ranges(zeitraum: str, days: int) -> None:
    assert fn.get_date_range(zeitraum, TODAY) == (TODAY - datetime.timedelta(days=days), TODAY)


# ---- groups ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values, expected",
    [
        ((), False),
        ((None, ""), False),
        (("x" * MAX_FIELD_LENGTH,), False),
        (("ok", "x" * (MAX_FIELD_LENGTH + 1)), True),
    ],
)
def test_too_long_flags_only_values_beyond_the_column(values: tuple, expected: bool) -> None:
    request = make_request()
    assert too_long(request, *values) is expected
    assert flashed(request) == ([str(all_messages["too_long_input"])] if expected else [])


@pytest.mark.parametrize(
    "password, required, key",
    [
        (None, (), "missing_required_inputs"),
        ("", ("name",), "missing_required_inputs"),
        ("admin", ("name", ""), "missing_required_inputs"),
        ("admin", (None,), "missing_required_inputs"),
        ("wrong", ("name",), "invalid_admin_password"),
        ("admin", ("name",), None),
        ("admin", (), None),
    ],
)
def test_require_admin_password(password: str | None, required: tuple, key: str | None) -> None:
    from django.contrib.auth.hashers import make_password

    request = make_request()
    assert require_admin_password(request, password, make_password("admin"), *required) is (key is None)
    assert flashed(request) == ([str(all_messages[key])] if key else [])


@pytest.mark.parametrize("url", ["/fixed/", None, lambda user: f"/for/{user}/"])
def test_notify_sends_one_notification_per_user(url: str | Callable | None) -> None:
    request = make_request()
    with mock.patch("utils.groups.create_notification") as create:
        notify(request, ["anna", "ben"], "de", "en", url=url)
    expected = [url(user) if callable(url) else url for user in ("anna", "ben")]
    assert [call.args for call in create.call_args_list] == [
        (request, "de", "en", "anna"),
        (request, "de", "en", "ben"),
    ]
    assert [call.kwargs["url"] for call in create.call_args_list] == expected


def test_escaped_neutralises_markup_of_any_value() -> None:
    assert escaped("<b>\"&'</b>") == "&lt;b&gt;&quot;&amp;&#x27;&lt;/b&gt;"
    assert escaped(5) == "5"


# ---- action feedback --------------------------------------------------------------------------------------------------


@pytest.fixture
def announced() -> Iterator[mock.MagicMock]:
    with mock.patch("utils.action_feedback.create_notification") as create:
        yield create


def snapshot(level_number: int, streak: int, has_level: bool = True) -> tuple[dict, int]:
    level = Level(klimapunkte=0, description="Baum", description_en="Tree") if has_level else None
    return {"current_level": level, "level_number": level_number}, streak


ACTION_TYPE = AktionenListe(name="Fahrrad", name_en="Bike")


@pytest.mark.parametrize(
    "verb, old, new, expected",
    [
        ("created", snapshot(1, 1), snapshot(1, 1), []),
        ("created", snapshot(1, 0), snapshot(2, 1), ["aufgestiegen", "verlängert"]),
        ("created", snapshot(2, 2), snapshot(1, 1), []),  # creating an action can never cost points
        ("edited", snapshot(1, 0), snapshot(2, 1), ["aufgestiegen", "verlängert"]),
        ("edited", snapshot(2, 1), snapshot(1, 0), ["abgestiegen", "verkürzt"]),
        ("deleted", snapshot(2, 1), snapshot(1, 0), ["abgestiegen", "verkürzt"]),
        ("deleted", snapshot(1, 0), snapshot(2, 1), []),
        ("edited", snapshot(1, 0), snapshot(0, 1, has_level=False), ["verlängert"]),
    ],
)
def test_only_relevant_changes_are_announced(
    announced: mock.MagicMock, verb: str, old: tuple, new: tuple, expected: list[str]
) -> None:
    request = make_request()
    report_action_change(request, verb, ACTION_TYPE, old, new)
    texts = [call.args[1] for call in announced.call_args_list]
    assert len(texts) == len(expected)
    for text, word in zip(texts, expected, strict=True):
        assert word in text and "Fahrrad" in text
    assert [message for message in flashed(request) if message != "request-review"] == texts


def test_announcements_are_flashed_in_the_language_of_the_request(announced: mock.MagicMock) -> None:
    request = make_request(language="en")
    report_action_change(request, "created", ACTION_TYPE, snapshot(1, 0), snapshot(2, 0))
    german, english = announced.call_args.args[1:3]
    assert "Baum" in german and "Tree" in english and "Bike" in english
    assert flashed(request) == [english, "request-review"]
    assert announced.call_args.kwargs["url"] == reverse("level_me")


# ---- levels, weeks and streaks at their boundaries ------------------------------------------------------------------------


@pytest.mark.parametrize("points, level_number, progress", [(0, 1, 0), (99, 1, 99), (100, 2, 0), (101, 2, 0)])
def test_a_level_is_reached_exactly_at_its_threshold(
    user: User, levels: list[Level], points: int, level_number: int, progress: int
) -> None:
    data = fn.get_level(user, points)
    assert (data["level_number"], data["progress_percent"]) == (level_number, progress)


@pytest.fixture
def frozen_today() -> Iterator[None]:
    with mock.patch("utils.functions.timezone.localdate", return_value=TODAY):
        yield


def test_the_week_of_the_weekly_goal_starts_on_monday(
    user: User, action_type: AktionenListe, make_aktion: Callable[..., Aktion], frozen_today: None
) -> None:
    make_aktion(user, action_type, 1, date=MONDAY)
    make_aktion(user, action_type, 10, date=MONDAY - datetime.timedelta(days=1))
    assert fn.get_weekly_goal_from_user(user) == (20, 2, 10)


@pytest.mark.parametrize("weeks_ago, expected", [([1], 1), ([2], 0), ([0], 1), ([0, 2], 1), ([1, 2, 3], 3)])
def test_streak_has_to_reach_the_current_or_last_week(
    user: User,
    action_type: AktionenListe,
    make_aktion: Callable[..., Aktion],
    frozen_today: None,
    weeks_ago: list[int],
    expected: int,
) -> None:
    for weeks in weeks_ago:  # 10 units are exactly the weekly goal of 20 points
        make_aktion(user, action_type, 10, date=MONDAY - datetime.timedelta(weeks=weeks))
    assert fn.get_streak_from_user(user) == expected


def test_member_points_include_both_ends_of_the_period(
    user: User, action_type: AktionenListe, make_aktion: Callable[..., Aktion]
) -> None:
    for offset in (-1, 0, 1, 2):
        make_aktion(user, action_type, 1, date=TODAY + datetime.timedelta(days=offset))
    end = TODAY + datetime.timedelta(days=1)
    assert fn.get_klimapunkte_for_member(user, TODAY, end) == 4
    assert fn.get_klimapunkte_by_user([user.id], TODAY, end) == {user.id: 4}
    assert fn.get_klimapunkte_by_user([user.id], None, TODAY) == {user.id: 4}


def test_points_of_nobody(db) -> None:
    assert fn.get_klimapunkte_by_user([]) == {}


def test_members_with_equal_points_share_a_rank(user: User, other: User, worldwide) -> None:
    assert fn.get_family_rank_from_user(user, worldwide) == 1
    assert fn.get_family_rank_from_user(other, worldwide) == 1


# ---- internal errors, push and mail ---------------------------------------------------------------------------------------


def test_error_log_redacts_tokens_and_only_names_uploaded_files(settings, tmp_path) -> None:
    settings.LOGS_DIR = tmp_path
    request = RequestFactory().post(
        "/",
        {"apns_token": "hidden-a", "recaptchaToken": "hidden-b", "new_password": "hidden-c", "name": "shown"},
    )
    request.FILES["upload"] = SimpleUploadedFile("report.txt", b"file-content")
    request.user = AnonymousUser()
    fn.create_internal_error(request, "first", throw_exception=False)
    fn.create_internal_error(None, "second")
    log = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "hidden" not in log and "file-content" not in log
    assert "shown" in log and "report.txt" in log and "Anonymous" in log
    assert "first" in log and "second" in log  # entries are appended


def test_a_broken_ios_device_does_not_stop_the_other_devices(user: User, no_push: mock.MagicMock) -> None:
    for token in ("broken", "working"):
        IOSDevice.objects.create(user=user, apns_token=token)
    with mock.patch("utils.functions.send_ios_push", side_effect=[RuntimeError("gone"), None]) as send:
        fn.create_notification(make_request(user), "de <span>x</span>", "en", user=user)
    assert sorted(call.kwargs["device_token"] for call in send.call_args_list) == ["broken", "working"]
    assert send.call_args.kwargs["body"] == "de "
    focused = reverse("benachrichtigungen_view_focused", args=[Benachrichtigung.objects.get().id])
    assert send.call_args.kwargs["data"] == {"url": focused}
    no_push.assert_called_once()


def test_web_push_payload(user: User) -> None:
    with mock.patch("utils.functions.send_user_notification") as send:
        fn.send_push("text", user, url="/x/", head="Head")
    send.assert_called_once_with(
        user=user, payload={"head": "Head", "body": "text", "url": "/x/"}, ttl=fn.PUSH_TTL_SECONDS
    )


def test_notification_mail_links_to_the_target_and_escapes_it() -> None:
    body = fn.build_mail_html("s", "m", '/x/?a=1&b="2"', include_unsubscribe=True)
    assert 'href="/x/?a=1&amp;b=&quot;2&quot;"' in body
    assert "Hier abmelden" in body and "Impressum" in body


def test_mail_without_message_is_not_sent(user: User) -> None:
    assert fn.send_mail_function(request=make_request(user), subject="s", user=user) is False
    assert mail.outbox == []


# ---- template filters, context processor and models -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, total, expected", [(1, 4, 25), ("1", "2", 50), (None, 1, 0), (1, None, 0), (1, "0", 0), ("", 1, 0)]
)
def test_percent(value: object, total: object, expected: float) -> None:
    assert percent(value, total) == expected


@pytest.mark.parametrize("value", [None, "", 0])
def test_safe_html_of_empty_values(value: object) -> None:
    assert safe_html(value) == ""


def test_safe_html_removes_script_urls_and_accepts_non_strings() -> None:
    assert "javascript:" not in safe_html('<a href="javascript:alert(1)">x</a>')
    assert safe_html(12) == "12"


def test_answered_post_filter(user: User, other: User) -> None:
    post = mock.Mock()
    post.answers.all.return_value = [mock.Mock(creator_id=user.id)]
    assert request_user_has_answered_post(post, user) is True
    assert request_user_has_answered_post(post, other) is False
    assert request_user_has_answered_post(post, AnonymousUser()) is False


@pytest.mark.parametrize("request_object", [HttpRequest(), make_request()], ids=["no user", "anonymous"])
def test_notification_count_is_only_offered_to_logged_in_users(request_object: HttpRequest) -> None:
    assert notification_count(request_object) == {}


def test_notification_count_is_only_queried_when_used(user: User, django_assert_num_queries) -> None:
    Benachrichtigung.objects.create(user=user, benachrichtigung_de="a", benachrichtigung_en="a", url="/x/")
    with django_assert_num_queries(0):
        context = notification_count(make_request(user))
    with django_assert_num_queries(1):
        assert context["benachrichtigungen_count"] == 1


def test_notification_and_report_names(user: User, other: User) -> None:
    notification = Benachrichtigung(user=user, benachrichtigung_de="Hallo", benachrichtigung_en="Hi")
    assert str(notification) == "Hallo (alice)"
    report = ReportedUser.objects.create(reported_user=other, reporting_user=user, reason="r")
    user.delete()
    report.refresh_from_db()
    assert report.reporting_user is None and str(report) == "bob"  # reports outlive the reporting user


def test_planting_a_tree_uses_each_code_once(user: User, other: User) -> None:
    from personals.models import TreeCodes

    TreeCodes.objects.create(code="only", redeemURL="https://x.de", sponsor="s")
    assert user.usererweitert.plant_tree() is True
    assert other.usererweitert.plant_tree() is False
    assert (user.usererweitert.given_credits, other.usererweitert.given_credits) == (1, 0)
