"""End-to-end orchestration. One function, one report.

Suites:
  generated   DEFAULT. The LangGraph generator reads THIS agent's card and writes its
              tests. Works on any agent. Needs ANTHROPIC_API_KEY — missing key is an error.
  regression  The 26 hand-written DNS/TLS fixtures only. Proves our grader is honest;
              only meaningful for a DNS/TLS agent such as dnsdoc.
  both        Union.
"""
from __future__ import annotations
from pathlib import Path
from typing import Callable, Optional
from .config import Config
from .ans.registry import Registry
from .ans import identity as ident_mod
from .agent import card as card_mod, transport as transport_mod, adapter
from .assertions.fixtures import regression_assertions, generic_assertions, SUITES
from .assertions.runner import run_all
from .generator.graph import generate
from .report.builder import build
from .report import emit as emit_mod
from .models import Report, CoverageReport
from .oracles.registry import SPECS

STAGES = ("discover", "identity", "generate", "transport", "run", "report")


def run(cfg: Config, generate_tests: bool | None = None, log=print, suite: str | None = None,
        llm=None, on_stage: Optional[Callable[[str, str], None]] = None,
        on_result: Optional[Callable] = None) -> Report:
    """`generate_tests=False` is the legacy --no-gen switch: it selects the regression
    suite. `llm` may be injected (tests / server); otherwise it is built from the env."""
    suite = suite or ("regression" if generate_tests is False else cfg.suite)
    if suite not in SUITES:
        raise ValueError(f"suite must be one of {SUITES}, got {suite!r}")
    stage = on_stage or (lambda name, msg: None)
    notes: list[str] = []
    mode = "live" if cfg.live else "mock"
    host = cfg.target.host

    if suite == "regression":
        log("WARNING: regression suite = 26 hand-written DNS/TLS tests. It proves the grader, "
            "not the agent — it only covers DNS/TLS agents. Use --suite generated for anything else.")
        notes.append("regression suite: hand-written DNS/TLS tests only")
    elif suite in ("generated", "both") and llm is None:
        from .llm import make_llm
        llm = make_llm(cfg.generator.model)        # raises loudly if there is no key

    # [1] discover ---------------------------------------------------------------
    log(f"[1/6] discover {host} via ANS registry ({mode})")
    stage("discover", f"searching ANS registry for {host}")
    reg = Registry(cfg.ans.search_base, cfg.ans.transparency_base, cfg.ans.timeout_s, cfg.live)
    entry = reg.find_by_host(host)
    cards = card_mod.fetch(host, cfg.ans.timeout_s, cfg.live, registry_entry=entry)
    if entry is None and cards.agent_card.get("name"):
        entry = reg.find_by_host(host, display_name=cards.agent_card["name"])
        cards.registry_entry = entry or {}
    log(f"      registry={'found' if entry else 'not found'} endpoint={cards.endpoint} "
        f"skills={cards.declared_skills} protocols={cards.declared_protocols}")

    # [2] identity ---------------------------------------------------------------
    log("[2/6] verify identity: transparency log + live TLS fingerprint")
    stage("identity", "fetching transparency-log entry, live TLS handshake")
    identity = ident_mod.verify(host, reg, cfg.target.ans_id if cfg.target.host == host else "", cfg.live,
                                entry_summary=entry, trust_card=cards.trust_card)
    log(f"      status={identity.status} registry={identity.registry_found} tl={identity.tl_entry_found} "
        f"fp_match={identity.fingerprint_match} verified={identity.verified}")
    for n in identity.notes:
        log(f"      note: {n}")

    # [3] tests ------------------------------------------------------------------
    assertions = []
    coverage: Optional[CoverageReport] = None
    if suite in ("regression", "both"):
        assertions.extend(regression_assertions())
    if suite in ("generated", "both"):
        log(f"[3/6] LangGraph: read the agent's cards, extract claims, write tests ({cfg.generator.model})")
        stage("generate", "LLM reads the cards and writes oracle-backed tests")
        gen, coverage, gnotes = generate(cards, assertions, cfg.generator.model, cfg.generator.max_generated, llm=llm)
        notes.extend(gnotes)
        for n in gnotes:
            log(f"      note: {n}")
        log(f"      claims={coverage.claims_found} verifiable={coverage.verifiable} "
            f"schema_only={coverage.schema_only} unverifiable={coverage.unverifiable} "
            f"coverage={coverage.coverage_ratio:.0%}  +{len(gen)} generated tests")
        for c in coverage.claims:
            log(f"      {c.verifiability.value:<13} {c.claim_text[:70]:<72} {c.oracle_key or c.reason[:50]}")
        for a in gen:
            log(f"      + {a.id:<40} {a.claim:<28} {adapter.target_of(a)}")
        assertions.extend(gen)
        if suite == "generated":
            assertions.extend(generic_assertions(gen))
    else:
        log("[3/6] generator OFF (regression suite)")
        stage("generate", "skipped: regression suite")

    # [4] transport --------------------------------------------------------------
    log(f"[4/6] build transport ({cfg.target.transport if cfg.live else 'mock'})")
    stage("transport", f"{cfg.target.transport if cfg.live else 'mock'} -> {cfg.target.endpoint or cards.endpoint}")
    t = transport_mod.build(cfg, cards)
    extractor = adapter.LLMExtractor(llm, SPECS) if llm is not None else None

    # [5] run ---------------------------------------------------------------------
    log(f"[5/6] run {len(assertions)} assertions (oracles compute ground truth live)")
    stage("run", f"{len(assertions)} assertions")

    def _log_result(a):
        mark = "PASS" if a.passed else ("----" if a.passed is None else "FAIL")
        log(f"      {mark} {a.id:<36} {a.kind.value:<11} {a.evidence[:100]}")
        if on_result:
            on_result(a)
    results = run_all(assertions, t, cards, extractor=extractor, on_result=_log_result)

    # [6] report -------------------------------------------------------------------
    log("[6/6] build report")
    stage("report", "scoring")
    report = build(host, host, mode, identity, cards, results, cfg.report.weights, notes,
                   coverage=coverage, suite=suite)
    out_dir = Path(cfg.report.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.run_at.strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{host.replace('.', '_')}_{suite}_{stamp}.json"
    path.write_text(report.model_dump_json(indent=2))
    (out_dir / "latest.json").write_text(report.model_dump_json(indent=2))
    log(f"      behavior_score={report.behavior_score}  {report.explanation}")
    log(f"      wrote {path}")

    if cfg.emit.enabled and cfg.emit.import_url:
        try:
            code, body = emit_mod.emit(report, cfg.emit.import_url, cfg.emit.admin_key)
            log(f"      emitted to trust index: HTTP {code} {body[:120]}")
        except Exception as e:
            log(f"      emit failed: {e}")
    return report
