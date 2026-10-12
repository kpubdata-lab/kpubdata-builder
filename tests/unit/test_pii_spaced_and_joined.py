"""Value patterns for a spaced mobile number and a joined resident number (#1296).

No value here is, or could be, a person's resident number. The joined ones are either
"born" in a year that has not come, or built at run time for a date nobody alive was
born on.
"""

from __future__ import annotations

import pytest

from kpubdata_builder.stages.silver.pii import VALUE_PATTERNS

_RRN = VALUE_PATTERNS["rrn"]
_PHONE = VALUE_PATTERNS["phone"]

_WEIGHTS = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)


def _with_check_digit(first_twelve: str) -> str:
    total = sum(int(d) * w for d, w in zip(first_twelve, _WEIGHTS, strict=True))
    return first_twelve + str((11 - total % 11) % 10)


#: A birth on 1 January 1900, with the digit that checks.
_BORN_1900 = _with_check_digit("000101" + "100000")
#: The same number with another last digit: thirteen digits that do not check.
_BORN_1900_WRONG = _BORN_1900[:-1] + str((int(_BORN_1900[-1]) + 1) % 10)
#: A birth on 31 December 2099: issued, if ever, after the last digit stopped checking.
_BORN_2099 = "9912313000000"


# ------------------------------------------------------------------ mobile


@pytest.mark.parametrize(
    "value",
    [
        "010 0123 4567",
        "010 123 4567",
        "010-0123 4567",
        "연락처 010 0123 4567 입니다",
        # As before.
        "010-0123-4567",
        "01001234567",
    ],
)
def test_a_mobile_number_is_found_however_its_blocks_are_joined(value: str) -> None:
    assert _PHONE.search(value)


@pytest.mark.parametrize(
    "value",
    [
        "02 123 4567",  # not a mobile prefix
        "010 01 4567",  # a block too short
        "0100 123 45678",  # digits that run on past a number
        "010  0123  4567",  # two spaces: not one number written with spaces
        "1010 0123 4567",
    ],
)
def test_other_spaced_digits_are_not_a_mobile_number(value: str) -> None:
    assert not _PHONE.search(value)


# ------------------------------------------------------------------ resident number


@pytest.mark.parametrize(
    "value", [_BORN_1900, _BORN_2099, f"번호 {_BORN_2099} 확인", f"{_BORN_1900},"]
)
def test_a_joined_resident_number_is_found(value: str) -> None:
    assert _RRN.search(value)


@pytest.mark.parametrize(
    ("value", "why"),
    [
        (_BORN_1900_WRONG, "a 1900s number whose last digit does not check"),
        ("9913313000000", "month 13"),
        ("9900313000000", "month 00"),
        ("9902303000000", "30 February"),
        ("9912003000000", "day 00"),
        ("9912315000000", "a seventh digit that is not 1 to 4"),
        ("99123130000000", "fourteen digits"),
        ("991231300000", "twelve digits"),
        ("A9912313000000", "part of a longer word"),
    ],
)
def test_thirteen_digits_that_cannot_be_a_resident_number_are_not_one(value: str, why: str) -> None:
    assert not _RRN.search(value), why


@pytest.mark.parametrize(
    "value",
    [
        "900101-1234567",
        # The hyphenated form is asked nothing more than before: no date, no check digit.
        "991399-1000000",
        "000000-4999999",
    ],
)
def test_the_hyphenated_form_is_found_exactly_as_before(value: str) -> None:
    assert _RRN.search(value)


def test_a_cell_with_both_forms_is_one_finding_of_its_kind() -> None:
    assert _RRN.search(f"900101-1234567 / {_BORN_2099}")


def test_a_code_that_reads_as_a_2000s_birth_is_still_taken_for_one() -> None:
    """The limit of the rule, written down: nothing tells such a code from a number.

    A Korean barcode (prefix 880) whose next digits happen to be a day of January to
    September and whose seventh digit is 3 or 4. About six in a hundred such barcodes
    are found; the gate's answer to a finding that is not PII is `pii.allowed_findings`.
    """
    assert _RRN.search("8801234567893")


def test_few_thirteen_digit_codes_are_taken_for_a_resident_number() -> None:
    """The measurement behind the rule, kept as a bound (#1296).

    Thirteen digits with a seventh of 1 to 4 are two in five of all thirteen-digit
    codes. Asked for a date and, for a 1900s birth, a check digit, they are under one
    in a hundred.
    """
    import random

    rng = random.Random(1296)
    codes = ["".join(str(rng.randint(0, 9)) for _ in range(13)) for _ in range(50_000)]

    shaped = sum(1 for code in codes if code[6] in "1234")
    found = sum(1 for code in codes if _RRN.search(code))

    assert shaped / len(codes) > 0.38
    assert found / len(codes) < 0.012
