"""Run store: cache, in-flight joins, and the event log the page streams.

CACHING IS DEMO INSURANCE. dnsdoc degrades under repeated load — one run mid-sequence
scored 36, then 87 again on retry. A completed report is cached in memory keyed by
(host, suite) and served by default with "cached": true and the ORIGINAL run timestamp;
force=True bypasses. Every report is also written to out/ by the pipeline as always, so
after a restart the cache is warmed from disk before the first request.

Concurrent requests for the same (host, suite) join the one in-flight run instead of
starting a second; the second caller gets the same events and the same report.
"""
from __future__ import annotations
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import load, Config
from ..ans.registry import Registry, _first
from ..models import Report
from .. import pipeline

SUITES = ("generated", "regression", "both")
# Benchmarked at startup so the demo path is always a cache hit. BENCH_PREWARM overrides
# (comma-separated hosts; empty string disables).
PREWARM_HOSTS = tuple(h.strip() for h in os.getenv(
    "BENCH_PREWARM", "dnsdoc.webmesh.ai,impact.webmesh.ai,seo.webmesh.ai,anuabot.vip").split(",") if h.strip())
UNKNOWN_HOST_MAX_TESTS = 20      # a first-time host gets a smaller generated suite
STEPS = ("registry", "tl", "fingerprint", "card", "tests", "run")
STEP_LABELS = {
    "registry": "Discovered in ANS registry",
    "tl": "Transparency log entry found",
    "fingerprint": "Certificate fingerprint matched",
    "card": "Read agent card",
    "tests": "Generating tests",
    "run": "Running assertions",
}
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$", re.I)


def parse_agent(text: str) -> Optional[str]:
    """Pull a host out of free text: an ans:// name, a URL, or a bare domain. The ans
    name form ans://v1.0.6.dnsdoc.webmesh.ai carries the version as leading labels."""
    text = (text or "").strip()
    m = re.search(r"ans://v?[0-9]+(?:\.[0-9]+)*\.([a-z0-9.-]+)", text, re.I)
    if m and _DOMAIN.match(m.group(1)):
        return m.group(1).lower().rstrip(".")
    m = re.search(r"https?://([a-z0-9.-]+)", text, re.I)
    if m and _DOMAIN.match(m.group(1)):
        return m.group(1).lower().rstrip(".")
    for w in re.split(r"[\s,;:()<>\"'`]+", text):
        w = w.strip(".?!").lower()
        if _DOMAIN.match(w) and not w.startswith("e.g"):
            return w
    return None


_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$", re.I)


def resolve_name(name: str, store: "RunStore") -> Optional[str]:
    """A bare name ("dnsdoc") is not a host. Try, in order: a cached report whose first
    label is the name; then the registry search (its own fuzzy ranking), keeping the
    ACTIVE hit whose host starts with the name or whose display name equals it."""
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        return None
    with store._lock:
        for (h, _s) in store._cache:
            if h.split(".")[0] == name:
                return h
    if not store.live:
        return None
    reg = Registry(store._base_cfg.ans.search_base, store._base_cfg.ans.transparency_base,
                   store._base_cfg.ans.timeout_s, live=True)
    hits = []
    for a in reg.search(name, limit=50):
        host = str(_first(a, "agentHost", "host", default="")).lower()
        disp = str(_first(a, "displayName", "name", default="")).lower()
        if not host:
            continue
        if host.split(".")[0] == name or disp == name:
            hits.append((str(_first(a, "lifecycle.status", default="")).upper() == "ACTIVE", host))
    return max(hits)[1] if hits else None


class Job:
    """One run in progress. Events are appended by the worker thread and read by any
    number of SSE subscribers, each replaying from index 0."""
    def __init__(self, host: str, suite: str):
        self.host, self.suite = host, suite
        self.events: list[dict[str, Any]] = []
        self.log: list[str] = []
        self.cond = threading.Condition()
        self.report: Optional[Report] = None
        self.error: Optional[str] = None
        self.done = False
        self.started_at = datetime.now(timezone.utc)
        self.future: Optional[Future] = None
        self._step_state: dict[str, dict] = {}

    def emit(self, kind: str, payload: dict) -> None:
        with self.cond:
            self.events.append({"event": kind, "data": payload})
            self.cond.notify_all()

    def step(self, name: str, status: str, detail: str = "") -> None:
        self._step_state[name] = {"step": name, "label": STEP_LABELS[name], "status": status, "detail": detail}
        self.emit("step", self._step_state[name])

    def finish(self, report: Optional[Report], error: Optional[str]) -> None:
        with self.cond:
            self.report, self.error, self.done = report, error, True
            self.cond.notify_all()

    def wait(self, idx: int, timeout: float) -> list[dict]:
        """Events from idx onward, blocking up to `timeout` for new ones. Empty list on
        timeout (caller sends a keepalive); the caller checks `done` itself."""
        with self.cond:
            if idx >= len(self.events) and not self.done:
                self.cond.wait(timeout)
            return list(self.events[idx:])


def _fp_short(fp: Optional[str]) -> str:
    return f"sha256 …{fp[-12:]}" if fp else ""


class RunStore:
    def __init__(self, config_path: str = "config.yaml", live: bool = True, workers: int = 3):
        self.config_path, self.live = config_path, live
        self._cache: dict[tuple[str, str], Report] = {}
        self._jobs: dict[tuple[str, str], Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bench")
        self._base_cfg: Config = load(config_path, live=live)
        self.out_dir = Path(self._base_cfg.report.out_dir)

    # ---- cache -------------------------------------------------------------------
    def warm(self) -> int:
        """Load the newest report per (host, suite) from out/. Older files that predate
        the `suite` field or a schema change are skipped, never trusted blindly."""
        for p in sorted(self.out_dir.glob("*.json")):
            if p.name == "latest.json":
                continue
            try:
                r = Report.model_validate_json(p.read_text())
            except Exception:
                continue
            if (self.live and r.mode != "live") or (not self.live and r.mode != "mock"):
                continue
            key = (r.target_host, r.suite)
            if key not in self._cache or r.run_at > self._cache[key].run_at:
                self._cache[key] = r
        return len(self._cache)

    def cached(self, host: str, suite: str) -> Optional[Report]:
        return self._cache.get((host, suite))

    def stats(self) -> dict:
        with self._lock:
            return {"cached_reports": len(self._cache), "in_flight": sum(1 for j in self._jobs.values() if not j.done),
                    "cached_keys": [f"{h}:{s}" for h, s in self._cache]}

    # ---- runs --------------------------------------------------------------------
    def get_or_start(self, host: str, suite: str = "generated", force: bool = False) -> tuple[Optional[Job], Optional[Report]]:
        """(job, None) for a run in flight or just started; (None, report) for a cache hit."""
        if suite not in SUITES:
            raise ValueError(f"suite must be one of {SUITES}")
        with self._lock:
            if not force and (host, suite) in self._cache:
                return None, self._cache[(host, suite)]
            job = self._jobs.get((host, suite))
            if job is not None and not job.done:
                return job, None                     # join the in-flight run
            job = Job(host, suite)
            self._jobs[(host, suite)] = job
            job.future = self._pool.submit(self._work, job)
            return job, None

    def wait_report(self, host: str, suite: str = "generated", force: bool = False,
                    timeout: Optional[float] = None) -> tuple[Report, bool]:
        """Blocking: the report, and whether it came from cache. Raises RuntimeError
        with the pipeline's message if the run failed."""
        job, report = self.get_or_start(host, suite, force)
        if report is not None:
            return report, True
        assert job is not None
        job.future.result(timeout=timeout)
        if job.error:
            raise RuntimeError(job.error)
        assert job.report is not None
        return job.report, False

    def _config_for(self, host: str, suite: str) -> Config:
        cfg = load(self.config_path, live=self.live)
        if cfg.target.host != host:
            cfg.target.ans_id = ""                   # the yaml's ans_id belongs to the yaml's host
        cfg.target.host = host
        cfg.suite = suite
        if not self._known(host):
            # Never seen this host: cap the generated suite so a live run on stage stays
            # short. Hosts with a generated suite already on disk keep their full cap (the
            # suite cache is keyed by cap, so a cached example run is never regenerated).
            cfg.generator.max_generated = min(cfg.generator.max_generated, UNKNOWN_HOST_MAX_TESTS)
        return cfg

    def _known(self, host: str) -> bool:
        prefix = host.replace(".", "_") + "_"
        return any(k[0] == host for k in self._cache) or any(
            p.name.startswith(prefix) for p in (self.out_dir / "tests").glob("*.json"))

    # ---- examples for the landing page ---------------------------------------------
    def examples(self, limit: int = 8) -> list[dict]:
        """Cached generated-suite reports only, highest score first, no-score last. Reads
        memory, never runs anything."""
        with self._lock:
            reports = [r for (h, s), r in self._cache.items() if s == "generated"]
        rows = [{"host": r.target_host, "score": r.behavior_score, "status": r.score_status,
                 "identity": r.identity.status, "confidence": (r.summary or {}).get("score_confidence"),
                 "run_at": r.run_at.isoformat()} for r in reports]
        rows.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0), x["host"]))
        return rows[:limit]

    def prewarm(self, hosts=PREWARM_HOSTS) -> None:
        """Benchmark the example hosts that are not already cached. Runs in the
        background after startup; every error is swallowed — a warm cache is a
        convenience, never a requirement."""
        for h in hosts:
            try:
                if self.cached(h, "generated") is None:
                    self.wait_report(h, "generated")
            except Exception:
                pass

    def _work(self, job: Job) -> None:
        try:
            cfg = self._config_for(job.host, job.suite)
            report = pipeline.run(cfg, log=job.log.append, suite=job.suite,
                                  on_stage=lambda name, msg: self._on_stage(job, name, msg),
                                  on_event=lambda name, payload: self._on_event(job, name, payload))
            degraded = any("failed" in n or "NOT cached" in n for n in (report.coverage.notes if report.coverage else []))
            # A flaky agent (some calls answered, some 5xx / timed out) produced a report
            # full of NO VERDICT. Serving that from cache on stage would be the worst
            # outcome, so it is not cached; the next request re-runs. A host that never
            # answered at all is consistent and is cached.
            errs = [a.error or "" for a in report.assertions]
            transient = any(e.startswith("agent unavailable") and ("HTTP 5" in e or "timed out" in e or "429" in e) for e in errs)
            answered = any(a.raw_response for a in report.assertions)
            if degraded or (transient and answered):
                job.log.append("report served but not cached: " + ("generation had failures" if degraded else "the agent was flaky during this run"))
            else:
                with self._lock:
                    self._cache[(job.host, job.suite)] = report
            job.finish(report, None)
        except Exception as e:                       # the page must see WHY, never hang
            msg = _explain(e, job.host)
            job.emit("error", {"message": msg})
            job.finish(None, msg)

    # ---- pipeline events -> six page steps ----------------------------------------
    def _on_stage(self, job: Job, name: str, msg: str) -> None:
        if name == "discover":
            job.step("registry", "running", f"searching ANS registry for {job.host}")
            job.step("card", "running", "fetching /.well-known/agent-card.json")
        elif name == "identity":
            job.step("tl", "running", "fetching transparency-log entry")
            job.step("fingerprint", "running", "live TLS handshake")
        elif name == "generate":
            job.step("tests", "running", "LLM reads the cards…" if "skipped" not in msg else msg)
        elif name == "run":
            job.step("run", "running", msg)

    def _on_event(self, job: Job, name: str, p: dict) -> None:
        if name == "discover":
            if p.get("registry_found"):
                job.step("registry", "done", p.get("ans_name") or "found")
            else:
                job.step("registry", "warn", "not in registry search — using the agent's own cards")
            skills = p.get("skills") or []
            if p.get("card_error"):
                job.card_error = p["card_error"]
                job.step("card", "fail", p["card_error"] + " — falling back to the registry entry")
            else:
                job.step("card", "running", f"{p.get('card_name') or job.host}: {len(skills)} skill(s) declared")
        elif name == "identity":
            if p.get("tl_entry_found"):
                job.step("tl", "done", f"agentId {p.get('ans_id') or ''}".strip())
            else:
                job.step("tl", "warn", "no transparency-log entry" + (" (registration pending)" if p.get("registry_found") else ""))
            m, sealed, live = p.get("fingerprint_match"), p.get("tl_server_fingerprint"), p.get("live_server_fingerprint")
            if m is True:
                job.step("fingerprint", "done", f"{_fp_short(live)} = sealed")
            elif m is False:
                job.step("fingerprint", "fail", f"MISMATCH live {_fp_short(live)} ≠ sealed {_fp_short(sealed)}")
            elif p.get("tl_entry_found") and not sealed:
                job.step("fingerprint", "warn", "certificate not yet sealed — PENDING")
            elif not live:
                job.step("fingerprint", "warn", "TLS handshake failed" + (f" — sealed {_fp_short(sealed)} never compared" if sealed else ""))
            else:
                job.step("fingerprint", "warn", f"no sealed baseline to compare {_fp_short(live)} against")
        elif name == "claims":
            job.step("card", "running", f"{p.get('claims_found', 0)} claims extracted, classifying…")
        elif name == "routed":
            counts = (f"{p['claims_found']} claims: {p['self']} self / {p['capability']} capability, "
                      f"{p['unverifiable']} unverifiable")
            if getattr(job, "card_error", None):     # the card never loaded; the claims came from the registry
                job.step("card", "fail", f"{job.card_error} — {counts} (from the registry entry)")
            else:
                job.step("card", "done", counts)
        elif name == "tests":
            job.step("tests", "running", f"{p.get('written', 0)} written" +
                     (f" (batch {p['batch']}/{p['batches']})" if p.get("batches") else ""))
        elif name == "generated":
            if p.get("skipped"):
                job.step("card", "done", "regression suite — claims not read")
                job.step("tests", "done", f"skipped — {p.get('regression_tests', 0)} hand-written regression tests")
            else:
                job.step("tests", "done", f"{p['capability_tests']} capability tests + {p['selfclaim_tests']} self-claim checks")
        elif name == "run":
            job.step("run", "running", f"0/{p['total']}")
        elif name == "result":
            job.step("run", "running", f"{p['done']}/{p['total']}")
        elif name == "report":
            job.step("run", "done", "graded")


def _explain(e: Exception, host: str) -> str:
    s = f"{type(e).__name__}: {e}"
    if "agent-card.json" in s or "404" in s:
        return f"{host} does not serve /.well-known/agent-card.json — not an A2A agent we can read ({s[:160]})"
    if "ANTHROPIC_API_KEY" in s:
        return "ANTHROPIC_API_KEY is not set on the server; the generated suite cannot run"
    if "Name or service not known" in s or "nodename nor servname" in s or "ConnectError" in s:
        return f"could not connect to {host} ({s[:160]})"
    return s[:400]


def cached_steps(report: Report) -> list[dict]:
    """The six steps, already ticked, for a report served from cache — so the page
    renders the same checklist whether the run was live or replayed."""
    ident, cov = report.identity, report.coverage
    steps = []
    steps.append({"step": "registry", "status": "done" if ident.registry_found else "warn",
                  "detail": ident.ans_name or ("found" if ident.registry_found else "not in registry search")})
    steps.append({"step": "tl", "status": "done" if ident.tl_entry_found else "warn",
                  "detail": f"agentId {ident.ans_id}" if ident.tl_entry_found else "no transparency-log entry"})
    if ident.fingerprint_match is True:
        steps.append({"step": "fingerprint", "status": "done", "detail": f"{_fp_short(ident.live_server_fingerprint)} = sealed"})
    elif ident.fingerprint_match is False:
        steps.append({"step": "fingerprint", "status": "fail", "detail": "MISMATCH live ≠ sealed"})
    elif ident.tl_server_fingerprint and not ident.live_server_fingerprint:
        steps.append({"step": "fingerprint", "status": "warn",
                      "detail": f"TLS handshake failed — sealed {_fp_short(ident.tl_server_fingerprint)} never compared"})
    else:
        steps.append({"step": "fingerprint", "status": "warn",
                      "detail": "certificate not yet sealed — PENDING" if ident.status == "PENDING" else "no sealed baseline"})
    card_err = getattr(report.cards, "card_error", None)
    if cov:
        counts = f"{cov.claims_found} claims: {cov.self_claims} self / {cov.capability_claims} capability, {cov.unverifiable} unverifiable"
        steps.append({"step": "card", "status": "fail" if card_err else "done",
                      "detail": f"{card_err} — {counts} (from the registry entry)" if card_err else counts})
        steps.append({"step": "tests", "status": "done",
                      "detail": f"{cov.capability_tests} capability tests + {cov.selfclaim_tests} self-claim checks"})
    else:
        steps.append({"step": "card", "status": "done", "detail": f"{len(report.cards.declared_skills)} skill(s) declared"})
        steps.append({"step": "tests", "status": "done", "detail": "regression suite — hand-written tests"})
    steps.append({"step": "run", "status": "done", "detail": f"{len(report.assertions)}/{len(report.assertions)}"})
    for s in steps:
        s["label"] = STEP_LABELS[s["step"]]
    return steps


def report_payload(report: Report, cached: bool) -> dict:
    d = report.model_dump(mode="json")
    d["cached"] = cached
    d["served_at"] = datetime.now(timezone.utc).isoformat()
    return d
