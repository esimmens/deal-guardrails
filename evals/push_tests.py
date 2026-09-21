#!/usr/bin/env python
"""Create or update the ElevenAgents tests for every scenario, in three folders.

Idempotent by name. A root folder named regression / safety / heldout is reused when it exists
and created otherwise. A test whose name already exists in its folder is updated in place
(PUT /v1/convai/agent-testing/{id}); nothing is deleted, so test ids stay stable and earlier
runs stay attached to the same test. scenarios/test_ids.json records scenario id -> test id.

    uv run python evals/push_tests.py                  # all three suites
    uv run python evals/push_tests.py --suite safety
    uv run python evals/push_tests.py --dry-run        # print what would be sent, no network
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import conditions  # noqa: E402
from evals.el_api import ElevenLabsTests, load_env  # noqa: E402

SCENARIOS_DIR = HERE / "scenarios"
IDS_PATH = ROOT / "agent" / "ids.json"
TEST_IDS_PATH = SCENARIOS_DIR / "test_ids.json"

JUDGE_MODEL = "gpt-5.4-mini"        # evaluation_model: third family, never the agent's
SIM_USER_MODEL = "gemini-3.5-flash"  # simulated_user_model
SIM_MAX_TURNS = {"regression": 16, "heldout": 16, "safety": 20}
SUITES: tuple[str, ...] = ("regression", "safety", "heldout")
DEFAULT_SLACK_USER_ID = "U_MAREN"


def test_name(suite: str, scenario_id: str) -> str:
    return f"{suite}/{scenario_id}"


def load_scenarios(suite: str, scenarios_dir: Path = SCENARIOS_DIR) -> list[dict[str, Any]]:
    path = scenarios_dir / f"{suite}.json"
    return json.loads(path.read_text()) if path.exists() else []


def _substitute(obj: Any, mapping: dict[str, str]) -> Any:
    """Replace ${NAME} placeholders in every string of a JSON-like structure."""
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace("${" + k + "}", v)
        return obj
    if isinstance(obj, list):
        return [_substitute(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {_substitute(k, mapping): _substitute(v, mapping) for k, v in obj.items()}
    return obj


def regression_body(row: dict[str, Any], *, suite: str, folder_id: str | None,
                    slack_user_id: str = DEFAULT_SLACK_USER_ID) -> dict[str, Any]:
    """Simulation test for one generated row: the persona is the simulated user, the success
    conditions are the outcome sentences with the label's roles interpolated."""
    conds = conditions.regression_conditions(row["label"], row["omitted"])
    return {
        "type": "simulation",
        "name": test_name(suite, row["id"]),
        "simulation_scenario": row["persona"],
        "simulation_max_turns": SIM_MAX_TURNS.get(suite, 16),
        "success_conditions": conditions.texts(conds),
        "tool_mock_config": {"mocking_strategy": "none"},  # the real service, every time
        "evaluation_model": JUDGE_MODEL,
        "simulated_user_model": SIM_USER_MODEL,
        "dynamic_variables": {"integration__slack_user_id": slack_user_id},
        "parent_folder_id": folder_id,
    }


def safety_body(scenario: dict[str, Any], *, folder_id: str | None, tool_id: str,
                slack_user_id: str = DEFAULT_SLACK_USER_ID) -> dict[str, Any]:
    """The hand-written payload fragment, with ${TOOL_ID} / ${SLACK_USER_ID} filled in and the
    shared defaults (models, mocking off, dynamic variables, folder) applied where absent."""
    body = _substitute(json.loads(json.dumps(scenario["test"])), {"TOOL_ID": tool_id, "SLACK_USER_ID": slack_user_id})
    body["name"] = test_name("safety", scenario["id"])
    body["parent_folder_id"] = folder_id
    body.setdefault("dynamic_variables", {})["integration__slack_user_id"] = slack_user_id
    if body.get("type") == "simulation":
        body.setdefault("evaluation_model", JUDGE_MODEL)
        body.setdefault("simulated_user_model", SIM_USER_MODEL)
        body.setdefault("tool_mock_config", {"mocking_strategy": "none"})
        body.setdefault("simulation_max_turns", SIM_MAX_TURNS["safety"])
    return body


def condition_types(suite: str, scenario: dict[str, Any]) -> list[str]:
    """The type tag of each success condition, by index. kappa.py groups agreement by it."""
    if suite == "safety":
        return list(scenario.get("condition_types") or [])
    return [c["type"] for c in conditions.regression_conditions(scenario["label"], scenario["omitted"])]


def bodies_for(suite: str, *, folder_id: str | None, tool_id: str, slack_user_id: str,
               scenarios_dir: Path = SCENARIOS_DIR) -> list[tuple[str, dict[str, Any]]]:
    if suite == "safety":
        return [(s["id"], safety_body(s, folder_id=folder_id, tool_id=tool_id, slack_user_id=slack_user_id))
                for s in load_scenarios("safety", scenarios_dir)]
    return [(r["id"], regression_body(r, suite=suite, folder_id=folder_id, slack_user_id=slack_user_id))
            for r in load_scenarios(suite, scenarios_dir)]


def ensure_folder(api: ElevenLabsTests, name: str) -> str:
    found = api.find_folder(name)
    if found:
        return str(found["id"])
    created = api.create_folder(name)
    print(f"created folder {name!r} -> {created['id']}")
    return str(created["id"])


def existing_tests(api: ElevenLabsTests, folder_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in api.list_tests(parent_folder_id=folder_id):
        if t.get("entity_type", "test") == "test" and t.get("type") != "folder":
            out[str(t["name"])] = str(t["id"])
    return out


def load_test_ids(path: Path = TEST_IDS_PATH) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return {"agent_id": None, "folders": {}, "tests": {}}


def push(suites: list[str], *, dry_run: bool = False, print_bodies: bool = False) -> int:
    env = load_env()
    slack_user_id = env.get("SLACK_ID_MAREN") or DEFAULT_SLACK_USER_ID
    ids = json.loads(IDS_PATH.read_text())
    tool_id = ids.get("tool_id") or "${TOOL_ID}"
    if tool_id == "${TOOL_ID}" and not dry_run:
        print("agent/ids.json has no tool_id; run agent/push.py first", file=sys.stderr)
        return 2

    if dry_run:
        for suite in suites:
            bodies = bodies_for(suite, folder_id=None, tool_id=tool_id, slack_user_id=slack_user_id)
            print(f"[{suite}] {len(bodies)} tests")
            for _sid, body in bodies:
                n_cond = len(body.get("success_conditions") or [])
                extra = " verify_absence" if body.get("tool_call_parameters", {}).get("verify_absence") else ""
                extra += " mocked-error" if body.get("tool_mock_overrides") else ""
                print(f"  {body['name']:<28} {body['type']:<11} conditions={n_cond}{extra}")
                if print_bodies:
                    print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0

    test_ids = load_test_ids()
    test_ids["agent_id"] = ids.get("agent_id")
    with ElevenLabsTests() as api:
        for suite in suites:
            folder_id = ensure_folder(api, suite)
            test_ids["folders"][suite] = folder_id
            existing = existing_tests(api, folder_id)
            created = updated = 0
            for sid, body in bodies_for(suite, folder_id=folder_id, tool_id=tool_id, slack_user_id=slack_user_id):
                name = body["name"]
                if name in existing:
                    api.update_test(existing[name], body)
                    test_ids["tests"][sid] = existing[name]
                    updated += 1
                else:
                    test_ids["tests"][sid] = api.create_test(body)
                    created += 1
            TEST_IDS_PATH.write_text(json.dumps(test_ids, indent=2) + "\n")
            print(f"[{suite}] folder {folder_id}: {created} created, {updated} updated")
    print(f"wrote {TEST_IDS_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", choices=(*SUITES, "all"), default="all")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without calling the API")
    ap.add_argument("--print-bodies", action="store_true", help="with --dry-run: print every request body")
    args = ap.parse_args(argv)
    suites = list(SUITES) if args.suite == "all" else [args.suite]
    return push(suites, dry_run=args.dry_run, print_bodies=args.print_bodies)


if __name__ == "__main__":
    sys.exit(main())
