"""Job posting collector using SerpAPI (paid) with Google News RSS fallback."""

import os
import time
from dataclasses import dataclass, field

import requests

from .utils import company_in_text

def _get_serp_key() -> str:
    return os.getenv("SERPAPI_KEY", "")

RELEVANT_JOB_KEYWORDS = [
    "software engineer", "it manager", "devops", "cloud architect",
    "data engineer", "erp", "crm", "salesforce", "sap", "operations manager",
    "systems administrator", "solutions architect", "infrastructure",
]


@dataclass
class JobSignal:
    signal_name: str
    found: bool
    job_count: int = 0
    detail: str = ""
    jobs: list[dict] = field(default_factory=list)
    articles: list[dict] = field(default_factory=list)


def _search_serpapi(company_name: str) -> list[dict]:
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_jobs",
        "q": f"{company_name} software OR IT OR engineer OR cloud OR operations",
        "api_key": _get_serp_key(),
        "num": 10,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        jobs = resp.json().get("jobs_results", [])
        # Filter: job must be at the target company
        return [
            j for j in jobs
            if company_in_text(company_name, j.get("company_name", ""))
        ]
    except Exception:
        return []


def _search_rss_fallback(company_name: str) -> list[dict]:
    from urllib.parse import quote
    query = quote(f'"{company_name}" hiring OR jobs OR careers OR "job opening" OR "we are hiring"')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")[:10]
        time.sleep(0.3)
        results = []
        for item in items:
            title = item.find("title").text if item.find("title") else ""
            link = item.find("link").text if item.find("link") else ""
            # Must mention the company AND a hiring keyword
            if company_in_text(company_name, title) and any(
                kw in title.lower() for kw in ["hiring", "job", "career", "recruit", "opening", "position"]
            ):
                results.append({"title": title, "link": link, "source": "news_rss"})
        return results
    except Exception:
        return []


class JobsCollector:
    def collect(self, company_name: str) -> list[JobSignal]:
        if _get_serp_key():
            jobs = _search_serpapi(company_name)
            relevant = [j for j in jobs if any(kw in j.get("title", "").lower() for kw in RELEVANT_JOB_KEYWORDS)]
            found = bool(relevant)
            articles = [{"title": j.get("title", ""), "link": j.get("link", "")} for j in relevant[:5]]
            hiring = JobSignal(
                "hiring_activity", found,
                len(relevant),
                f"{len(relevant)} relevant job posting(s) found via SerpAPI" if found else "No relevant job postings",
                relevant[:5],
                articles,
            )
            no_openings = JobSignal(
                "no_job_openings", not bool(jobs), 0,
                "No job openings found" if not jobs else f"{len(jobs)} total job(s) found",
            )
        else:
            news_hits = _search_rss_fallback(company_name)
            found = bool(news_hits)
            articles = [{"title": h.get("title", ""), "link": h.get("link", "")} for h in news_hits[:5]]
            hiring = JobSignal(
                "hiring_activity", found,
                len(news_hits),
                f"{len(news_hits)} hiring signal(s) in news (no SerpAPI key — add SERPAPI_KEY for job board data)" if found
                else "No hiring signals found (no SerpAPI key — add SERPAPI_KEY for job board data)",
                news_hits[:5],
                articles,
            )
            no_openings = JobSignal(
                "no_job_openings", not found, 0,
                "No job openings detected" if not found else "Hiring activity detected",
            )

        return [hiring, no_openings]
