"""
The operator landing page.

§5.3 item 5 requires the [MOCK] banner on every UI page. That is a safety
affordance, not decoration — an operator must never be unsure whether the
instance they are looking at can reach real recipients.
"""

import pytest
from django.urls import reverse


@pytest.fixture
def html(client):
    return client.get(reverse("ui:home")).content.decode()


def test_home_renders(client):
    assert client.get(reverse("ui:home")).status_code == 200


def test_mock_banner_is_present(html):
    assert "[MOCK]" in html
    assert "Nothing reaches the internet" in html


def test_kill_switch_state_is_visible(html):
    assert "GLOBAL_SEND_ENABLED=false" in html


def test_page_states_it_cannot_send(html):
    """The foundation build must not look like a working platform."""
    assert "It cannot send anything" in html


def test_health_probes_are_not_shadowed_by_the_catch_all(client):
    """`ui.urls` is included at "" — a routing slip would swallow the probes."""
    assert client.get("/health/live").status_code == 200
