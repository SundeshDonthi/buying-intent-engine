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

from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
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


async def _stream_analysis(company: str, domain: str) -> AsyncGenerator[str, None]:
    def send(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield send("start", {"company": company, "domain": domain, "total_steps": len(COLLECTORS)})

    all_signals = []
    for i, (key, label, collector_fn) in enumerate(COLLECTORS):
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
async def analyze(company: str, domain: str | None = None):
    inferred_domain = _clean_domain(domain) if domain else f"https://www.{company.lower().replace(' ', '')}.com"
    return StreamingResponse(
        _stream_analysis(company, inferred_domain),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html") as f:
        return f.read()
