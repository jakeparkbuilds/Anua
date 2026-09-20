"""Recompute behavior_score by hand from a report's raw assertion list and compare it to
the number the report shows. Usage: python scripts/recompute.py out/<report>.json [...]

The arithmetic, spelled out so a judge can follow it:
  capability rate  = passed / graded over ORACLE + CONSISTENCY assertions (passed is not None)
  self-claim rate  = passed / graded over SELFCLAIM
  schema rate      = passed / graded over SCHEMA
  quality          = the quality assertion's score / 100, only if one ran
  score = 100 * Σ(weight_i * rate_i) / Σ(weight_i) over the pools that have a graded item
  weights: report.weights from config.yaml (defaults .70 / .25 / .20 / .10)
A pool with no graded item is not in either sum. Fewer than 3 graded assertions in
total (quality has no verdict and does not count) means no score (INSUFFICIENT_COVERAGE).
"""
import json
import sys

# The weights come from config.yaml exactly as the pipeline reads them (a key missing
# from the yaml falls back to the same default builder.py uses).
def _weights(path="config.yaml"):
    try:
        import yaml
        w = (yaml.safe_load(open(path)) or {}).get("report", {}).get("weights", {}) or {}
    except Exception:
        w = {}
    return {"capability": w.get("oracle", .70), "selfclaim": w.get("selfclaim", .25),
            "schema": w.get("schema", .20), "quality": w.get("quality", .10)}


W = _weights()


def recompute(path: str) -> None:
    r = json.load(open(path))
    A = r["assertions"]
    pools = {"capability": [a for a in A if a["kind"] in ("oracle", "consistency")],
             "selfclaim": [a for a in A if a["kind"] == "selfclaim"],
             "schema": [a for a in A if a["kind"] == "schema"]}
    print(f"\n{path}  ({r['target_host']}, suite {r['suite']}, run {r['run_at'][:19]})")
    num = den = 0.0
    for name, items in pools.items():
        graded = [a for a in items if a["passed"] is not None]
        skipped = len(items) - len(graded)
        if not graded:
            print(f"  {name:<11} 0 graded ({skipped} skipped/no-verdict) -> not in the blend")
            continue
        p = sum(1 for a in graded if a["passed"])
        rate = p / len(graded)
        print(f"  {name:<11} {p}/{len(graded)} graded = {rate:.3f}  x {W[name]}   ({skipped} skipped/no-verdict, excluded)")
        num += W[name] * rate; den += W[name]
    q = [a for a in A if a["kind"] == "quality"]
    if q:
        qs = q[0]["score"] / 100
        print(f"  {'quality':<11} score {q[0]['score']} -> {qs:.3f}  x {W['quality']}   (never a verdict)")
        num += W["quality"] * qs; den += W["quality"]
    n_graded = sum(1 for a in A if a["kind"] != "quality" and a["passed"] is not None)
    if n_graded < 3:
        mine = None
    else:
        mine = round(100 * num / den)
    print(f"  => by hand: {mine if mine is not None else 'INSUFFICIENT_COVERAGE'}   report says: "
          f"{r['behavior_score'] if r['score_status'] == 'OK' else r['score_status']}   "
          f"{'OK' if mine == r['behavior_score'] else 'MISMATCH'}")
    fails = [a for a in A if a["passed"] is False]
    print(f"  failures: {[(a['id'], a['severity']) for a in fails]}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        recompute(p)
