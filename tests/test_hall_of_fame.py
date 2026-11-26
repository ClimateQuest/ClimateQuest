"""Who qualifies for each weekly hall of fame challenge, tested at the exact thresholds."""

import datetime
from collections.abc import Callable

import pytest
from django.contrib.auth.models import User

from aktionen.models import Aktion, AktionenListe, Category
from artikel.models import Artikel
from artikel.models import Category as ArtikelCategory
from events.models import Event
from hall_of_fame import services
from hall_of_fame.models import HallOfFameData, HallOfFameEntry
from home.models import Benachrichtigung
from petitions.models import Category as PetitionCategory
from petitions.models import Petition

pytestmark = pytest.mark.usefixtures("no_push")

SUNDAY = datetime.date(2026, 3, 22)
WEEK_START = datetime.date(2026, 3, 16)
LAST_SUNDAY = WEEK_START - datetime.timedelta(days=1)


def special_action(pk: int, category: Category) -> AktionenListe:
    """One of the predefined actions, one climate point per unit."""
    return AktionenListe.objects.create(
        id=pk,
        name=f"special{pk}",
        name_en=f"special{pk}",
        klimapunkte=1,
        mengeBeschreibung="x",
        mengeBeschreibung_en="x",
        mengeBeschreibungSingular="x",
        mengeBeschreibungSingular_en="x",
        category=category,
    )


def qualified(evaluate: Callable) -> list[str]:
    return sorted(user.username for user in evaluate(WEEK_START, SUNDAY))


@pytest.mark.parametrize(
    "quantity, date, qualifies", [(25, WEEK_START, True), (24.9, WEEK_START, False), (25, LAST_SUNDAY, False)]
)
def test_fifty_points_in_this_week(
    user: User, action_type: AktionenListe, make_aktion: Callable[..., Aktion], quantity: float, date, qualifies: bool
) -> None:
    make_aktion(user, action_type, quantity, date=date)  # two points per unit
    assert qualified(services._qualified_users_50_points) == (["alice"] if qualifies else [])


def test_top_five_of_the_worldwide_ranking(
    make_user: Callable[..., User], worldwide, action_type: AktionenListe, make_aktion: Callable[..., Aktion]
) -> None:
    for points in range(1, 8):
        make_aktion(make_user(f"p{points}"), action_type, points, date=SUNDAY)
    outsider = make_user("outsider")
    worldwide.members.remove(outsider)
    make_aktion(outsider, action_type, 100, date=SUNDAY)
    old = make_user("old")
    make_aktion(old, action_type, 100, date=LAST_SUNDAY)
    assert qualified(services._qualified_users_top_5) == ["p3", "p4", "p5", "p6", "p7"]


def test_users_tied_for_fifth_place_all_qualify(
    make_user: Callable[..., User], action_type: AktionenListe, make_aktion: Callable[..., Aktion]
) -> None:
    for name, quantity in [("a", 9), ("b", 8), ("c", 7), ("d", 6), ("e", 5), ("f", 5), ("g", 4)]:
        make_aktion(make_user(name), action_type, quantity, date=SUNDAY)
    assert qualified(services._qualified_users_top_5) == ["a", "b", "c", "d", "e", "f"]


@pytest.mark.parametrize("quantity, qualifies", [(50, True), (49.9, False)])
def test_fifty_kilometers_by_bike(
    user: User,
    category: Category,
    make_aktion: Callable[..., Aktion],
    quantity: float,
    qualifies: bool,
) -> None:
    make_aktion(user, special_action(services.BIKE_ACTION_ID, category), quantity, date=WEEK_START)
    # Other actions do not count as kilometers. A fixed id cannot collide with the special ones on PostgreSQL.
    make_aktion(user, special_action(1000, category), 500, date=WEEK_START)
    assert qualified(services._qualified_users_bike) == (["alice"] if qualifies else [])


@pytest.mark.parametrize(
    "action_id, quantity, date, vegetarian, vegan",
    [
        (services.VEGAN_ACTION_ID, 1, SUNDAY, True, True),
        (22, 1, SUNDAY, True, False),
        (services.VEGAN_ACTION_ID, 0.9, SUNDAY, False, False),
        (services.VEGAN_ACTION_ID, 1, SUNDAY - datetime.timedelta(days=1), False, False),
        (services.BIKE_ACTION_ID, 1, SUNDAY, False, False),
    ],
)
def test_vegetarian_and_vegan_need_an_entry_of_today(
    user: User,
    category: Category,
    make_aktion: Callable[..., Aktion],
    action_id: int,
    quantity: float,
    date: datetime.date,
    vegetarian: bool,
    vegan: bool,
) -> None:
    make_aktion(user, special_action(action_id, category), quantity, date=date)
    assert qualified(services._qualified_users_vegetarian) == (["alice"] if vegetarian else [])
    assert qualified(services._qualified_users_vegan) == (["alice"] if vegan else [])


def create_artikel(user: User) -> Artikel:
    category = ArtikelCategory.objects.create(title="c")
    return Artikel.objects.create(
        name_de="a", name_en="a", content_de="c", content_en="c", creator=user, category=category
    )


def create_event(user: User) -> Event:
    return Event.objects.create(
        name="e", description="d", adress="a", date_time="2026-05-01T10:00Z", duration=1, creator=user
    )


def create_petition(user: User) -> Petition:
    category = PetitionCategory.objects.create(title="c")
    return Petition.objects.create(
        title_de="p", title_en="p", content_de="c", content_en="c", goal=10, creator=user, category=category
    )


@pytest.mark.parametrize(
    "create, evaluate",
    [
        (create_artikel, services._qualified_users_article),
        (create_event, services._qualified_users_event),
        (create_petition, services._qualified_users_petition),
    ],
)
@pytest.mark.parametrize(
    "created_at, qualifies",
    [(WEEK_START, True), (SUNDAY, True), (LAST_SUNDAY, False), (SUNDAY + datetime.timedelta(days=1), False)],
)
def test_content_has_to_be_created_in_this_week(
    user: User, create: Callable, evaluate: Callable, created_at: datetime.date, qualifies: bool
) -> None:
    content = create(user)
    create(user)  # a second one must not list the user twice
    type(content).objects.update(created_at=created_at)
    assert qualified(evaluate) == (["alice"] if qualifies else [])


def test_weekly_run_describes_the_week_and_announces_the_next_challenge(
    user: User, other: User, action_type: AktionenListe, make_aktion: Callable[..., Aktion]
) -> None:
    make_aktion(user, action_type, 25, date=WEEK_START)
    services.run_weekly_hall_of_fame(today=SUNDAY)
    entry = HallOfFameEntry.objects.get()
    assert entry.user == user
    assert "16.03.2026" in entry.description_de and "22.03.2026" in entry.description_de
    assert "03/16/2026" in entry.description_en and "03/22/2026" in entry.description_en
    data = HallOfFameData.objects.get(id=1)
    assert (data.weeks_count, data.to_do_en) == (1, services.WEEKLY_STEPS[0][3])
    assert Benachrichtigung.objects.filter(user=other).count() == 1  # the new challenge
    assert Benachrichtigung.objects.filter(user=user).count() == 2  # induction and new challenge


def test_a_week_without_winners_still_moves_on(user: User) -> None:
    services.run_weekly_hall_of_fame(today=SUNDAY)
    assert not HallOfFameEntry.objects.exists()
    assert HallOfFameData.objects.get(id=1).weeks_count == 1


def test_every_challenge_has_a_description_and_leads_to_the_next_one() -> None:
    assert [step[1] for step in services.WEEKLY_STEPS] == [1, 2, 3, 4, 5, 6, 7, 0]
    assert [step[0] for step in services.WEEKLY_STEPS] == list(services.DESCRIPTIONS)


def test_induction_of_nobody_creates_nothing(db) -> None:
    services.induct_into_hall_of_fame(None, [], "de", "en")
    assert not HallOfFameEntry.objects.exists() and not Benachrichtigung.objects.exists()
