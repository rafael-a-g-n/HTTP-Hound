from collections import deque
import csv
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


def normalize_url(url):
    """Remove fragments and normalize empty paths."""
    clean_url, _ = urldefrag(url)
    parsed_url = urlparse(clean_url)
    path = parsed_url.path or "/"

    return parsed_url._replace(path=path).geturl()


def is_internal_url(url, base_netloc):
    """Return True when a URL belongs to the same site being crawled."""
    parsed_url = urlparse(url)

    return parsed_url.scheme in {"http", "https"} and parsed_url.netloc == base_netloc


def fetch_page(url, session):
    """Fetch an HTML page for crawling."""
    response = session.get(url, timeout=10)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        return None

    return response


def extract_page_links(page_url, html):
    """Extract normalized HTTP links from a page."""
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for link in soup.find_all("a", href=True):
        full_url = normalize_url(urljoin(page_url, link["href"]))
        if full_url.startswith("http"):
            links.append(full_url)

    return links


def probe_link(url, session):
    """Check a link with HEAD first and retry with GET when needed."""
    try:
        head_response = session.head(url, allow_redirects=True, timeout=5)
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
        get_response = session.get(url, allow_redirects=True, timeout=8, stream=True)
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
            "status": head_status if head_status is not None else "FAILED TO CONNECT",
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


def check_links(base_url):
    base_url = normalize_url(base_url)
    print(f"--- Starting crawl on: {base_url} ---")

    session = build_session()
    base_netloc = urlparse(base_url).netloc
    pages_to_visit = deque([base_url])
    queued_pages = {base_url}
    visited_pages = set()
    checked_links = set()
    results = []

    while pages_to_visit:
        current_page = pages_to_visit.popleft()
        print(f"Crawling page: {current_page}")

        try:
            response = fetch_page(current_page, session)
        except requests.exceptions.RequestException as error:
            print(f"Could not crawl page: {current_page} ({error})")
            visited_pages.add(current_page)
            continue

        visited_pages.add(current_page)
        if response is None:
            continue

        for full_url in extract_page_links(current_page, response.text):
            if full_url not in checked_links:
                checked_links.add(full_url)
                result = probe_link(full_url, session)
                record_result(results, full_url, result)

            if not is_internal_url(full_url, base_netloc):
                continue

            if full_url in queued_pages or full_url in visited_pages:
                continue

            queued_pages.add(full_url)
            pages_to_visit.append(full_url)

    save_to_csv(results)


def save_to_csv(broken_links):
    filename = "broken_links_report.csv"
    keys = ["url", "status", "classification", "method", "final_url"]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(broken_links)

    print(f"\n--- Report generated: {filename} ({len(broken_links)} issues found) ---")


if __name__ == "__main__":
    target_site = "test link"  # Replace with your target URL
    check_links(target_site)