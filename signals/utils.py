"""Shared helpers for signal collectors."""

import re

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
