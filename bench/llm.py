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
    def __init__(self, model: str, temperature: float = 0.2, max_tokens: int = 4000):
        from langchain_anthropic import ChatAnthropic
        self._llm = ChatAnthropic(model=model, temperature=temperature, max_tokens=max_tokens)
        self.model = model
        self.calls = 0

    def complete(self, system: str, human: str) -> str:
        self.calls += 1
        msg = self._llm.invoke([("system", system), ("human", human)])
        c = msg.content
        return c if isinstance(c, str) else "".join(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else "") for b in c)


def make_llm(model: str) -> LLM:
    if not have_key():
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. The generated suite needs it to read the agent card "
            "and write tests. Put it in .env or the environment, or run --suite regression "
            "(hand-written DNS/TLS tests only).")
    return AnthropicLLM(model)


def parse_json(text: str, want: str = "any") -> Any:
    """Pull the first JSON object/array out of a model reply (tolerates fences/prose)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
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
    raise ValueError(f"no JSON found in model reply: {text[:120]!r}")
