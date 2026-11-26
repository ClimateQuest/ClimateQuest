"""Information pages, third-party login pages and the exact addresses listed in the sitemap."""

from collections.abc import Callable

import pytest
from allauth.socialaccount.models import SocialApp
from django import forms
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sites.models import Site
from django.template.loader import render_to_string
from django.urls import resolve, reverse
from django.utils import timezone

from artikel.models import Artikel
from artikel.models import Category as ArtikelCategory
from ClimateQuest.urls import raw_sitemap_classes, sitemaps
from events.models import Event
from forum.models import Category as ForumCategory
from forum.models import ForumPost
from tests.conftest import LangClient


@pytest.mark.parametrize(
    "url_name, template",
    [
        ("mobile_apps", "mobile_apps.html"),
        ("aknachhaltigkeit", "aknachhaltigkeit.html"),
        ("pastel_done", "pastel_done.html"),
        ("facts", "facts.html"),
    ],
)
@pytest.mark.parametrize("language", ["de", "en"])
def test_information_pages_render_for_everybody(
    client: LangClient, db, url_name: str, template: str, language: str
) -> None:
    client.language = language
    response = client.get(reverse(url_name))
    assert response.status_code == 200
    assert template in [used.name.removeprefix("./") for used in response.templates if used.name]


def test_facts_page_includes_the_next_button_of_every_chapter(client: LangClient, db) -> None:
    response = client.get(reverse("facts"))
    html = response.content.decode()
    assert "_facts_next.html" in [used.name for used in response.templates]
    assert html.count("data-next>Weiter") > 1


def test_facts_page_is_translated(en_client: LangClient, db) -> None:
    html = en_client.get(reverse("facts")).content.decode()
    assert "data-next>Next" in html


def test_facts_page_invites_visitors_to_register_and_users_to_the_dashboard(client: LangClient, user: User) -> None:
    register_link = f'class="hq-btn" href="{reverse("register_view")}"'
    dashboard_link = f'class="hq-btn" href="{reverse("dashboard")}"'
    anonymous = client.get(reverse("facts")).content.decode()
    assert register_link in anonymous and dashboard_link not in anonymous
    client.force_login(user)
    logged_in = client.get(reverse("facts")).content.decode()
    assert dashboard_link in logged_in and register_link not in logged_in


# ---- third-party login ------------------------------------------------------------------------------


@pytest.fixture
def google_app(db) -> None:
    app = SocialApp.objects.create(provider="google", name="google", client_id="id", secret="s")
    app.sites.add(Site.objects.get_current())


@pytest.mark.parametrize("provider, label", [("google", "Google"), ("apple", "Apple")])
def test_provider_login_page_asks_before_redirecting(
    client: LangClient, google_app: None, provider: str, label: str
) -> None:
    response = client.get(f"/accounts/{provider}/login/")
    assert response.status_code == 200
    html = response.content.decode()
    assert f"Login über {label}" in html
    assert f"/accounts/{provider}/login/" in html


def test_provider_error_page_links_back_to_the_login(client: LangClient, google_app: None) -> None:
    response = client.get("/accounts/3rdparty/login/error/")
    html = response.content.decode()
    assert "Fehler beim Anmelden über Drittanbieter" in html
    assert reverse("login_view") in html


def test_provider_signup_form_posts_to_allauth(rf, google_app: None) -> None:
    class SignupForm(forms.Form):
        email = forms.EmailField()

    request = rf.get("/")
    request.user = AnonymousUser()
    html = render_to_string("socialaccount/signup.html", {"form": SignupForm()}, request=request)
    assert f'action="{reverse("socialaccount_signup")}"' in html
    assert 'name="email"' in html


@pytest.mark.parametrize(
    "url, target", [("/accounts/login/", "/personals/login/"), ("/accounts/signup/", "/personals/register/")]
)
def test_allauth_account_pages_are_replaced_by_our_own(client: LangClient, db, url: str, target: str) -> None:
    assert client.get(url).url == target


# ---- sitemap addresses ----------------------------------------------------------------------------------

# Sitemaps of single pages: sitemap name -> URL name of the page it has to list.
STATIC_SITEMAP_PAGES = {
    "home": "home",
    "nutzungsbedingungen": "nutzungsbedingungen",
    "support": "support",
    "feedback": "feedback",
    "family_overview": "families_view",
    "add_family": "create_family",
    "join_family": "join_family",
    "community_overview": "communities_view",
    "add_community": "create_community",
    "join_community": "join_community",
    "login": "login_view",
    "register": "register_view",
    "personal_settings": "settings_view",
    "reset_password": "reset_password",
    "add_aktionen": "add",
    "artikel_overview": "artikel_overview",
    "add_artikel": "add_artikel",
    "events_overview": "events_overview",
    "add_event": "add_event",
    "forum_overview": "forum_overview",
    "add_forum_post": "add_forum_post",
    "mobile_apps": "mobile_apps",
    "aknachhaltigkeit": "aknachhaltigkeit",
    "hall_of_fame_detail": "hall_of_fame_detail",
}


@pytest.mark.parametrize("sitemap_name, url_name", STATIC_SITEMAP_PAGES.items())
def test_static_sitemaps_list_exactly_their_page(sitemap_name: str, url_name: str) -> None:
    sitemap = sitemaps[sitemap_name]
    assert [sitemap.location(item) for item in sitemap.items()] == [f"/de{reverse(url_name)}"]


def make_artikel(user: User) -> Artikel:
    return Artikel.objects.create(
        name_de="a",
        name_en="a",
        content_de="c",
        content_en="c",
        creator=user,
        category=ArtikelCategory.objects.create(title="c"),
    )


def make_event(user: User) -> Event:
    return Event.objects.create(
        name="e", description="d", adress="a", date_time=timezone.now(), duration=1, creator=user
    )


def make_post(user: User) -> ForumPost:
    return ForumPost.objects.create(
        title="t", content="c", creator=user, category=ForumCategory.objects.create(title="c")
    )


@pytest.mark.parametrize(
    "sitemap_name, factory, url_name, lastmod_field",
    [
        ("artikel_detail", make_artikel, "artikel_detail", "updated_at"),
        ("event_detail", make_event, "event_detail", "last_modified"),
        ("forum_post_detail", make_post, "post_detail", "date_time"),
    ],
)
def test_detail_sitemaps_list_every_object_with_its_modification_date(
    user: User, sitemap_name: str, factory: Callable, url_name: str, lastmod_field: str
) -> None:
    obj = factory(user)
    sitemap = sitemaps[sitemap_name]
    assert list(sitemap.items()) == [obj]
    assert sitemap.location(obj) == f"/de{reverse(url_name, args=[obj.id])}"
    assert sitemap.lastmod(obj) == getattr(obj, lastmod_field)


@pytest.mark.parametrize(
    "sitemap_name, url_name",
    [("klimapunkte_view", "klimapunkte_view"), ("level_view", "level_view"), ("history", "history")],
)
def test_user_sitemaps_list_active_users_only(make_user, sitemap_name: str, url_name: str) -> None:
    active = make_user("active")
    make_user("inactive", is_active=False)
    sitemap = sitemaps[sitemap_name]
    assert list(sitemap.items()) == [active]
    assert sitemap.location(active) == f"/de{reverse(url_name, args=[active.id])}"
    assert sitemap.lastmod(active) == active.last_login


def test_every_sitemap_is_covered_by_these_tests() -> None:
    dynamic = {"artikel_detail", "event_detail", "forum_post_detail", "klimapunkte_view", "level_view", "history"}
    assert set(raw_sitemap_classes) == set(STATIC_SITEMAP_PAGES) | dynamic


@pytest.mark.parametrize("url_name", STATIC_SITEMAP_PAGES.values())
def test_sitemap_addresses_resolve(url_name: str) -> None:
    assert resolve(reverse(url_name)).url_name == url_name
