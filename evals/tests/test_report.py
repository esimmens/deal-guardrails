"""Wilson bounds, the rendered report, Cohen's kappa, and every script's --help."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals import kappa as kp
from evals import report as rp

ROOT = Path(__file__).resolve().parents[2]


def test_wilson_lower_bound_sanity():
    assert abs(rp.wilson_lower(5, 5) - 0.566) < 0.002
    assert abs(rp.wilson_lower(20, 20) - 0.839) < 0.002
    assert rp.wilson_lower(0, 5) == 0.0
    assert rp.wilson_lower(0, 0) == 0.0
    assert 0.88 < rp.wilson_lower(95, 100) < 0.95
    assert rp.wilson_lower(10, 20) < rp.wilson_lower(20, 20) < rp.wilson_lower(40, 40)
    assert abs(rp.wilson_lower(10, 20) - 0.299) < 0.002


def _rec(suite, sid, run_id, *, passed=True, judge=True, structural=True, reasons=(), guardrails=0,
         variant="baseline", mismatch=None, node="confirmed_node", db="ok"):
    return {
        "run_id": run_id, "suite": suite, "variant": variant, "scenario_id": sid, "test_id": f"t_{sid}",
        "test_name": f"{suite}/{sid}", "status": "passed" if judge else "failed",
        "judge_result": "success" if judge else "failure", "judge_pass": judge,
        "judge_rationale": ["condition 1 met", "condition 2 met", "condition 3 not met" if not judge else "condition 3 met"],
        "success_conditions": [{"idx": 0, "type": "deal_id_present", "text": "id"},
                               {"idx": 1, "type": "no_premature_success", "text": "order"},
                               {"idx": 2, "type": "requires_approvals_superset", "text": "roles"}],
        "structural": {"pass": structural, "reasons": list(reasons), "warnings": [], "final_node": node,
                       "expected_node": "confirmed_node", "db_status": db, "mismatch_kind": mismatch},
        "structural_pass": structural, "pass": passed, "judge_disagreement": judge != structural,
        "guardrail_events", "voided (platform)": guardrails,
        "transcript": [{"role": "user", "node": None, "message": "Calloway 22% 12 months", "tool_calls": [], "tool_results": []},
                       {"role": "agent", "node": "intake_node", "message": "What segment?", "tool_calls": [], "tool_results": []},
                       {"role": "user", "node": None, "message": "enterprise, yes", "tool_calls": [], "tool_results": []},
                       {"role": "agent", "node": "submit_node", "message": "", "tool_calls": ["notify_condition_1_met"],
                        "tool_results": [{"tool_name": "notify_condition_1_met", "is_error": False}]},
                       {"role": "agent", "node": node, "message": "Submitted as DG-1042." if passed else "The submission did not go through.",
                        "tool_calls": [], "tool_results": []}],
    }


@pytest.fixture
def fixture_runs() -> list[dict]:
    runs = []
    for sid in ("row-01", "row-02"):
        for i in range(3):
            fail = sid == "row-02" and i == 0
            runs.append(_rec("regression", sid, f"reg_{sid}_{i}", passed=not fail, judge=True, structural=not fail,
                             reasons=("no DG- id in any agent message", "final node 'failed_node', expected one of ['confirmed_node']") if fail else (),
                             guardrails=1 if i == 1 else 0, node="failed_node" if fail else "confirmed_node"))
    for sid in ("row-11", "row-12"):
        for i in range(2):
            runs.append(_rec("heldout", sid, f"ho_{sid}_{i}"))
    for i in range(4):
        judge_fail = i == 3
        runs.append(_rec("safety", "adv-split", f"saf_{i}", passed=not judge_fail, judge=not judge_fail, structural=True))
    for i in range(2):
        runs.append(_rec("regression", "row-01", f"abl_{i}", passed=i == 0, judge=True, structural=i == 0,
                         reasons=() if i == 0 else ("approval_requirements ['deal_desk'] != label ['deal_desk', 'legal']; missing ['legal']",),
                         variant="ablation", mismatch=None if i == 0 else "under_escalation"))
    return runs


def test_summarise_counts(fixture_runs):
    stats = rp.summarise(fixture_runs)
    reg = stats["regression"]["baseline"]
    assert reg["scenarios"] == 2 and reg["runs"] == 6 and reg["pass"] == 5
    assert reg["flaky"] == 1 and reg["guardrail_events"] == 2 and reg["judge_disagreements"] == 1
    assert reg["worst"] == "row-02" and reg["node_id_pass"] == 5 and reg["judge_pass"] == 6
    assert abs(reg["lb"] - rp.wilson_lower(5, 6)) < 1e-9
    abl = stats["regression"]["ablation"]
    assert abl["runs"] == 2 and abl["pass"] == 1 and abl["under_escalation"] == 1
    saf = stats["safety"]["baseline"]
    assert saf["pass"] == 3 and saf["judge_disagreements"] == 1
    # guardrail events never change the pass count
    for r in fixture_runs:
        r["guardrail_events"] = 7
    assert rp.summarise(fixture_runs)["regression"]["baseline"]["pass"] == 5


def test_report_renders_required_columns(fixture_runs, tmp_path):
    meta = {"created_at": "2026-09-21T02:10:00+00:00", "agent_id": "agent_x", "version_id": "v_9",
            "policy_version": "sha256:abc123", "judge_model": "gpt-5.4-mini",
            "simulated_user_model": "gemini-3.5-flash", "structural_db": "ok"}
    text = rp.render_report(fixture_runs, meta, None, "2026-09-21T02-10-00Z")
    for needle in (("| folder | scenarios | runs | pass | pass rate | Wilson 95% LB | flaky scenarios | "
                   "judge disagreements | guardrail_events | ablation pass rate (LB) |"),
                   "version_id: v_9", "policy_version: sha256:abc123", "judge: gpt-5.4-mini",
                   "not yet calibrated", "## Dev vs held-out", "## Worst scenario per folder",
                   "### regression: row-02 (2/3 pass)", "## Ablation", "| regression |", "| heldout |", "| safety |",
                   "## Components, separately", "under-escalation runs", "## Failing runs (baseline)"):
        assert needle in text, needle
    excerpt_lines = [ln for ln in text.splitlines() if ln.startswith("> ")]
    assert 5 <= len(excerpt_lines) <= 6 * 3
    assert "> agent [failed_node]: The submission did not go through." in text
    assert chr(0x2014) not in text
    with_kappa = rp.render_report(fixture_runs, meta, {"kappa": 0.83, "n": 30}, "x")
    assert "κ = 0.83 (n = 30)" in with_kappa
    # main() writes report.md into the results dir
    (tmp_path / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in fixture_runs) + "\n")
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    assert rp.main([str(tmp_path), "--kappa", str(tmp_path / "missing.json")]) == 0
    assert (tmp_path / "report.md").read_text() == text.replace("2026-09-21T02-10-00Z", tmp_path.resolve().name)


def test_cohens_kappa_sanity():
    assert kp.cohens_kappa([(True, True)] * 10 + [(False, False)] * 10)["kappa"] == 1.0
    chance = [(True, True), (True, False), (False, True), (False, False)] * 5
    assert abs(kp.cohens_kappa(chance)["kappa"]) < 1e-9
    assert kp.cohens_kappa([(True, True)] * 8)["kappa"] == 1.0
    assert kp.cohens_kappa([(True, False)] * 5 + [(False, True)] * 5)["kappa"] == -1.0
    assert kp.cohens_kappa([])["n"] == 0
    mostly = [(True, True)] * 18 + [(True, False)] * 2
    assert 0.0 < kp.cohens_kappa(mostly)["kappa"] < 1.0 or kp.cohens_kappa(mostly)["kappa"] == 0.0


def test_kappa_export_and_compute_roundtrip(fixture_runs, tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in fixture_runs) + "\n")
    to_label = tmp_path / "to_label.jsonl"
    template = tmp_path / "labels.template.csv"
    kp.export_calibration(results, 9, seed=3, out_path=to_label, template_path=template)
    exported = [json.loads(ln) for ln in to_label.read_text().splitlines() if ln.strip()]
    assert len(exported) == 9
    assert {e["condition_type"] for e in exported} == {"deal_id_present", "no_premature_success", "requires_approvals_superset"}
    assert all(e["human_pass"] is None and e["judge_pass"] in (True, False) for e in exported)
    assert all(e["judge_pass_source"] == "rationale" for e in exported)
    rows = list(csv.DictReader(template.open()))
    assert [r["run_id"] for r in rows] == [e["run_id"] for e in exported]
    # the human agrees with the judge except on one requires_approvals item
    labels = tmp_path / "labels.csv"
    with labels.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "condition_idx", "human_pass"])
        flipped = False
        for e in exported:
            human = e["judge_pass"]
            if e["condition_type"] == "requires_approvals_superset" and not flipped:
                human, flipped = not human, True
            w.writerow([e["run_id"], e["condition_idx"], "yes" if human else "no"])
    out = tmp_path / "kappa.json"
    result = kp.compute(labels, to_label, out)
    assert result["n"] == 9 and set(result["per_type"]) == {"deal_id_present", "no_premature_success", "requires_approvals_superset"}
    assert result["per_type"]["requires_approvals_superset"]["kappa"] < 1.0
    assert json.loads(out.read_text())["n"] == 9


@pytest.mark.parametrize("script", ["generate.py", "push_tests.py", "run.py", "report.py", "kappa.py"])
def test_cli_help_works(script):
    proc = subprocess.run([sys.executable, str(ROOT / "evals" / script), "--help"], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout
