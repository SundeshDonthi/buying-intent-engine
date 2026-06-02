"""News signal collector — SerpAPI primary, Google News RSS fallback."""

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .utils import company_in_text

GNEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# signal_name -> (search_query_template, title_keywords, body_keywords_optional)
#
# Keyword design rationale:
# - erp_crm_migration: require active CHANGE verbs (implementing, migrating, deploying)
#   to avoid matching partnership/integration press releases
# - operational_pain: require hard failure language (outage, breach, failure)
#   to avoid matching every article that mentions "challenges"
# - budget_cuts / facility_closures: new negative signals from the original signal list
SIGNAL_QUERIES: dict[str, tuple[str, list[str]]] = {
    # ── Positive ──────────────────────────────────────────────────────────────
    "leadership_change": (
        '"{company}" CEO OR CTO OR CFO OR "chief" hired OR appointed OR departed OR resigned OR "steps down" OR "names new"',
        # Require subject to be the company's own executive (not "former X exec joins Y")
        # Exclude patterns like "former {company}" or "ex-{company}" which indicate alumni, not current change
        ["appointed", "appoints", "hires", "hired", "departed", "departs", "resign", "steps down",
         "names new", "new ceo", "new cto", "new cfo", "new chief"],
    ),
    "erp_crm_migration": (
        '"{company}" implementing OR migrating OR deploying OR "rolling out" OR "digital transformation" '
        'OR "modernization" OR "new ERP" OR "new CRM" OR "system upgrade" OR "platform migration"',
        # Require active change verbs — not just a mention of the technology
        ["implement", "migrat", "deploy", "rolling out", "digital transformation",
         "moderniz", "new erp", "new crm", "system upgrade", "platform migration",
         "replacing", "overhaul", "transition"],
    ),
    "operational_pain": (
        '"{company}" outage OR downtime OR "data breach" OR "system failure" OR "service disruption" '
        'OR "security incident" OR hack OR "system down"',
        # Hard failure signals only — not vague "challenges"
        ["outage", "downtime", "data breach", "breach", "hack", "system failure",
         "service disruption", "security incident", "system down", "offline", "cyberattack"],
    ),
    "geographic_expansion": (
        '"{company}" "new office" OR "new headquarters" OR "expanding to" OR "opens office" '
        'OR "new location" OR "entering" OR "new market"',
        ["new office", "new headquarter", "expanding to", "opens office", "new location",
         "entering", "new market", "new region"],
    ),
    # ── Negative ──────────────────────────────────────────────────────────────
    "budget_cuts": (
        '"{company}" "budget cut" OR "cost cutting" OR "cost reduction" OR "spending cuts" '
        'OR "cut spending" OR "austerity" OR "tighten budget"',
        # Require direct budget-cut language — exclude partnership articles mentioning "reduce costs" generically
        ["budget cut", "cost cut", "cost reduc", "spending cut", "cut spending",
         "austerity", "tighten budget", "reduce headcount", "slash budget"],
    ),
    "facility_closures": (
        '"{company}" "closing office" OR "shut down" OR "closes office" OR "facility closure" '
        'OR "office closure" OR "plant closure" OR "shutting down"',
        ["clos", "shut down", "shutting", "facility closure", "office closure",
         "plant closure", "vacating", "consolidat"],
    ),
    # ── Neutral ───────────────────────────────────────────────────────────────
    "internal_promotion": (
        '"{company}" promoted OR "new role" OR "new position" OR "named" internal',
        ["promot", "new role", "new position"],
    ),
    "reorganization_news": (
        '"{company}" restructuring OR reorganization OR reorg OR "cost cutting" OR "cost reduction"',
        ["restructur", "reorganiz", "reorg"],
    ),
}


@dataclass
class NewsSignal:
    signal_name: str
    found: bool
    detail: str = ""
    articles: list[dict] = field(default_factory=list)


def _fetch_serpapi(query: str, max_results: int = 15) -> list[dict]:
    """Fetch news via SerpAPI Google News — works from any server IP."""
    api_key = os.getenv("SERPAPI_KEY", "")
    if not api_key:
        return []
    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={"engine": "google_news", "q": query, "api_key": api_key, "num": max_results},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("news_results", [])[:max_results]
        return [
            {
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "pubDate": item.get("date", ""),
                "source": item.get("source", {}).get("name", "") if isinstance(item.get("source"), dict) else str(item.get("source", "")),
            }
            for item in items
        ]
    except Exception:
        return []


def _fetch_rss(query: str, max_results: int = 15) -> list[dict]:
    """Fetch news via Google News RSS — free but may be blocked on cloud servers."""
    url = GNEWS_RSS.format(query=quote(query))
    try:
        resp = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")[:max_results]
        results = [
            {
                "title": item.find("title").text if item.find("title") else "",
                "link": item.find("link").text if item.find("link") else "",
                "pubDate": item.find("pubDate").text if item.find("pubDate") else "",
                "source": item.find("source").text if item.find("source") else "",
            }
            for item in items
        ]
        return results
    except Exception:
        return []


def _fetch_news(query: str, max_results: int = 15) -> list[dict]:
    """Try SerpAPI first (reliable on servers), fall back to Google News RSS."""
    results = _fetch_serpapi(query, max_results)
    if results:
        return results
    return _fetch_rss(query, max_results)


_ALUMNI_RE = re.compile(r'\b(former|ex[-\s]|alumni|alumnus)\b', re.IGNORECASE)


def _filter_articles(company_name: str, articles: list[dict], keywords: list[str],
                     exclude_alumni: bool = False) -> list[dict]:
    """Keep articles where (1) the company name appears AND (2) a signal keyword appears."""
    matched = []
    for article in articles:
        title = article["title"]
        title_lower = title.lower()
        if not company_in_text(company_name, title):
            continue
        # For leadership signals, skip articles that reference alumni of the company
        # e.g. "Former Stripe CTO joins Anthropic" — Stripe is not changing leadership
        if exclude_alumni and _ALUMNI_RE.search(title):
            continue
        if any(kw in title_lower for kw in keywords):
            matched.append(article)
    return matched


class NewsCollector:
    def collect(self, company_name: str) -> list[NewsSignal]:
        signals = list(SIGNAL_QUERIES.items())

        def _fetch_one(item):
            signal_name, (query_template, keywords) = item
            query = query_template.replace("{company}", company_name)
            articles = _fetch_news(query)
            exclude_alumni = signal_name == "leadership_change"
            matched = _filter_articles(company_name, articles, keywords, exclude_alumni=exclude_alumni)
            found = bool(matched)
            return signal_name, NewsSignal(
                signal_name=signal_name,
                found=found,
                detail=f"{len(matched)} relevant article(s) found" if found else "No confirmed news found",
                articles=matched[:5],
            )

        # Fetch all 8 signals in parallel — reduces wall time from ~80s worst case to ~6s
        results_map: dict[str, NewsSignal] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_one, item): item for item in signals}
            for future in as_completed(futures):
                try:
                    signal_name, result = future.result()
                    results_map[signal_name] = result
                except Exception:
                    pass

        # Return in original order
        return [results_map[name] for name, _ in signals if name in results_map]
