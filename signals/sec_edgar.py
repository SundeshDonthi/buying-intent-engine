"""SEC EDGAR signal collector — free, no API key required."""

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "BuyingIntentEngine research@example.com"}

# 8-K item codes that indicate specific events
BANKRUPTCY_ITEMS = {"1.03"}          # Bankruptcy or Receivership
REORG_ITEMS      = {"5.02", "1.01"}  # Leadership/reorg-adjacent (supplemental)
MA_FORMS         = {"SC 13D", "SC 13E3", "DEFM14A", "425", "SC TO-T", "SC TO-I"}
IPO_FORMS        = {"S-1", "S-1/A"}


@dataclass
class EdgarSignal:
    signal_name: str
    found: bool
    detail: str = ""
    filings: list[dict] = field(default_factory=list)


# ── CIK resolution ────────────────────────────────────────────────────────────

@lru_cache(maxsize=64)
def _resolve_cik(company_name: str) -> str | None:
    """Return the SEC CIK for a company name, or None if not found.

    Strategy:
    1. Search EFTS for 10-K/8-K filers — this matches operating companies, not funds/SPVs.
    2. Among results, prefer the hit whose display_name most closely matches the query.
    3. Fall back to EFTS general search if no 10-K filers found.
    """
    name_lower = company_name.lower().strip()

    def _best_cik_from_hits(hits: list) -> str | None:
        candidates = []
        for h in hits:
            src = h.get("_source", {})
            ciks = src.get("ciks", [])
            display_names = src.get("display_names", [])
            for dn in display_names:
                dn_lower = dn.lower()
                # Exact match (ignoring suffixes like Inc., Corp.)
                import re as _re
                clean = _re.sub(r'\b(inc\.?|corp\.?|llc\.?|ltd\.?|co\.?)\b', '', dn_lower).strip(", .")
                if (name_lower == clean or name_lower in dn_lower) and ciks:
                    # Score: prefer shorter names (more specific match)
                    candidates.append((len(dn), ciks[0]))
        if candidates:
            candidates.sort()
            raw = candidates[0][1]
            return raw.lstrip("0") or raw
        return None

    # Step 1: look for 10-K filers (public operating companies)
    url = (
        f"https://efts.sec.gov/LATEST/search-index?"
        f"q=%22{requests.utils.quote(company_name)}%22"
        f"&forms=10-K,10-K%2FA,8-K"
        f"&dateRange=custom&startdt=2015-01-01&enddt={date.today().isoformat()}"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        hits = r.json().get("hits", {}).get("hits", [])
        time.sleep(0.2)
        cik = _best_cik_from_hits(hits)
        if cik:
            return cik
    except Exception:
        pass

    # Step 2: broader EFTS search (catches private companies that filed S-1, SC 13D, etc.)
    url2 = (
        f"https://efts.sec.gov/LATEST/search-index?"
        f"q=%22{requests.utils.quote(company_name)}%22"
        f"&dateRange=custom&startdt=2010-01-01&enddt={date.today().isoformat()}"
    )
    try:
        r = requests.get(url2, headers=HEADERS, timeout=10)
        hits = r.json().get("hits", {}).get("hits", [])
        time.sleep(0.2)
        cik = _best_cik_from_hits(hits)
        if cik:
            return cik
    except Exception:
        pass

    return None


# ── Submissions API ───────────────────────────────────────────────────────────

def _get_submissions(cik: str) -> dict:
    """Fetch the SEC submissions JSON for a CIK (structured filing history)."""
    padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        time.sleep(0.2)
        return r.json()
    except Exception:
        return {}


def _recent_filings(submissions: dict, form_types: set[str], lookback_days: int) -> list[dict]:
    """Extract recent filings of given types from the submissions JSON."""
    cutoff = date.today() - timedelta(days=lookback_days)
    filings_data = submissions.get("filings", {}).get("recent", {})
    if not filings_data:
        return []

    forms      = filings_data.get("form", [])
    dates      = filings_data.get("filingDate", [])
    accessions = filings_data.get("accessionNumber", [])
    descriptions = filings_data.get("primaryDocument", [])
    items_list = filings_data.get("items", [])

    results = []
    for i, form in enumerate(forms):
        if form not in form_types:
            continue
        try:
            filing_date = date.fromisoformat(dates[i])
        except (ValueError, IndexError):
            continue
        if filing_date < cutoff:
            continue

        acc = accessions[i] if i < len(accessions) else ""
        cik = submissions.get("cik", "")
        link = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}&dateb=&owner=include&count=5"
        items = items_list[i] if i < len(items_list) else ""

        results.append({
            "form": form,
            "date": dates[i],
            "accession": acc,
            "items": items,
            "title": f"{form} filed {dates[i]}" + (f" — items {items}" if items else ""),
            "link": link,
        })

    return results


# ── Atom feed helper (for companies without CIK resolution) ──────────────────

def _atom_filings(company_name: str, form_type: str, lookback_days: int) -> list[dict]:
    """Fallback: browse-edgar Atom feed, reading summary for item descriptions."""
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar"
        f"?company={requests.utils.quote(company_name)}&CIK=&type={requests.utils.quote(form_type)}"
        f"&dateb=&owner=include&count=20&search_text=&action=getcompany&output=atom"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "xml")
        time.sleep(0.2)
        results = []
        for entry in soup.find_all("entry"):
            updated = entry.find("updated")
            updated_str = updated.text[:10] if updated else ""
            if updated_str < cutoff:
                continue
            link_tag = entry.find("link")
            link = link_tag["href"] if link_tag and link_tag.get("href") else ""
            summary_tag = entry.find("summary")
            summary = BeautifulSoup(summary_tag.text, "html.parser").get_text() if summary_tag else ""
            content_tag = entry.find("content")
            items_desc = ""
            if content_tag:
                items_tag = content_tag.find("items-desc")
                if items_tag:
                    items_desc = items_tag.text
            results.append({
                "form": form_type,
                "date": updated_str,
                "items": items_desc,
                "summary": summary,
                "title": f"{form_type} filed {updated_str}" + (f" — {items_desc}" if items_desc else ""),
                "link": link,
            })
        return results
    except Exception:
        return []


# ── Collector ─────────────────────────────────────────────────────────────────

class SecEdgarCollector:
    def __init__(self, lookback_days: int = 365):  # 1 year — keeps signals current
        self.lookback_days = lookback_days

    def collect(self, company_name: str) -> list[EdgarSignal]:
        # Resolve CIK once; all checks reuse submissions data
        cik = _resolve_cik(company_name)
        submissions = _get_submissions(cik) if cik else {}

        return [
            self._check_bankruptcy(company_name, cik, submissions),
            self._check_ma(company_name, cik, submissions),
            self._check_ipo(company_name, cik, submissions),
            self._check_reorganization(company_name, cik, submissions),
        ]

    def _check_bankruptcy(self, company_name: str, cik: str | None, submissions: dict) -> EdgarSignal:
        if submissions:
            filings = _recent_filings(submissions, {"8-K"}, self.lookback_days)
            # Item 1.03 = "Bankruptcy or Receivership"
            confirmed = [f for f in filings if "1.03" in (f.get("items") or "")]
        else:
            # Fallback: Atom feed + keyword scan in summary
            filings = _atom_filings(company_name, "8-K", self.lookback_days)
            confirmed = [
                f for f in filings
                if any(kw in (f.get("summary", "") + f.get("items", "")).lower()
                       for kw in ["bankruptcy", "chapter 11", "chapter 7", "receivership", "1.03"])
            ]

        found = bool(confirmed)
        return EdgarSignal(
            "bankruptcy", found,
            f"{len(confirmed)} bankruptcy/receivership 8-K filing(s) found (Item 1.03)" if found
            else "No bankruptcy filings found",
            confirmed[:5],
        )

    def _check_ma(self, company_name: str, cik: str | None, submissions: dict) -> EdgarSignal:
        if submissions:
            filings = _recent_filings(submissions, MA_FORMS, self.lookback_days)
        else:
            filings = []
            for form in ["SC 13D", "DEFM14A", "425"]:
                filings += _atom_filings(company_name, form, self.lookback_days)

        found = bool(filings)
        return EdgarSignal(
            "ma_activity", found,
            f"{len(filings)} M&A-related filing(s) found ({', '.join({f['form'] for f in filings})})" if found
            else "No M&A filings found",
            filings[:5],
        )

    def _check_ipo(self, company_name: str, cik: str | None, submissions: dict) -> EdgarSignal:
        if submissions:
            filings = _recent_filings(submissions, IPO_FORMS, self.lookback_days)
        else:
            filings = _atom_filings(company_name, "S-1", self.lookback_days)

        found = bool(filings)
        return EdgarSignal(
            "ipo_preparation", found,
            f"S-1 filing found — IPO in progress ({filings[0]['date']})" if found else "No S-1 filing found",
            filings[:3],
        )

    def _check_reorganization(self, company_name: str, cik: str | None, submissions: dict) -> EdgarSignal:
        if submissions:
            filings = _recent_filings(submissions, {"8-K"}, self.lookback_days)
            # Item 5.02 = leadership changes; Item 2.05 = cost associated exit activities (layoffs/reorg)
            confirmed = [
                f for f in filings
                if any(item in (f.get("items") or "") for item in ["2.05", "5.02"])
                and any(item in (f.get("items") or "") for item in ["2.05"])  # 2.05 is the reorg signal
            ]
        else:
            filings = _atom_filings(company_name, "8-K", self.lookback_days)
            confirmed = [
                f for f in filings
                if any(kw in (f.get("summary", "") + f.get("items", "")).lower()
                       for kw in ["restructur", "reorganiz", "2.05", "workforce"])
            ]

        found = bool(confirmed)
        return EdgarSignal(
            "reorganization", found,
            f"{len(confirmed)} restructuring/reorg 8-K filing(s) found (Item 2.05)" if found
            else "No reorg filings found",
            confirmed[:5],
        )
