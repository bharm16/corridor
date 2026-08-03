"""The one call shape, for both text and image extraction.

Retry, backoff and usage accounting live in one place, so image support is
an extra argument to the existing call rather than a second client.
"""

import base64
import json

import httpx
import pytest

from corridor.llm import OpenAIClient, complete_many

SCHEMA = {"type": "object", "properties": {}}


def client(capture, *, body=None):
    """An OpenAIClient whose transport records the request it was given."""

    def handler(request: httpx.Request) -> httpx.Response:
        capture.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=body
            or {
                "choices": [{"message": {"content": '{"rows": []}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    c = OpenAIClient(model="test-model", api_key="k")
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_a_text_only_call_sends_a_plain_string(tmp_path):
    sent = []
    client(sent).complete(system="sys", user="page text", schema=SCHEMA)

    assert sent[0]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "page text"},
    ]


def test_an_image_reaches_the_request_beside_the_text(tmp_path):
    image = tmp_path / "0001.png"
    image.write_bytes(b"\x89PNG fake bytes")
    sent = []

    client(sent).complete(
        system="sys", user="page text", schema=SCHEMA, images=[image]
    )

    content = sent[0]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "page text"}
    assert content[1]["type"] == "image_url"
    encoded = base64.b64encode(b"\x89PNG fake bytes").decode()
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{encoded}"


def test_an_empty_image_list_is_the_same_as_no_images(tmp_path):
    """So a caller can pass through whatever it has without branching."""
    sent = []
    client(sent).complete(system="sys", user="t", schema=SCHEMA, images=[])

    assert sent[0]["messages"][1]["content"] == "t"


def test_a_missing_image_is_an_error_not_a_silent_text_only_call(tmp_path):
    """Falling back to text would quietly run the vision extractor blind."""
    with pytest.raises(FileNotFoundError):
        client([]).complete(
            system="s", user="u", schema=SCHEMA, images=[tmp_path / "gone.png"]
        )


def test_complete_many_carries_one_image_set_per_user():
    seen = []

    class Recorder:
        model = "test-model"
        max_workers = 2

        def complete(self, *, system, user, schema, images=()):
            seen.append((user, list(images)))
            return {"user": user}

    results = complete_many(
        Recorder(),
        system="s",
        schema=SCHEMA,
        users=["a", "b"],
        images=[["a.png"], ["b.png"]],
        max_workers=2,
    )

    assert [r["user"] for r in results] == ["a", "b"]
    assert sorted(seen) == [("a", ["a.png"]), ("b", ["b.png"])]


def test_complete_many_without_images_does_not_pass_the_argument():
    """Existing text-only stubs take no `images` kwarg and must keep working."""

    class TextOnly:
        max_workers = 2

        def complete(self, *, system, user, schema):
            return {"user": user}

    results = complete_many(
        TextOnly(), system="s", schema=SCHEMA, users=["a", "b"], max_workers=2
    )

    assert [r["user"] for r in results] == ["a", "b"]
