"""ingestion.tabular_ingest: the streaming JSON array reader's resource bounds (#920).

A definite syntax error is refused where it is found instead of after reading the rest
of the content, an incomplete element is bounded by ``max_element_chars``, and valid
content split at any chunk boundary still parses.
"""

from __future__ import annotations

import io
import json
import tracemalloc
from collections.abc import Iterator

import pytest

from kpubdata_builder.ingestion import IngestionError, tabular_ingest
from kpubdata_builder.ingestion.tabular_ingest import (
    _JsonArrayReader,
    _may_be_incomplete,
    iter_tabular_batches,
)

_CHUNK = 64 * 1024


class _Chunks:
    """Text chunks that count how many were handed out."""

    def __init__(self, head: str, *, spaces_mib: int, tail: str = "]") -> None:
        self.head = head
        self.spaces_mib = spaces_mib
        self.tail = tail
        self.taken = 0
        self.chars = 0

    def __iter__(self) -> Iterator[str]:
        pieces = [self.head]
        pieces += [" " * _CHUNK] * (self.spaces_mib * 1024 * 1024 // _CHUNK)
        pieces.append(self.tail)
        for piece in pieces:
            self.taken += 1
            self.chars += len(piece)
            yield piece


def _read(chunks: Iterator[str], **kwargs: int) -> list[object]:
    return list(_JsonArrayReader(chunks, **kwargs).elements())


# A document whose elements hold every kind of token a chunk boundary can cut: strings
# with escapes, a surrogate pair escape, non-ASCII text, numbers with fraction and
# exponent, every literal Python's decoder accepts, and nesting.
_TRICKY = (
    '[{"s": "a\\"b\\\\c\\/d\\n\\t\\u00e9\\ud83d\\ude00", "k\\u00e9y": "한글😀",'
    ' "n": [-0, 12345, -1.5e-7, 6.02E+23, 0.25, 1e3], "l": [true, false, null],'
    ' "x": [NaN, Infinity, -Infinity], "o": {"p": {"q": []}}},'
    ' {"empty": "", "e": {}}, {"last": 9}]'
)


def _same(left: object, right: object) -> bool:
    # NaN never equals itself, so compare the canonical text.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def test_a_malformed_first_element_is_refused_without_reading_to_the_end() -> None:
    # The #920 reproduction: before the fix every chunk was read (32 MiB, 2 s, a buffer
    # the size of the content) before the error surfaced.
    chunks = _Chunks('[{"broken": INVALID},', spaces_mib=32)
    tracemalloc.start()
    try:
        with pytest.raises(IngestionError, match="failed to parse json content: Expecting"):
            _read(iter(chunks))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert chunks.taken == 1
    assert chunks.chars < 100
    assert peak < 1024 * 1024


def test_a_malformed_element_after_a_chunk_boundary_is_refused_at_once() -> None:
    # The cut text first looks incomplete ("tru"); the next chunk settles it.
    for head, rest in (("tru", "x"), ("1.", "x"), ("1e", "x"), ('"\\u12', "zz"), ("-", "-")):
        chunks = iter([f'[{{"a": {head}', f"{rest}}}]", *[" " * _CHUNK] * 64])
        with pytest.raises(IngestionError, match="failed to parse json content"):
            _read(chunks)
        # Reading on at least doubles the buffered element, so one chunk past the one
        # holding the error may be read — never the rest.
        assert len(list(chunks)) >= 63, head


def test_an_incomplete_element_stops_at_the_element_limit() -> None:
    limit = 1024 * 1024
    chunks = _Chunks('[{"a": 1', spaces_mib=8)

    with pytest.raises(IngestionError, match="MAX_JSON_ELEMENT_CHARS") as caught:
        _read(iter(chunks), max_element_chars=limit)

    assert "each element must fit" in str(caught.value)
    assert chunks.chars <= limit + 2 * _CHUNK


def test_an_incomplete_element_within_the_limit_still_fails_at_the_end() -> None:
    with pytest.raises(IngestionError, match="Expecting ',' delimiter"):
        _read(iter(_Chunks('[{"a": 1', spaces_mib=1)), max_element_chars=4 * 1024 * 1024)


def test_the_limit_bounds_one_element_not_the_whole_array() -> None:
    element = json.dumps({"v": "x" * 1000})
    count = 5000  # about 5 MiB of array, each element ~1 KiB
    text = "[" + ",".join([element] * count) + "]"
    chunks = (text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK))

    assert len(_read(chunks, max_element_chars=4096)) == count


def test_a_large_element_within_the_limit_is_decoded_a_few_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = {"big": "y" * (4 * 1024 * 1024)}
    text = "[" + json.dumps(value) + "]"
    reader = _JsonArrayReader(text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK))
    decoder = reader._decoder
    attempts = 0

    def counting(s: str, idx: int = 0) -> tuple[object, int]:
        nonlocal attempts
        attempts += 1
        return decoder.raw_decode(s, idx)

    monkeypatch.setattr(reader, "_decoder", type("Counting", (), {"raw_decode": counting}))

    assert list(reader.elements()) == [value]
    # The buffer at least doubles between attempts: 64 chunks need about log2(64) tries,
    # not one per chunk.
    assert attempts <= 10


def test_many_small_elements_still_stream() -> None:
    element = '{"id": 1, "name": "row"},'
    per_chunk = _CHUNK // len(element)
    taken = 0

    def chunks() -> Iterator[str]:
        nonlocal taken
        yield "["
        for _ in range(64):  # ~4 MiB in all
            taken += 1
            yield element * per_chunk
        yield '{"id": 2}]'

    tracemalloc.start()
    try:
        elements = _JsonArrayReader(chunks()).elements()
        next(elements)
        assert taken == 1
        count = 1 + sum(1 for _ in elements)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert count == 64 * per_chunk + 1
    assert peak < 1024 * 1024


def test_every_two_chunk_split_parses_like_the_whole_text() -> None:
    expected = json.loads(_TRICKY)
    for cut in range(len(_TRICKY) + 1):
        got = _read(iter([_TRICKY[:cut], _TRICKY[cut:]]))
        assert _same(got, expected), cut


def test_one_character_chunks_parse_like_the_whole_text() -> None:
    assert _same(_read(iter(list(_TRICKY))), json.loads(_TRICKY))


def test_every_prefix_of_a_valid_value_counts_as_incomplete() -> None:
    decoder = json.JSONDecoder()
    element = _TRICKY[1 : _TRICKY.index("}, {") + 1]
    for cut in range(len(element)):
        try:
            decoder.raw_decode(element[:cut])
        except json.JSONDecodeError as exc:
            assert _may_be_incomplete(exc), element[:cut]


def test_utf8_split_across_byte_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _TRICKY.encode("utf-8")
    expected = json.loads(_TRICKY)
    for size in range(1, 6):
        monkeypatch.setattr(tabular_ingest, "_CHUNK_BYTES", size)
        batches = iter_tabular_batches(io.BytesIO(raw), format="json")
        assert _same([r for b in batches for r in b], expected), size


def test_a_malformed_non_array_document_is_refused_without_reading_to_the_end() -> None:
    chunks = _Chunks('{"a": INVALID}', spaces_mib=8, tail="")

    with pytest.raises(IngestionError, match="failed to parse json content"):
        _read(iter(chunks))

    assert chunks.taken == 1


def test_a_large_non_array_document_is_bounded_by_the_element_limit() -> None:
    chunks = _Chunks('{"a": "', spaces_mib=8, tail='"}')

    with pytest.raises(IngestionError, match="MAX_JSON_ELEMENT_CHARS"):
        _read(iter(chunks), max_element_chars=1024 * 1024)

    assert chunks.chars <= 1024 * 1024 + 2 * _CHUNK


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'{"data": [{"id": 1}]}', "array of objects"),
        (b'{"a": 1} {"b": 2}', "extra data after the top-level value"),
        (b"   ", "failed to parse json content: Expecting value"),
        (b'[{"a": 2.', "failed to parse json content: Expecting ','"),
    ],
)
def test_non_array_and_cut_documents_keep_their_errors(raw: bytes, message: str) -> None:
    with pytest.raises(IngestionError, match=message):
        list(iter_tabular_batches(io.BytesIO(raw), format="json"))
