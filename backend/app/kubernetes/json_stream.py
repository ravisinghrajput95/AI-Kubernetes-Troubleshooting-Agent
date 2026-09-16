"""Read a kubectl list document without ever holding all of it.

F5's built half caps how many objects a list read *retains*
(`app/kubernetes/list_limit.py`). Its remaining half is the *spike*: the cap
was applied to a document `json.loads` had already built in full, out of a
string `subprocess.run` had already buffered in full, so one read of a large
cluster transiently cost two copies of it. Measured through the real executor
with `scripts/payload_bench.py --parse-scan`: peak parse 5.9 MB at 2,000 pods,
29.7 MB at 10,000, 74.3 MB at 25,000 — about 2.95 KB per pod — while retained
stayed flat at 1.09 MB. `docs/PRODUCTION_READINESS.md` recorded it as open and
`kubectl --chunk-size` does not close it: that bounds the API server and etcd
per request, and kubectl still assembles the whole list before printing it.

The shape of a list response is what makes streaming possible without a
dependency: `{"apiVersion": …, "kind": …, "metadata": {…}, "items": [ … ]}`,
where everything except `items` is a few hundred bytes. So this scans the text
up to the top-level `items` array — the only hand-written scanning here, and it
covers a tiny prefix — then decodes the array one element at a time with
`json.JSONDecoder.raw_decode`, which is the stdlib's own parser and therefore
the only thing that has to be right about escapes, unicode and nesting.
Elements past the cap are decoded, counted and dropped rather than kept, so
`returned` in a truncation record is still the number the cluster returned.

**Anything that is not a list is parsed whole**, deliberately: a named read is
one object and small, and a reader that guessed would be a second rule to keep
in step with `cap_items`. The document this returns is the capped one, so the
caller's text and its data cannot disagree about what was read.
"""

import json
from typing import Any, TextIO

CHUNK_SIZE = 65_536
_DECODER = json.JSONDecoder()
_WHITESPACE = " \t\n\r"


class JsonStreamError(ValueError):
    """The stream did not hold a JSON document this reader could finish."""


def read_capped_list(stream: TextIO, limit: int, chunk_size: int = CHUNK_SIZE) -> tuple[Any, int]:
    """Parse a JSON document from `stream`, keeping at most `limit` items.

    Returns the document and the number of items the cluster returned, which is
    larger than the number kept whenever the cap applied. `limit <= 0` keeps
    every item, matching `cap_items`.
    """
    reader = _Reader(stream, chunk_size)
    prefix = reader.scan_to_items()
    if prefix is None:
        return reader.decode_all(), 0

    kept: list[Any] = []
    total = 0
    for element in reader.elements():
        total += 1
        if limit <= 0 or total <= limit:
            kept.append(element)

    envelope = reader.finish(prefix)
    envelope["items"] = kept
    return envelope, total


class _Reader:
    def __init__(self, stream: TextIO, chunk_size: int) -> None:
        self._stream = stream
        self._chunk = chunk_size
        self._buffer = ""
        self._pos = 0
        self._eof = False

    # -- buffer ---------------------------------------------------------- #

    def _fill(self) -> bool:
        """Read one more chunk. False at end of stream."""
        if self._eof:
            return False
        block = self._stream.read(self._chunk)
        if not block:
            self._eof = True
            return False
        self._buffer += block
        return True

    def _compact(self) -> None:
        """Drop what has been consumed, so the buffer holds one element at most."""
        if self._pos:
            self._buffer = self._buffer[self._pos :]
            self._pos = 0

    def _at(self) -> str | None:
        """The next non-whitespace character, or None at end of stream."""
        while True:
            while self._pos < len(self._buffer):
                if self._buffer[self._pos] not in _WHITESPACE:
                    return self._buffer[self._pos]
                self._pos += 1
            self._compact()
            if not self._fill():
                return None

    # -- phases ---------------------------------------------------------- #

    def scan_to_items(self) -> str | None:
        """Consume up to the `[` of the top-level `items` array.

        Returns the text before it — the document's opening, which a list
        response keeps small — or None when there is no such array, in which
        case nothing has been consumed and the whole document is still to parse.

        Character by character, with no lookahead: the first version decided by
        peeking at the text after the key, which is text that has not been read
        yet when the reader is fed in small chunks, so it found `items` at
        64 KiB and never at 1 byte. A parser that works only when its input
        arrives in big enough pieces is a parser that works until a slow
        cluster streams.
        """
        depth = 0
        in_string = False
        escaped = False
        key_start: int | None = None
        pending: str | None = None
        stage: str | None = None  # None | "colon" | "value"
        index = 0

        while True:
            while index < len(self._buffer):
                char = self._buffer[index]

                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                        if depth == 1 and key_start is not None:
                            pending, stage = self._buffer[key_start:index], "colon"
                        key_start = None
                    index += 1
                    continue

                if char in _WHITESPACE:
                    index += 1
                    continue

                if char == '"':
                    in_string = True
                    key_start = index + 1
                    index += 1
                    continue

                if stage == "colon":
                    if char == ":":
                        stage = "value"
                        index += 1
                        continue
                    pending, stage = None, None
                elif stage == "value":
                    if char == "[" and depth == 1 and pending == "items":
                        prefix = self._buffer[:index]
                        self._pos = index + 1
                        return prefix
                    pending, stage = None, None

                if char in "{[":
                    depth += 1
                elif char in "}]":
                    depth -= 1
                    pending, stage = None, None
                index += 1

            if not self._fill():
                return None

    def elements(self):
        """Yield each element of the array whose `[` has been consumed."""
        while True:
            char = self._at()
            if char is None:
                raise JsonStreamError("the items array never closed")
            if char == "]":
                self._pos += 1
                return
            if char == ",":
                self._pos += 1
                continue
            yield self._decode_one()

    def _decode_one(self) -> Any:
        """Decode one element, and only once it is certainly whole.

        **A value that parses is not a value that finished.** `raw_decode`
        reads `3` out of `3.` and reports success, and `1234` out of
        `12345678901234`, so an element split across reads came back as a
        different number — found by fuzzing this against `json.loads` one byte
        at a time, which is the only reason it is not in production. Every
        element of an array is followed by `,` or `]`, so the delimiter is what
        says it ended; anything else means the value continues into text that
        has not arrived.
        """
        while True:
            try:
                element, end = _DECODER.raw_decode(self._buffer, self._pos)
            except ValueError:
                # Either the element is not all here yet, or the document is
                # malformed; only end of stream can tell the two apart.
                if not self._fill():
                    raise JsonStreamError("an item was incomplete or malformed") from None
                continue

            index = end
            while index < len(self._buffer) and self._buffer[index] in _WHITESPACE:
                index += 1
            if index >= len(self._buffer):
                if self._fill():
                    continue
                raise JsonStreamError("the items array never closed")
            if self._buffer[index] not in ",]":
                if self._fill():
                    continue
                raise JsonStreamError("an item was incomplete or malformed")

            self._pos = end
            self._compact()
            return element

    def finish(self, prefix: str) -> dict[str, Any]:
        """Parse the document around the items array, which is small."""
        while self._fill():
            pass
        suffix = self._buffer[self._pos :]
        self._buffer = ""
        self._pos = 0
        try:
            envelope = json.loads(f"{prefix}[]{suffix}")
        except ValueError as exc:
            raise JsonStreamError(f"the document around its items did not parse: {exc}") from exc
        if not isinstance(envelope, dict):
            raise JsonStreamError("a list response must be an object")
        return envelope

    def decode_all(self) -> Any:
        """The document is not a list: parse it whole, which is the small case."""
        while self._fill():
            pass
        text = self._buffer[self._pos :]
        self._buffer = ""
        self._pos = 0
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError as exc:
            raise JsonStreamError(str(exc)) from exc
