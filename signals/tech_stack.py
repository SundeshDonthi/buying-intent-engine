"""Tech stack detector using HTTP headers and HTML meta tags (Wappalyzer-style, free)."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

# Simplified tech fingerprints: {tech_name: {header/meta/script patterns}}
TECH_FINGERPRINTS: dict[str, dict] = {
    "WordPress": {"meta": ["generator.*wordpress"], "script": ["wp-content", "wp-includes"]},
    "Drupal": {"meta": ["generator.*drupal"], "script": ["drupal.js", "/sites/default/"]},
    "Shopify": {"script": ["cdn.shopify.com"], "header": {"x-shopify-stage": ".*"}},
    "Salesforce": {"script": ["force.com", "salesforce.com", "lightning.force"]},
    "HubSpot": {"script": ["js.hs-scripts.com", "hs-analytics.net"]},
    "Marketo": {"script": ["munchkin.marketo.net"]},
    "Google Analytics": {"script": ["google-analytics.com/analytics", "gtag/js"]},
    "React": {"script": ["react.production.min.js", "react.development.js"], "html": ["__REACT"]},
    "Angular": {"script": ["angular.min.js", "ng-version"]},
    "Vue.js": {"script": ["vue.min.js", "vue.global.js"]},
    "AWS": {"header": {"x-amz": ".*", "x-amzn": ".*"}},
    "Cloudflare": {"header": {"cf-ray": ".*", "server": "cloudflare"}},
    "Intercom": {"script": ["widget.intercom.io", "js.intercom.com"]},
    "Zendesk": {"script": ["static.zdassets.com", "ekr.zdassets.com"]},
}

LEGACY_TECH = {"WordPress", "Drupal"}
MODERN_TECH = {"React", "Angular", "Vue.js", "AWS"}


@dataclass
class TechStackSignal:
    signal_name: str
    found: bool
    detail: str = ""
    detected_tech: list[str] = field(default_factory=list)
    legacy_detected: list[str] = field(default_factory=list)
    modern_detected: list[str] = field(default_factory=list)


def _detect_tech(url: str) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {"detected": [], "error": []}
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        html = resp.text.lower()
        headers_str = str(resp.headers).lower()

        for tech, patterns in TECH_FINGERPRINTS.items():
            for pattern_type, pattern_list in patterns.items():
                if isinstance(pattern_list, list):
                    for pattern in pattern_list:
                        if pattern_type in ("script", "html", "meta"):
                            if re.search(pattern.lower(), html):
                                results["detected"].append(tech)
                                break
                        elif pattern_type == "header":
                            if re.search(pattern.lower(), headers_str):
                                results["detected"].append(tech)
                                break
                elif isinstance(pattern_list, dict):
                    for header_key, header_pattern in pattern_list.items():
                        header_val = resp.headers.get(header_key, "")
                        if re.search(header_pattern, header_val, re.IGNORECASE):
                            results["detected"].append(tech)
                            break
    except Exception as e:
        results["error"].append(str(e))

    results["detected"] = list(dict.fromkeys(results["detected"]))  # dedupe preserve order
    return results


class TechStackCollector:
    def collect(self, company_name: str, domain: str | None = None) -> list[TechStackSignal]:
        if not domain:
            # Attempt to infer domain from company name (best-effort)
            slug = re.sub(r"[^a-z0-9]", "", company_name.lower().replace(" ", ""))
            domain = f"https://www.{slug}.com"

        result = _detect_tech(domain)
        detected = result["detected"]

        legacy = [t for t in detected if t in LEGACY_TECH]
        modern = [t for t in detected if t in MODERN_TECH]

        # Signal: meaningful tech change evidence = legacy stack (ripe for modernization)
        # OR modern stack with marketing/CRM tools indicating active investment
        found = bool(legacy) or bool(modern)

        if legacy:
            detail = f"Legacy tech detected ({', '.join(legacy)}) — modernization opportunity"
        elif modern:
            detail = f"Modern stack detected ({', '.join(modern)}) — active tech investment"
        elif result["error"]:
            detail = f"Could not reach {domain}: {result['error'][0]}"
            found = False
        else:
            detail = f"No strong tech signals detected at {domain}"

        return [
            TechStackSignal(
                signal_name="tech_stack_change",
                found=found,
                detail=detail,
                detected_tech=detected,
                legacy_detected=legacy,
                modern_detected=modern,
            )
        ]
