#!/usr/bin/env python
"""Judge calibration: sample (run, condition) pairs to hand-label, then Cohen's kappa.

    uv run python evals/kappa.py --export 30                  # from evals/results/latest
    uv run python evals/kappa.py --compute                    # reads evals/calibration/labels.csv

--export writes evals/calibration/to_label.jsonl (judge verdict, rationale, transcript excerpt,
human_pass left null) and labels.template.csv (run_id, condition_idx, human_pass). Fill the
template in, save it as labels.csv, then --compute prints kappa per condition type and overall
and writes kappa.json, which report.py quotes. A type below 0.8 means rewrite that condition
or replace it with a structural check before quoting a number that depends on it.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CALIB_DIR = HERE / "calibration"
TO_LABEL_PATH = CALIB_DIR / "to_label.jsonl"
LABELS_PATH = CALIB_DIR / "labels.csv"
TEMPLATE_PATH = CALIB_DIR / "labels.template.csv"
KAPPA_PATH = CALIB_DIR / "kappa.json"
RESULTS_LATEST = HERE / "results" / "latest"

_FAIL_RE = re.compile(r"\b(fail|failed|not met|not satisfied|unmet|violat\w*|did not|does not|never)\b", re.IGNORECASE)
_PASS_RE = re.compile(r"\b(pass|passed|success\w*|met|satisfied|fulfilled|correctly)\b", re.IGNORECASE)


def judge_verdict_for_condition(record: dict[str, Any], idx: int) -> tuple[bool | None, str]:
    """(verdict, source). The API reports one verdict per run; the rationale usually carries one
    message per condition, so read that when the counts line up, else fall back to the overall."""
    messages = record.get("judge_rationale") or []
    conds = record.get("success_conditions") or []
    per_condition = [m for m in messages if not str(m).startswith("summary:")]
    if len(per_condition) == len(conds) and idx < len(per_condition):
        text = str(per_condition[idx])
        if _FAIL_RE.search(text) and not _PASS_RE.search(text):
            return False, "rationale"
        if _PASS_RE.search(text) and not _FAIL_RE.search(text):
            return True, "rationale"
    return record.get("judge_pass"), "overall"


def _load_runs(results_dir: Path) -> list[dict[str, Any]]:
    path = Path(results_dir) / "runs.jsonl"
    if not path.exists():
        raise SystemExit(f"no runs.jsonl in {results_dir}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def export_calibration(results_dir: Path, n: int, *, seed: int = 11, out_path: Path = TO_LABEL_PATH,
                       template_path: Path = TEMPLATE_PATH) -> Path:
    """Sample n (run, condition) pairs, round-robin across condition types so no type is missed."""
    runs = _load_runs(results_dir)
    by_type: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for rec in runs:
        for cond in rec.get("success_conditions") or []:
            by_type[cond.get("type", "unknown")].append((rec, cond))
    rng = random.Random(seed)
    for pairs in by_type.values():
        rng.shuffle(pairs)
    picked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    types = sorted(by_type)
    while len(picked) < n and any(by_type[t] for t in types):
        for t in types:
            if by_type[t] and len(picked) < n:
                picked.append(by_type[t].pop())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh, template_path.open("w", newline="") as tf:
        writer = csv.writer(tf)
        writer.writerow(["run_id", "condition_idx", "human_pass"])
        for rec, cond in picked:
            verdict, source = judge_verdict_for_condition(rec, cond["idx"])
            transcript = rec.get("transcript") or []
            fh.write(json.dumps({
                "run_id": rec["run_id"], "condition_idx": cond["idx"], "condition_type": cond.get("type"),
                "condition_text": cond.get("text"), "suite": rec["suite"], "scenario_id": rec["scenario_id"],
                "variant": rec.get("variant", "baseline"), "judge_result": rec.get("judge_result"),
                "judge_pass": verdict, "judge_pass_source": source, "rationale": rec.get("judge_rationale"),
                "transcript_excerpt": [f"{m.get('role')}{' [' + m['node'] + ']' if m.get('node') else ''}: "
                                       f"{' '.join((m.get('message') or '').split())[:300]}" for m in transcript[-8:]],
                "human_pass": None,
            }, ensure_ascii=False) + "\n")
            writer.writerow([rec["run_id"], cond["idx"], ""])
    print(f"exported {len(picked)} pairs across {len(types)} condition types -> {out_path}; "
          f"fill in {template_path.name} and save it as {LABELS_PATH.name}")
    return out_path


def cohens_kappa(pairs: list[tuple[bool, bool]]) -> dict[str, float | int]:
    """Two raters, two categories. pe from each rater's marginal; degenerate marginals give 1.0
    on perfect agreement and 0.0 otherwise."""
    n = len(pairs)
    if n == 0:
        return {"kappa": float("nan"), "po": float("nan"), "pe": float("nan"), "n": 0}
    po = sum(1 for a, b in pairs if a == b) / n
    pa = sum(1 for a, _ in pairs if a) / n
    pb = sum(1 for _, b in pairs if b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe >= 1.0 - 1e-12:
        kappa = 1.0 if po >= 1.0 - 1e-12 else 0.0
    else:
        kappa = (po - pe) / (1 - pe)
    return {"kappa": kappa, "po": po, "pe": pe, "n": n}


def _parse_bool(raw: str) -> bool | None:
    v = (raw or "").strip().lower()
    if v in ("1", "true", "yes", "y", "pass", "p", "t"):
        return True
    if v in ("0", "false", "no", "n", "fail", "f"):
        return False
    return None


def compute(labels_path: Path = LABELS_PATH, to_label_path: Path = TO_LABEL_PATH,
            out_path: Path = KAPPA_PATH) -> dict[str, Any]:
    if not labels_path.exists():
        raise SystemExit(f"{labels_path} not found; export first, label, then save as labels.csv")
    if not to_label_path.exists():
        raise SystemExit(f"{to_label_path} not found; run --export first")
    exported: dict[tuple[str, int], dict[str, Any]] = {}
    for line in to_label_path.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            exported[(str(rec["run_id"]), int(rec["condition_idx"]))] = rec
    by_type: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    skipped = 0
    with labels_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            human = _parse_bool(row.get("human_pass", ""))
            key = (str(row.get("run_id", "")).strip(), int(str(row.get("condition_idx", "0")).strip() or 0))
            rec = exported.get(key)
            if human is None or rec is None or rec.get("judge_pass") is None:
                skipped += 1
                continue
            by_type[rec.get("condition_type") or "unknown"].append((bool(rec["judge_pass"]), human))
    all_pairs = [p for pairs in by_type.values() for p in pairs]
    overall = cohens_kappa(all_pairs)
    per_type = {t: cohens_kappa(pairs) for t, pairs in sorted(by_type.items())}
    result = {"kappa": overall["kappa"], "n": overall["n"], "po": overall["po"], "pe": overall["pe"],
              "per_type": per_type, "skipped_rows": skipped,
              "computed_at": datetime.now(UTC).isoformat(timespec="seconds"), "labels_file": str(labels_path)}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"{'condition type':<32} {'n':>4} {'po':>6} {'kappa':>7}")
    for t, s in per_type.items():
        flag = "  <- below 0.8, rewrite or make structural" if s["n"] and s["kappa"] < 0.8 else ""
        print(f"{t:<32} {s['n']:>4} {s['po']:>6.2f} {s['kappa']:>7.2f}{flag}")
    print(f"{'overall':<32} {overall['n']:>4} {overall['po']:>6.2f} {overall['kappa']:>7.2f}")
    if skipped:
        print(f"skipped {skipped} rows (blank label, unknown pair, or no judge verdict)")
    print(f"wrote {out_path}")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", type=int, default=0, metavar="N", help="sample N pairs to hand-label")
    ap.add_argument("--results", default=str(RESULTS_LATEST), help="results dir for --export")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--compute", action="store_true", help="compute kappa from labels.csv")
    ap.add_argument("--labels", default=str(LABELS_PATH))
    args = ap.parse_args(argv)
    if args.export:
        export_calibration(Path(args.results), args.export, seed=args.seed)
    if args.compute:
        compute(Path(args.labels))
    if not args.export and not args.compute:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
