import pytest

from bitsai.auth.pasteback import PASTE_REDIRECT_URI, contains_redirect, parse_redirect

U = PASTE_REDIRECT_URI


def test_redirect_is_loopback_http():
    assert U.startswith("http://localhost:")


@pytest.mark.parametrize("text", [
    f"{U}?code=CODE&state=STATE",
    f"here you go: {U}?state=STATE&code=CODE thanks",
    f"<{U}?code=CODE&state=STATE>",
    f"({U}?code=CODE&state=STATE).",
    f"`{U}?code=CODE&state=STATE`",
])
def test_parse_variants(text):
    p = parse_redirect(text)
    assert p is not None and (p.code, p.state, p.error) == ("CODE", "STATE", None)
    assert contains_redirect(text)


def test_error_redirect():
    p = parse_redirect(f"{U}?error=access_denied&state=STATE")
    assert p.error == "access_denied" and p.state == "STATE" and p.code == ""
    weird = parse_redirect(f"{U}?error=%3Cscript%3E&state=STATE")
    assert weird.error == "error"


@pytest.mark.parametrize("text", [
    "", "hello", f"{U}", f"{U}?code=CODE", f"{U}?state=S", f"{U}?state=&code=C",
    f"{U}?state=A&state=B&code=C", f"{U}?code=A&code=B&state=S",
    "http://localhost:8765/other?code=C&state=S", "https://evil.example/?code=C&state=S",
])
def test_parse_rejects(text):
    assert parse_redirect(text) is None


def test_repr_hides_secrets():
    p = parse_redirect(f"{U}?code=SECRETCODE&state=SECRETSTATE")
    assert "SECRET" not in repr(p) and "SECRET" not in str(p)
