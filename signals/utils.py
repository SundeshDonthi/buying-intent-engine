"""Shared helpers for signal collectors."""

import re
from datetime import datetime, timezone


def is_recent(pub_date_str: str, max_days: int = 365) -> bool:
    """Return True if pub_date_str is within max_days of today (or unparseable/missing).

    Handles multiple date formats:
    - RFC 2822 ("Mon, 15 Jan 2024 10:00:00 +0000") — RSS feeds
    - ISO 8601 ("2024-01-15") — SEC EDGAR
    - Relative strings ("2 days ago", "3 months ago", "1 year ago") — SerpAPI
    - Human-readable ("Jan 15, 2025", "January 15, 2025") — SerpAPI
    When the date cannot be parsed we allow the item through (fail open).
    """
    if not pub_date_str:
        return True

    s = pub_date_str.strip().lower()
    now = datetime.now(timezone.utc)

    # ── Relative strings ("X unit ago") ──────────────────────────────────────
    relative_match = re.match(
        r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", s
    )
    if relative_match:
        n, unit = int(relative_match.group(1)), relative_match.group(2)
        unit_days = {"second": 0, "minute": 0, "hour": 0, "day": 1,
                     "week": 7, "month": 30, "year": 365}
        age_days = n * unit_days.get(unit, 1)
        return age_days <= max_days

    # ── RFC 2822 (RSS pubDate) ────────────────────────────────────────────────
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_date_str)
        return (now - dt).days <= max_days
    except Exception:
        pass

    # ── ISO 8601 / YYYY-MM-DD ─────────────────────────────────────────────────
    try:
        dt = datetime.fromisoformat(pub_date_str[:10]).replace(tzinfo=timezone.utc)
        return (now - dt).days <= max_days
    except Exception:
        pass

    # ── Human-readable ("Jan 15, 2025" / "January 15, 2025") ─────────────────
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y"):
        try:
            dt = datetime.strptime(pub_date_str.strip(), fmt).replace(tzinfo=timezone.utc)
            return (now - dt).days <= max_days
        except Exception:
            continue

    return True  # unparseable — don't discard

# Common corporate suffixes to strip before matching
_STRIP_SUFFIXES = re.compile(
    r"\b(inc\.?|corp\.?|llc\.?|ltd\.?|co\.?|company|group|holdings?|technologies?|tech|solutions?|services?|systems?|the)\b",
    re.IGNORECASE,
)


def company_name_variants(company_name: str) -> list[str]:
    """Return a list of name variants to match against article titles."""
    name = company_name.strip()
    stripped = _STRIP_SUFFIXES.sub("", name).strip(" ,.")
    variants = list({name.lower(), stripped.lower()})
    # Also add first significant word if multi-word
    words = [w for w in stripped.lower().split() if len(w) > 2]
    if len(words) >= 2:
        variants.append(words[0])
    return [v for v in variants if v]


def company_in_text(company_name: str, text: str) -> bool:
    """Return True if the company name (or a meaningful variant) appears in text.

    Matches the capitalized company name as a standalone word, so 'Stripe' does not
    match 'Red Stripe' (different brand) or 'stripehype.com' (URL fragment).
    """
    # Build variants from the original (preserves casing for matching)
    name = company_name.strip()
    stripped = _STRIP_SUFFIXES.sub("", name).strip(" ,.")
    variants = list({name, stripped})
    words = [w for w in stripped.split() if len(w) > 2]
    if len(words) >= 2:
        variants.append(words[0])

    for variant in variants:
        if not variant:
            continue
        # Case-sensitive word-boundary match against original text
        # \b works for ASCII word boundaries
        pattern = r'\b' + re.escape(variant) + r'\b'
        if re.search(pattern, text):
            return True
    return False


def filter_by_company(company_name: str, items: list[dict], title_key: str = "title") -> list[dict]:
    """Keep only items whose title field contains the company name."""
    return [item for item in items if company_in_text(company_name, item.get(title_key, ""))]
