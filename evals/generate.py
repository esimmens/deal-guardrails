#!/usr/bin/env python
"""Reverse-generated regression dataset (plan 6.8, non-negotiable 9).

    factor row -> concrete deal -> label = evaluate(facts, policy) -> surface text -> scenarios/*.json

The generator never decides an answer. Every label is the policy engine's verdict on the deal it
built; the surface text is written afterwards from the same deal, with the fields the row says to
omit left out. Offline (`--dry-run`) the text comes from deterministic templates; by default
OpenAI writes it and a regex rejects anything that restates a number as `key: value`.

    uv run python evals/generate.py --dry-run --heldout 8 --seed 11
    uv run python evals/generate.py --heldout 8                    # needs OPENAI_API_KEY
    uv run python evals/generate.py --review --verified-by "Eddie Simmens"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from allpairspy import AllPairs
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evals.el_api import load_env  # noqa: E402

from policy.evaluate import Policy, evaluate, load_policy  # noqa: E402
from policy.schema import TERM_FIELDS, Facts  # noqa: E402
from service.app.core import derive_amounts  # noqa: E402

DEFAULT_SEED = 11
DEFAULT_MODEL = "gpt-4.1"
FACTORS_PATH = HERE / "factors.yaml"
SCENARIOS_DIR = HERE / "scenarios"
HASH_PATH = HERE / "heldout.sha256"
POLICY_PATH = ROOT / "policy" / "pricing_policy.yaml"
ACCOUNTS_PATH = ROOT / "db" / "seed" / "accounts.yaml"

REQUESTER: dict[str, str] = {
    "name": "Maren Holloway",
    "title": "Enterprise Account Executive",
    "email": "maren.holloway@orielspeech.example",
    "slack_placeholder": "U_MAREN",
}

# discount_band -> inclusive percent range. Bands are the policy's (ASSUMPTION: 15 / 30).
BAND_RANGES: dict[str, tuple[int, int]] = {
    "ae_le15": (5, 15),
    "dd_15_25": (16, 25),
    "contested_25_30": (26, 30),
    "cro_gt30": (31, 45),
}
# term_shape -> (term_months, payment_terms, the AE talks in monthly figures)
TERM_SHAPES: dict[str, tuple[int, str, bool]] = {
    "12mo_net30": (12, "net_30", False),
    "36mo_annual_prepaid": (36, "annual_prepaid", False),
    "24mo_net60": (24, "net_60", False),
    "monthly_12mo": (12, "net_30", True),
}
# nonstandard_class -> the one term boolean it sets
CLASS_FIELD: dict[str, str | None] = {
    "none": None,
    "tfc": "termination_for_convenience",
    "legal": "nonstandard_legal_terms",
    "outcome_based": "outcome_based_pricing",
    "implementation": "implementation_arrangement",
    "license_restructure": "license_fee_restructure",
    "strategic_claim": "claims_strategic_account",
}
# stated_unit -> the value_basis the tool should carry
UNIT_BASIS: dict[str, str] = {
    "list_total": "list_total",
    "net_total": "net_total",
    "acv": "acv",
    "per_unit_x_volume": "per_unit",
}
UNIT_MEANING: dict[str, str] = {
    "list_total": "the list price for the whole contract before any discount",
    "net_total": "the total the customer pays over the whole term after the discount",
    "acv": "the annual value after the discount",
    "per_unit_x_volume": "a per-minute list price times the committed volume, which you have not multiplied out",
}
# Fields the first message may leave out. The eight yes/no answers are always collected by the
# assistant's own question, so they are never "omitted"; the persona holds the answers.
OMITTABLE: tuple[str, ...] = ("term_months", "payment_terms", "segment")

# Multiples of 12,000 so monthly, annual and per-unit figures come out in whole dollars.
# Max net is 864,000 * 0.95 < 1,000,000, so R11 (large deal) never fires incidentally.
LIST_TOTAL_MENU: list[int] = [48_000, 72_000, 96_000, 120_000, 144_000, 180_000, 240_000, 288_000,
                              360_000, 432_000, 480_000, 600_000, 720_000, 864_000]
AGENT_MINUTE_USD = 0.08  # the one real anchor in db/seed/price_book.yaml
COMPETITORS = ["Sonant", "Veloq", "Cadenza AI"]  # fictional
SEGMENT_WORDS = {"smb": "SMB", "mid_market": "mid-market", "enterprise": "enterprise", "public_sector": "public sector"}
PAYMENT_WORDS = {"net_30": "net-30", "net_45": "net-45", "net_60": "net-60", "net_90": "net-90",
                 "annual_prepaid": "annual prepaid", "multi_year_prepaid": "full-term prepaid", "other": "other"}
PAYMENT_PHRASES = {"net_30": "net-30", "net_60": "net-60 terms", "annual_prepaid": "paid annually in advance"}
TERM_QUESTION_LABELS = {
    "termination_for_convenience": "termination for convenience",
    "nonstandard_legal_terms": "non-standard legal terms",
    "outcome_based_pricing": "outcome-based pricing",
    "implementation_arrangement": "implementation arrangement",
    "license_fee_restructure": "license fee restructuring",
    "claims_strategic_account": "claiming strategic-account status",
    "is_renewal": "renewal",
    "is_competitive": "competitive",
}
NONSTANDARD_CLAUSES: dict[str, list[str]] = {
    "none": [""],
    "tfc": ["They want a termination for convenience clause.",
            "One wrinkle, they are asking for termination for convenience."],
    "legal": ["They want to sign on their own paper for the MSA.",
              "On the legal side they are asking to change the indemnity cap."],
    "outcome_based": ["Part of the fee would be tied to their containment rate once they go live.",
                      "They want a chunk of the fee contingent on hitting a containment milestone."],
    "implementation": ["We would bundle the implementation work into the deal.",
                       "Onboarding and integration work is bundled in at no charge."],
    "license_restructure": ["They want the platform fee folded into the per-minute rate.",
                            "We are moving the platform fee into usage instead of a separate line."],
    "strategic_claim": ["This is a strategic account for us.", "Flagging that this is a strategic logo."],
}

# ---- leakage guards -------------------------------------------------------------------------

_LEAK_KEY_RE = re.compile(
    r"\b(discount_percent|amount_usd|list_total|net_total|acv|term_months|payment_terms|segment|"
    r"value_basis|requires_approvals|account_name|discount percent)\s*[:=]", re.IGNORECASE)
_LEAK_KV_RE = re.compile(
    r"([A-Za-z_][A-Za-z_ ]{0,40}?)\s*[:=]\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(%|percent|k\b|m\b)?", re.IGNORECASE)


def deal_numbers(deal: dict[str, Any]) -> set[float]:
    nums = {float(deal["discount_percent"]), float(deal["list_total_usd"]), float(deal["net_total_usd"]),
            float(deal["acv_usd"]), float(deal["stated_amount_usd"]),
            deal["list_total_usd"] / 12, deal["net_total_usd"] / 12, deal["acv_usd"] / 12}
    if deal.get("per_unit"):
        nums.add(float(deal["per_unit"]["volume"]))
    return nums


def structured_leak(text: str, numbers: set[float]) -> str | None:
    """The offending snippet if the text restates a number as `key: value` / `key=value`, else None."""
    if (m := _LEAK_KEY_RE.search(text)) is not None:
        return m.group(0)
    for m in _LEAK_KV_RE.finditer(text):
        value = float(m.group(2).replace(",", ""))
        suffix = (m.group(3) or "").lower()
        if suffix == "k":
            value *= 1_000
        elif suffix == "m":
            value *= 1_000_000
        if any(abs(value - n) < 0.5 for n in numbers):
            return m.group(0)
    return None


def omitted_leak(text: str, deal: dict[str, Any], omitted: list[str]) -> str | None:
    """The offending snippet if the text mentions a field the row says to omit, else None."""
    t = deal["term_months"]
    patterns = {
        "term_months": rf"\b{t}[- ]month|\b{t} months|\b{t // 12}[- ]year|\b{t}mo\b",
        "payment_terms": r"\bnet[- ]?\d{2}\b|prepaid|in advance|up ?front",
        "segment": r"\bSMB\b|mid[- ]market|\benterprise\b|public[- ]sector",
    }
    for field in omitted:
        if (m := re.search(patterns[field], text, re.IGNORECASE)) is not None:
            return f"{field}: {m.group(0)!r}"
    return None


# ---- output schema ---------------------------------------------------------------------------


class Label(BaseModel):
    required: list[str]
    unresolved: list[str]
    flags: list[str]
    matched_rule_ids: list[str]
    gate_role: str | None
    policy_version: str


class Deal(BaseModel):
    account_name: str
    segment: str
    list_total_usd: int = Field(gt=0)
    net_total_usd: float = Field(ge=0)
    acv_usd: float = Field(ge=0)
    discount_percent: int = Field(ge=0, le=100)
    term_months: int = Field(gt=0)
    payment_terms: str
    terms: dict[str, bool]
    stated_unit: str
    value_basis: str
    stated_amount_usd: float
    stated_amount_text: str
    monthly: bool
    per_unit: dict[str, Any] | None
    requester: dict[str, str]


class ScenarioRow(BaseModel):
    id: str
    factors: dict[str, str]
    deal: Deal
    omitted: list[str]
    label: Label
    surface_text: str = Field(min_length=20)
    surface_source: str
    persona: str = Field(min_length=50)
    verified_by: str | None = None
    verified_at: str | None = None


# ---- factors and covering array ------------------------------------------------------------


def load_factors(path: Path = FACTORS_PATH) -> dict[str, list[str]]:
    data = yaml.safe_load(Path(path).read_text())
    return {str(k): [str(v) for v in vals] for k, vals in data["factors"].items()}


def covering_array(factors: dict[str, list[str]]) -> list[dict[str, str]]:
    """Pairwise covering array over the factors, in the YAML's factor order. allpairspy is a
    deterministic greedy algorithm, so the same factors give the same rows every time."""
    names = list(factors)
    return [dict(zip(names, row, strict=True)) for row in AllPairs([factors[n] for n in names])]


def load_accounts(path: Path = ACCOUNTS_PATH) -> list[dict[str, Any]]:
    return [dict(a) for a in yaml.safe_load(Path(path).read_text())]


# ---- one row -> one deal ---------------------------------------------------------------------


def money(x: float) -> str:
    return f"${x:,.0f}"


def _stated(unit: str, list_total: int, net: float, acv: float, monthly: bool) -> tuple[float, str, dict | None]:
    """(the number the AE says, how they say it, per-unit detail) for the stated unit."""
    per_unit: dict[str, Any] | None = None
    if unit == "list_total":
        if monthly:
            return list_total / 12, f"{money(list_total / 12)} a month at list", None
        return float(list_total), f"{money(list_total)} at list for the whole contract", None
    if unit == "net_total":
        if monthly:
            return net / 12, f"{money(net / 12)} a month after the discount", None
        return net, f"{money(net)} net over the full term, after the discount", None
    if unit == "acv":
        if monthly:
            return acv / 12, f"{money(acv / 12)} a month net, so {money(acv)} for the year after the discount", None
        return acv, f"an annual value of {money(acv)} after the discount", None
    if unit == "per_unit_x_volume":
        if monthly:
            vol = round(list_total / 12 / AGENT_MINUTE_USD)
            per_unit = {"sku": "agent_minute", "unit_price_usd": AGENT_MINUTE_USD, "volume": vol, "per_month": True}
            return list_total / 12, f"{vol:,} agent minutes a month at ${AGENT_MINUTE_USD:.2f} a minute list", per_unit
        vol = round(list_total / AGENT_MINUTE_USD)
        per_unit = {"sku": "agent_minute", "unit_price_usd": AGENT_MINUTE_USD, "volume": vol, "per_month": False}
        return float(list_total), f"{vol:,} agent minutes over the term at ${AGENT_MINUTE_USD:.2f} a minute list", per_unit
    raise ValueError(f"unknown stated_unit {unit!r}")


def _omitted(completeness: str, rng: random.Random) -> list[str]:
    if completeness == "complete":
        return []
    k = {"missing_1": 1, "missing_2": 2}[completeness]
    chosen = set(rng.sample(OMITTABLE, k))
    return [f for f in OMITTABLE if f in chosen]


def materialise(factors: dict[str, str], rng: random.Random, accounts: list[dict[str, Any]]) -> tuple[dict, list[str]]:
    """Concrete deal for one factor row. Every number the AE could say is derived from one
    list total, one discount and one term, with the same arithmetic the service uses."""
    lo, hi = BAND_RANGES[factors["discount_band"]]
    discount = rng.randint(lo, hi)
    term, pay, monthly = TERM_SHAPES[factors["term_shape"]]
    segment = factors["segment"]
    candidates = [a for a in accounts if a["segment"] == segment] or accounts
    account = rng.choice(candidates)["name"]
    list_total = rng.choice(LIST_TOTAL_MENU)
    _, net_cents, acv_cents = derive_amounts(float(list_total), "list_total", float(discount), term)
    net, acv = net_cents / 100, acv_cents / 100

    terms = {k: False for k in TERM_FIELDS}
    if (field := CLASS_FIELD[factors["nonstandard_class"]]) is not None:
        terms[field] = True
    band = factors["discount_band"]
    if band == "dd_15_25":
        renewal, competitive = True, True  # a clean Deal Desk band row, nothing contested
    elif band == "contested_25_30":
        renewal, competitive = rng.choice([(False, True), (True, False), (False, False)])  # R10 must fire
    else:
        renewal, competitive = rng.random() < 0.5, rng.random() < 0.4
    terms["is_renewal"], terms["is_competitive"] = renewal, competitive

    unit = factors["stated_unit"]
    stated_amount, stated_text, per_unit = _stated(unit, list_total, net, acv, monthly)
    omitted = _omitted(factors["completeness"], rng)
    deal = {
        "account_name": account,
        "segment": segment,
        "list_total_usd": list_total,
        "net_total_usd": net,
        "acv_usd": acv,
        "discount_percent": discount,
        "term_months": term,
        "payment_terms": pay,
        "terms": terms,
        "stated_unit": unit,
        "value_basis": UNIT_BASIS[unit],
        "stated_amount_usd": stated_amount,
        "stated_amount_text": stated_text,
        "monthly": monthly,
        "per_unit": per_unit,
        "requester": dict(REQUESTER),
    }
    return deal, omitted


def facts_for(deal: dict[str, Any]) -> Facts:
    return Facts(discount_bps=round(deal["discount_percent"] * 100), term_months=int(deal["term_months"]),
                 payment_terms=deal["payment_terms"], segment=deal["segment"],
                 net_total_cents=round(deal["net_total_usd"] * 100), **deal["terms"])


def label_for(deal: dict[str, Any], policy: Policy) -> dict[str, Any]:
    """The answer, computed by the policy engine and nothing else."""
    d = evaluate(facts_for(deal), policy)
    return {"required": list(d["required"]), "unresolved": list(d["unresolved"]), "flags": list(d["flags"]),
            "matched_rule_ids": list(d["matched_rule_ids"]), "gate_role": d["gate_role"],
            "policy_version": d["policy_version"]}


# ---- surface text ----------------------------------------------------------------------------


def _rc_clause(terms: dict[str, bool], rng: random.Random, competitor: str) -> str:
    r, c = terms["is_renewal"], terms["is_competitive"]
    if r and c:
        return rng.choice([f"It is a competitive renewal, {competitor} is bidding.",
                           f"Renewal, and {competitor} is in the running."])
    if r:
        return rng.choice(["It is a straight renewal, no competitor in the mix.", "Renewal, nobody else is bidding."])
    if c:
        return rng.choice([f"New logo, and {competitor} is in the running.",
                           f"This is new business and {competitor} has a quote in."])
    return rng.choice(["New logo, no competitor in play.", "Net new, nobody else is bidding."])


def _term_pay(deal: dict[str, Any], omitted: list[str], rng: random.Random) -> str:
    parts: list[str] = []
    if "term_months" not in omitted:
        t = deal["term_months"]
        parts.append("for 12 months" if deal["monthly"] else rng.choice([f"over {t} months", f"on a {t}-month term"]))
    if "payment_terms" not in omitted:
        parts.append(PAYMENT_PHRASES.get(deal["payment_terms"], PAYMENT_WORDS[deal["payment_terms"]]))
    if len(parts) == 2:
        return f" {parts[0]}, {parts[1]}"
    if "term_months" not in omitted:
        return f" {parts[0]}"
    if parts:
        return f", {parts[0]}"
    return ""


def template_text(deal: dict[str, Any], omitted: list[str], rng: random.Random) -> str:
    """Deterministic AE message. First person, stated unit kept, omitted fields absent, no
    `key: value` anywhere. Used offline and as the fallback when OpenAI keeps leaking."""
    acct = deal["account_name"]
    seg = "" if "segment" in omitted else f", {SEGMENT_WORDS[deal['segment']]}"
    amount = deal["stated_amount_text"]
    d = deal["discount_percent"]
    disc = rng.choice([f"{d}% off list", f"a {d} percent discount", f"{d}% off"])
    term_pay = _term_pay(deal, omitted, rng)
    cls = next((k for k, v in CLASS_FIELD.items() if v and deal["terms"].get(v)), "none")
    nonstd = rng.choice(NONSTANDARD_CLAUSES[cls])
    rc = _rc_clause(deal["terms"], rng, rng.choice(COMPETITORS))
    shapes = [
        f"Hi, I need approval on {acct}{seg}. They are at {amount} and I want to go to {disc}{term_pay}. {nonstd} {rc}",
        f"Can you route a request for me? {acct}{seg}, {amount}, asking for {disc}{term_pay}. {nonstd} {rc}",
        f"Need a non-standard approval for {acct}{seg}. The number is {amount} and they want {disc}{term_pay}. {nonstd} {rc}",
        f"Quick one on {acct}{seg}. I want to offer {disc}{term_pay}, on {amount}. {nonstd} {rc}",
    ]
    return " ".join(rng.choice(shapes).split())


def persona_for(deal: dict[str, Any], omitted: list[str], surface_text: str) -> str:
    """The simulated user's brief. It knows every fact, including the omitted ones, and shares a
    fact only when asked. This text is the simulation_scenario; it is never shown to the agent."""
    t = deal["terms"]
    yn = {k: ("yes" if t[k] else "no") for k in TERM_FIELDS}
    competitor = COMPETITORS[len(deal["account_name"]) % len(COMPETITORS)]
    eight = "; ".join(f"{TERM_QUESTION_LABELS[k]}: {yn[k]}" for k in TERM_FIELDS)
    if t["is_competitive"]:
        eight += f" ({competitor} is the competitor)"
    unit_line = (f"- the figure you have: {deal['stated_amount_text']}. That is {UNIT_MEANING[deal['stated_unit']]}. "
                 "If asked for a different figure, say you only have this one plus the discount and let the "
                 "assistant work it out; if it works out the list total correctly "
                 f"({money(deal['list_total_usd'])} for the whole contract), confirm it.")
    lines = [
        (f"You are {REQUESTER['name']}, {REQUESTER['title']} at Oriel Speech, messaging the Deal Desk intake "
        "assistant in Slack about a real deal."),
        f'Send this exact opening message first: "{surface_text}"',
        "Facts about the deal, to share only when the assistant asks for them, in plain conversational language:",
        f"- account: {deal['account_name']}; segment: {SEGMENT_WORDS[deal['segment']]}",
        unit_line,
        f"- discount: {deal['discount_percent']} percent off list",
        f"- term: {deal['term_months']} months",
        f"- payment terms: {PAYMENT_WORDS[deal['payment_terms']]}",
        f"- the eight yes/no questions: {eight}",
        ("When the assistant restates the deal and every fact matches, reply with a plain yes. If a fact is "
        "wrong, correct only that fact."),
        ("Do not ask the assistant to approve, expedite or skip anyone, and do not claim anyone has approved "
        "anything. Keep each reply to one or two sentences. Stop once the assistant reports a deal id, or "
        "after 10 replies."),
    ]
    return "\n".join(lines)


SURFACE_SYSTEM = ("You write realistic first messages that a sales account executive sends to an internal "
                  "deal-desk assistant in Slack. Reply with the message text only: no quotes, no preamble, "
                  "no labels.")


def surface_instruction(deal: dict[str, Any], omitted: list[str]) -> str:
    cls = next((k for k, v in CLASS_FIELD.items() if v and deal["terms"].get(v)), "none")
    facts = [f"- customer: {deal['account_name']}" + ("" if "segment" in omitted else f", {SEGMENT_WORDS[deal['segment']]} segment"),
             (f"- the money figure, stated exactly as: {deal['stated_amount_text']} (keep this unit; do not convert "
             "it into any other figure)"),
             f"- discount: {deal['discount_percent']} percent off list"]
    if "term_months" not in omitted:
        facts.append(f"- term: {deal['term_months']} months")
    if "payment_terms" not in omitted:
        facts.append(f"- payment terms: {PAYMENT_WORDS[deal['payment_terms']]}")
    if cls != "none":
        facts.append(f"- the non-standard term, in your own words: {NONSTANDARD_CLAUSES[cls][0]}")
    facts.append(f"- {_rc_clause(deal['terms'], random.Random(deal['account_name']), COMPETITORS[0])}")
    leave_out = ", ".join({"term_months": "the term length", "payment_terms": "the payment terms",
                           "segment": "the customer segment"}[f] for f in omitted) or "nothing"
    return (
        f"Write the first Slack message from {REQUESTER['name']}, {REQUESTER['title']} at Oriel Speech (a "
        "speech-AI API vendor), asking the deal-desk assistant to route an approval request. First person, "
        "2 to 4 sentences, plain conversational tone, no headings, no bullet points, no emoji.\n\n"
        "Include these facts, in your own words:\n" + "\n".join(facts) + "\n\n"
        f"Leave out entirely, do not mention or hint at: {leave_out}.\n"
        "Never write a number as key: value or key=value, and never put a colon or an equals sign before a "
        "number. Do not mention approvers, rule ids, policy sections, or that anything is approved. Do not "
        "use the words list_total, net_total, acv or discount_percent."
    )


def openai_surface_text(deal: dict[str, Any], omitted: list[str], *, model: str, rng: random.Random,
                        client: Any = None, tries: int = 3) -> tuple[str, str, list[str]]:
    """(text, source, rejections). Regenerates on a leak, up to `tries`; falls back to the template."""
    if client is None:
        from openai import OpenAI  # imported here so the module never needs a key
        client = OpenAI(api_key=load_env().get("OPENAI_API_KEY"))
    numbers = deal_numbers(deal)
    rejections: list[str] = []
    for attempt in range(1, tries + 1):
        resp = client.chat.completions.create(
            model=model, temperature=0.9, seed=rng.randint(1, 2**31 - 1),
            messages=[{"role": "system", "content": SURFACE_SYSTEM},
                      {"role": "user", "content": surface_instruction(deal, omitted)}],
        )
        text = " ".join((resp.choices[0].message.content or "").strip().strip('"').split())
        problem = structured_leak(text, numbers) or omitted_leak(text, deal, omitted)
        if problem is None and len(text) >= 20:
            return text, "openai", rejections
        rejections.append(f"try {attempt}: {problem or 'empty response'}")
    return template_text(deal, omitted, rng), "template_fallback", rejections


# ---- build, split, write -----------------------------------------------------------------------


def build_rows(*, seed: int = DEFAULT_SEED, dry_run: bool = True, model: str = DEFAULT_MODEL,
               factors: dict[str, list[str]] | None = None, accounts: list[dict[str, Any]] | None = None,
               policy: Policy | None = None, client: Any = None, verbose: bool = False) -> list[dict[str, Any]]:
    factors = factors or load_factors()
    accounts = accounts or load_accounts()
    policy = policy or load_policy(POLICY_PATH)
    rows: list[dict[str, Any]] = []
    for i, f in enumerate(covering_array(factors), start=1):
        row_rng = random.Random(f"{seed}:{i}")  # per-row stream: one row's draws never shift another's
        deal, omitted = materialise(f, row_rng, accounts)
        label = label_for(deal, policy)
        if dry_run:
            text, source = template_text(deal, omitted, row_rng), "template"
        else:
            text, source, rejections = openai_surface_text(deal, omitted, model=model, rng=row_rng, client=client)
            if rejections and verbose:
                print(f"  row-{i:02d}: {'; '.join(rejections)} -> {source}", file=sys.stderr)
        row = {
            "id": f"row-{i:02d}",
            "factors": dict(f),
            "deal": deal,
            "omitted": omitted,
            "label": label,
            "surface_text": text,
            "surface_source": source,
            "persona": persona_for(deal, omitted, text),
        }
        ScenarioRow.model_validate(row)
        rows.append(row)
    return rows


def split_heldout(rows: list[dict[str, Any]], n: int, seed: int) -> tuple[list[dict], list[dict]]:
    """Deterministic in (seed, n) only: the pick uses its own RNG stream."""
    if n <= 0:
        return list(rows), []
    if n >= len(rows):
        raise ValueError(f"--heldout {n} would leave no regression rows (have {len(rows)})")
    picker = random.Random(f"heldout:{seed}")
    chosen = set(picker.sample(range(len(rows)), n))
    return ([r for i, r in enumerate(rows) if i not in chosen], [r for i, r in enumerate(rows) if i in chosen])


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_hash(heldout_path: Path, hash_path: Path) -> str:
    """sha256sum-compatible line, path relative to the hash file's directory, so
    `cd evals && sha256sum -c heldout.sha256` works."""
    digest = sha256_file(heldout_path)
    try:
        rel = heldout_path.resolve().relative_to(hash_path.resolve().parent)
    except ValueError:
        rel = heldout_path.resolve()
    hash_path.write_text(f"{digest}  {rel.as_posix()}\n")
    return digest


def read_hash(hash_path: Path) -> str | None:
    if not Path(hash_path).exists():
        return None
    first = Path(hash_path).read_text().strip().split()
    return first[0] if first else None


def _dump(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")


def write_outputs(rows: list[dict[str, Any]], *, heldout_n: int, seed: int, out_dir: Path = SCENARIOS_DIR,
                  hash_path: Path = HASH_PATH) -> dict[str, Any]:
    regression, heldout = split_heldout(rows, heldout_n, seed)
    _dump(regression, out_dir / "regression.json")
    summary: dict[str, Any] = {"rows": len(rows), "regression": len(regression), "heldout": len(heldout)}
    if heldout:
        _dump(heldout, out_dir / "heldout.json")
        summary["heldout_sha256"] = write_hash(out_dir / "heldout.json", hash_path)
    return summary


# ---- review ----------------------------------------------------------------------------------


def _load(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text()) if path.exists() else []


def review(*, out_dir: Path = SCENARIOS_DIR, hash_path: Path = HASH_PATH, verified_by: str | None = None) -> int:
    """Print surface text / expected required / unresolved for the one-pass hand check.
    With verified_by, stamp every row and re-freeze the held-out hash."""
    files = {"regression": out_dir / "regression.json", "heldout": out_dir / "heldout.json"}
    total = 0
    for suite, path in files.items():
        rows = _load(path)
        if not rows:
            continue
        print(f"\n=== {suite}: {len(rows)} rows ({path}) ===")
        for r in rows:
            f = r["factors"]
            fac = " | ".join(f[k] for k in f)
            print(f"\n{r['id']}  [{fac}]  omitted: {', '.join(r['omitted']) or 'none'}  source: {r['surface_source']}")
            print(f"  text:       {r['surface_text']}")
            print(f"  required:   {', '.join(r['label']['required']) or 'none (cleared)'}")
            print(f"  unresolved: {', '.join(r['label']['unresolved']) or 'none'}"
                  f"    flags: {', '.join(r['label']['flags']) or 'none'}")
            if r.get("verified_by"):
                print(f"  verified:   {r['verified_by']} on {r.get('verified_at')}")
            total += 1
        if verified_by:
            stamp = datetime.now(UTC).date().isoformat()
            for r in rows:
                r["verified_by"], r["verified_at"] = verified_by, stamp
            _dump(rows, path)
            print(f"\nstamped {len(rows)} {suite} rows: verified_by={verified_by!r} verified_at={stamp}")
            if suite == "heldout":
                print(f"re-froze {hash_path.name}: {write_hash(path, hash_path)}")
    if total == 0:
        print("no scenario files found; run generate.py first", file=sys.stderr)
        return 1
    return 0


# ---- CLI -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"RNG seed (default {DEFAULT_SEED})")
    ap.add_argument("--heldout", type=int, default=0, metavar="N",
                    help="move N rows to scenarios/heldout.json and freeze evals/heldout.sha256")
    ap.add_argument("--dry-run", action="store_true", help="template surface text, no network")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI chat model (default {DEFAULT_MODEL})")
    ap.add_argument("--out-dir", type=Path, default=SCENARIOS_DIR, help="where the scenario files go")
    ap.add_argument("--hash-path", type=Path, default=HASH_PATH, help="where heldout.sha256 goes")
    ap.add_argument("--review", action="store_true", help="print text / required / unresolved for a hand pass")
    ap.add_argument("--verified-by", metavar="NAME", help="with --review: stamp every row as verified")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.review:
        return review(out_dir=args.out_dir, hash_path=args.hash_path, verified_by=args.verified_by)
    if args.verified_by:
        ap.error("--verified-by only makes sense with --review")

    factors = load_factors()
    rows = build_rows(seed=args.seed, dry_run=args.dry_run, model=args.model, factors=factors, verbose=args.verbose)
    print(f"covering array: {len(rows)} rows, pairwise over {len(factors)} factors "
          f"({' x '.join(str(len(v)) for v in factors.values())} levels); seed {args.seed}; "
          f"surface text: {'template (dry run)' if args.dry_run else args.model}")
    summary = write_outputs(rows, heldout_n=args.heldout, seed=args.seed, out_dir=args.out_dir,
                            hash_path=args.hash_path)
    print(f"wrote {args.out_dir / 'regression.json'} ({summary['regression']} rows)")
    if summary["heldout"]:
        print(f"wrote {args.out_dir / 'heldout.json'} ({summary['heldout']} rows); "
              f"{args.hash_path} = {summary['heldout_sha256']}")
    sources = sorted({r["surface_source"] for r in rows})
    print(f"surface sources: {', '.join(sources)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
