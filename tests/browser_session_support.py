"""Sign in the way a browser signs in, and submit the form the page rendered.

``access_support`` exists for the tests that override identity to a fixed
principal; this module is for the ones that may not.  A test that proves a
*form* works has to hold two things at once — a session established by the real
magic-link flow, and a payload taken out of the rendered HTML rather than
composed — and the two halves lived in different test modules, neither
importable: the sign-in half in ``test_sign_in_access.py``, the rendered-form
reader in ``test_issue_path_end_to_end.py``.  So the Issue forms shipped without
the request-forgery field ``get_human_principal`` requires on every write, and
the end-to-end test that drove them replaced that dependency rather than
reaching for a sign-in it could not import (#821).

Both halves live here now, so the next form ticket proves its screen through one
seam.  Nothing here sends a header a browser form would not send: ``authed_post``
echoes the readable forgery cookie the way a script would, and ``submit_form``
carries only the fields the page itself emitted.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit

from corridor.web import auth


# --- the real sign-in flow, as a person walks it --------------------------


def request_link(client, email, next_path=""):
    """Ask for a magic link; the recording sender captures it, nothing mails."""
    data = {"email": email}
    if next_path:
        data["next"] = next_path
    return client.post("/sign-in/request", data=data, follow_redirects=False)


def link_token(sender) -> str:
    """The single-use token out of the most recent captured link."""
    _email, link = sender.sent[-1]
    return parse_qs(urlsplit(link).query)["token"][0]


def sign_in(client, sender, email, *, next_path=""):
    """Request and consume a link; leaves the session cookie on the client."""
    before = len(sender.sent)
    response = request_link(client, email, next_path)
    assert response.status_code == 200
    assert len(sender.sent) == before + 1, "a member must receive exactly one link"
    consume = client.get(
        "/sign-in/consume",
        params={"token": link_token(sender)},
        follow_redirects=False,
    )
    assert consume.status_code == 303
    return consume


def csrf_headers(client) -> dict[str, str]:
    token = client.cookies.get(auth.CSRF_COOKIE)
    return {auth.CSRF_HEADER: token} if token else {}


def authed_post(client, url, data=None):
    return client.post(
        url,
        data=data or {},
        headers=csrf_headers(client),
        follow_redirects=False,
    )


# --- the form the page actually rendered ----------------------------------

_FORM = re.compile(
    r'<form[^>]*action="(?P<action>[^"]*)"[^>]*>(?P<body>.*?)</form>', re.S
)
_HIDDEN = re.compile(
    r'<input[^>]*type="hidden"[^>]*name="(?P<name>[^"]*)"[^>]*'
    r'value="(?P<value>[^"]*)"'
)
# A hidden textarea is a hidden field: the browser submits it exactly like an
# `<input type="hidden">`, and the Key dates confirmation carries the previewed
# CSV in one (#880).  Reading only the inputs would have handed back a payload
# missing a required field, and a test that filled that field in from its own
# variable would be composing the submission this module exists not to compose.
_HIDDEN_TEXTAREA = re.compile(
    r'<textarea(?=[^>]*\bhidden\b)[^>]*name="(?P<name>[^"]*)"[^>]*>'
    r"(?P<value>.*?)</textarea>",
    re.S,
)


def form_fields(body: str, action_suffix: str) -> dict[str, str] | None:
    """The hidden fields of the one form on the page with this action.

    Reading the payload out of the rendered page is the point: a test that
    composed its own would prove that the route accepts a payload, not that the
    screen offers one a coordinator could submit.  The request-forgery field is
    a hidden input like any other, so a form that omits it hands back a payload
    the write path refuses — which is the defect this reader is meant to catch.

    What a person types is not here and should not be: these are the fields the
    *page* supplies, so a caller adds the typed ones itself.
    """

    for match in _FORM.finditer(body):
        if match.group("action").endswith(action_suffix):
            found = match.group("body")
            return {
                one.group("name"): html.unescape(one.group("value"))
                for pattern in (_HIDDEN, _HIDDEN_TEXTAREA)
                for one in pattern.finditer(found)
            }
    return None


def submit_form(client, url, fields):
    """Post exactly these fields, and nothing a browser form would not send."""

    return client.post(url, data=fields, follow_redirects=False)


# --- the page's own markup, without the shell every page carries ------------

_SHELL = re.compile(r'<nav class="shell".*?</nav>', re.S)


def page_without_shell(body: str) -> str:
    """This page's own markup, with the navigation shell taken out (#843).

    Every customer page renders the same shell, and the shell carries the one
    control that leaves the product: a POST form with the forgery field and a
    Sign out button. A test asking what *this page* offers -- "the only form
    here is the GET search", "the one act on the week is the approval" -- is
    asking about the page, not about the navigation every page carries, so it
    reads the page with the shell removed rather than counting it.
    """

    return _SHELL.sub("", body)
