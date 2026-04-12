import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import csv
import time
import urllib.robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

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
        ("script", "src"), # JavaScript files
    ]

    urls = []
    for tag, attr in tag_attr_pairs:
        for element in soup.find_all(tag, attrs={attr: True}):
            full_url = normalize_url(urljoin(page_url, element[attr]))
            if full_url.startswith("http"):
                urls.append(full_url)

    return urls


def probe_link(url, timeout):
    """Check a link with HEAD first and retry with GET when needed."""
    try:
        head_response = requests.head(
            url,
            headers=REQUEST_HEADERS,
            allow_redirects=True,
            timeout=timeout,
        )
        head_status = head_response.status_code

        if head_status < 400:
            return {
                "status": head_status,
                "classification": "ok",
                "method": "HEAD",
                "final_url": head_response.url,
            }

        if head_status not in {403, 404, 405}:
            return {
                "status": head_status,
                "classification": "broken",
                "method": "HEAD",
                "final_url": head_response.url,
            }
    except requests.exceptions.RequestException:
        head_status = None

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
        final_url = get_response.url
        get_response.close()

        if get_status < 400:
            return {
                "status": get_status,
                "classification": "ok",
                "method": "GET",
                "final_url": final_url,
            }

        classification = "blocked" if get_status in {401, 403} else "broken"
        return {
            "status": get_status,
            "classification": classification,
            "method": "GET",
            "final_url": final_url,
        }
    except requests.exceptions.RequestException:
        return {
            "status": (
                head_status if head_status is not None else "FAILED TO CONNECT"
            ),
            "classification": "failed",
            "method": "GET" if head_status is not None else "NONE",
            "final_url": url,
        }


def record_result(results, url, result):
    """Store only links that still fail after probing."""
    classification = result["classification"]
    status = result["status"]
    method = result["method"]

    if classification in {"broken", "blocked", "failed"}:
        print(f"[{classification.upper()}] {status} via {method} - {url}")
        results.append(
            {
                "url": url,
                "status": status,
                "classification": classification,
                "method": method,
                "final_url": result["final_url"],
            }
        )
        return

    print(f"[OK] {status} via {method} - {url}")


def check_links(base_url, workers=10, timeout=10, max_depth=None, delay=0.0):
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
        for full_url, result in zip(
            links_to_probe,
            executor.map(lambda url: probe_link(url, timeout), links_to_probe),
        ):
            record_result(results, full_url, result)

    crawl_stats = {
        "base_url": base_url,
        "pages_crawled": len(visited_pages),
        "unique_links_parsed": len(checked_links),
        "problem_links_found": len(results),
    }
    save_to_csv(results, crawl_stats)


def save_to_csv(problem_links, crawl_stats):
    filename = "broken_links_report.csv"
    keys = ["url", "status", "classification", "method", "final_url"]

    type_counts = Counter(link["classification"] for link in problem_links)
    status_counts = Counter(str(link["status"]) for link in problem_links)

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

        writer.writerow(["Problem Link Details"])
        writer.writerow(keys)
        for row in problem_links:
            writer.writerow([row[key] for key in keys])

    print(
        f"\n--- Report generated: {filename} "
        f"({crawl_stats['problem_links_found']} issues found out of "
        f"{crawl_stats['unique_links_parsed']} parsed links) ---"
    )


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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    check_links(
        args.url,
        workers=args.workers,
        timeout=args.timeout,
        max_depth=args.max_depth,
        delay=args.delay,
    )