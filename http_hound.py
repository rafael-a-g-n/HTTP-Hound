import csv
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def build_session():
    """Create a session that looks closer to a browser request."""
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    return session


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


def check_links(base_url):
    print(f"--- Starting crawl on: {base_url} ---")
    session = build_session()

    try:
        response = session.get(base_url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Could not access the base URL: {e}")
        return

    soup = BeautifulSoup(response.text, "html.parser")
    links = soup.find_all("a", href=True)

    results = []
    seen_links = set()

    for link in links:
        raw_url = link["href"]
        full_url = urljoin(base_url, raw_url)

        if full_url in seen_links or not full_url.startswith("http"):
            continue

        seen_links.add(full_url)

        result = probe_link(full_url, session)
        status = result["status"]
        classification = result["classification"]
        method = result["method"]
        final_url = result["final_url"]

        if classification in {"broken", "blocked", "failed"}:
            print(f"[{classification.upper()}] {status} via {method} - {full_url}")
            results.append(
                {
                    "url": full_url,
                    "status": status,
                    "classification": classification,
                    "method": method,
                    "final_url": final_url,
                }
            )
        else:
            print(f"[OK] {status} via {method} - {full_url}")

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
    target_site = "site to be tested"  # Replace with your target URL
    check_links(target_site)