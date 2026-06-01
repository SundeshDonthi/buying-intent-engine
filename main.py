#!/usr/bin/env python3
"""B2B Buying Intent Engine — MVP CLI."""

import argparse
import json
import sys

from scoring import score_signals
from signals import (
    JobsCollector,
    LayoffsCollector,
    NewsCollector,
    SecEdgarCollector,
    TechStackCollector,
)


def run(company_name: str, domain: str | None, output_json: bool = False) -> None:
    inferred_domain = domain or f"https://www.{company_name.lower().replace(' ', '')}.com"

    print(f"\nAnalyzing buying intent for: {company_name}")
    print(f"Domain: {inferred_domain}")
    print("Collecting signals...\n")

    all_signals = []

    collectors = [
        ("SEC EDGAR (bankruptcy, M&A, IPO, reorg)", lambda: SecEdgarCollector().collect(company_name)),
        ("News (leadership, migration, pain signals)", lambda: NewsCollector().collect(company_name)),
        ("Jobs (hiring activity)", lambda: JobsCollector().collect(company_name)),
        ("Layoffs (Layoffs.fyi + news)", lambda: LayoffsCollector().collect(company_name)),
        ("Tech Stack (HTTP fingerprint)", lambda: TechStackCollector().collect(company_name, inferred_domain)),
    ]

    for label, collector_fn in collectors:
        print(f"  → {label}...")
        try:
            signals = collector_fn()
            all_signals.extend(signals)
        except Exception as e:
            print(f"    [ERROR] {e}")

    report = score_signals(company_name, inferred_domain, all_signals)

    if output_json:
        data = {
            "company": report.company_name,
            "domain": report.domain,
            "total_score": report.total_score,
            "intent_level": report.intent_level,
            "positive_score": report.positive_score,
            "negative_score": report.negative_score,
            "neutral_score": report.neutral_score,
            "signals": [
                {
                    "name": s.signal_name,
                    "type": s.signal_type,
                    "found": s.found,
                    "score_contribution": s.score_contribution,
                    "detail": s.detail,
                }
                for s in report.signal_results
            ],
        }
        print(json.dumps(data, indent=2))
    else:
        print(report.summary())


def main():
    parser = argparse.ArgumentParser(
        description="B2B Buying Intent Engine — analyze a company's buying signals"
    )
    parser.add_argument("company", help="Company name to analyze (e.g. 'Acme Corp')")
    parser.add_argument("--domain", help="Company website URL (e.g. https://www.acme.com)", default=None)
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    run(args.company, args.domain, output_json=args.json)


if __name__ == "__main__":
    main()
