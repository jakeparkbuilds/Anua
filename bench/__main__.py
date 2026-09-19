"""CLI.

  python -m bench run [--live] [--emit] [--no-gen] [--host H] [--config config.yaml]
  python -m bench selftest              # prove the oracles are honest in THIS environment
  python -m bench probe-agent --live    # print raw agent card + one raw response
  python -m bench probe-registry --live # print raw registry search + TL entry
  python -m bench list                  # list hand-written assertions
"""
from __future__ import annotations
import argparse
import json
import sys
from .config import load


def cmd_run(args):
    cfg = load(args.config, live=args.live)
    if args.host: cfg.target.host = args.host
    if args.emit: cfg.emit.enabled = True
    from .pipeline import run
    r = run(cfg, generate_tests=(False if args.no_gen else None))
    print(json.dumps({"agent": r.agent, "behavior_score": r.behavior_score,
                      "failures": len(r.failures), "high": r.summary["high_severity_failures"]}, indent=2))
    return 0


def cmd_selftest(args):
    """Oracles must agree with badssl.com's documented truths. If this fails, your
    environment (proxy, DNS, roots) is lying and no score should be trusted."""
    from .oracles.registry import compute
    checks = [
        ("tls.chain_valid", "expired.badssl.com", False),
        ("tls.expired", "expired.badssl.com", True),
        ("tls.chain_valid", "self-signed.badssl.com", False),
        ("tls.hostname_match", "wrong.host.badssl.com", False),
        ("tls.chain_valid", "cloudflare.com", True),
        ("dns.dnssec", "cloudflare.com", True),
        ("dns.resolves", "this-domain-does-not-exist-zz.invalid", False),
        ("email.spf", "google.com", True),
        ("email.dmarc", "google.com", True),
    ]
    bad = 0
    for claim, dom, exp in checks:
        try:
            got = compute(claim, dom)
            ok = got == exp
        except Exception as e:
            got, ok = f"ERR {type(e).__name__}", False
        bad += (not ok)
        print(f"{'OK  ' if ok else 'BAD '} {claim:<20} {dom:<40} expected={exp!s:<6} got={got}")
    print("\nSELFTEST", "PASSED — oracles are honest here" if not bad else f"FAILED — {bad} mismatch(es); fix environment before trusting scores")
    return 1 if bad else 0


def cmd_probe_agent(args):
    cfg = load(args.config, live=args.live)
    if args.host: cfg.target.host = args.host
    from .agent import card as card_mod, transport as transport_mod, adapter
    from .models import Assertion, Kind
    cards = card_mod.fetch(cfg.target.host, cfg.ans.timeout_s, cfg.live)
    print("=== AGENT CARD ===");  print(json.dumps(cards.agent_card, indent=2)[:3000])
    print("=== TRUST CARD ===");  print(json.dumps(cards.trust_card, indent=2)[:2000])
    print("endpoint:", cards.endpoint, "| skills:", cards.declared_skills, "| protocols:", cards.declared_protocols)
    t = transport_mod.build(cfg, cards)
    a = Assertion(id="probe", kind=Kind.ORACLE, claim="tls.chain_valid", input={"domain": args.domain})
    raw, ms = t.send(adapter.build_prompt(a))
    print(f"=== RAW RESPONSE ({ms} ms) ===");  print(raw[:3000])
    print("=== EXTRACTION ===")
    for c in ("tls.chain_valid", "tls.expired", "dns.dnssec", "http.status", "email.spf"):
        print(f"  {c:<18} -> {adapter.extract(raw, c)!r}")
    return 0


def cmd_probe_registry(args):
    cfg = load(args.config, live=args.live)
    if args.host: cfg.target.host = args.host
    from .ans.registry import Registry
    reg = Registry(cfg.ans.search_base, cfg.ans.transparency_base, cfg.ans.timeout_s, cfg.live)
    print("=== SEARCH ===");  print(json.dumps(reg.search(cfg.target.host, 5), indent=2)[:3000])
    if cfg.target.ans_id:
        print("=== TL ENTRY ===");  print(json.dumps(reg.tl_entry(cfg.target.ans_id), indent=2)[:3000])
    return 0


def cmd_list(args):
    from .assertions.fixtures import all_assertions
    for a in all_assertions():
        print(f"{a.kind.value:<11} {a.severity.value:<6} {a.id:<28} {a.claim:<20} {a.input}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="bench")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run");           r.set_defaults(fn=cmd_run)
    r.add_argument("--live", action="store_true"); r.add_argument("--emit", action="store_true")
    r.add_argument("--no-gen", action="store_true"); r.add_argument("--host")

    s = sub.add_parser("selftest");      s.set_defaults(fn=cmd_selftest)

    pa = sub.add_parser("probe-agent");  pa.set_defaults(fn=cmd_probe_agent)
    pa.add_argument("--live", action="store_true"); pa.add_argument("--host")
    pa.add_argument("--domain", default="expired.badssl.com")

    pr = sub.add_parser("probe-registry"); pr.set_defaults(fn=cmd_probe_registry)
    pr.add_argument("--live", action="store_true"); pr.add_argument("--host")

    l = sub.add_parser("list");          l.set_defaults(fn=cmd_list)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
