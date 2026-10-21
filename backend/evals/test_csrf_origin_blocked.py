"""Cookie CSRF must allow same-origin SPA rewrite edge cases without opening cross-site."""

from app.edge_security import origin_blocked

ALLOWED = {
    "https://casesifter-alex-app.onrender.com",
    "https://casesifter-app.onrender.com",
}


def test_allowed_origin_ok():
    assert (
        origin_blocked(
            "https://casesifter-alex-app.onrender.com",
            ALLOWED,
            production=True,
        )
        is False
    )


def test_wrong_origin_blocked():
    assert (
        origin_blocked(
            "https://evil.example",
            ALLOWED,
            production=True,
        )
        is True
    )


def test_empty_origin_blocked_in_production_without_hints():
    assert origin_blocked("", ALLOWED, production=True) is True


def test_empty_origin_allowed_with_same_origin_fetch_site():
    assert (
        origin_blocked(
            "",
            ALLOWED,
            production=True,
            sec_fetch_site="same-origin",
        )
        is False
    )


def test_empty_origin_allowed_with_allowlisted_referer():
    assert (
        origin_blocked(
            "",
            ALLOWED,
            production=True,
            referer="https://casesifter-alex-app.onrender.com/matters/new",
        )
        is False
    )


def test_wrong_origin_not_rescued_by_referer():
    assert (
        origin_blocked(
            "https://evil.example",
            ALLOWED,
            production=True,
            referer="https://casesifter-alex-app.onrender.com/",
            sec_fetch_site="same-origin",
        )
        is True
    )


def test_dev_empty_origin_allowed():
    assert origin_blocked("", ALLOWED, production=False) is False
