"""Scoring engine — combines signals into a buying intent score."""

from dataclasses import dataclass, field

from config import (
    SIGNAL_MAP,
    STRONG_BUYER_THRESHOLD,
    WEAK_BUYER_THRESHOLD,
    POOR_FIT_THRESHOLD,
    HARD_DISQUALIFY_SCORE,
    BANKRUPTCY_SIGNAL,
)

IntentLevel = str  # "Strong Buyer" | "Potential Buyer" | "Neutral" | "Poor Fit" | "Disqualified"


@dataclass
class SignalResult:
    signal_name: str
    signal_type: str
    weight: int
    found: bool
    detail: str
    score_contribution: int
    evidence: list[dict] = field(default_factory=list)  # [{"title": str, "url": str|None}]


@dataclass
class IntentReport:
    company_name: str
    domain: str
    total_score: int
    intent_level: IntentLevel
    signal_results: list[SignalResult] = field(default_factory=list)
    positive_score: int = 0
    negative_score: int = 0
    neutral_score: int = 0

    def summary(self) -> str:
        lines = [
            f"\n{'='*60}",
            f"  BUYING INTENT REPORT: {self.company_name}",
            f"{'='*60}",
            f"  Domain:        {self.domain}",
            f"  Intent Level:  {self.intent_level}",
            f"  Total Score:   {self.total_score:+d}",
            f"  Positive: +{self.positive_score}  |  Negative: {self.negative_score}  |  Neutral noted: {self.neutral_score}",
            f"{'='*60}",
            "",
            "  SIGNAL BREAKDOWN:",
        ]
        for r in sorted(self.signal_results, key=lambda x: abs(x.score_contribution), reverse=True):
            status = "✓" if r.found else "✗"
            contrib = f"{r.score_contribution:+d}" if r.found else " —"
            lines.append(f"  [{status}] {r.signal_name:<30} {contrib:>5}   {r.detail}")
        lines.append(f"\n{'='*60}\n")
        return "\n".join(lines)


def _intent_level(total_score: int, found_signal_names: set[str]) -> IntentLevel:
    # Hard disqualify: bankruptcy is present regardless of other positive signals
    if BANKRUPTCY_SIGNAL in found_signal_names:
        return "Disqualified"
    # Score-based disqualify
    if total_score <= HARD_DISQUALIFY_SCORE:
        return "Disqualified"
    if total_score >= STRONG_BUYER_THRESHOLD:
        return "Strong Buyer"
    if total_score >= WEAK_BUYER_THRESHOLD:
        return "Potential Buyer"
    if total_score <= POOR_FIT_THRESHOLD:
        return "Poor Fit"
    return "Neutral"


def _extract_evidence(raw) -> list[dict]:
    """Pull evidence items from any signal dataclass that has an evidence-like field."""
    # Try standardized evidence field first
    if hasattr(raw, "evidence") and raw.evidence:
        return raw.evidence[:5]
    # Fallback: articles field (news/layoffs)
    if hasattr(raw, "articles") and raw.articles:
        return [{"title": a.get("title", ""), "url": a.get("link", "")} for a in raw.articles[:5]]
    # Fallback: sources field (layoffs)
    if hasattr(raw, "sources") and raw.sources:
        return [{"title": str(s.get("title", s.get("row", ""))), "url": ""} for s in raw.sources[:5]]
    # Fallback: filings (SEC)
    if hasattr(raw, "filings") and raw.filings:
        return [{"title": f.get("title", ""), "url": f.get("link", "")} for f in raw.filings[:5]]
    # Fallback: jobs
    if hasattr(raw, "jobs") and raw.jobs:
        return [{"title": j.get("title", j.get("title", "")), "url": j.get("link", "")} for j in raw.jobs[:5]]
    # Tech stack: detected tech list
    if hasattr(raw, "detected_tech") and raw.detected_tech:
        return [{"title": t, "url": ""} for t in raw.detected_tech]
    return []


def score_signals(
    company_name: str,
    domain: str,
    raw_signals: list,
) -> IntentReport:
    signal_results: list[SignalResult] = []
    total = 0
    pos_total = 0
    neg_total = 0
    neutral_total = 0
    found_names: set[str] = set()

    for raw in raw_signals:
        name = raw.signal_name
        cfg = SIGNAL_MAP.get(name)
        if cfg is None:
            continue

        contribution = 0
        if raw.found:
            found_names.add(name)
            if cfg.signal_type == "positive":
                contribution = cfg.weight
                pos_total += contribution
            elif cfg.signal_type == "negative":
                contribution = -cfg.weight
                neg_total += contribution
            elif cfg.signal_type == "neutral":
                neutral_total += cfg.weight

        total += contribution
        signal_results.append(SignalResult(
            signal_name=name,
            signal_type=cfg.signal_type,
            weight=cfg.weight,
            found=raw.found,
            detail=raw.detail,
            score_contribution=contribution,
            evidence=_extract_evidence(raw),
        ))

    return IntentReport(
        company_name=company_name,
        domain=domain,
        total_score=total,
        intent_level=_intent_level(total, found_names),
        signal_results=signal_results,
        positive_score=pos_total,
        negative_score=neg_total,
        neutral_score=neutral_total,
    )
