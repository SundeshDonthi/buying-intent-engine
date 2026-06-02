"""FastAPI server for the Buying Intent Engine web app."""

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncGenerator

# Load .env before anything else
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import re
from urllib.parse import urlparse, urlunparse

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scoring import score_signals
from signals import (
    JobsCollector,
    LayoffsCollector,
    NewsCollector,
    SecEdgarCollector,
    TechStackCollector,
)

app = FastAPI(title="Buying Intent Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")


class AnalyzeRequest(BaseModel):
    company: str
    domain: str | None = None


COLLECTORS = [
    ("sec_edgar",  "SEC EDGAR filings",              lambda c, d: SecEdgarCollector().collect(c)),
    ("news",       "News signals",                   lambda c, d: NewsCollector().collect(c)),
    ("jobs",       "Job postings",                   lambda c, d: JobsCollector().collect(c)),
    ("layoffs",    "Layoffs.fyi + news",             lambda c, d: LayoffsCollector().collect(c)),
    ("tech_stack", "Tech stack fingerprint",         lambda c, d: TechStackCollector().collect(c, d)),
]

# Which signals each collector produces — used to skip collectors when only specific signals are requested
COLLECTOR_SIGNALS: dict[str, set[str]] = {
    "sec_edgar":  {"ipo_preparation", "ma_activity", "bankruptcy", "reorganization"},
    "news":       {"leadership_change", "erp_crm_migration", "operational_pain", "geographic_expansion",
                   "budget_cuts", "facility_closures", "internal_promotion", "reorganization_news"},
    "jobs":       {"hiring_activity", "no_job_openings"},
    "layoffs":    {"layoffs"},
    "tech_stack": {"tech_stack_change"},
}


def _validate_company(company: str, domain: str) -> bool:
    """Return True if the company name can be verified against its homepage."""
    try:
        resp = requests.get(
            domain, timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BuyingIntentBot/1.0)"},
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return False
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text[:80_000], "lxml")
        # Check title + meta description + og:title + og:site_name
        candidates = []
        if soup.title and soup.title.string:
            candidates.append(soup.title.string)
        for attr in ("description", "og:title", "og:site_name", "application-name"):
            tag = soup.find("meta", attrs={"property": attr}) or soup.find("meta", attrs={"name": attr})
            if tag and tag.get("content"):
                candidates.append(tag["content"])
        text = " ".join(candidates).lower()

        # Build match tokens from company name (strip common suffixes)
        name_clean = re.sub(r"\b(inc|corp|llc|ltd|co|plc|group|holdings|international|technologies|technology|solutions)\b\.?", "", company, flags=re.IGNORECASE).strip()
        tokens = [t for t in re.split(r"\W+", name_clean) if len(t) > 2]
        if not tokens:
            tokens = [re.split(r"\W+", company)[0]]

        # At least one meaningful token must appear in the page metadata
        return any(tok.lower() in text for tok in tokens)
    except Exception:
        # Can't reach the site — let it through rather than block on network issues
        return True


async def _stream_analysis(company: str, domain: str, selected_signals: set[str] | None = None) -> AsyncGenerator[str, None]:
    def send(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    # ── Validate company against domain before running any collectors ──────
    valid = await asyncio.to_thread(_validate_company, company, domain)
    if not valid:
        yield send("error_event", {"code": "company_not_found", "company": company, "domain": domain})
        return

    # Filter collectors to only those needed for the selected signals
    active_collectors = [
        (key, label, fn) for key, label, fn in COLLECTORS
        if selected_signals is None or COLLECTOR_SIGNALS.get(key, set()) & selected_signals
    ]

    yield send("start", {"company": company, "domain": domain, "total_steps": len(active_collectors)})

    all_signals = []
    for i, (key, label, collector_fn) in enumerate(active_collectors):
        yield send("progress", {"step": i + 1, "key": key, "label": label, "status": "running"})
        try:
            signals = await asyncio.to_thread(collector_fn, company, domain)
            all_signals.extend(signals)
            found_count = sum(1 for s in signals if s.found)
            yield send("progress", {
                "step": i + 1, "key": key, "label": label,
                "status": "done", "found": found_count, "total": len(signals),
            })
        except Exception as e:
            yield send("progress", {"step": i + 1, "key": key, "label": label, "status": "error", "error": str(e)})

    report = score_signals(company, domain, all_signals)

    signals_data = [
        {
            "name": s.signal_name,
            "type": s.signal_type,
            "weight": s.weight,
            "found": s.found,
            "score_contribution": s.score_contribution,
            "detail": s.detail,
            "evidence": s.evidence,
        }
        for s in sorted(report.signal_results, key=lambda x: abs(x.score_contribution), reverse=True)
    ]

    yield send("result", {
        "company": report.company_name,
        "domain": report.domain,
        "total_score": report.total_score,
        "intent_level": report.intent_level,
        "positive_score": report.positive_score,
        "negative_score": report.negative_score,
        "neutral_score": report.neutral_score,
        "signals": signals_data,
    })


def _clean_domain(url: str) -> str:
    """Strip query params and fragments, keep just scheme + hostname."""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, "", "", "", ""))
    except Exception:
        return url.split("?")[0]


@app.get("/analyze")
async def analyze(company: str, domain: str | None = None, signals: str | None = None):
    # Normalize company name: title-case so "nike" → "Nike", "microsoft corp" → "Microsoft Corp"
    company = company.strip().title()
    inferred_domain = _clean_domain(domain) if domain else f"https://www.{company.lower().replace(' ', '')}.com"
    selected = set(signals.split(",")) if signals else None
    return StreamingResponse(
        _stream_analysis(company, inferred_domain, selected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/company-info")
async def company_info(company: str):
    def _fetch():
        try:
            headers = {"User-Agent": "BuyingIntentEngine/1.0"}

            # 1. Wikipedia: search → title + wikibase QID + description
            search_r = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search", "srsearch": company,
                        "format": "json", "srlimit": 1},
                timeout=5, headers=headers,
            ).json()
            hits = search_r.get("query", {}).get("search", [])
            if not hits:
                return {}
            title = hits[0]["title"]

            page_r = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "prop": "extracts|pageprops",
                        "exintro": True, "exsentences": 2,
                        "titles": title, "format": "json", "redirects": True},
                timeout=5, headers=headers,
            ).json()
            pages = page_r.get("query", {}).get("pages", {})
            page = next(iter(pages.values()))
            raw = page.get("extract", "")
            description = re.sub(r"<[^>]+>", "", raw).strip()
            sentences = re.split(r"(?<=[.!?])\s+", description)
            description = " ".join(sentences[:2]).strip()

            # 2. Wikidata: revenue, HQ, industry via QID
            qid = page.get("pageprops", {}).get("wikibase_item", "")
            revenue = hq = industry = None
            if qid:
                wd_r = requests.get(
                    "https://www.wikidata.org/w/api.php",
                    params={"action": "wbgetentities", "ids": qid,
                            "props": "claims", "languages": "en", "format": "json"},
                    timeout=6, headers=headers,
                ).json()
                claims = wd_r.get("entities", {}).get(qid, {}).get("claims", {})

                # Collect QIDs to resolve in one batch request
                qids_to_resolve = set()
                hq_qid = ind_qid = None

                # P159 = HQ location
                hq_claims = claims.get("P159", [])
                if hq_claims:
                    v = hq_claims[0]["mainsnak"].get("datavalue", {}).get("value", {})
                    hq_qid = v.get("id") if isinstance(v, dict) else None
                    if hq_qid:
                        qids_to_resolve.add(hq_qid)

                # P452 = industry
                ind_claims = claims.get("P452", [])
                if ind_claims:
                    v = ind_claims[0]["mainsnak"].get("datavalue", {}).get("value", {})
                    ind_qid = v.get("id") if isinstance(v, dict) else None
                    if ind_qid:
                        qids_to_resolve.add(ind_qid)

                # P17 = country (always fetch to combine with city)
                country_qid = None
                country_claims = claims.get("P17", [])
                if country_claims:
                    v = country_claims[0]["mainsnak"].get("datavalue", {}).get("value", {})
                    country_qid = v.get("id") if isinstance(v, dict) else None
                    if country_qid:
                        qids_to_resolve.add(country_qid)

                # Batch-resolve all QID labels
                labels = {}
                if qids_to_resolve:
                    label_r = requests.get(
                        "https://www.wikidata.org/w/api.php",
                        params={"action": "wbgetentities", "ids": "|".join(qids_to_resolve),
                                "props": "labels", "languages": "en", "format": "json"},
                        timeout=6, headers=headers,
                    ).json()
                    for q, e in label_r.get("entities", {}).items():
                        labels[q] = e.get("labels", {}).get("en", {}).get("value", "")

                city = labels.get(hq_qid, "") if hq_qid else ""
                country = labels.get(country_qid, "") if country_qid else ""
                if city and country:
                    hq = f"{city}, {country}"
                elif city:
                    hq = city
                elif country:
                    hq = country
                if ind_qid and labels.get(ind_qid):
                    industry = labels[ind_qid]

                # P2139 = total revenue (most recent)
                rev_claims = claims.get("P2139", [])
                if rev_claims:
                    # Pick claim with most recent point-in-time qualifier (P585) if available
                    best = rev_claims[0]
                    best_year = 0
                    for cl in rev_claims:
                        qualifiers = cl.get("qualifiers", {})
                        pts = qualifiers.get("P585", [])
                        if pts:
                            try:
                                yr = int(pts[0]["datavalue"]["value"]["time"][1:5])
                                if yr > best_year:
                                    best_year = yr
                                    best = cl
                            except Exception:
                                pass
                    rv = best["mainsnak"].get("datavalue", {}).get("value", {})
                    amount_str = rv.get("amount", "")
                    if amount_str:
                        amount = abs(float(amount_str))
                        if amount >= 1e9:
                            revenue = f"${amount / 1e9:.1f}B"
                        elif amount >= 1e6:
                            revenue = f"${amount / 1e6:.0f}M"
                        else:
                            revenue = f"${amount:,.0f}"
                        if best_year:
                            revenue += f" ({best_year})"

            return {
                "name": title,
                "description": description,
                "hq": hq,
                "revenue": revenue,
                "industry": industry,
            }
        except Exception:
            return {}
    info = await asyncio.to_thread(_fetch)
    return JSONResponse(info)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html") as f:
        return f.read()
