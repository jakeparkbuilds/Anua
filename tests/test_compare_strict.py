"""The bool comparator must never invent a verdict from a value it cannot read.

Before this, Python truthiness made every unrecognised non-empty string True, so an
agent saying "certificate validation failed" was graded as if it had said "valid".
"""
from __future__ import annotations
import pytest

from bench.assertions.compare import UNKNOWN, _b, compare


@pytest.mark.parametrize("value", [
    "certificate validation failed",
    "not valid",            # negative phrase, must not read as True
    "chain is broken",
    "unknown",
    "maybe",
    "see evidence below",
    "",
    "2026-12-04",
    200,
    443,
    3.5,
])
def test_unreadable_values_never_pass_as_true(value):
    """Either interpreted correctly, or flagged — never silently True."""
    coerced = _b(value)
    assert coerced is not True, f"{value!r} was read as True"


def test_unrecognised_string_fails_with_explanatory_evidence():
    ok, ev = compare("bool", True, "see the evidence section")
    assert not ok
    assert "could not interpret" in ev
    # the offending value must appear, so the report says what went wrong
    assert "see the evidence section" in ev


def test_negative_phrases_read_as_false():
    for word in ("failed", "error", "not valid", "invalid", "untrusted", "expired", "missing"):
        assert _b(word) is False, word


def test_positive_words_read_as_true():
    for word in ("true", "yes", "valid", "ok", "present", "trusted", "reachable"):
        assert _b(word) is True, word


def test_coercion_tolerates_punctuation_and_case():
    assert _b(" Valid. ") is True
    assert _b("FAILED!") is False


def test_agent_saying_failed_is_graded_against_expected_false():
    """The regression that matters: expected False + agent 'failed' should PASS."""
    ok, _ = compare("bool", False, "failed")
    assert ok
    ok, _ = compare("bool", True, "failed")
    assert not ok


def test_zero_and_one_are_booleans_other_numbers_are_not():
    assert _b(1) is True
    assert _b(0) is False
    assert _b(200) is UNKNOWN


def test_collections_use_presence_semantics():
    """Empty record list = absent. This is how SPF/MX lookups arrive."""
    assert _b([]) is False
    assert _b(["v=spf1 -all"]) is True


def test_unusable_oracle_value_is_reported_not_guessed():
    ok, ev = compare("bool", "something odd", True)
    assert not ok
    assert "not a usable boolean" in ev


def test_eq_ignores_representation_differences():
    assert compare("eq", 200, "200")[0]
    assert compare("eq", "ISRG Root X1", "isrg root x1")[0]
    assert compare("eq", 1.0, "1")[0]


def test_eq_still_rejects_real_mismatches():
    assert not compare("eq", 200, "404")[0]
    assert not compare("eq", "DigiCert", "Let's Encrypt")[0]
    # a bool must not compare equal to a number
    assert not compare("eq", True, "1")[0]


def test_none_is_still_extraction_failure():
    ok, ev = compare("bool", False, None)
    assert not ok and "extraction failed" in ev
