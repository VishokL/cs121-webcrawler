import atexit
import json
import os
import re
from collections import Counter, defaultdict
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup


# ============================================================
# Configuration
# ============================================================

# The four domains we are allowed to crawl, per the assignment spec.
# Any hostname that equals one of these or has it as a dotted suffix
# (e.g. "vision.ics.uci.edu" ends with ".ics.uci.edu") is in scope.
ALLOWED_DOMAINS = (
    "ics.uci.edu",
    "cs.uci.edu",
    "informatics.uci.edu",
    "stat.uci.edu",
)

# Skip pages whose raw byte length is outside this range. The lower bound
# guards against "dead" 200-OK pages with no real content; the upper bound
# guards against very large files with low information value.
MIN_PAGE_BYTES = 500
MAX_PAGE_BYTES = 5_000_000

# Heuristics for trap detection in is_valid.
MAX_URL_LENGTH = 300
MAX_PATH_SEGMENTS = 8
MAX_PATH_SEGMENT_REPEATS = 2

# Persisted state and report file locations (relative to project root).
STOP_WORDS_FILE = "stop_words.txt"
ANALYTICS_FILE = "analytics.json"
REPORT_FILE = "report.txt"

# Flush analytics to disk every N newly-crawled pages.
SAVE_EVERY_N_PAGES = 25


# ============================================================
# Stop words
# ============================================================

def load_stop_words(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as stop_words_file:
        return {line.strip().lower() for line in stop_words_file if line.strip()}


STOP_WORDS = load_stop_words(STOP_WORDS_FILE)


# ============================================================
# Analytics state (persisted across crawler runs)
# ============================================================

def _empty_analytics():
    return {
        "unique_urls": set(),
        "longest_page_url": "",
        "longest_page_word_count": 0,
        "word_counts": Counter(),
        "subdomain_pages": defaultdict(set),
    }


def load_analytics(path):
    if not os.path.exists(path):
        return _empty_analytics()
    try:
        with open(path, encoding="utf-8") as analytics_file:
            saved_state = json.load(analytics_file)
    except (json.JSONDecodeError, OSError):
        return _empty_analytics()
    return {
        "unique_urls": set(saved_state.get("unique_urls", [])),
        "longest_page_url": saved_state.get("longest_page_url", ""),
        "longest_page_word_count": saved_state.get("longest_page_word_count", 0),
        "word_counts": Counter(saved_state.get("word_counts", {})),
        "subdomain_pages": defaultdict(
            set,
            {
                hostname: set(urls)
                for hostname, urls in saved_state.get("subdomain_pages", {}).items()
            },
        ),
    }


analytics = load_analytics(ANALYTICS_FILE)
_pages_since_last_save = 0


def save_analytics(path=ANALYTICS_FILE):
    snapshot = {
        "unique_urls": sorted(analytics["unique_urls"]),
        "longest_page_url": analytics["longest_page_url"],
        "longest_page_word_count": analytics["longest_page_word_count"],
        "word_counts": dict(analytics["word_counts"]),
        "subdomain_pages": {
            hostname: sorted(urls)
            for hostname, urls in analytics["subdomain_pages"].items()
        },
    }
    with open(path, "w", encoding="utf-8") as analytics_file:
        json.dump(snapshot, analytics_file, indent=2)


atexit.register(save_analytics)


def generate_report(path=REPORT_FILE):
    """Write answers to the four report questions to a text file."""
    lines = []
    lines.append(
        f"1. Unique pages found: {len(analytics['unique_urls'])}\n\n"
    )
    lines.append(
        "2. Longest page (by word count): "
        f"{analytics['longest_page_url']} "
        f"({analytics['longest_page_word_count']} words)\n\n"
    )

    lines.append("3. 50 most common words (word, count):\n")
    for word, count in analytics["word_counts"].most_common(50):
        lines.append(f"{word}, {count}\n")
    lines.append("\n")

    lines.append("4. Subdomains under uci.edu (subdomain, unique pages):\n")
    for hostname in sorted(analytics["subdomain_pages"]):
        page_count = len(analytics["subdomain_pages"][hostname])
        lines.append(f"{hostname}, {page_count}\n")

    with open(path, "w", encoding="utf-8") as report_file:
        report_file.writelines(lines)


# ============================================================
# Required crawler interface
# ============================================================

def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]


def extract_next_links(url, resp):
    # url: the URL that was used to get the page
    # resp.url: the actual url of the page
    # resp.status: the status code returned by the server (200 is OK)
    # resp.error: when status is not 200, this contains the error
    # resp.raw_response: the underlying response object
    #     resp.raw_response.url: the url, again
    #     resp.raw_response.content: the raw HTML bytes of the page
    if not _is_successful_response(resp):
        return []

    page_bytes = resp.raw_response.content
    if not _has_acceptable_size(page_bytes):
        return []

    base_url = resp.raw_response.url or resp.url or url

    try:
        soup = BeautifulSoup(page_bytes, "html.parser")
    except Exception:
        return []

    record_page_analytics(base_url, soup)

    extracted_links = []
    for anchor_tag in soup.find_all("a", href=True):
        href_value = anchor_tag["href"].strip()
        if not href_value:
            continue
        absolute_url = urljoin(base_url, href_value)
        defragmented_url, _fragment = urldefrag(absolute_url)
        if defragmented_url:
            extracted_links.append(defragmented_url)

    return extracted_links


def is_valid(url):
    # Decide whether to crawl this url or not.
    # Returns True if the url should be added to the frontier, False otherwise.
    try:
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"}:
            return False

        hostname = (parsed_url.hostname or "").lower()
        if not _is_in_allowed_domains(hostname):
            return False

        if _has_disallowed_extension(parsed_url.path):
            return False

        if len(url) > MAX_URL_LENGTH:
            return False
        if _has_too_many_path_segments(parsed_url.path):
            return False
        if _has_repeated_path_segments(parsed_url.path):
            return False
        if _looks_like_calendar_trap(parsed_url):
            return False

        return True

    except TypeError:
        print("TypeError for ", parsed_url)
        raise


# ============================================================
# Helper functions
# ============================================================

def _is_successful_response(resp):
    if resp.status != 200:
        return False
    if resp.raw_response is None:
        return False
    if not resp.raw_response.content:
        return False
    return True


def _has_acceptable_size(page_bytes):
    return MIN_PAGE_BYTES <= len(page_bytes) <= MAX_PAGE_BYTES


def _is_in_allowed_domains(hostname):
    if not hostname:
        return False
    for domain in ALLOWED_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def _has_disallowed_extension(path):
    return bool(
        re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            r"|png|tiff?|mid|mp2|mp3|mp4"
            r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            r"|epub|dll|cnf|tgz|sha1"
            r"|thmx|mso|arff|rtf|jar|csv"
            r"|rm|smil|wmv|swf|wma|zip|rar|gz)$",
            path.lower(),
        )
    )


def _has_too_many_path_segments(path):
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) > MAX_PATH_SEGMENTS


def _has_repeated_path_segments(path):
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return False
    segment_counts = Counter(segments)
    return max(segment_counts.values()) > MAX_PATH_SEGMENT_REPEATS


def _looks_like_calendar_trap(parsed_url):
    # Many academic event pages link to neighboring days indefinitely.
    path = parsed_url.path
    query = parsed_url.query.lower()
    if re.search(r"/\d{4}-\d{2}-\d{2}(/|$)", path):
        return True
    if re.search(r"/\d{4}/\d{1,2}/\d{1,2}(/|$)", path):
        return True
    if re.search(r"\b(year|month|day|date|when)=\d", query):
        return True
    return False


def record_page_analytics(page_url, soup):
    global _pages_since_last_save

    defragmented_url, _fragment = urldefrag(page_url)
    if not defragmented_url or defragmented_url in analytics["unique_urls"]:
        return
    analytics["unique_urls"].add(defragmented_url)

    page_text = soup.get_text(separator=" ")
    page_words = tokenize(page_text)

    if len(page_words) > analytics["longest_page_word_count"]:
        analytics["longest_page_word_count"] = len(page_words)
        analytics["longest_page_url"] = defragmented_url

    for word in page_words:
        if word not in STOP_WORDS:
            analytics["word_counts"][word] += 1

    hostname = (urlparse(defragmented_url).hostname or "").lower()
    if hostname == "uci.edu" or hostname.endswith(".uci.edu"):
        analytics["subdomain_pages"][hostname].add(defragmented_url)

    _pages_since_last_save += 1
    if _pages_since_last_save >= SAVE_EVERY_N_PAGES:
        save_analytics()
        _pages_since_last_save = 0


def tokenize(text):
    return [match.lower() for match in re.findall(r"[a-zA-Z]+", text)]


# Allow `python3 scraper.py` to generate the report from saved analytics.
if __name__ == "__main__":
    generate_report()
    print(f"Report written to {REPORT_FILE}")
