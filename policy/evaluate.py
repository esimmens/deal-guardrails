"""Deterministic policy evaluator.

The language model never runs this. It reads the AE's sentence and fills a row of
facts; this module decides which approvers policy requires and names the rule that
said so. Two rules are deliberately unresolved and escalate to Deal Desk by name.

Public surface:
    load_policy(path) -> Policy
    evaluate(facts, policy) -> Decision
    merge_terms(llm, confirmed) -> (merged, flags)          # escalate, never de-escalate
    merge_required(engine, llm_claimed, policy) -> (required, llm_added)
    parse_requires_approvals(text) -> list[str]
"""
from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import yaml

from policy.schema import FORBIDDEN_APPROVERS, REVIEWER_ROLES, ROLE_KEYS, TERM_FIELDS, Facts


class PolicyError(ValueError):
    """Raised when the YAML is malformed or references unknown roles/fields."""


class RuleFired(TypedDict):
    id: str
    title: str
    effect: str
    note: str | None


class Decision(TypedDict):
    required: list[str]
    gate_role: str | None
    flags: list[str]
    matched_rule_ids: list[str]
    unresolved: list[str]
    rules_fired: list[RuleFired]
    policy_version: str


@dataclass(frozen=True)
class Policy:
    policy_id: str
    version_label: str
    version: str  # "sha256:<hex of the YAML bytes>"
    roles: tuple[str, ...]
    role_rank: Mapping[str, int]
    rules: tuple[Mapping[str, Any], ...]
    unresolved_escalates_to: str
    yaml_text: str

    @property
    def rule_ids(self) -> list[str]:
        return [str(r["id"]) for r in self.rules]


_FACT_FIELDS = {f.name for f in dataclasses.fields(Facts)}
_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in"}


def load_policy(path: str | Path) -> Policy:
    raw_bytes = Path(path).read_bytes()
    data = yaml.safe_load(raw_bytes)
    roles = tuple(str(r) for r in data.get("roles", []))
    if tuple(roles) != ROLE_KEYS:
        raise PolicyError(f"roles must be exactly {ROLE_KEYS}, got {roles}")
    role_rank = {str(k): int(v) for k, v in data.get("role_rank", {}).items()}
    if set(role_rank) != set(roles):
        raise PolicyError("role_rank must cover every role exactly once")
    esc = str(data.get("precedence", {}).get("unresolved_escalates_to", "deal_desk"))
    if esc not in REVIEWER_ROLES:
        raise PolicyError(f"unresolved_escalates_to must be a reviewer role, got {esc}")
    rules = tuple(data.get("rules", []))
    seen: set[str] = set()
    for rule in rules:
        rid = str(rule.get("id", ""))
        if not rid or rid in seen:
            raise PolicyError(f"duplicate or missing rule id: {rid!r}")
        seen.add(rid)
        _validate_condition(rule.get("when"), rid)
        then = rule.get("then", {}) or {}
        for role in then.get("require", []) or []:
            if role not in roles:
                raise PolicyError(f"rule {rid} requires unknown role {role!r}")
            if role in FORBIDDEN_APPROVERS:
                raise PolicyError(f"rule {rid} requires forbidden approver {role!r}")
    return Policy(
        policy_id=str(data.get("policy_id", "policy")),
        version_label=str(data.get("version_label", "")),
        version="sha256:" + hashlib.sha256(raw_bytes).hexdigest(),
        roles=roles,
        role_rank=role_rank,
        rules=rules,
        unresolved_escalates_to=esc,
        yaml_text=raw_bytes.decode("utf-8"),
    )


def _validate_condition(cond: Any, rid: str) -> None:
    if not isinstance(cond, Mapping):
        raise PolicyError(f"rule {rid}: 'when' must be a mapping")
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            raise PolicyError(f"rule {rid}: '{key}' must be a non-empty list")
        for sub in cond[key]:
            _validate_condition(sub, rid)
        return
    field = cond.get("field")
    if field not in _FACT_FIELDS:
        raise PolicyError(f"rule {rid}: unknown field {field!r}")
    ops = set(cond) - {"field"}
    if not ops or not ops <= _OPS:
        raise PolicyError(f"rule {rid}: unsupported operators {ops - _OPS or 'none'}")


def _cond(cond: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    if "all" in cond:
        return all(_cond(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(_cond(c, facts) for c in cond["any"])
    value = facts[cond["field"]]
    for op, rhs in cond.items():
        if op == "field":
            continue
        if op == "eq" and not value == rhs:
            return False
        if op == "neq" and not value != rhs:
            return False
        if op == "gt" and not value > rhs:
            return False
        if op == "gte" and not value >= rhs:
            return False
        if op == "lt" and not value < rhs:
            return False
        if op == "lte" and not value <= rhs:
            return False
        if op == "in" and value not in rhs:
            return False
        if op == "not_in" and value in rhs:
            return False
    return True


def _sort_roles(roles: set[str], policy: Policy) -> list[str]:
    return sorted(roles, key=lambda r: (-policy.role_rank[r], r))


def evaluate(facts: Facts, policy: Policy) -> Decision:
    """Pure function of (facts, policy). Same inputs, same answer, forever."""
    fd = dataclasses.asdict(facts)
    required: set[str] = set()
    flags: list[str] = []
    matched: list[str] = []
    unresolved: list[str] = []
    fired: list[RuleFired] = []

    for rule in policy.rules:
        if not _cond(rule["when"], fd):
            continue
        rid = str(rule["id"])
        matched.append(rid)
        then = rule.get("then", {}) or {}
        effects: list[str] = []
        for role in then.get("require", []) or []:
            if role == "ae":
                continue  # the requester's own authority is never a reviewer
            required.add(role)
            effects.append(f"require {role}")
        if flag := then.get("flag"):
            flags.append(str(flag))
            effects.append(f"flag {flag}")
        if then.get("unresolved"):
            unresolved.append(rid)
            required.add(policy.unresolved_escalates_to)
            effects.append(f"unresolved, escalate to {policy.unresolved_escalates_to}")
        fired.append(RuleFired(id=rid, title=str(rule.get("title", "")), effect="; ".join(effects),
                               note=then.get("note")))

    req_sorted = _sort_roles(required, policy)
    return Decision(
        required=req_sorted,
        gate_role=req_sorted[0] if req_sorted else None,
        flags=flags,
        matched_rule_ids=matched,
        unresolved=unresolved,
        rules_fired=fired,
        policy_version=policy.version,
    )


def merge_terms(
    llm: Mapping[str, bool | None], confirmed: Mapping[str, bool | None]
) -> tuple[dict[str, bool], list[str]]:
    """Escalate, never de-escalate.

    A term is True if the model read it as True or a human confirmed it True.
    A human-confirmed False only wins when the model did not say True; if the model
    said True and a human says False, the term stays True and a flag names the field so
    Deal Desk resolves it. Silence on both sides is False.
    """
    merged: dict[str, bool] = {}
    flags: list[str] = []
    for key in TERM_FIELDS:
        model_true = bool(llm.get(key)) if llm.get(key) is not None else False
        human = confirmed.get(key)
        if model_true:
            merged[key] = True
            if human is False:
                flags.append(f"human_downgrade_blocked:{key}")
        else:
            merged[key] = bool(human) if human is not None else False
    return merged, flags


def parse_requires_approvals(text: str | None) -> list[str]:
    """'deal_desk, Finance ' -> ['deal_desk', 'finance']; unknown tokens are dropped."""
    if not text:
        return []
    out: list[str] = []
    for tok in str(text).replace(";", ",").split(","):
        t = tok.strip().lower().replace(" ", "_")
        if t in REVIEWER_ROLES and t not in out:
            out.append(t)
    return out


def merge_required(
    engine: list[str], llm_claimed: list[str], policy: Policy
) -> tuple[list[str], list[str]]:
    """The model may add a reviewer. It may never remove one, and it may never add 'ae'."""
    base = set(engine)
    added = [r for r in llm_claimed if r in REVIEWER_ROLES and r not in base]
    return _sort_roles(base | set(added), policy), _sort_roles(set(added), policy)


def gate_for(required: list[str], policy: Policy) -> str | None:
    return _sort_roles(set(required), policy)[0] if required else None
