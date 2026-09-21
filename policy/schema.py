"""Shared vocabulary for the policy engine, the service and the evals.

Everything the evaluator reasons about is in `Facts`. The LLM never produces a
Facts object directly; the service builds it from the tool payload after
merging LLM-extracted booleans with any human-confirmed values.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PaymentTerms(StrEnum):
    NET_30 = "net_30"
    NET_45 = "net_45"
    NET_60 = "net_60"
    NET_90 = "net_90"
    ANNUAL_PREPAID = "annual_prepaid"
    MULTI_YEAR_PREPAID = "multi_year_prepaid"
    OTHER = "other"


class Segment(StrEnum):
    SMB = "smb"
    MID_MARKET = "mid_market"
    ENTERPRISE = "enterprise"
    PUBLIC_SECTOR = "public_sector"


# Approver role keys. "ae" is the requester's own authority and is never a reviewer.
ROLE_KEYS: tuple[str, ...] = ("ae", "deal_desk", "finance", "legal", "product", "cro")
REVIEWER_ROLES: frozenset[str] = frozenset(ROLE_KEYS) - {"ae"}

# Boolean non-standard-term fields, in the order the agent asks about them.
TERM_FIELDS: tuple[str, ...] = (
    "termination_for_convenience",
    "nonstandard_legal_terms",
    "outcome_based_pricing",
    "implementation_arrangement",
    "license_fee_restructure",
    "claims_strategic_account",
    "is_renewal",
    "is_competitive",
)

# Names an approver set must never contain, at any layer.
FORBIDDEN_APPROVERS: frozenset[str] = frozenset({"ai", "agent", "bot", "system", "assistant", "llm"})


@dataclass(frozen=True)
class Facts:
    discount_bps: int
    term_months: int
    payment_terms: PaymentTerms
    segment: Segment
    net_total_cents: int
    termination_for_convenience: bool = False
    nonstandard_legal_terms: bool = False
    outcome_based_pricing: bool = False
    implementation_arrangement: bool = False
    license_fee_restructure: bool = False
    claims_strategic_account: bool = False
    is_renewal: bool = False
    is_competitive: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.discount_bps <= 10_000:
            raise ValueError(f"discount_bps out of range: {self.discount_bps}")
        if self.term_months <= 0:
            raise ValueError(f"term_months must be positive: {self.term_months}")
        if self.net_total_cents < 0:
            raise ValueError(f"net_total_cents must be non-negative: {self.net_total_cents}")
        object.__setattr__(self, "payment_terms", PaymentTerms(self.payment_terms))
        object.__setattr__(self, "segment", Segment(self.segment))
