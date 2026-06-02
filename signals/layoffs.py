"""Layoffs signal collector using Layoffs.fyi dataset and Google News RSS."""

import re
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from .utils import company_in_text, is_recent

LAYOFFS_FYI_URL = "https://layoffs.fyi"


@dataclass
class LayoffSignal:
    signal_name: str
    found: bool
    detail: str = ""
    sources: list[dict] = field(default_factory=list)
    # Standardized evidence field
    articles: list[dict] = field(default_factory=list)


def _check_layoffs_fyi(company_name: str) -> list[dict]:
    try:
        resp = requests.get(LAYOFFS_FYI_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        matches = []
        for row in soup.find_all("tr"):
            row_text = row.get_text(separator=" ")
            if company_in_text(company_name, row_text):
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if cells and len(cells) >= 2:
                    # layoffs.fyi typically has date in one of the first few cells (ISO format)
                    row_date = next(
                        (c for c in cells[:6] if re.match(r"\d{4}-\d{2}-\d{2}", c)), ""
                    )
                    if row_date and not is_recent(row_date, max_days=365):
                        continue  # skip entries older than 1 year
                    label = " | ".join(c for c in cells[:4] if c)
                    matches.append({"title": label, "link": "", "pubDate": row_date, "source": "layoffs.fyi"})
        time.sleep(0.3)
        return matches
    except Exception:
        return []


def _check_news_layoffs(company_name: str) -> list[dict]:
    from urllib.parse import quote
    query = quote(f'"{company_name}" layoffs OR "laid off" OR "workforce reduction" OR "job cuts"')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")[:10]
        time.sleep(0.3)
        results = []
        for item in items:
            title = item.find("title").text if item.find("title") else ""
            link = item.find("link").text if item.find("link") else ""
            pub_date = item.find("pubDate").text if item.find("pubDate") else ""
            # Must mention the company AND a layoff keyword
            if (is_recent(pub_date, max_days=180)  # layoffs older than 6 months aren't actionable
                    and company_in_text(company_name, title)
                    and any(kw in title.lower() for kw in ["layoff", "laid off", "cut", "workforce", "reduction", "job"])):
                results.append({"title": title, "link": link, "pubDate": pub_date, "source": "google_news"})
        return results
    except Exception:
        return []


class LayoffsCollector:
    def collect(self, company_name: str) -> list[LayoffSignal]:
        fyi_hits = _check_layoffs_fyi(company_name)
        news_hits = _check_news_layoffs(company_name)
        all_hits = fyi_hits + news_hits
        found = bool(all_hits)

        sources_desc = []
        if fyi_hits:
            sources_desc.append(f"{len(fyi_hits)} entry(ies) on Layoffs.fyi")
        if news_hits:
            sources_desc.append(f"{len(news_hits)} confirmed news article(s)")

        # Normalize to articles format for evidence display
        articles = [
            {"title": h.get("title", ""), "link": h.get("link", ""), "source": h.get("source", "")}
            for h in all_hits[:5]
        ]

        return [LayoffSignal(
            signal_name="layoffs",
            found=found,
            detail=", ".join(sources_desc) if found else "No confirmed layoff signals found",
            sources=all_hits[:5],
            articles=articles,
        )]
