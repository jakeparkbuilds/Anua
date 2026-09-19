"""Tier 3 — classical ML quality scoring. Deterministic, cheap, clearly labelled.
NEVER changes pass/fail. Only contributes to the small `quality` weight in behavior_score.

  readability : Flesch reading ease (0-100, higher = simpler) — implemented inline, no dep
  grounding   : fraction of oracle-known values that literally appear in the response
                (catches invented numbers/dates without an LLM)
  actionability: share of sentences containing an imperative/remediation cue
"""
from __future__ import annotations
import re
from typing import Any, Iterable
from ..models import QualityScores

_VOWELS = re.compile(r"[aeiouy]+")
_IMPERATIVE = re.compile(r"\b(renew|replace|enable|configure|add|remove|update|fix|install|set|rotate|should|must|recommend)\b", re.I)


def _syllables(word: str) -> int:
    w = word.lower().strip(".,;:!?()\"'")
    if not w: return 0
    n = len(_VOWELS.findall(w))
    if w.endswith("e") and n > 1: n -= 1
    return max(n, 1)


def flesch(text: str) -> float:
    sents = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    words = re.findall(r"[A-Za-z']+", text)
    if not sents or not words:
        return 0.0
    syl = sum(_syllables(w) for w in words)
    score = 206.835 - 1.015 * (len(words) / len(sents)) - 84.6 * (syl / len(words))
    return round(max(0.0, min(100.0, score)), 1)


def grounding(text: str, known_values: Iterable[Any]) -> float:
    vals = [str(v) for v in known_values if v not in (None, "", [], True, False)]
    if not vals:
        return 1.0
    t = text.lower()
    hits = sum(1 for v in vals if v.lower() in t)
    return round(hits / len(vals), 2)


def actionability(text: str) -> float:
    sents = [s for s in re.split(r"[.!?\n]+", text) if s.strip()]
    if not sents:
        return 0.0
    return round(sum(1 for s in sents if _IMPERATIVE.search(s)) / len(sents), 2)


def score(response_text: str, known_values: Iterable[Any]) -> QualityScores:
    return QualityScores(
        readability=flesch(response_text),
        grounding=grounding(response_text, known_values),
        actionability=actionability(response_text),
        confidence="low",
        method="classical",
    )
