"""One place to build the LLM and parse its JSON. The generator uses it to read cards
and propose tests; the extractor uses it to READ the agent's answer. Neither uses it to
JUDGE — every verdict is oracle vs comparator.

The key comes from the environment (or a .env file next to config.yaml). Missing key is
an error in generated mode, never a silent skip — that is how the original bug hid."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Protocol


class LLM(Protocol):
    def complete(self, system: str, human: str) -> str: ...


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.getenv(k):
            os.environ[k] = v


def have_key() -> bool:
    load_dotenv()
    return bool(os.getenv("ANTHROPIC_API_KEY"))


class AnthropicLLM:
    # 4000 was too small: generate_tests writes long JSON and the reply was being cut
    # mid-word, so the array never closed and nothing parsed. Callers also batch their
    # work into several smaller calls rather than one big one.
    def __init__(self, model: str, temperature: float = 0.2, max_tokens: int = 8000):
        from langchain_anthropic import ChatAnthropic
        self._llm = ChatAnthropic(model=model, temperature=temperature, max_tokens=max_tokens,
                                  timeout=120, max_retries=2)   # a hung model call must not hang the run
        self.model = model
        self.calls = 0
        self.truncated = 0

    def complete(self, system: str, human: str) -> str:
        self.calls += 1
        msg = self._llm.invoke([("system", system), ("human", human)])
        if (msg.response_metadata or {}).get("stop_reason") == "max_tokens":
            self.truncated += 1
        c = msg.content
        return c if isinstance(c, str) else "".join(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else "") for b in c)


def make_llm(model: str) -> LLM:
    if not have_key():
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. The generated suite needs it to read the agent card "
            "and write tests. Put it in .env or the environment, or run --suite regression "
            "(hand-written DNS/TLS tests only).")
    return AnthropicLLM(model)


def strip_fences(text: str) -> str:
    """Remove markdown code fences. Handles the UNTERMINATED case too: a truncated reply
    opens ```json and never closes it, and the old anchored regex left the opener in."""
    t = text.strip()
    t = re.sub(r"^\s*```[a-zA-Z0-9_-]*[ \t]*\r?\n?", "", t)   # opening fence, closed or not
    t = re.sub(r"\r?\n?[ \t]*```\s*$", "", t)                  # closing fence, if it arrived
    return t.strip()


def salvage_list(text: str) -> list:
    """Recover the complete objects from a TRUNCATED JSON array.

    A reply cut off at max_tokens ends mid-object, so the array never closes and strict
    parsing yields nothing at all — nine good tests thrown away because the tenth was
    half-written. Walk the array and keep every element that decoded cleanly."""
    t = strip_fences(text)
    i = t.find("[")
    if i < 0:
        return []
    dec, out, j = json.JSONDecoder(), [], i + 1
    while j < len(t):
        while j < len(t) and t[j] in ", \t\r\n":
            j += 1
        if j >= len(t) or t[j] == "]":
            break
        try:
            obj, end = dec.raw_decode(t, j)
        except json.JSONDecodeError:
            break                      # the truncated tail — everything before it is good
        out.append(obj)
        j = end
    return out


def parse_json(text: str, want: str = "any") -> Any:
    """Pull the first JSON object/array out of a model reply (tolerates fences/prose,
    and recovers the complete items from a reply that was cut off mid-write)."""
    t = strip_fences(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    opener = "[" if want == "list" else ("{" if want == "dict" else None)
    starts = [i for i, ch in enumerate(t) if ch in ("[", "{")] if opener is None else [i for i, ch in enumerate(t) if ch == opener]
    for i in starts:
        dec = json.JSONDecoder()
        try:
            obj, _ = dec.raw_decode(t[i:])
            return obj
        except json.JSONDecodeError:
            continue
    if want == "list":
        rescued = salvage_list(t)
        if rescued:
            return rescued
    raise ValueError(f"no JSON found in model reply: {text[:120]!r}")
