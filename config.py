from dataclasses import dataclass
from typing import Literal

SignalType = Literal["positive", "negative", "neutral"]


@dataclass
class SignalConfig:
    name: str
    signal_type: SignalType
    weight: int
    description: str


# ── Weight design ──────────────────────────────────────────────────────────────
# Each category (positive / negative) sums to 100.
# Score range: -100 to +100.
#
# Positive signals (9 signals, sum = 100):
#   Weighted by how direct the signal is to active procurement.
#   RFP = most direct; tech stack fingerprint = least direct.
#
# Negative signals (5 signals, sum = 100):
#   Weighted by how severely they indicate budget unavailability.
#   Bankruptcy = near-certain block; no_job_openings = weak indicator.
#
# Neutral signals: informational only, no score impact.
# ──────────────────────────────────────────────────────────────────────────────

SIGNALS: list[SignalConfig] = [
    # ── Positive (sum = 100) ──────────────────────────────────────────────────
    SignalConfig("rfp_rfq_rfi",          "positive", 25,
                 "Active RFP/RFQ/RFI — company is literally evaluating vendors"),
    SignalConfig("erp_crm_migration",    "positive", 20,
                 "Actively implementing/migrating ERP, CRM, or cloud platform"),
    SignalConfig("leadership_change",    "positive", 15,
                 "C-suite/VP hire or departure — new exec typically re-evaluates vendors"),
    SignalConfig("ma_activity",          "positive", 12,
                 "M&A filing — creates consolidation needs and new spend cycles"),
    SignalConfig("operational_pain",     "positive", 10,
                 "Confirmed outage/breach/failure — creates urgency for better tooling"),
    SignalConfig("hiring_activity",      "positive",  8,
                 "Active job postings in tech/ops roles — investment signal"),
    SignalConfig("geographic_expansion", "positive",  5,
                 "Opening new offices/markets — scaling operations"),
    SignalConfig("tech_stack_change",    "positive",  3,
                 "Tech stack fingerprint suggests active modernization"),
    SignalConfig("ipo_preparation",      "positive",  2,
                 "S-1 filing — companies invest in scalable systems pre-IPO"),

    # ── Negative (sum = 100) ─────────────────────────────────────────────────
    SignalConfig("bankruptcy",           "negative", 45,
                 "Bankruptcy filing (Item 1.03) — near-certain disqualifier"),
    SignalConfig("layoffs",              "negative", 25,
                 "Confirmed layoffs — strong budget pressure signal"),
    SignalConfig("budget_cuts",          "negative", 15,
                 "Budget cuts / cost reduction initiatives in news"),
    SignalConfig("facility_closures",    "negative", 10,
                 "Office/facility closures — contraction or operational downsizing"),
    SignalConfig("no_job_openings",      "negative",  5,
                 "No job openings found — suggests limited growth investment"),

    # ── Neutral (informational only, no score impact) ────────────────────────
    SignalConfig("reorganization",       "neutral",  55,
                 "Restructuring/reorg SEC filing (Item 2.05) — could go either way"),
    SignalConfig("internal_promotion",   "neutral",  45,
                 "Internal promotion — could signal stability or strategic shift"),
    SignalConfig("reorganization_news",  "neutral",   0,
                 "Supplemental news reorg signal (deduped with SEC EDGAR)"),
]

SIGNAL_MAP: dict[str, SignalConfig] = {s.name: s for s in SIGNALS}

# Score thresholds (range -100 to +100)
STRONG_BUYER_THRESHOLD  =  30
WEAK_BUYER_THRESHOLD    =  10
POOR_FIT_THRESHOLD      = -10
HARD_DISQUALIFY_SCORE   = -50
BANKRUPTCY_SIGNAL       = "bankruptcy"
