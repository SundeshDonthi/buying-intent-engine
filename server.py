"""FastAPI server for the Buying Intent Engine web app."""

import asyncio
import json
import os
import smtplib
from email.mime.text import MIMEText
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


_CORP_SUFFIXES = re.compile(
    r"\b(inc|corp|llc|ltd|co|plc|group|holdings|international|"
    r"technologies|technology|solutions|services|enterprises|global)\b\.?",
    re.IGNORECASE,
)


def _name_tokens(text: str) -> list[str]:
    """Return meaningful lowercase alpha-numeric tokens from a company name."""
    cleaned = _CORP_SUFFIXES.sub("", text)
    return [t for t in re.split(r"[^a-z0-9]+", cleaned.lower()) if len(t) >= 2]


def _domain_slug(domain: str) -> str:
    """Extract just the registrable part of a hostname, e.g. 'salesforce' from 'www.salesforce.com'."""
    parsed = urlparse(domain if "://" in domain else "https://" + domain)
    host = (parsed.hostname or "").lower()
    # strip leading "www."
    host = re.sub(r"^www\.", "", host)
    # strip TLD(s) — everything from the last dot onward (handles .com, .co.uk, .io, etc.)
    host = re.sub(r"\.[^.]+$", "", host)   # strip last segment (.com)
    host = re.sub(r"\.[^.]+$", "", host)   # strip again for .co.uk etc.
    return re.sub(r"[^a-z0-9]", "", host)  # remove remaining punctuation (hyphens etc.)


def _validate_company(company: str, domain: str) -> bool:
    """
    Stage 1 (instant): token-exact match between company name and domain slug.
      e.g. tokens("Nike Inc") = ["nike"] must equal domain_slug("nike.com") = "nike"  ✓
           tokens("Salesforc") = ["salesforc"] ≠ "salesforce"  → fail → Stage 2
    Stage 2 (network): homepage meta tags contain every company token as a whole word.
    """
    try:
        tokens = _name_tokens(company)
        slug = _domain_slug(domain)

        if not tokens or not slug:
            return True  # can't validate — let it through

        # ── Stage 1: each token must exactly match the slug, OR the joined
        #    tokens must exactly equal the slug (handles multi-word names).
        joined = "".join(tokens)
        stage1 = (joined == slug) or all(tok == slug for tok in tokens) or slug in tokens

        if stage1:
            return True

        # ── Stage 2: fetch homepage, check meta tags with whole-word matching ──
        try:
            resp = requests.get(
                domain, timeout=6,
                headers={"User-Agent": "Mozilla/5.0 (compatible; BuyingIntentBot/1.0)"},
                allow_redirects=True,
            )
            if resp.status_code >= 400:
                return False

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text[:60_000], "lxml")

            candidates = []
            if soup.title and soup.title.string:
                candidates.append(soup.title.string)
            for attr in ("og:site_name", "og:title", "application-name"):
                tag = (soup.find("meta", attrs={"property": attr})
                       or soup.find("meta", attrs={"name": attr}))
                if tag and tag.get("content"):
                    candidates.append(tag["content"])

            # Split page text into whole words for exact matching (no substring)
            page_words = set(re.split(r"[^a-z0-9]+", " ".join(candidates).lower()))
            return all(tok in page_words for tok in tokens)

        except Exception:
            return True  # network error — don't block the scan

    except Exception:
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
async def company_info(company: str, domain: str | None = None):
    def _fetch():
        try:
            headers = {"User-Agent": "BuyingIntentEngine/1.0"}

            # ── 1. Wikipedia REST summary — single reliable call ──────────────
            # Try exact title first, then fall back to search
            slug = company.replace(" ", "_").replace(",", "%2C")
            def _is_disambiguation(s: dict) -> bool:
                return s.get("type") == "disambiguation" or "may refer to" in s.get("extract", "").lower()

            rest = requests.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}",
                headers=headers, timeout=5,
            )
            summary = rest.json() if rest.status_code == 200 else {}

            # If disambiguation page or not found, search for "<company> company"
            if rest.status_code != 200 or _is_disambiguation(summary):
                sr = requests.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search",
                            "srsearch": f"{company} company", "format": "json", "srlimit": 1},
                    headers=headers, timeout=5,
                ).json()
                hits = sr.get("query", {}).get("search", [])
                if not hits:
                    return {}
                title = hits[0]["title"]
                slug2 = title.replace(" ", "_").replace(",", "%2C")
                rest2 = requests.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug2}",
                    headers=headers, timeout=5,
                )
                if rest2.status_code == 200:
                    summary = rest2.json()

            if not summary:
                return {}

            title = summary.get("title", company)
            description = summary.get("description", "") or ""
            # Prefer the first sentence of extract (more informative than the short description)
            extract = summary.get("extract", "")
            if extract and not _is_disambiguation(summary):
                first_sentence = re.split(r"(?<=[.!?])\s+", extract)[0]
                if len(first_sentence) > len(description):
                    description = first_sentence

            # ── 2. QID from pageprops ─────────────────────────────────────────
            pr = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "prop": "pageprops", "titles": title,
                        "format": "json", "redirects": True},
                headers=headers, timeout=5,
            ).json()
            pages = pr.get("query", {}).get("pages", {})
            qid = next(iter(pages.values())).get("pageprops", {}).get("wikibase_item", "")

            revenue = hq = industry = None
            if qid:
                # ── 3. Wikidata entity — one call for all claims ──────────────
                wr = requests.get(
                    "https://www.wikidata.org/w/api.php",
                    params={"action": "wbgetentities", "ids": qid,
                            "props": "claims", "languages": "en", "format": "json"},
                    headers=headers, timeout=6,
                ).json()
                claims = wr.get("entities", {}).get(qid, {}).get("claims", {})

                qids_to_resolve: set[str] = set()
                hq_qid = ind_qid = country_qid = None

                def _qid(claim_list):
                    if not claim_list:
                        return None
                    v = claim_list[0]["mainsnak"].get("datavalue", {}).get("value", {})
                    return v.get("id") if isinstance(v, dict) else None

                hq_qid = _qid(claims.get("P159", []))
                ind_qid = _qid(claims.get("P452", []))
                country_qid = _qid(claims.get("P17", []))
                for q in (hq_qid, ind_qid, country_qid):
                    if q:
                        qids_to_resolve.add(q)

                # ── 4. Batch-resolve labels ───────────────────────────────────
                labels: dict[str, str] = {}
                if qids_to_resolve:
                    lr = requests.get(
                        "https://www.wikidata.org/w/api.php",
                        params={"action": "wbgetentities", "ids": "|".join(qids_to_resolve),
                                "props": "labels", "languages": "en", "format": "json"},
                        headers=headers, timeout=5,
                    ).json()
                    for q, e in lr.get("entities", {}).items():
                        labels[q] = e.get("labels", {}).get("en", {}).get("value", "")

                city_raw = labels.get(hq_qid, "") if hq_qid else ""
                country = labels.get(country_qid, "") if country_qid else ""
                # If HQ resolved to a building/tower, look up its city via P131
                if hq_qid and city_raw and any(w in city_raw.lower() for w in ("tower", "building", "campus", "plaza", "center", "centre", "park")):
                    p131_claims = wr.get("entities", {}).get(qid, {}).get("claims", {})
                    # actually fetch P131 of the HQ entity itself
                    hq_entity = requests.get(
                        "https://www.wikidata.org/w/api.php",
                        params={"action": "wbgetentities", "ids": hq_qid,
                                "props": "claims", "languages": "en", "format": "json"},
                        headers=headers, timeout=5,
                    ).json()
                    hq_claims2 = hq_entity.get("entities", {}).get(hq_qid, {}).get("claims", {})
                    city_qid2 = _qid(hq_claims2.get("P131", []))
                    if city_qid2:
                        lr2 = requests.get(
                            "https://www.wikidata.org/w/api.php",
                            params={"action": "wbgetentities", "ids": city_qid2,
                                    "props": "labels", "languages": "en", "format": "json"},
                            headers=headers, timeout=4,
                        ).json()
                        city_raw = lr2.get("entities", {}).get(city_qid2, {}).get("labels", {}).get("en", {}).get("value", city_raw)
                hq = ", ".join(filter(None, [city_raw, country])) or None

                raw_ind = labels.get(ind_qid, "") if ind_qid else ""
                # Tidy up Wikidata's generic labels like "clothing industry"
                industry = re.sub(r"\s+industry$", "", raw_ind, flags=re.IGNORECASE).title() if raw_ind else None

                # ── 5. Revenue — most recent year ─────────────────────────────
                best_rev = best_year = None
                for cl in claims.get("P2139", []):
                    pts = cl.get("qualifiers", {}).get("P585", [])
                    yr = int(pts[0]["datavalue"]["value"]["time"][1:5]) if pts else 0
                    if best_year is None or yr >= best_year:
                        best_year = yr or None
                        best_rev = cl["mainsnak"].get("datavalue", {}).get("value", {}).get("amount")
                if best_rev:
                    amt = abs(float(best_rev))
                    revenue = f"${amt / 1e9:.1f}B" if amt >= 1e9 else f"${amt / 1e6:.0f}M"
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


class ReportRequest(BaseModel):
    company: str = ""
    domain: str = ""
    intent_level: str = ""
    total_score: str = ""
    issues_selected: str = ""
    message: str = ""
    email: str = ""


@app.post("/report")
async def submit_report(body: ReportRequest):
    def _send():
        smtp_user = os.environ.get("REPORT_EMAIL", "")
        smtp_pass = os.environ.get("REPORT_EMAIL_PASSWORD", "")
        to_addr   = os.environ.get("REPORT_TO_EMAIL", smtp_user)

        if not smtp_user or not smtp_pass:
            # Fallback: just log to stdout (visible in Railway logs)
            print(f"[REPORT] Company={body.company} | Issues={body.issues_selected} "
                  f"| Score={body.total_score} | From={body.email} | Note={body.message}")
            return {"success": True}

        subject = f"Inaccuracy Report — {body.company or 'Unknown'}"
        body_text = (
            f"Company:       {body.company}\n"
            f"Domain:        {body.domain}\n"
            f"Intent Level:  {body.intent_level}\n"
            f"Score:         {body.total_score}\n"
            f"Issues:        {body.issues_selected}\n"
            f"Reporter:      {body.email or '(anonymous)'}\n\n"
            f"Details:\n{body.message or '(none)'}"
        )

        msg = MIMEText(body_text)
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = to_addr
        if body.email:
            msg["Reply-To"] = body.email

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [to_addr], msg.as_string())
        return {"success": True}

    try:
        result = await asyncio.to_thread(_send)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html") as f:
        return f.read()
