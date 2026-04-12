import argparse
import csv
import json
import time
import urllib.robotparser
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from html import escape as html_escape
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from tqdm import tqdm  # type: ignore[import-untyped]
except ImportError:
    # tqdm is optional; fall back to a no-op wrapper when not installed.
    def tqdm(iterable, **kwargs):
        return iterable

# Retry config for transient network errors in probe_link.
MAX_RETRIES = 2
RETRY_BACKOFF = 1.0  # seconds to wait before each successive retry

# Redirect chains longer than this are flagged as an SEO concern.
MAX_REDIRECT_HOPS = 3

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def build_session():
    """Create a session that looks closer to a browser request."""
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    return session


def build_robots_parser(base_url):
    """Fetch and parse the site's robots.txt; allow all if unavailable."""
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(urljoin(base_url, "/robots.txt"))
    try:
        parser.read()
    except Exception:
        # If robots.txt cannot be fetched, default to allowing everything.
        pass
    return parser


def normalize_url(url):
    """Remove fragments and normalize empty paths."""
    clean_url, _ = urldefrag(url)
    parsed_url = urlparse(clean_url)
    path = parsed_url.path or "/"

    return parsed_url._replace(path=path).geturl()


def is_internal_url(url, base_netloc):
    """Return True when a URL belongs to the same site being crawled."""
    parsed_url = urlparse(url)

    return (
        parsed_url.scheme in {"http", "https"}
        and parsed_url.netloc == base_netloc
    )


def fetch_page(url, session, timeout):
    """Fetch an HTML page for crawling."""
    response = session.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        return None

    return response


def extract_page_links(page_url, html):
    """Extract normalized <a href> URLs used to drive BFS page crawling."""
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for link in soup.find_all("a", href=True):
        full_url = normalize_url(urljoin(page_url, link["href"]))
        if full_url.startswith("http"):
            links.append(full_url)

    return links


def extract_page_resources(page_url, html):
    """Extract all resource URLs from a page for broken-link probing.

    Covers hyperlinks, images, stylesheets, and scripts so that broken
    assets are caught alongside broken navigation links.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Map each tag to the attribute that carries its URL.
    tag_attr_pairs = [
        ("a", "href"),     # hyperlinks
        ("img", "src"),    # images
        ("link", "href"),  # stylesheets / favicons
        ("script", "src"),  # JavaScript files
    ]

    urls = []
    for tag, attr in tag_attr_pairs:
        for element in soup.find_all(tag, attrs={attr: True}):
            full_url = normalize_url(urljoin(page_url, element[attr]))
            if full_url.startswith("http"):
                urls.append(full_url)

    return urls


def _build_redirect_chain(response):
    """Return the ordered list of URLs traversed to reach the final page."""
    chain = [r.url for r in response.history]
    chain.append(response.url)
    return chain


def probe_link(url, timeout):
    """Check a link with HEAD first, fall back to GET, retry on failure.

    Returns a dict with status, classification, method,
    redirect_hops, and redirect_chain.
    """
    head_status = None

    # --- HEAD attempt ---
    try:
        head_response = requests.head(
            url,
            headers=REQUEST_HEADERS,
            allow_redirects=True,
            timeout=timeout,
        )
        head_status = head_response.status_code
        chain = _build_redirect_chain(head_response)
        hops = len(chain) - 1

        if head_status < 400:
            # Flag redirect chains that are too long as an SEO concern.
            classification = (
                "redirect_chain" if hops > MAX_REDIRECT_HOPS else "ok"
            )
            return {
                "status": head_status,
                "classification": classification,
                "method": "HEAD",
                "final_url": head_response.url,
                "redirect_hops": hops,
                "redirect_chain": " -> ".join(chain),
            }

        if head_status not in {403, 404, 405}:
            return {
                "status": head_status,
                "classification": "broken",
                "method": "HEAD",
                "final_url": head_response.url,
                "redirect_hops": hops,
                "redirect_chain": " -> ".join(chain),
            }
    except requests.exceptions.RequestException:
        head_status = None

    # --- GET attempt with retry backoff ---
    # Used when HEAD is rejected (403/404/405) or fails outright.
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            # Wait before retrying to handle transient network errors.
            time.sleep(RETRY_BACKOFF * attempt)
        try:
            # Some sites reject HEAD requests but answer normally to GET.
            get_response = requests.get(
                url,
                headers=REQUEST_HEADERS,
                allow_redirects=True,
                timeout=timeout,
                stream=True,
            )
            get_status = get_response.status_code
            chain = _build_redirect_chain(get_response)
            hops = len(chain) - 1
            get_response.close()

            if get_status < 400:
                classification = (
                    "redirect_chain" if hops > MAX_REDIRECT_HOPS else "ok"
                )
                return {
                    "status": get_status,
                    "classification": classification,
                    "method": "GET",
                    "final_url": chain[-1],
                    "redirect_hops": hops,
                    "redirect_chain": " -> ".join(chain),
                }

            classification = (
                "blocked" if get_status in {401, 403} else "broken"
            )
            return {
                "status": get_status,
                "classification": classification,
                "method": "GET",
                "final_url": chain[-1],
                "redirect_hops": hops,
                "redirect_chain": " -> ".join(chain),
            }
        except requests.exceptions.RequestException:
            pass  # retry on next iteration or fall through to failure

    # All retries exhausted — classify as a connection failure.
    return {
        "status": (
            head_status if head_status is not None else "FAILED TO CONNECT"
        ),
        "classification": "failed",
        "method": "GET" if head_status is not None else "NONE",
        "final_url": url,
        "redirect_hops": 0,
        "redirect_chain": url,
    }


def record_result(results, url, result, link_type, source_page):
    """Store links that fail or have a notable redirect chain."""
    classification = result["classification"]
    status = result["status"]
    method = result["method"]
    hops = result["redirect_hops"]

    notable = {"broken", "blocked", "failed", "redirect_chain"}
    if classification in notable:
        hop_info = f" [{hops} hops]" if hops else ""
        print(
            f"[{classification.upper()}] {status} via {method}"
            f"{hop_info} - {url}"
        )
        results.append(
            {
                "url": url,
                "status": status,
                "classification": classification,
                "method": method,
                "redirect_hops": hops,
                "redirect_chain": result["redirect_chain"],
                "link_type": link_type,
                "source_page": source_page,
            }
        )
        return

    print(f"[OK] {status} via {method} - {url}")


def check_links(
    base_url, workers=10, timeout=10, max_depth=None,
    delay=0.0, output_format="csv"
):
    """Crawl base_url and probe all discovered links for breakage."""
    base_url = normalize_url(base_url)
    print(f"--- Starting crawl on: {base_url} ---")

    session = build_session()
    base_netloc = urlparse(base_url).netloc

    # Load robots.txt once so every page fetch can be checked against it.
    robots = build_robots_parser(base_url)

    # Queue stores (url, depth) tuples for BFS traversal.
    pages_to_visit = deque([(base_url, 0)])
    queued_pages = {base_url}
    visited_pages = set()
    checked_links = set()
    results = []
    links_to_probe = []
    # Maps each URL to "internal" or "external" for report separation.
    link_types = {}
    # Maps each URL to the first page on which it was discovered.
    source_pages = {}

    while pages_to_visit:
        current_page, depth = pages_to_visit.popleft()
        print(f"Crawling page: {current_page}")

        # Respect robots.txt before fetching each page.
        if not robots.can_fetch("*", current_page):
            print(f"Blocked by robots.txt: {current_page}")
            visited_pages.add(current_page)
            continue

        try:
            response = fetch_page(current_page, session, timeout)
        except requests.exceptions.RequestException as error:
            print(f"Could not crawl page: {current_page} ({error})")
            visited_pages.add(current_page)
            continue

        visited_pages.add(current_page)
        if response is None:
            continue

        # Pause between page fetches to avoid overwhelming the server.
        if delay:
            time.sleep(delay)

        # Probe every resource URL (images, scripts, stylesheets, links).
        for full_url in extract_page_resources(current_page, response.text):
            if full_url not in checked_links:
                checked_links.add(full_url)
                links_to_probe.append(full_url)
                # Record the page where this URL was first seen.
                source_pages[full_url] = current_page
                # Record whether this resource belongs to the crawled site.
                link_types[full_url] = (
                    "internal"
                    if is_internal_url(full_url, base_netloc)
                    else "external"
                )

        # Only follow <a> href links to discover new pages to crawl.
        for full_url in extract_page_links(current_page, response.text):
            if not is_internal_url(full_url, base_netloc):
                continue

            if full_url in queued_pages or full_url in visited_pages:
                continue

            # Only enqueue child pages within the allowed depth.
            if max_depth is not None and depth + 1 > max_depth:
                continue

            queued_pages.add(full_url)
            pages_to_visit.append((full_url, depth + 1))

    print(
        f"\nProbing {len(links_to_probe)} unique links "
        f"with {workers} workers..."
    )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        # executor.map preserves input order, so URL/result pairs stay aligned.
        probe_iter = executor.map(
            lambda url: probe_link(url, timeout), links_to_probe
        )
        # tqdm wraps the iterator to display a real-time progress bar.
        progress = tqdm(
            zip(links_to_probe, probe_iter),
            total=len(links_to_probe),
            desc="Probing links",
            unit="link",
        )
        for full_url, result in progress:
            record_result(
                results, full_url, result,
                link_types[full_url], source_pages[full_url],
            )

    crawl_stats = {
        "base_url": base_url,
        "pages_crawled": len(visited_pages),
        "unique_links_parsed": len(checked_links),
        "problem_links_found": len(results),
    }
    if output_format in {"csv", "both", "all"}:
        save_to_csv(results, crawl_stats)
    if output_format in {"json", "both", "all"}:
        save_to_json(results, crawl_stats)
    if output_format in {"html", "all"}:
        save_to_html(results, crawl_stats)


def save_to_csv(problem_links, crawl_stats):
    """Write a structured CSV report with summary, counts, and details."""
    filename = "broken_links_report.csv"
    keys = [
        "url", "status", "classification", "method",
        "redirect_hops", "redirect_chain",
        "link_type", "source_page",
    ]

    type_counts = Counter(link["classification"] for link in problem_links)
    status_counts = Counter(str(link["status"]) for link in problem_links)

    # Split into internal and external for separate report sections.
    internal_links = [
        link for link in problem_links if link["link_type"] == "internal"
    ]
    external_links = [
        link for link in problem_links if link["link_type"] == "external"
    ]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        writer.writerow(["HTTP Hound Crawl Summary"])
        writer.writerow(["base_url", crawl_stats["base_url"]])
        writer.writerow(["pages_crawled", crawl_stats["pages_crawled"]])
        writer.writerow(
            ["unique_links_parsed", crawl_stats["unique_links_parsed"]]
        )
        writer.writerow(
            ["problem_links_found", crawl_stats["problem_links_found"]]
        )
        writer.writerow([])

        writer.writerow(["Problem Links by Type"])
        writer.writerow(["classification", "count"])
        for classification, count in sorted(type_counts.items()):
            writer.writerow([classification, count])
        writer.writerow([])

        writer.writerow(["Problem Links by Status"])
        writer.writerow(["status", "count"])
        for status, count in sorted(
            status_counts.items(), key=lambda item: item[0]
        ):
            writer.writerow([status, count])
        writer.writerow([])

        writer.writerow(["Internal Problem Links"])
        writer.writerow(keys)
        for row in internal_links:
            writer.writerow([row[key] for key in keys])
        writer.writerow([])

        writer.writerow(["External Problem Links"])
        writer.writerow(keys)
        for row in external_links:
            writer.writerow([row[key] for key in keys])

    print(
        f"\n--- CSV report generated: {filename} "
        f"({crawl_stats['problem_links_found']} issues found out of "
        f"{crawl_stats['unique_links_parsed']} parsed links) ---"
    )


def save_to_json(problem_links, crawl_stats):
    """Write a structured JSON report for programmatic consumption."""
    filename = "broken_links_report.json"

    type_counts = Counter(link["classification"] for link in problem_links)
    status_counts = Counter(str(link["status"]) for link in problem_links)

    # Split problem links by origin for easier downstream processing.
    internal = [
        link for link in problem_links if link["link_type"] == "internal"
    ]
    external = [
        link for link in problem_links if link["link_type"] == "external"
    ]

    report = {
        "crawl_summary": crawl_stats,
        "counts_by_type": dict(sorted(type_counts.items())),
        "counts_by_status": dict(sorted(status_counts.items())),
        "problem_links": {
            "internal": internal,
            "external": external,
        },
    }

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\n--- JSON report generated: {filename} ---")


def save_to_html(problem_links, crawl_stats):
    """Write a self-contained HTML report with styled, color-coded tables."""
    filename = "broken_links_report.html"

    type_counts = Counter(
        link["classification"] for link in problem_links
    )
    status_counts = Counter(
        str(link["status"]) for link in problem_links
    )
    internal_links = [
        link for link in problem_links
        if link["link_type"] == "internal"
    ]
    external_links = [
        link for link in problem_links
        if link["link_type"] == "external"
    ]

    # Row highlight class keyed by classification.
    _row_css = {
        "broken": "row-err",
        "failed": "row-err",
        "blocked": "row-warn",
        "redirect_chain": "row-info",
    }

    def _td(value):
        """Return an HTML-escaped <td> cell."""
        return f"          <td>{html_escape(str(value))}</td>"

    def _detail_rows(links):
        if not links:
            return (
                "        <tr>\n"
                "          <td colspan='8'>"
                "No issues found.</td>\n"
                "        </tr>"
            )
        rows = []
        for lnk in links:
            css = _row_css.get(lnk["classification"], "")
            cells = "\n".join([
                _td(lnk["url"]),
                _td(lnk["status"]),
                _td(lnk["classification"]),
                _td(lnk["method"]),
                _td(lnk["redirect_hops"]),
                _td(lnk["redirect_chain"]),
                _td(lnk["link_type"]),
                _td(lnk["source_page"]),
            ])
            rows.append(
                f'        <tr class="{css}">\n'
                f"{cells}\n"
                "        </tr>"
            )
        return "\n".join(rows)

    def _count_rows(counts):
        rows = []
        for k, v in sorted(counts.items()):
            rows.append(
                "        <tr>\n"
                f"          <td>{html_escape(k)}</td>\n"
                f"          <td>{v}</td>\n"
                "        </tr>"
            )
        return "\n".join(rows)

    def _card(label, value):
        label_safe = html_escape(str(label))
        return (
            '      <div class="stat-card">\n'
            f'        <span class="label">{label_safe}</span>\n'
            f'        <span class="value">{value}</span>\n'
            "      </div>"
        )

    base = html_escape(crawl_stats["base_url"])
    n_int = len(internal_links)
    n_ext = len(external_links)
    cards = "\n".join([
        _card("Pages Crawled", crawl_stats["pages_crawled"]),
        _card("Links Parsed", crawl_stats["unique_links_parsed"]),
        _card("Problems Found", crawl_stats["problem_links_found"]),
        _card("Internal Issues", n_int),
    ])
    th_detail = (
        "        <tr>\n"
        "          <th>URL</th>\n"
        "          <th>Status</th>\n"
        "          <th>Classification</th>\n"
        "          <th>Method</th>\n"
        "          <th>Hops</th>\n"
        "          <th>Redirect Chain</th>\n"
        "          <th>Type</th>\n"
        "          <th>Source Page</th>\n"
        "        </tr>"
    )

    # CSS as a plain string (not an f-string) so braces need no escaping.
    styles = (
        "      body {\n"
        "        font-family: 'Segoe UI', Arial, sans-serif;\n"
        "        background: #f5f7fa;\n"
        "        color: #222;\n"
        "        margin: 0;\n"
        "        padding: 24px;\n"
        "      }\n"
        "      h1 { color: #1a1a2e; }\n"
        "      h2 {\n"
        "        color: #16213e;\n"
        "        border-bottom: 2px solid #dde;\n"
        "        padding-bottom: 6px;\n"
        "        margin-top: 36px;\n"
        "      }\n"
        "      .summary {\n"
        "        display: grid;\n"
        "        grid-template-columns: repeat(4, 1fr);\n"
        "        gap: 16px;\n"
        "        margin: 20px 0;\n"
        "      }\n"
        "      .stat-card {\n"
        "        background: #fff;\n"
        "        border-radius: 8px;\n"
        "        padding: 18px 24px;\n"
        "        box-shadow: 0 1px 4px rgba(0, 0, 0, 0.1);\n"
        "      }\n"
        "      .stat-card .label {\n"
        "        display: block;\n"
        "        font-size: 0.75rem;\n"
        "        color: #666;\n"
        "        text-transform: uppercase;\n"
        "        letter-spacing: 0.05em;\n"
        "      }\n"
        "      .stat-card .value {\n"
        "        display: block;\n"
        "        font-size: 2rem;\n"
        "        font-weight: 700;\n"
        "        color: #1a1a2e;\n"
        "        margin-top: 4px;\n"
        "      }\n"
        "      table {\n"
        "        width: 100%;\n"
        "        border-collapse: collapse;\n"
        "        background: #fff;\n"
        "        border-radius: 8px;\n"
        "        overflow: hidden;\n"
        "        box-shadow: 0 1px 4px rgba(0, 0, 0, 0.1);\n"
        "        margin-bottom: 28px;\n"
        "      }\n"
        "      th {\n"
        "        background: #1a1a2e;\n"
        "        color: #fff;\n"
        "        padding: 10px 14px;\n"
        "        text-align: left;\n"
        "        font-size: 0.82rem;\n"
        "      }\n"
        "      td {\n"
        "        padding: 8px 14px;\n"
        "        border-bottom: 1px solid #eee;\n"
        "        font-size: 0.82rem;\n"
        "        word-break: break-all;\n"
        "      }\n"
        "      tr:last-child td { border-bottom: none; }\n"
        "      tr.row-err td { background: #fff0f0; }\n"
        "      tr.row-warn td { background: #fff8ec; }\n"
        "      tr.row-info td { background: #fffbe6; }"
    )

    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="UTF-8" />\n'
        f"    <title>HTTP Hound \u2014 {base}</title>\n"
        "    <style>\n"
        f"{styles}\n"
        "    </style>\n"
        "  </head>\n"
        "  <body>\n"
        "    <h1>HTTP Hound Report</h1>\n"
        f"    <p>Crawled: <strong>{base}</strong></p>\n"
        '    <div class="summary">\n'
        f"{cards}\n"
        "    </div>\n"
        "    <h2>Problem Links by Type</h2>\n"
        "    <table>\n"
        "      <thead>\n"
        "        <tr>\n"
        "          <th>Classification</th>\n"
        "          <th>Count</th>\n"
        "        </tr>\n"
        "      </thead>\n"
        "      <tbody>\n"
        f"{_count_rows(type_counts)}\n"
        "      </tbody>\n"
        "    </table>\n"
        "    <h2>Problem Links by Status</h2>\n"
        "    <table>\n"
        "      <thead>\n"
        "        <tr>\n"
        "          <th>HTTP Status</th>\n"
        "          <th>Count</th>\n"
        "        </tr>\n"
        "      </thead>\n"
        "      <tbody>\n"
        f"{_count_rows(status_counts)}\n"
        "      </tbody>\n"
        "    </table>\n"
        f"    <h2>Internal Problem Links ({n_int})</h2>\n"
        "    <table>\n"
        "      <thead>\n"
        f"{th_detail}\n"
        "      </thead>\n"
        "      <tbody>\n"
        f"{_detail_rows(internal_links)}\n"
        "      </tbody>\n"
        "    </table>\n"
        f"    <h2>External Problem Links ({n_ext})</h2>\n"
        "    <table>\n"
        "      <thead>\n"
        f"{th_detail}\n"
        "      </thead>\n"
        "      <tbody>\n"
        f"{_detail_rows(external_links)}\n"
        "      </tbody>\n"
        "    </table>\n"
        "  </body>\n"
        "</html>"
    )

    with open(filename, "w", encoding="utf-8") as f:
        f.write(doc)

    print(f"\n--- HTML report generated: {filename} ---")


def parse_args():
    parser = argparse.ArgumentParser(
        description="HTTP Hound — crawl a site and report broken links."
    )
    parser.add_argument("url", help="Base URL of the site to crawl")
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        metavar="N",
        help="Number of concurrent link-probe workers (default: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        metavar="S",
        help="Request timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        metavar="D",
        help="Maximum crawl depth from the base URL (default: unlimited)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        metavar="S",
        help="Seconds to wait between page fetches (default: 0)",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json", "html", "both", "all"],
        default="csv",
        metavar="FORMAT",
        help=(
            "Output format: csv, json, html, "
            "both (csv+json), or all (default: csv)"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    check_links(
        args.url,
        workers=args.workers,
        timeout=args.timeout,
        max_depth=args.max_depth,
        delay=args.delay,
        output_format=args.format,
    )