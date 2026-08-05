"""The one call shape, for both text and image extraction.

Retry, backoff and usage accounting live in one place, so image support is
an extra argument to the existing call rather than a second client.

The wire shape is the Responses API: the provider documents it as the
surface reasoning models belong on, and every GPT-5.6 control we need —
reasoning effort, image detail, prompt caching, logprobs — is reachable
only there. The `StructuredClient` protocol does not move, so every stub
in this suite and every text-only extractor is untouched by that.
"""

import base64
import json

import httpx
import pytest

from corridor.llm import OpenAIClient, complete_many

SCHEMA = {"type": "object", "properties": {}}


def responded(text='{"rows": []}', **over):
    body = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 80},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    body.update(over)
    return body


def client(capture, *, body=None, status=200, **kwargs):
    """An OpenAIClient whose transport records the request it was given."""

    def handler(request: httpx.Request) -> httpx.Response:
        capture.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(status, json=body if body is not None else responded())

    c = OpenAIClient(model="test-model", api_key="k", **kwargs)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


# ------------------------------------------------------------- the wire shape


def test_the_call_goes_to_the_responses_endpoint():
    sent = []
    client(sent).complete(system="sys", user="page text", schema=SCHEMA)

    assert sent[0]["url"].endswith("/responses")


def test_the_system_text_is_carried_as_instructions():
    """Not a message role: on Responses it is a top-level field."""
    sent = []
    client(sent).complete(system="sys", user="page text", schema=SCHEMA)

    assert sent[0]["body"]["instructions"] == "sys"


def test_a_text_only_call_sends_one_input_text_part():
    sent = []
    client(sent).complete(system="sys", user="page text", schema=SCHEMA)

    assert sent[0]["body"]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "page text"}]}
    ]


def test_the_schema_is_flattened_into_text_format_with_strict_explicit(tmp_path):
    """Omitting strict makes the API attempt strict and silently fall back.

    Believing you have schema guarantees while running without them is
    exactly how malformed rows reach a reviewer, so it is always explicit.
    """
    sent = []
    client(sent).complete(system="s", user="u", schema=SCHEMA)

    fmt = sent[0]["body"]["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["schema"] == SCHEMA


def test_reasoning_effort_is_pinned_rather_than_defaulted():
    """The silent default is medium, billed as output tokens.

    Measured on a structure question: no reasoning parameter spends 333
    reasoning tokens and 466 output tokens; effort none spends 0 and 130.
    Transcription and structure-mapping are perception, not deliberation.
    """
    sent = []
    client(sent).complete(system="s", user="u", schema=SCHEMA)

    assert sent[0]["body"]["reasoning"] == {"effort": "none"}


def test_effort_is_configurable_for_a_caller_that_wants_deliberation():
    sent = []
    client(sent, effort="low").complete(system="s", user="u", schema=SCHEMA)

    assert sent[0]["body"]["reasoning"] == {"effort": "low"}


def test_responses_are_not_stored():
    """The default retains them 30 days; a stateless pipeline wants none of it."""
    sent = []
    client(sent).complete(system="s", user="u", schema=SCHEMA)

    assert sent[0]["body"]["store"] is False


def test_a_cache_key_is_sent_and_is_stable_for_the_life_of_the_client():
    """GPT-5.6 requires one for reliable prefix matching."""
    sent = []
    c = client(sent)
    c.complete(system="s", user="a", schema=SCHEMA)
    c.complete(system="s", user="b", schema=SCHEMA)

    keys = {call["body"]["prompt_cache_key"] for call in sent}
    assert len(keys) == 1 and keys != {""}


def test_two_clients_do_not_share_a_cache_key():
    a, b = [], []
    client(a).complete(system="s", user="u", schema=SCHEMA)
    client(b).complete(system="s", user="u", schema=SCHEMA)

    assert a[0]["body"]["prompt_cache_key"] != b[0]["body"]["prompt_cache_key"]


# -------------------------------------------------------------------- images


def test_an_image_reaches_the_request_beside_the_text(tmp_path):
    image = tmp_path / "0001.png"
    image.write_bytes(b"\x89PNG fake bytes")
    sent = []

    client(sent).complete(system="sys", user="page text", schema=SCHEMA, images=[image])

    content = sent[0]["body"]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "page text"}
    encoded = base64.b64encode(b"\x89PNG fake bytes").decode()
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"] == f"data:image/png;base64,{encoded}"


def test_an_image_is_sent_at_original_detail(tmp_path):
    """The explicit no-resize contract for dense numeric pages.

    Measured: original and auto both cost 5,085 input tokens on a 2550x1650
    page, high costs 3,069 — high downscales. Pinning it means fidelity
    never depends on what a default resolves to.
    """
    image = tmp_path / "0001.png"
    image.write_bytes(b"\x89PNG")
    sent = []

    client(sent).complete(system="s", user="u", schema=SCHEMA, images=[image])

    assert sent[0]["body"]["input"][0]["content"][1]["detail"] == "original"


def test_the_image_comes_after_the_text_so_the_static_prefix_caches(tmp_path):
    image = tmp_path / "0001.png"
    image.write_bytes(b"\x89PNG")
    sent = []

    client(sent).complete(system="s", user="u", schema=SCHEMA, images=[image])

    types = [part["type"] for part in sent[0]["body"]["input"][0]["content"]]
    assert types == ["input_text", "input_image"]


def test_an_empty_image_list_is_the_same_as_no_images():
    sent = []
    client(sent).complete(system="sys", user="t", schema=SCHEMA, images=[])

    content = sent[0]["body"]["input"][0]["content"]
    assert content == [{"type": "input_text", "text": "t"}]


def test_a_missing_image_is_an_error_not_a_silent_text_only_call(tmp_path):
    """Falling back to text would quietly run the vision extractor blind."""
    with pytest.raises(FileNotFoundError):
        client([]).complete(
            system="s", user="u", schema=SCHEMA, images=[tmp_path / "gone.png"]
        )


# ------------------------------------------------------- reading the response


def test_the_message_is_found_by_type_not_by_position():
    """A reasoning item can precede it; indexing output[0] is the classic bug."""
    body = responded()
    body["output"].insert(0, {"type": "reasoning", "summary": []})

    assert client([], body=body).complete(
        system="s", user="u", schema=SCHEMA
    ) == {"rows": []}


def test_a_refusal_raises_rather_than_failing_to_parse():
    body = responded()
    body["output"][0]["content"] = [
        {"type": "refusal", "refusal": "I cannot help with that."}
    ]

    with pytest.raises(RuntimeError, match="refused"):
        client([], body=body).complete(system="s", user="u", schema=SCHEMA)


def test_a_truncated_response_raises_rather_than_returning_half_a_page():
    body = responded(status="incomplete", incomplete_details={"reason": "max_output_tokens"})

    with pytest.raises(RuntimeError, match="max_output_tokens"):
        client([], body=body).complete(system="s", user="u", schema=SCHEMA)


def test_a_200_with_no_message_raises_a_clear_error():
    body = responded()
    body["output"] = []

    with pytest.raises(RuntimeError, match="returned 200 with no message"):
        client([], body=body).complete(system="s", user="u", schema=SCHEMA)


def test_a_200_with_no_output_text_part_raises_a_clear_error():
    body = responded()
    body["output"][0]["content"] = [{"type": "reasoning", "summary": []}]

    with pytest.raises(RuntimeError, match="returned 200 with no output_text"):
        client([], body=body).complete(system="s", user="u", schema=SCHEMA)


def test_a_200_with_blank_output_text_raises_a_clear_error():
    body = responded(text="   ")

    with pytest.raises(RuntimeError, match="returned 200 with blank output_text"):
        client([], body=body).complete(system="s", user="u", schema=SCHEMA)


def test_a_valid_empty_json_object_is_a_legitimate_extraction():
    assert client([], body=responded(text="{}")).complete(
        system="s", user="u", schema=SCHEMA
    ) == {}


# --------------------------------------------------------- usage and logprobs


def test_usage_records_reasoning_and_cached_tokens():
    """Without both, a run's cost is one opaque number."""
    c = client([])
    c.complete(system="s", user="u", schema=SCHEMA)

    assert c.usage.prompt_tokens == 100
    assert c.usage.completion_tokens == 20
    assert c.usage.cached_tokens == 80
    assert c.usage.reasoning_tokens == 0


def test_logprobs_are_not_requested_unless_asked_for():
    sent = []
    client(sent).complete(system="s", user="u", schema=SCHEMA)

    assert "include" not in sent[0]["body"]


def test_logprobs_come_back_under_a_reserved_key():
    """The transcription tier needs them; the model's own confidence is
    blind to its misreads, so a measured signal replaces a self-reported one."""
    body = responded()
    body["output"][0]["content"][0]["logprobs"] = [
        {"token": "114", "logprob": -0.01},
        {"token": "9", "logprob": -2.3},
    ]
    sent = []

    result = client(sent, body=body).complete(
        system="s", user="u", schema=SCHEMA, logprobs=True
    )

    assert sent[0]["body"]["include"] == ["message.output_text.logprobs"]
    assert result["_meta"]["logprobs"][1]["token"] == "9"
    assert result["rows"] == []


# ---------------------------------------------------------------------- flex


def test_the_service_tier_is_standard_unless_asked_otherwise():
    sent = []
    client(sent).complete(system="s", user="u", schema=SCHEMA)

    assert "service_tier" not in sent[0]["body"]


def test_flex_is_opt_in_and_raises_the_timeout():
    sent = []
    c = client(sent, flex=True)
    c.complete(system="s", user="u", schema=SCHEMA)

    assert sent[0]["body"]["service_tier"] == "flex"
    assert c.timeout >= 900


# -------------------------------------------------------------- complete_many


def test_complete_many_carries_one_image_set_per_user():
    seen = []

    class Recorder:
        model = "test-model"
        max_workers = 2

        def complete(self, *, system, user, schema, images=(), logprobs=False):
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

    assert [r.value["user"] for r in results] == ["a", "b"]
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

    assert [r.value["user"] for r in results] == ["a", "b"]


def test_a_completion_separates_the_answer_from_the_metadata():
    """The reserved key cannot leak into a Candidate because it is not in
    the value. It used to travel inside the schema's own dict, and four
    caller sites had to recognise and strip it."""
    from corridor.llm import META_KEY, complete_many

    class WithMeta:
        max_workers = 1

        def complete(self, *, system, user, schema):
            return {"rows": [{"utility_id": "FOC1-1"}], META_KEY: {"logprobs": [1]}}

    [completion] = complete_many(WithMeta(), system="s", schema={}, users=["p"])

    assert completion.value == {"rows": [{"utility_id": "FOC1-1"}]}
    assert META_KEY not in completion.value
    assert completion.meta == {"logprobs": [1]}
    assert completion.failed is False


def test_a_returned_error_key_is_not_a_failure():
    """Outcome is the type's, not the payload's.

    A model answering with a field called `_error` used to be
    indistinguishable from the call having failed — and every stub that
    wanted to simulate a failure returned one rather than raising.
    """
    from corridor.llm import complete_many

    class Odd:
        max_workers = 1

        def complete(self, *, system, user, schema):
            return {"_error": "a value the document actually printed"}

    [completion] = complete_many(Odd(), system="s", schema={}, users=["p"])

    assert completion.failed is False
    assert completion.value == {"_error": "a value the document actually printed"}


def test_a_raised_failure_is_the_failure():
    from corridor.llm import complete_many

    class Broken:
        max_workers = 1

        def complete(self, *, system, user, schema):
            raise RuntimeError("503 upstream")

    [completion] = complete_many(Broken(), system="s", schema={}, users=["p"])

    assert completion.failed is True
    assert "503 upstream" in completion.error
    assert completion.value == {}


def test_complete_many_marks_only_the_bad_page_failed_for_a_200_with_no_message():
    sent = []
    bodies = [
        responded(),
        {"output": [], "usage": responded()["usage"]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        user_text = sent[-1]["input"][0]["content"][0]["text"]
        body = bodies[0] if user_text == "good page" else bodies[1]
        return httpx.Response(200, json=body)

    c = OpenAIClient(model="test-model", api_key="k", max_workers=2)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))

    results = complete_many(
        c,
        system="s",
        schema=SCHEMA,
        users=["good page", "bad page"],
        max_workers=2,
    )

    assert results[0].failed is False
    assert results[0].value == {"rows": []}
    assert results[1].failed is True
    assert "returned 200 with no message" in results[1].error
