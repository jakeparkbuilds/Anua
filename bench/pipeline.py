"""End-to-end orchestration. One function, one report."""
from __future__ import annotations
import json
from pathlib import Path
from .config import Config
from .ans.registry import Registry
from .ans import identity as ident_mod
from .agent import card as card_mod, transport as transport_mod
from .assertions.fixtures import all_assertions
from .assertions.runner import run_all
from .generator.graph import generate
from .report.builder import build
from .report import emit as emit_mod
from .models import Report


def run(cfg: Config, generate_tests: bool | None = None, log=print) -> Report:
    notes: list[str] = []
    mode = "live" if cfg.live else "mock"
    host = cfg.target.host
    log(f"[1/6] discover + verify identity for {host} ({mode})")
    reg = Registry(cfg.ans.search_base, cfg.ans.transparency_base, cfg.ans.timeout_s, cfg.live)
    identity = ident_mod.verify(host, reg, cfg.target.ans_id, cfg.live)
    log(f"      registry={identity.registry_found} tl={identity.tl_entry_found} "
        f"fp_match={identity.fingerprint_match} verified={identity.verified}")
    for n in identity.notes:
        log(f"      note: {n}")

    log("[2/6] fetch agent card + trust card")
    cards = card_mod.fetch(host, cfg.ans.timeout_s, cfg.live)
    log(f"      endpoint={cards.endpoint} skills={cards.declared_skills} protocols={cards.declared_protocols}")

    assertions = all_assertions()
    do_gen = cfg.generator.enabled if generate_tests is None else generate_tests
    if do_gen:
        log("[3/6] LangGraph: generate additional tests from the agent card")
        gen, gnotes = generate(cards, assertions, cfg.generator.model, cfg.generator.max_generated)
        notes.extend(gnotes)
        for n in gnotes: log(f"      note: {n}")
        log(f"      +{len(gen)} generated assertions")
        assertions.extend(gen)
    else:
        log("[3/6] generator disabled")

    log(f"[4/6] build transport ({cfg.target.transport if cfg.live else 'mock'})")
    t = transport_mod.build(cfg, cards)

    log(f"[5/6] run {len(assertions)} assertions (oracles compute ground truth live)")
    results = run_all(assertions, t, cards)
    for a in results:
        mark = "PASS" if a.passed else ("----" if a.passed is None else "FAIL")
        log(f"      {mark} {a.id:<28} {a.kind.value:<11} {a.evidence[:100]}")

    log("[6/6] build report")
    report = build(host, host, mode, identity, cards, results, cfg.report.weights, notes)
    out_dir = Path(cfg.report.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.run_at.strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{host.replace('.', '_')}_{stamp}.json"
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
