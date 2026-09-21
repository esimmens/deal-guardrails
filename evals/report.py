#!/usr/bin/env python
"""Render a results directory into report.md (and print it).

    uv run python evals/report.py                                  # evals/results/latest
    uv run python evals/report.py evals/results/2026-09-21T02-10-00Z --out report.md

Per folder: scenarios, runs, pass rate, Wilson 95% lower bound, flaky scenarios (at least one
failure), judge disagreements, guardrail_events (counted, never folded into pass), the ablation
column when an ablation ran, the worst scenario with a six-line transcript excerpt, and a
dev-vs-held-out table when both folders are present. Wilson is computed here, no scipy.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evals.el_api import platform_error  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
KAPPA_PATH = HERE / "calibration" / "kappa.json"
Z95 = 1.959964
FOLDER_ORDER = ("regression", "heldout", "safety", "spike")


def wilson_lower(k: int, n: int, z: float = Z95) -> float:
    """Lower bound of the Wilson score interval for k successes in n trials."""
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / denom)


def load_results(results_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    runs_path = results_dir / "runs.jsonl"
    if not runs_path.exists():
        raise SystemExit(f"no runs.jsonl in {results_dir}")
    runs = [json.loads(line) for line in runs_path.read_text().splitlines() if line.strip()]
    meta_path = results_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return runs, meta


def load_kappa(path: Path = KAPPA_PATH) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if Path(path).exists() else None


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def summarise(runs: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """stats[folder][variant] -> counts, rates, per-scenario detail."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        groups[(r["suite"], r.get("variant", "baseline"))].append(r)
    stats: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (folder, variant), rs in groups.items():
        voided = [r for r in rs if r.get("status") == "voided" or platform_error(r.get("judge_rationale") or [])]
        infra = [r for r in rs if r not in voided and (r.get("status") == "infra_error" or r.get("infra_reason"))]
        rs = [r for r in rs if r not in voided and r not in infra]
        n = len(rs)
        k = sum(1 for r in rs if r.get("pass"))
        per: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rs:
            per[r["scenario_id"]].append(r)
        scen = {}
        for sid, srs in per.items():
            sk = sum(1 for r in srs if r.get("pass"))
            scen[sid] = {"runs": len(srs), "pass": sk, "rate": sk / len(srs),
                         "failing": [r for r in srs if not r.get("pass")]}
        worst_id = min(scen, key=lambda s: (scen[s]["rate"], -scen[s]["runs"], s)) if scen else None
        stats[folder][variant] = {
            "scenarios": len(per), "runs": n, "pass": k, "rate": k / n if n else 0.0, "lb": wilson_lower(k, n),
            "flaky": sum(1 for s in scen.values() if s["pass"] < s["runs"]),
            "judge_disagreements": sum(1 for r in rs if r.get("judge_disagreement")),
            "guardrail_events": sum(int(r.get("guardrail_events") or 0) for r in rs),
            "judge_pass": sum(1 for r in rs if r.get("judge_pass")),
            "node_id_pass": sum(1 for r in rs if _node_and_id_ok(r)),
            "db_pass": sum(1 for r in rs if _db_ok(r)),
            "under_escalation": sum(1 for r in rs if (r.get("structural") or {}).get("mismatch_kind") == "under_escalation"),
            "over_escalation": sum(1 for r in rs if (r.get("structural") or {}).get("mismatch_kind") == "over_escalation"),
            "per_scenario": scen, "worst": worst_id,
            "voided": len(voided),
            "infra": len(infra),
            "infra_reasons": sorted({str(r.get("infra_reason") or "infra error")[:60] for r in infra}),
            "voided_reasons": sorted({(r.get("voided_reason") or platform_error(r.get("judge_rationale") or []) or "platform error")[:60] for r in voided}),
        }
    return stats


def _node_and_id_ok(r: dict[str, Any]) -> bool:
    s = r.get("structural") or {}
    reasons = s.get("reasons") or []
    return not any(x.startswith(("final node", "no DG- id", "a DG- id appeared", "tool returned")) for x in reasons)


def _db_ok(r: dict[str, Any]) -> bool:
    s = r.get("structural") or {}
    reasons = s.get("reasons") or []
    return s.get("db_status") == "ok" and not any(
        x.startswith(("postgres", "no deals row", "approval_requirements", "status ", "discount_bps", "receipt.",
                      "deal_terms", "forbidden row", "spoken id", "a deals row exists")) for x in reasons)


def excerpt(run: dict[str, Any], lines: int = 6) -> list[str]:
    """The last `lines` transcript entries, one line each, tool activity noted inline."""
    out: list[str] = []
    for m in (run.get("transcript") or [])[-lines:]:
        who = m.get("role") or "?"
        node = f" [{m['node']}]" if m.get("node") else ""
        text = " ".join((m.get("message") or "").split())[:160]
        tools = ""
        if m.get("tool_calls"):
            tools += " (calls: " + ", ".join(str(t) for t in m["tool_calls"]) + ")"
        if m.get("tool_results"):
            tools += " (results: " + ", ".join(f"{t.get('tool_name')}{' ERROR' if t.get('is_error') else ''}"
                                                 for t in m["tool_results"]) + ")"
        out.append(f"> {who}{node}: {text or '(no text)'}{tools}")
    return out or ["> (no transcript captured)"]


def render_report(runs: list[dict[str, Any]], meta: dict[str, Any], kappa: dict[str, Any] | None,
                  results_name: str = "") -> str:
    stats = summarise(runs)
    folders = [f for f in FOLDER_ORDER if f in stats] + sorted(f for f in stats if f not in FOLDER_ORDER)
    has_ablation = any("ablation" in stats[f] for f in folders)
    kappa_line = (f"κ = {kappa['kappa']:.2f} (n = {kappa['n']})" if kappa and kappa.get("n")
                  else "not yet calibrated")
    lines = [
        "# Deal Guardrails evaluation report",
        "",
        f"- results: {results_name or '(unnamed)'}; generated {meta.get('created_at', 'unknown')}",
        f"- agent: {meta.get('agent_id', 'unknown')}; version_id: {meta.get('version_id') or 'unknown'}",
        f"- policy_version: {meta.get('policy_version', 'unknown')}",
        f"- judge: {meta.get('judge_model', 'unknown')}; simulated user: {meta.get('simulated_user_model', 'unknown')}",
        f"- judge calibration: {kappa_line}",
        f"- postgres check: {meta.get('structural_db', 'unknown')}",
        ("- pass = judge success AND structural pass (expected final node, DG- id, Postgres row whose "
        "approval_requirements equal the label as a set). guardrail_events are counted and never folded into pass."),
        *([f"- **voided runs: {sum(v.get('voided', 0) for f in stats.values() for v in f.values())}**, platform errors such as {', '.join(sorted({x for f in stats.values() for v in f.values() for x in v.get('voided_reasons', [])}))}; excluded from every count and rate below"] if any(v.get("voided") for f in stats.values() for v in f.values()) else []),
        *([f"- **infra errors: {sum(v.get('infra', 0) for f in stats.values() for v in f.values())}**, submit calls that died in the test harness itself, the tunnel or a test/tool configuration error, before the service could be reached ({', '.join(sorted({x for f in stats.values() for v in f.values() for x in v.get('infra_reasons', [])}))}); they say nothing about the agent and are excluded from every count and rate below"] if any(v.get("infra") for f in stats.values() for v in f.values()) else []),
        "",
        "## Per folder",
        "",
    ]
    header = "| folder | scenarios | runs | pass | pass rate | Wilson 95% LB | flaky scenarios | judge disagreements | guardrail_events | voided (platform) | infra errors (tunnel) |"
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    if has_ablation:
        header += " ablation pass rate (LB) |"
        sep += "---:|"
    lines += [header, sep]
    for f in folders:
        b = stats[f].get("baseline")
        if b is None:
            continue
        row = (f"| {f} | {b['scenarios']} | {b['runs']} | {b['pass']} | {_pct(b['rate'])} | {b['lb']:.3f} | "
               f"{b['flaky']} | {b['judge_disagreements']} | {b['guardrail_events']} | {b['voided']} | {b['infra']} |")
        if has_ablation:
            a = stats[f].get("ablation")
            row += (f" {_pct(a['rate'])} ({a['lb']:.3f}, n={a['runs']}) |" if a else " n/a |")
        lines.append(row)
    lines += [
        "",
        "## Components, separately",
        "",
        "| folder | judge pass | node + DG- id | Postgres approvals = label | under-escalation runs | over-escalation runs |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for f in folders:
        b = stats[f].get("baseline")
        if b is None:
            continue
        n = b["runs"]
        lines.append(f"| {f} | {b['judge_pass']}/{n} (LB {wilson_lower(b['judge_pass'], n):.3f}) | "
                     f"{b['node_id_pass']}/{n} (LB {wilson_lower(b['node_id_pass'], n):.3f}) | "
                     f"{b['db_pass']}/{n} (LB {wilson_lower(b['db_pass'], n):.3f}) | "
                     f"{b['under_escalation']} | {b['over_escalation']} |")
    lines += ["", "## Worst scenario per folder", ""]
    for f in folders:
        b = stats[f].get("baseline")
        if b is None or not b["worst"]:
            continue
        w = b["per_scenario"][b["worst"]]
        lines.append(f"### {f}: {b['worst']} ({w['pass']}/{w['runs']} pass)")
        lines.append("")
        if w["failing"]:
            bad = w["failing"][0]
            reasons = (bad.get("structural") or {}).get("reasons") or []
            judge = bad.get("judge_result")
            lines.append(f"- judge: {judge}; structural: {'pass' if bad.get('structural_pass') else 'fail'}"
                         + (f"; reasons: {'; '.join(reasons[:3])}" if reasons else ""))
            if bad.get("judge_rationale"):
                lines.append(f"- judge rationale: {' | '.join(str(x)[:200] for x in bad['judge_rationale'][:2])}")
            lines.append("")
            lines += excerpt(bad)
        else:
            lines.append("- every run passed")
        lines.append("")
    if "regression" in stats and "heldout" in stats and "baseline" in stats["regression"] and "baseline" in stats["heldout"]:
        r, h = stats["regression"]["baseline"], stats["heldout"]["baseline"]
        lines += [
            "## Dev vs held-out",
            "",
            "| | regression (dev) | heldout | delta |",
            "|---|---:|---:|---:|",
            f"| runs | {r['runs']} | {h['runs']} | |",
            f"| pass rate | {_pct(r['rate'])} | {_pct(h['rate'])} | {100 * (h['rate'] - r['rate']):+.0f} pts |",
            f"| Wilson 95% LB | {r['lb']:.3f} | {h['lb']:.3f} | {h['lb'] - r['lb']:+.3f} |",
            f"| flaky scenarios | {r['flaky']} | {h['flaky']} | |",
            "",
        ]
    if has_ablation:
        lines += ["## Ablation (policy stripped from the prompt)", "",
                  "| folder | baseline pass rate (LB) | ablation pass rate (LB) | delta |", "|---|---:|---:|---:|"]
        for f in folders:
            b, a = stats[f].get("baseline"), stats[f].get("ablation")
            if b and a:
                lines.append(f"| {f} | {_pct(b['rate'])} ({b['lb']:.3f}) | {_pct(a['rate'])} ({a['lb']:.3f}) | "
                             f"{100 * (a['rate'] - b['rate']):+.0f} pts |")
        lines.append("")
    failures = [r for r in runs if not r.get("pass") and r.get("variant", "baseline") == "baseline"]
    lines += ["## Failing runs (baseline)", ""]
    if not failures:
        lines.append("none")
    else:
        lines += ["| folder | scenario | run | judge | structural reasons |", "|---|---|---|---|---|"]
        for r in failures:
            reasons = "; ".join((r.get("structural") or {}).get("reasons") or []) or "(judge only)"
            lines.append(f"| {r['suite']} | {r['scenario_id']} | {str(r.get('run_id'))[:12]} | {r.get('judge_result')} | "
                         f"{reasons[:220]} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results_dir", nargs="?", default=str(RESULTS_DIR / "latest"))
    ap.add_argument("--out", default=None, help="where to write report.md (default: inside results_dir)")
    ap.add_argument("--kappa", default=str(KAPPA_PATH), help="path to kappa.json")
    args = ap.parse_args(argv)
    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute() and not results_dir.exists():
        results_dir = HERE.parent / results_dir
    runs, meta = load_results(results_dir)
    text = render_report(runs, meta, load_kappa(Path(args.kappa)), results_name=results_dir.resolve().name)
    out = Path(args.out) if args.out else results_dir / "report.md"
    out.write_text(text)
    print(text)
    print(f"\nwrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
