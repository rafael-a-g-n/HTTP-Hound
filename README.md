# HTTP-Hound

[![GitHub release](https://img.shields.io/github/v/release/rafael-a-g-n/HTTP-Hound)](https://github.com/rafael-a-g-n/HTTP-Hound/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)

> A production-quality, multi-threaded broken link crawler for websites — built in pure Python.

HTTP-Hound crawls a target site, probes every discovered link, image, stylesheet, and script, then produces structured reports (CSV and/or JSON) showing exactly what is broken, blocked, or redirecting too many times.

---

## Features

| Capability | Detail |
|---|---|
| **Concurrent probing** | `ThreadPoolExecutor` probes up to N links simultaneously — dramatically faster than sequential checks |
| **Smart HEAD → GET fallback** | Tries HEAD first (bandwidth-efficient); falls back to a streaming GET when servers reject HEAD |
| **Retry with exponential backoff** | Up to 2 automatic retries on transient failures, with configurable sleep between attempts |
| **Redirect chain tracking** | Records every hop in a redirect chain; chains longer than 3 hops are flagged as an SEO concern |
| **Broad resource coverage** | Discovers `<a href>`, `<img src>`, `<link href>`, and `<script src>` — broken assets caught alongside broken navigation links |
| **BFS depth-limited crawl** | Breadth-first page crawl with an optional `--max-depth` cap to scope large sites |
| **robots.txt compliance** | Reads `/robots.txt` once before crawling and respects disallow rules |
| **Rate limiting** | Configurable `--delay` between page fetches to avoid overwhelming the server |
| **Browser-like headers** | Sends a realistic `User-Agent`, `Accept`, and `Accept-Language` header set to avoid bot-detection false positives |
| **HTML report** | Self-contained browser report with color-coded tables: red for broken/failed, amber for blocked, yellow for long redirect chains |
| **Structured CSV output** | Six-section report: crawl summary, counts by type, counts by status, and separate detail tables for internal vs. external problem links |
| **JSON output** | Machine-readable report with the same structure for downstream processing or CI pipelines |
| **Real-time progress bar** | `tqdm` progress bar during the probe phase; degrades gracefully when `tqdm` is not installed |
| **Source page tracking** | Every problem link records the page it was found on — `source_page` column makes issues immediately actionable |
| **Full CLI** | `argparse`-powered interface — no code changes needed to adjust any crawl parameter |

---

## Requirements

- Python 3.8+
- [requests](https://pypi.org/project/requests/)
- [BeautifulSoup4](https://pypi.org/project/beautifulsoup4/)
- [tqdm](https://pypi.org/project/tqdm/) *(optional — progress bar)*

Install dependencies:

```bash
pip install -r requirements.txt
```

Or install manually:

```bash
pip install requests beautifulsoup4 tqdm
```

---

## Usage

```
python http_hound.py <url> [options]
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `url` | *(required)* | Base URL of the site to crawl |
| `--workers N` | `10` | Number of concurrent link-probe workers |
| `--timeout S` | `10` | Request timeout in seconds |
| `--max-depth D` | unlimited | Maximum BFS crawl depth from the base URL |
| `--delay S` | `0` | Seconds to wait between page fetches |
| `--format` | `csv` | Output format: `csv`, `json`, `html`, `both` (csv+json), or `all` |
| `--version` | — | Print the version number and exit |

### Examples

**Basic crawl — default settings, CSV output:**
```bash
python http_hound.py https://example.com
```

**High-throughput crawl with JSON output:**
```bash
python http_hound.py https://example.com --workers 20 --timeout 15 --format json
```

**Scoped crawl — depth 3, polite delay, both report formats:**
```bash
python http_hound.py https://example.com --max-depth 3 --delay 0.5 --format both
```

**HTML report for easy reading in a browser:**
```bash
python http_hound.py https://example.com --format html
```

**All three output formats at once:**
```bash
python http_hound.py https://example.com --format all
```

**Large site crawl with a generous timeout:**
```bash
python http_hound.py https://example.com --workers 30 --timeout 20 --max-depth 5
```

---

## Use Case Example

Real-world run used for the screenshots below:

```bash
python http_hound.py https://centro-de-estudos.vercel.app/ --max-depth 2 --delay 0.5 --format html
```

What this demonstrates:
- Fast, actionable scan output for a live site.
- A clear HTML report with summary metrics and issue breakdowns.
- Immediate visibility into external broken links for quick remediation.

![image1](https://github.com/user-attachments/assets/fa2684ca-50c7-4b43-b4c0-b0a6a9a0d57a)
![image2](https://github.com/user-attachments/assets/174b7c6d-cf60-4273-855a-2969996aa8bd)

---

## Output

Both report formats are saved in the working directory.

### HTML — `broken_links_report.html`

Open in any browser. The report includes:

- **Stat cards** at the top: pages crawled, links parsed, problems found, internal issues
- **Problem Links by Type** table — counts per classification
- **Problem Links by Status** table — counts per HTTP status code
- **Internal Problem Links** table — color-coded detail rows with `source_page`
- **External Problem Links** table — same structure

Row colors:

| Color | Meaning |
|---|---|
| Red | `broken` or `failed` |
| Amber | `blocked` (401/403) |
| Yellow | `redirect_chain` (> 3 hops) |

### CSV — `broken_links_report.csv`

The CSV is structured into six labeled sections:

```
HTTP Hound Crawl Summary
base_url,           https://example.com
pages_crawled,      42
unique_links_parsed,318
problem_links_found,11

Problem Links by Type
classification, count
blocked,        2
broken,         7
redirect_chain, 2

Problem Links by Status
status, count
403,    2
404,    6
500,    1
...

Internal Problem Links
url, status, classification, method, redirect_hops, redirect_chain, link_type, source_page
...

External Problem Links
url, status, classification, method, redirect_hops, redirect_chain, link_type, source_page
...
```

### JSON — `broken_links_report.json`

```json
{
  "crawl_summary": { ... },
  "counts_by_type": { "broken": 7, "blocked": 2, "redirect_chain": 2 },
  "counts_by_status": { "403": 2, "404": 6, "500": 1 },
  "problem_links": {
    "internal": [ ... ],
    "external": [ ... ]
  }
}
```

---

## Link Classifications

| Classification | Meaning |
|---|---|
| `ok` | Link resolved successfully |
| `broken` | HTTP 4xx / 5xx status after all retries |
| `blocked` | HTTP 401 or 403 — server denied access |
| `redirect_chain` | More than 3 redirect hops — potential SEO issue |
| `failed` | Could not connect after all retries |

---

## How It Works

1. **Seed** the BFS queue with the base URL at depth 0.
2. **Fetch** each page, respecting `robots.txt` and the configured delay.
3. **Discover** new pages via `<a href>` links (internal only, within depth limit).
4. **Collect** all resource URLs (links, images, stylesheets, scripts) from every crawled page.
5. **Probe** all unique URLs concurrently using `ThreadPoolExecutor`.
   - HEAD request first; streaming GET fallback if HEAD is rejected.
   - Retry up to 2× with 1-second backoff on network errors.
   - Record full redirect chain for every request.
6. **Report** problem links to stdout in real time and write the final report on completion.

---

## Project Structure

```
HTTP-Hound/
├── http_hound.py   # Complete crawler — all logic in a single, readable file
├── requirements.txt
├── LICENSE
└── README.md
```

---

## Changelog

### v1.0.0 — 2026-04-12

Initial public release.

- Multi-threaded BFS crawler with configurable depth, workers, and delay
- HEAD → GET fallback with retry and exponential backoff
- robots.txt compliance
- Redirect chain tracking (flags chains longer than 3 hops)
- `source_page` field on every problem link for immediate actionability
- Output formats: CSV (structured six-section report), JSON, and self-contained HTML
- HTML report with stat cards and color-coded tables
- `--version` flag
- MIT license

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
