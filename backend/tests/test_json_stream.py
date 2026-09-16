"""Reading a list response without building it (F5's remaining half).

The cap on retained items was applied to a document `json.loads` had already
built in full, so peak parse memory was proportional to the cluster: 5.9 MB at
2,000 pods, 29.7 MB at 10,000, 74.3 MB at 25,000, against a retained 1.09 MB.

The property that matters is that the reader agrees with `json.loads` on every
document it is given, because the moment it does not, evidence quietly differs
from what the cluster said. So the first test is a seeded fuzz against the
stdlib rather than a handful of examples, and the second is the measurement the
change exists for.
"""

import io
import json
import random
import tracemalloc

import pytest

from app.kubernetes.json_stream import JsonStreamError, read_capped_list


def value(rng: random.Random, depth: int = 0):
    """A JSON value, with the shapes a hand-written scanner gets wrong."""
    choices = ["string", "number", "bool", "null"]
    if depth < 3:
        choices += ["object", "array"]
    kind = rng.choice(choices)
    if kind == "string":
        return rng.choice(
            [
                'quotes " and \\ backslashes',
                "braces { } and brackets [ ]",
                '"items": [ not really ]',
                "unicode ✓ ✗ — ü",
                "new\nline\tand\ttabs",
                "",
                "\\\\",
                '\\"',
            ]
        )
    if kind == "number":
        return rng.choice([0, -1, 3.5, 1e10, 12345678901234])
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "null":
        return None
    if kind == "object":
        return {f"key{index}": value(rng, depth + 1) for index in range(rng.randint(0, 3))}
    return [value(rng, depth + 1) for _ in range(rng.randint(0, 3))]


def document(rng: random.Random, items: int) -> dict:
    body = {
        "apiVersion": "v1",
        "kind": "PodList",
        "metadata": {"resourceVersion": "12345", "note": 'a } and a ] and "items"'},
        "items": [value(rng, 1) for _ in range(items)],
    }
    keys = list(body)
    rng.shuffle(keys)  # key order is the server's, not ours
    return {key: body[key] for key in keys}


@pytest.mark.parametrize("seed", range(25))
def test_it_reads_what_json_loads_reads(seed):
    rng = random.Random(seed)
    original = document(rng, rng.randint(0, 12))
    text = json.dumps(original, indent=rng.choice([None, 2]))

    data, total = read_capped_list(io.StringIO(text), limit=0, chunk_size=rng.choice([1, 7, 4096]))

    assert data == original
    assert total == len(original["items"])


def test_a_document_that_is_not_a_list_is_parsed_whole():
    pod = {"kind": "Pod", "metadata": {"name": "web-0"}, "spec": {"containers": []}}

    data, total = read_capped_list(io.StringIO(json.dumps(pod)), limit=5)

    assert data == pod
    assert total == 0


def test_an_empty_read_is_an_empty_document():
    assert read_capped_list(io.StringIO(""), limit=5) == ({}, 0)


def test_the_cap_keeps_the_first_items_and_counts_them_all():
    listing = {"kind": "PodList", "items": [{"n": index} for index in range(50)]}

    data, total = read_capped_list(io.StringIO(json.dumps(listing)), limit=3)

    assert data["items"] == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert total == 50
    assert data["kind"] == "PodList"


def test_a_truncated_stream_is_an_error_not_a_short_list():
    """Half a document is not a small cluster."""
    listing = json.dumps({"kind": "PodList", "items": [{"n": index} for index in range(10)]})

    with pytest.raises(JsonStreamError):
        read_capped_list(io.StringIO(listing[: len(listing) // 2]), limit=100)


def test_items_belonging_to_something_else_are_not_the_list():
    body = {
        "kind": "ConfigMap",
        "data": {"items": [1, 2, 3]},
        "items": [{"n": 1}, {"n": 2}],
    }

    data, total = read_capped_list(io.StringIO(json.dumps(body)), limit=1)

    assert total == 2
    assert data["items"] == [{"n": 1}]
    assert data["data"] == {"items": [1, 2, 3]}


def test_it_does_not_hold_the_document_it_is_capping():
    """The measurement the change exists for: peak heap while reading 20,000
    items with a cap of 200, against the size of the text being read."""
    pod = {
        "metadata": {"name": "web", "namespace": "prod", "labels": {"app": "web"}},
        "spec": {"containers": [{"name": "web", "image": "registry.example.com/web:1.2.3"}]},
        "status": {"phase": "Running", "podIP": "10.1.2.3"},
    }
    text = json.dumps({"kind": "PodList", "items": [pod] * 20_000})
    assert len(text) > 4_000_000, "the fixture must be big enough to tell the two apart"

    # Built before the measurement: `io.StringIO(text)` copies the text, and
    # counting that copy measures the fixture rather than the reader.
    stream = io.StringIO(text)
    tracemalloc.start()
    data, total = read_capped_list(stream, limit=200)
    _, streamed = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert total == 20_000
    assert len(data["items"]) == 200

    tracemalloc.start()
    whole = json.loads(text)
    _, built = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(whole["items"]) == 20_000

    # Reading the stream costs the cap, not the cluster: an order of magnitude
    # below building the same document, and far below the text itself.
    assert streamed < built / 10, (streamed, built)
    assert streamed < len(text) / 4, (streamed, len(text))
