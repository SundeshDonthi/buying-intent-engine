# B2B Buying Intent Engine — MVP

Scores a company's buying intent using free public data sources. No paid APIs required for baseline functionality.

## Quick Start

```bash
pip install -r requirements.txt
python main.py "Acme Corp" --domain https://www.acme.com
python main.py "WeWork" --json   # JSON output for downstream use
```

## Signals Tracked

### Positive (increase score)
| Signal | Weight | Source |
|---|---|---|
| RFP/RFQ/RFI activity | 10 | SAM.gov (gov sector) |
| ERP/CRM/cloud migration keywords | 8 | Google News RSS |
| Operational pain signals | 8 | Google News RSS |
| M&A activity | 6 | SEC EDGAR (free) |
| Leadership change | 6 | Google News RSS |
| Relevant hiring activity | 5 | SerpAPI ($25/mo) or News RSS fallback |
| Tech stack signal | 5 | HTTP fingerprinting (free) |
| Geographic expansion | 4 | Google News RSS |
| IPO preparation (S-1 filing) | 3 | SEC EDGAR (free) |

### Negative (decrease score)
| Signal | Weight | Source |
|---|---|---|
| Bankruptcy filing | 18 | SEC EDGAR (free) |
| Layoffs | 14 | Layoffs.fyi + Google News RSS |
| No job openings | 6 | Jobs collector |

### Neutral (informational, no score impact)
| Signal | Weight | Source |
|---|---|---|
| Reorganization/restructuring | 30 | SEC EDGAR + News |
| Internal promotion | 25 | Google News RSS |

## Intent Levels

| Level | Criteria |
|---|---|
| **Strong Buyer** | Score ≥ 15 |
| **Potential Buyer** | Score 5–14 |
| **Neutral** | Score -4 to 4 |
| **Poor Fit** | Score ≤ -5 |
| **Disqualified** | Negative score ≤ -10 |

## Optional: SerpAPI for Job Signals

Add your SerpAPI key for structured job posting data (more accurate than the RSS fallback):

```bash
export SERPAPI_KEY=your_key_here
python main.py "Acme Corp"
```

## Architecture

```
main.py              — CLI entry point
config.py            — signal weights and thresholds
scoring.py           — combines raw signals into a scored report
signals/
  sec_edgar.py       — bankruptcy, M&A, IPO, reorganization (SEC EDGAR API)
  news.py            — leadership, migration, pain, expansion (Google News RSS)
  jobs.py            — hiring activity (SerpAPI or RSS fallback)
  layoffs.py         — layoffs (Layoffs.fyi + news)
  tech_stack.py      — tech fingerprinting via HTTP headers/HTML
```

## Known Limitations (MVP)

- **EDGAR searches** are by company name string match — very common names ("Acme") will return false positives. Use precise legal company names for best results.
- **News signals** rely on Google News RSS which has no date filtering — signals may be older than intended.
- **No SerpAPI key** means job signals fall back to a news-based approximation (less precise).
- **Tech stack detection** only works for web-facing companies and misses internal systems.
