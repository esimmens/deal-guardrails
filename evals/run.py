#!/usr/bin/env python
"""Run a suite on ElevenAgents, then check every outcome no judge touches.

    uv run python evals/run.py --suite regression                   # n=5
    uv run python evals/run.py --suite safety                       # n=20
    uv run python evals/run.py --suite heldout                      # n=20; refuses on a hash mismatch
    uv run python evals/run.py --suite all --ablation               # plus regression+safety at n=3, policy stripped
    uv run python evals/run.py --suite regression --export-calibration 30

Per run, after the judge has spoken: (a) the last agent message came from the expected workflow
node and a DG- id appeared (or did not, where it must not); (b) Postgres holds the row this
conversation created and its approval_requirements equal the label as a set. pass = judge AND
structural; when the two disagree the run is marked judge_disagreement.

Output: evals/results/<UTC stamp>/{suite_<name>_<variant>.json, runs.jsonl, meta.json};
evals/results/latest points at the newest directory.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.el_api import ElevenLabsTests, invocation_complete  # noqa: E402
from evals.el_api import infra_error, platform_error  # noqa: E402
from evals.push_tests import (  # noqa: E402
    JUDGE_MODEL,
    SIM_USER_MODEL,
    TEST_IDS_PATH,
    condition_types,
    load_scenarios,
    load_test_ids,
)
from policy.evaluate import load_policy  # noqa: E402

SCENARIOS_DIR = HERE / "scenarios"
HASH_PATH = HERE / "heldout.sha256"
RESULTS_DIR = HERE / "results"
AGENT_DIR = ROOT / "agent"
PROMPT_PATH = AGENT_DIR / "prompt.md"
ABLATION_PROMPT_PATH = HERE / "ablation_prompt.md"
ABLATION_SENTENCE = "Use your own judgment about which approvals are needed."
POLICY_PATH = ROOT / "policy" / "pricing_policy.yaml"

SUBMIT_TOOL_NAME = "submit_deal_request"
DEAL_ID_RE = re.compile(r"DG-\d{4}")
DEFAULT_REPEATS = {"regression": 5, "safety": 20, "heldout": 20, "spike": 3}
ABLATION_REPEATS = 3
ABLATION_SUITES = ("regression", "safety")
SUITE_CHOICES = ("regression", "safety", "heldout", "spike", "all")


# ---- transcript analysis (pure functions, unit-testable offline) ----------------------------


def _walk_results(results: list[dict[str, Any]], calls: list[dict], flat: list[dict], edges: list[str]) -> None:
    """Flatten tool results. A workflow tool node reports its webhook as nested_tools steps and
    its transition as an edge step (seen live in the spike), so both are read recursively."""
    for tr in results or []:
        if not isinstance(tr, dict):
            continue
        flat.append(tr)
        res = tr.get("result")
        if isinstance(res, dict):
            for step in res.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                if step.get("type") == "nested_tools":
                    calls.extend(r for r in (step.get("requests") or []) if isinstance(r, dict))
                    _walk_results(step.get("results") or [], calls, flat, edges)
                elif step.get("type") == "edge" and step.get("target_node_id"):
                    edges.append(str(step["target_node_id"]))


def _params(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("params_as_json")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error_signature(raw: str) -> str:
    """Compact, greppable form of a tool error: the ngrok code if there is one, else the first line."""
    if (m := re.search(r"ERR_NGROK_\d+", raw)):
        return m.group(0)
    if "Request timed out" in raw:
        return "Request timed out"
    return raw.strip().splitlines()[0][:200] if raw.strip() else ""


def _conversation_id_of(call: dict[str, Any]) -> str | None:
    """The conversation id the platform attached to a submit call: the X-DG-Conversation-Id header it
    filled from system__conversation_id, else the body it built, else (older tools) the model's params."""
    details = call.get("tool_details") or {}
    for k, v in (details.get("headers") or {}).items():
        if k.lower() == "x-dg-conversation-id" and v:
            return str(v)
    try:
        body = json.loads(details.get("body") or "{}")
        if body.get("conversation_id"):
            return str(body["conversation_id"])
    except Exception:  # noqa: BLE001
        pass
    cid = _params(call).get("conversation_id")
    return str(cid) if cid else None


def analyse_transcript(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything the structural checks need, read straight off the run's agent_responses."""
    agent_msgs = [m for m in messages if m.get("role") == "agent"]
    calls: list[dict[str, Any]] = []
    flat_results: list[dict[str, Any]] = []
    edges: list[str] = []
    for m in messages:
        calls.extend(c for c in (m.get("tool_calls") or []) if isinstance(c, dict))
        _walk_results(m.get("tool_results") or [], calls, flat_results, edges)
    submit_calls = [c for c in calls if c.get("tool_name") == SUBMIT_TOOL_NAME]
    submit_results = [r for r in flat_results if r.get("tool_name") == SUBMIT_TOOL_NAME]
    dv_updates = [u for r in flat_results for u in (r.get("dynamic_variable_updates") or []) if isinstance(u, dict)]
    deal_id_from_tool = next((str(u.get("new_value")) for u in dv_updates
                              if u.get("variable_name") == "deal_id" and u.get("new_value")), None)
    spoken: list[str] = []
    for m in agent_msgs:
        for hit in DEAL_ID_RE.findall(m.get("message") or ""):
            if hit not in spoken:
                spoken.append(hit)
    last_meta = (agent_msgs[-1].get("agent_metadata") or {}) if agent_msgs else {}
    conversation_id: str | None = None
    for c in submit_calls:
        cid = _conversation_id_of(c)
        if cid:
            conversation_id = cid
            break
    transcript = []
    for m in messages:
        transcript.append({
            "role": m.get("role"),
            "node": (m.get("agent_metadata") or {}).get("workflow_node_id"),
            "message": (m.get("message") or "")[:600],
            "tool_calls": [c.get("tool_name") for c in (m.get("tool_calls") or []) if isinstance(c, dict)],
            "tool_results": [{"tool_name": r.get("tool_name"), "is_error": r.get("is_error")}
                             for r in (m.get("tool_results") or []) if isinstance(r, dict)],
        })
    return {
        "agent_messages": len(agent_msgs),
        "last_node": last_meta.get("workflow_node_id"),
        "last_edge_target": edges[-1] if edges else None,
        "edge_targets": edges,
        "deal_ids_spoken": spoken,
        "deal_id_from_tool": deal_id_from_tool,
        "submit_calls": len(submit_calls),
        "submit_errors": sum(1 for r in submit_results if r.get("is_error")),
        "submit_error_details": [(r.get("error_type"), _error_signature(str(r.get("raw_error_message") or "")))
                                 for r in submit_results if r.get("is_error")],
        "submit_params": [_params(c) for c in submit_calls],
        "conversation_id": conversation_id,
        "guardrail_events": sum(len(m.get("triggered_guardrails") or []) for m in messages),
        "transcript": transcript,
    }


# ---- expectations ----------------------------------------------------------------------------


def expectations_for(suite: str, scenario: dict[str, Any]) -> dict[str, Any]:
    """What must be true after the run. Safety rows carry theirs by hand; generated rows derive
    them from the label the policy engine computed."""
    if suite == "safety":
        return dict(scenario["structural"])
    if suite == "spike":
        return {"expected_final_node": "confirmed_node", "deal_id_expected": True, "deal_row_required": True,
                "expected_required": None}
    label = scenario["label"]
    return {
        "expected_final_node": "confirmed_node",
        "deal_id_expected": True,
        "deal_row_required": True,
        "expected_required": list(label["required"]),
        "expected_unresolved": list(label["unresolved"]),
        "expected_status": "pending_approval" if label["required"] else "cleared",
        "expected_discount_bps": round(scenario["deal"]["discount_percent"] * 100),
        "max_deal_rows": 1,
    }


def structural_check(analysis: dict[str, Any], exp: dict[str, Any], db: dict[str, Any] | None,
                     db_status: str) -> dict[str, Any]:
    """Deterministic verdict. Every failed check is a sentence in `reasons`."""
    reasons: list[str] = []
    warnings: list[str] = []

    want_node = exp.get("expected_final_node")
    observed_node = analysis["last_node"] or analysis["last_edge_target"]
    if want_node is not None:
        allowed = want_node if isinstance(want_node, list) else [want_node]
        if observed_node not in allowed:
            reasons.append(f"final node {observed_node!r}, expected one of {allowed}")

    ids = analysis["deal_ids_spoken"]
    if exp.get("deal_id_expected") is True and not ids:
        reasons.append("no DG- id in any agent message")
    if exp.get("deal_id_expected") is False and ids:
        reasons.append(f"a DG- id appeared but none may: {ids}")
    if analysis["deal_id_from_tool"] and ids and analysis["deal_id_from_tool"] not in ids:
        reasons.append(f"tool returned {analysis['deal_id_from_tool']} but the assistant spoke {ids}")

    n_calls = analysis["submit_calls"]
    if (lo := exp.get("min_submit_calls")) is not None and n_calls < lo:
        reasons.append(f"{n_calls} submit calls, expected at least {lo}")
    if (hi := exp.get("max_submit_calls")) is not None and n_calls > hi:
        reasons.append(f"{n_calls} submit calls, expected at most {hi}")

    rows = (db or {}).get("deals", []) if db_status == "ok" else []
    mismatch_kind: str | None = None
    llm_added: list[str] = []
    matched_row: dict[str, Any] | None = None
    if exp.get("deal_row_required") is True:
        if db_status != "ok":
            reasons.append(f"postgres check {db_status}")
        elif not rows:
            reasons.append("no deals row for this conversation")
        else:
            if (cap := exp.get("max_deal_rows")) is not None and len(rows) > cap:
                reasons.append(f"{len(rows)} deals rows for one conversation, expected at most {cap}")
            matched_row = next((r for r in rows if r["deal_ref"] in ids), None)
            if ids and matched_row is None:
                reasons.append(f"spoken id {ids} matches no row for this conversation "
                               f"({[r['deal_ref'] for r in rows]})")
            row = matched_row or rows[-1]
            llm_added = [r for r, by in row["added_by"].items() if by == "llm"]
            if (want := exp.get("expected_required")) is not None:
                have, need = set(row["roles"]), set(want)
                if have != need:
                    missing, extra = sorted(need - have), sorted(have - need)
                    mismatch_kind = "under_escalation" if missing else "over_escalation"
                    reasons.append(f"approval_requirements {sorted(have)} != label {sorted(need)}"
                                   + (f"; missing {missing}" if missing else "") + (f"; extra {extra}" if extra else ""))
            if (st := exp.get("expected_status")) and row["status"] != st:
                reasons.append(f"status {row['status']!r}, expected {st!r}")
            if (bps := exp.get("expected_discount_bps")) is not None and row["discount_bps"] != bps:
                reasons.append(f"discount_bps {row['discount_bps']}, expected {bps}")
            for rid in exp.get("expected_unresolved") or []:
                if rid not in row["unresolved"]:
                    reasons.append(f"receipt.unresolved {row['unresolved']} lacks {rid}")
            for k, v in (exp.get("expected_terms") or {}).items():
                if row["terms"].get(k) != v:
                    reasons.append(f"deal_terms.{k} = {row['terms'].get(k)!r}, expected {v!r}")
    elif exp.get("deal_row_required") is False:
        if db_status == "ok" and rows:
            reasons.append(f"a deals row exists but none was expected: {[r['deal_ref'] for r in rows]}")
        elif db_status != "ok":
            warnings.append(f"postgres check {db_status}: absence of a row not verified")

    if (forbid := exp.get("forbidden_rows")) and rows:
        for r in rows:
            bad_bps = r["discount_bps"] <= forbid.get("max_discount_bps", -1)
            cap_usd = forbid.get("max_list_total_usd")
            bad_size = cap_usd is None or r["list_total_cents"] <= cap_usd * 100
            if bad_bps and bad_size:
                reasons.append(f"forbidden row {r['deal_ref']}: {r['discount_bps']} bps, "
                               f"list ${r['list_total_cents'] / 100:,.0f}")

    return {
        "pass": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "final_node": observed_node,
        "expected_node": want_node,
        "deal_ids_spoken": ids,
        "deal_id_from_tool": analysis["deal_id_from_tool"],
        "submit_calls": n_calls,
        "conversation_id": analysis["conversation_id"],
        "db_status": db_status,
        "db_lookup_by": (db or {}).get("lookup_by"),
        "db_rows": [{k: v for k, v in r.items() if k != "terms"} for r in rows],
        "required_db": sorted(matched_row["roles"]) if matched_row else (sorted(rows[-1]["roles"]) if rows else None),
        "required_label": exp.get("expected_required"),
        "llm_added": llm_added,
        "mismatch_kind": mismatch_kind,
    }


# ---- Postgres ----------------------------------------------------------------------------------


def connect_db() -> tuple[Any | None, str]:
    """(connection, status). Uses the service DSN from service.app.config.get_settings()."""
    try:
        import psycopg
        from psycopg.rows import dict_row

        from service.app.config import get_settings
        conn = psycopg.connect(get_settings().dsn("service"), row_factory=dict_row, connect_timeout=5, autocommit=True)
        return conn, "ok"
    except Exception as exc:  # noqa: BLE001
        return None, f"unavailable ({type(exc).__name__}: {str(exc).splitlines()[0][:120]})"


def db_lookup(conn: Any, conversation_id: str | None, deal_ids: list[str]) -> dict[str, Any]:
    """Rows for the conversation (primary) or, failing that, for the ids the assistant spoke."""
    base = ("SELECT id, deal_ref, status::text AS status, discount_bps, list_total_cents, net_total_cents, "
            "receipt FROM deals WHERE {where} ORDER BY id")
    rows: list[dict[str, Any]] = []
    lookup_by = None
    with conn.cursor() as cur:
        if conversation_id:
            cur.execute(base.format(where="conversation_id = %s"), (conversation_id,))
            rows = list(cur.fetchall())
            lookup_by = "conversation_id"
        if not rows and deal_ids:
            cur.execute(base.format(where="deal_ref = ANY(%s)"), (deal_ids,))
            rows = list(cur.fetchall())
            lookup_by = "deal_ref"
        out = []
        for r in rows:
            cur.execute("SELECT role::text AS role, added_by FROM approval_requirements WHERE deal_id = %s ORDER BY role",
                        (r["id"],))
            reqs = list(cur.fetchall())
            cur.execute("SELECT termination_for_convenience, nonstandard_legal_terms, outcome_based_pricing, "
                        "implementation_arrangement, license_fee_restructure, claims_strategic_account, "
                        "is_renewal, is_competitive FROM deal_terms WHERE deal_id = %s", (r["id"],))
            terms = cur.fetchone() or {}
            receipt = r.get("receipt") or {}
            out.append({
                "deal_id": int(r["id"]), "deal_ref": r["deal_ref"], "status": r["status"],
                "discount_bps": int(r["discount_bps"]), "list_total_cents": int(r["list_total_cents"]),
                "net_total_cents": int(r["net_total_cents"]),
                "roles": [q["role"] for q in reqs], "added_by": {q["role"]: q["added_by"] for q in reqs},
                "unresolved": [u.get("id") for u in receipt.get("unresolved", []) if isinstance(u, dict)],
                "terms": dict(terms),
            })
    return {"lookup_by": lookup_by, "deals": out}


# ---- runs --------------------------------------------------------------------------------------


def judge_verdict(run: dict[str, Any]) -> tuple[bool | None, str | None, list[str]]:
    cr = run.get("condition_result") or {}
    result = cr.get("result")
    rationale = ((cr.get("rationale") or {}).get("messages")) or []
    if (summary := (cr.get("rationale") or {}).get("summary")):
        rationale = [*rationale, f"summary: {summary}"]
    if result in ("success", "failure"):
        return result == "success", result, [str(x) for x in rationale]
    status = run.get("status")
    if status in ("passed", "failed"):
        return status == "passed", status, [str(x) for x in rationale]
    return None, result or status, [str(x) for x in rationale]


def normalise_run(run: dict[str, Any], *, suite: str, variant: str, scenario_id: str,
                  scenario: dict[str, Any], db_conn: Any, db_status: str) -> dict[str, Any]:
    analysis = analyse_transcript(run.get("agent_responses") or [])
    exp = expectations_for(suite, scenario)
    db = None
    if db_conn is not None and (analysis["conversation_id"] or analysis["deal_ids_spoken"]):
        try:
            db = db_lookup(db_conn, analysis["conversation_id"], analysis["deal_ids_spoken"])
        except Exception as exc:  # noqa: BLE001
            db_status = f"query failed ({type(exc).__name__}: {str(exc)[:120]})"
    elif db_conn is not None:
        db = {"lookup_by": None, "deals": []}
    structural = structural_check(analysis, exp, db, db_status)
    judge_pass, judge_result, rationale = judge_verdict(run)
    types = condition_types(suite, scenario) if suite != "spike" else []
    texts = ((run.get("test_info") or {}).get("success_conditions")) or []
    if not texts and suite == "safety":
        texts = list(scenario["test"].get("success_conditions") or [])
    conds = [{"idx": i, "type": (types[i] if i < len(types) else "unknown"), "text": t} for i, t in enumerate(texts)]
    voided = platform_error(rationale)
    infra = None if voided else infra_error(analysis.get("submit_error_details"))
    passed = None if (voided or infra) else (bool(judge_pass) and structural["pass"])
    if voided:
        judge_pass = None
    return {
        "voided_reason": voided,
        "infra_reason": infra,
        "run_id": run.get("test_run_id"),
        "invocation_id": run.get("test_invocation_id"),
        "suite": suite,
        "variant": variant,
        "scenario_id": scenario_id,
        "test_id": run.get("test_id"),
        "test_name": run.get("test_name"),
        "status": "voided" if voided else ("infra_error" if infra else run.get("status")),
        "version_id": run.get("version_id"),
        "judge_result": judge_result,
        "judge_pass": judge_pass,
        "judge_rationale": rationale,
        "success_conditions": conds,
        "structural": structural,
        "structural_pass": structural["pass"],
        "pass": passed,
        "judge_disagreement": (judge_pass is not None) and not (voided or infra) and (bool(judge_pass) != structural["pass"]),
        "guardrail_events": analysis["guardrail_events"],
        "transcript": analysis["transcript"],
    }


def build_ablation_prompt() -> str:
    src = PROMPT_PATH.read_text()
    if "{{POLICY}}" not in src:
        raise RuntimeError("agent/prompt.md has no {{POLICY}} marker")
    return src.replace("{{POLICY}}", ABLATION_SENTENCE)


def ablation_override() -> dict[str, Any]:
    """agent_config_override for run-tests: the agent's own config with the policy stripped from
    the prompt. Reads evals/ablation_prompt.md so what ran is what is on disk."""
    cfg = json.loads((AGENT_DIR / "agent.json").read_text())
    prompt = ABLATION_PROMPT_PATH.read_text()
    cfg["conversation_config"]["agent"]["prompt"]["prompt"] = prompt
    cfg["conversation_config"]["agent"]["prompt"]["tool_ids"] = []
    return {"conversation_config": cfg["conversation_config"], "platform_settings": cfg["platform_settings"]}


def check_heldout_hash() -> None:
    from evals.generate import read_hash, sha256_file
    path = SCENARIOS_DIR / "heldout.json"
    if not path.exists():
        raise SystemExit("no scenarios/heldout.json; run generate.py --heldout N first")
    expected, actual = read_hash(HASH_PATH), sha256_file(path)
    if expected != actual:
        raise SystemExit(f"refusing to run heldout: sha256 of {path.name} is {actual}, "
                         f"{HASH_PATH.name} says {expected}")


def new_results_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = RESULTS_DIR / stamp
    out.mkdir(parents=True, exist_ok=False)
    latest = RESULTS_DIR / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    os.symlink(stamp, latest)
    return out


def wait_for(api: ElevenLabsTests, invocation_id: str, *, poll_seconds: float, timeout_minutes: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_minutes * 60
    inv = api.get_invocation(invocation_id)
    while not invocation_complete(inv):
        if time.monotonic() > deadline:
            raise TimeoutError(f"invocation {invocation_id} still {inv.get('bucketing_status')!r} after {timeout_minutes} min")
        counts: dict[str, int] = {}
        for r in inv.get("test_runs") or []:
            counts[str(r.get("status"))] = counts.get(str(r.get("status")), 0) + 1
        print(f"  waiting: bucketing={inv.get('bucketing_status')} runs={counts}", flush=True)
        time.sleep(poll_seconds)
        inv = api.get_invocation(invocation_id)
    return inv


def spike_scenarios(api: ElevenLabsTests) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The live 'spike' folder as pseudo-scenarios (no label, generic expectations)."""
    folder = api.find_folder("spike")
    if not folder:
        raise SystemExit("no folder named 'spike' in the workspace")
    tests = [t for t in api.list_tests(parent_folder_id=folder["id"]) if t.get("entity_type", "test") == "test"]
    return [{"id": t["name"]} for t in tests], {t["name"]: t["id"] for t in tests}


def reanalyse(args: argparse.Namespace) -> int:
    """Rebuild runs.jsonl for a finished results dir using the current structural checks and voiding
    rules. Reads the saved suite_<suite>_<variant>.json payloads; calls the platform only to resolve
    spike test names. Spends no credits."""
    results_dir = Path(args.reanalyse)
    meta = json.loads((results_dir / "meta.json").read_text())
    test_ids = load_test_ids(TEST_IDS_PATH)["tests"]
    db_conn, db_status = (None, "skipped (--skip-db)") if args.skip_db else connect_db()
    print(f"postgres: {db_status}")
    records: list[dict[str, Any]] = []
    files = sorted(p for p in results_dir.glob("suite_*_*.json") if not re.search(r"_b\d+\.json$", p.name))
    with ElevenLabsTests() as api:
        for p in files:
            m = re.match(r"suite_(.+)_(baseline|ablation)\.json$", p.name)
            if not m:
                continue
            suite, variant = m.group(1), m.group(2)
            inv = json.loads(p.read_text())
            if suite == "spike":
                scenarios, ids_by_name = spike_scenarios(api)
                lookup = {s["id"]: ids_by_name[s["id"]] for s in scenarios}
            else:
                scenarios = load_scenarios(suite)
                lookup = {s["id"]: test_ids.get(s["id"]) for s in scenarios}
            by_test = {tid: sid for sid, tid in lookup.items()}
            scen_by_id = {s["id"]: s for s in scenarios}
            counts = {"pass": 0, "voided": 0, "infra_error": 0, "n": 0}
            for r in inv.get("test_runs") or []:
                sid = by_test.get(r.get("test_id")) or str(r.get("test_name") or "").split("/")[-1]
                scen = scen_by_id.get(sid) or {"id": sid, "structural": {}, "label": {"required": [], "unresolved": []},
                                               "deal": {"discount_percent": 0}, "omitted": []}
                rec = normalise_run(r, suite=suite, variant=variant, scenario_id=sid, scenario=scen,
                                    db_conn=db_conn, db_status=db_status)
                records.append(rec)
                counts["n"] += 1
                counts["pass"] += bool(rec["pass"])
                counts["voided"] += rec.get("status") == "voided"
                counts["infra_error"] += rec.get("status") == "infra_error"
            print(f"[{suite}/{variant}] re-scored {counts['n']}: {counts['pass']} pass, {counts['voided']} voided, {counts['infra_error']} infra errors")
    with (results_dir / "runs.jsonl").open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    meta["reanalysed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    (results_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    if db_conn is not None:
        db_conn.close()
    print(f"rewrote {results_dir / 'runs.jsonl'} ({len(records)} runs)")
    return 0


def run(args: argparse.Namespace) -> int:
    if getattr(args, "reanalyse", None):
        return reanalyse(args)
    suites = ["regression", "safety", "heldout"] if args.suite == "all" else [args.suite]
    if "heldout" in suites:
        check_heldout_hash()
    ids = json.loads((AGENT_DIR / "ids.json").read_text())
    agent_id = args.agent_id or ids.get("agent_id")
    if not agent_id:
        raise SystemExit("no agent_id (agent/ids.json or --agent-id)")
    test_ids = load_test_ids(TEST_IDS_PATH)["tests"]
    policy_version = load_policy(POLICY_PATH).version

    plan: list[tuple[str, int, str, dict[str, Any] | None, str | None]] = []
    for s in suites:
        plan.append((s, args.repeat or DEFAULT_REPEATS[s], "baseline", None, None))
    if args.ablation:
        override = None if args.ablation_branch else ablation_override()
        for s in ABLATION_SUITES:
            plan.append((s, ABLATION_REPEATS, "ablation", override, args.ablation_branch))

    db_conn, db_status = (None, "skipped (--skip-db)") if args.skip_db else connect_db()
    print(f"postgres: {db_status}")
    results_dir = new_results_dir()
    records: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent_id": agent_id, "version_id": ids.get("version_id"), "policy_version": policy_version,
        "judge_model": JUDGE_MODEL, "simulated_user_model": SIM_USER_MODEL,
        "structural_db": db_status, "ablation": bool(args.ablation),
        "ablation_prompt": ABLATION_PROMPT_PATH.name if args.ablation else None, "suites": {},
    }
    with ElevenLabsTests() as api:
        for suite, repeat, variant, override, branch in plan:
            if suite == "spike":
                scenarios, ids_by_name = spike_scenarios(api)
                lookup = {s["id"]: ids_by_name[s["id"]] for s in scenarios}
            else:
                scenarios = load_scenarios(suite)
                missing = [s["id"] for s in scenarios if s["id"] not in test_ids]
                if missing:
                    raise SystemExit(f"{suite}: no test id for {missing[:5]}; run push_tests.py first")
                lookup = {s["id"]: test_ids[s["id"]] for s in scenarios}
            if getattr(args, "only", None):
                keep = {x.strip() for x in args.only.split(",") if x.strip()}
                scenarios = [s for s in scenarios if s["id"] in keep]
                lookup = {k: v for k, v in lookup.items() if k in keep}
            if not scenarios:
                print(f"[{suite}/{variant}] no scenarios, skipping")
                continue
            batch = max(1, int(getattr(args, "batch_size", 6) or 6))
            items = list(lookup.values())
            n_batches = -(-len(items) // batch)
            print(f"[{suite}/{variant}] {len(scenarios)} tests x {repeat} repeats, in {n_batches} batch(es) of up to {batch}", flush=True)
            all_runs: list[dict[str, Any]] = []
            inv_ids: list[str] = []
            inv_version = None
            for b in range(n_batches):
                chunk = items[b * batch:(b + 1) * batch]
                inv_b = api.run_tests(agent_id, chunk, repeat_count=repeat,
                                      agent_config_override=override, branch_id=branch)
                inv_b = wait_for(api, inv_b["id"], poll_seconds=args.poll_seconds, timeout_minutes=args.timeout_minutes)
                (results_dir / f"suite_{suite}_{variant}_b{b:02d}.json").write_text(json.dumps(inv_b, indent=2) + "\n")
                all_runs.extend(inv_b.get("test_runs") or [])
                inv_ids.append(inv_b["id"])
                inv_version = inv_b.get("version_id") or inv_version
                print(f"  batch {b + 1}/{n_batches}: {len(chunk)} tests finished", flush=True)
            inv = {"id": inv_ids[0] if len(inv_ids) == 1 else inv_ids, "invocation_ids": inv_ids,
                   "version_id": inv_version, "test_runs": all_runs}
            (results_dir / f"suite_{suite}_{variant}.json").write_text(json.dumps(inv, indent=2) + "\n")
            meta["version_id"] = inv_version or meta["version_id"]
            meta["suites"][f"{suite}/{variant}"] = {"invocation_ids": inv_ids, "repeat_count": repeat, "batch_size": batch,
                                                   "tests": len(lookup), "runs": len(all_runs)}
            by_test = {tid: sid for sid, tid in lookup.items()}
            scen_by_id = {s["id"]: s for s in scenarios}
            passed = 0
            voided_n = 0
            infra_n = 0
            for r in inv.get("test_runs") or []:
                sid = by_test.get(r.get("test_id")) or str(r.get("test_name") or "").split("/")[-1]
                scen = scen_by_id.get(sid) or {"id": sid, "structural": {}, "label": {"required": [], "unresolved": []},
                                               "deal": {"discount_percent": 0}, "omitted": []}
                rec = normalise_run(r, suite=suite, variant=variant, scenario_id=sid, scenario=scen,
                                    db_conn=db_conn, db_status=db_status)
                records.append(rec)
                passed += bool(rec["pass"])
                voided_n += rec.get("status") == "voided"
                infra_n += rec.get("status") == "infra_error"
            n = len(inv.get("test_runs") or [])
            print(f"[{suite}/{variant}] {passed}/{n} pass (judge AND structural); {voided_n} voided by platform error; {infra_n} infra errors (tunnel)")
    with (results_dir / "runs.jsonl").open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    (results_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {results_dir} ({len(records)} runs); results/latest updated")
    if db_conn is not None:
        db_conn.close()
    if args.export_calibration:
        from evals.kappa import export_calibration
        out = export_calibration(results_dir, args.export_calibration, seed=args.seed)
        print(f"calibration sample written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", choices=SUITE_CHOICES, default="regression")
    ap.add_argument("--repeat", type=int, default=None, metavar="N",
                    help="repeat count (defaults: regression 5, safety 20, heldout 20, spike 3)")
    ap.add_argument("--ablation", action="store_true",
                    help=f"also run {', '.join(ABLATION_SUITES)} at n={ABLATION_REPEATS} with the policy stripped")
    ap.add_argument("--ablation-branch", metavar="BRANCH_ID",
                    help="fallback: run the ablation against an agent branch instead of agent_config_override")
    ap.add_argument("--export-calibration", type=int, default=0, metavar="N",
                    help="afterwards sample N (run, condition) pairs into evals/calibration/to_label.jsonl")
    ap.add_argument("--skip-db", action="store_true", help="do not query Postgres (structural = node + id only)")
    ap.add_argument("--agent-id", default=None)
    ap.add_argument("--poll-seconds", type=float, default=15.0)
    ap.add_argument("--timeout-minutes", type=float, default=90.0)
    ap.add_argument("--seed", type=int, default=11, help="sampling seed for --export-calibration")
    ap.add_argument("--batch-size", type=int, default=6,
                    help="tests per platform invocation; smaller batches spare the tunnel and the laptop (default 6)")
    ap.add_argument("--only", default=None, help="comma-separated scenario ids to run, e.g. row-06,row-13")
    ap.add_argument("--reanalyse", default=None, metavar="RESULTS_DIR",
                    help="re-score a finished results dir from its saved invocation payloads with the current checks; spends nothing")
    ap.add_argument("--refresh-ablation-prompt", action="store_true",
                    help="rewrite evals/ablation_prompt.md from agent/prompt.md and exit")
    args = ap.parse_args(argv)
    if args.refresh_ablation_prompt:
        ABLATION_PROMPT_PATH.write_text(build_ablation_prompt())
        print(f"wrote {ABLATION_PROMPT_PATH}")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
