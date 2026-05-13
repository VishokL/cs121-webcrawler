import atexit
import hashlib
import io
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
#
# These rules describe what makes a URL look like a "low information value"
# page in observable terms (URL shape and observed crawl behavior), rather
# than naming specific sites. The thresholds were chosen by inspecting
# Logs/Worker.log and confirming that legitimate UCI pages stay well below
# them while combinatorial trap pages exceed them rapidly.
MAX_URL_LENGTH = 300
MAX_PATH_SEGMENTS = 8
MAX_PATH_SEGMENT_REPEATS = 2

# Rule B: pages with many query parameters are usually dynamically-generated
# filter/sort/paginate views with little marginal content.
MAX_QUERY_PARAMS = 3

# Rule A: once we have crawled this many distinct query-string variants of a
# single (host, path), assume any further variant is part of a combinatorial
# trap (DokuWiki media manager, calendar pickers, faceted search, etc.).
# Legitimate UCI content pages rarely produce more than a handful of distinct
# query-string variants of the same path, so 10 leaves comfortable headroom
# while cutting off combinatorial traps quickly.
PATH_HIT_LIMIT = 10

# Rule C: ratio of visible-text bytes to total HTML bytes below which a page
# is treated as low-information (UI / navigation chrome rather than content).
# When this is hit we still count the URL as visited but do not propagate
# its outbound links.
MIN_TEXT_DENSITY = 0.05

# Rule E: after tokenization, pages with fewer than this many "meaningful"
# tokens (alphanumeric runs of length >= 2 from the HW1 tokenizer) are thin
# caption/list pages — not enough prose to justify crawling their out-links.
MIN_CONTENT_TOKENS = 20

# Minimum token length when counting words for the report (longest page,
# word frequencies). Single-character runs are not English words.
MIN_TOKEN_LEN_FOR_ANALYTICS = 2

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


# --- Tokenizer: same algorithm as vishokl_hw1/PartA.py (Assignment 1); reads the
# string in 4096-char chunks via StringIO instead of a file path. @vishokl_hw1
def tokenize(text):
    tokens = []
    current = []

    f = io.StringIO(text)
    while True:
        chunk = f.read(4096)
        if not chunk:
            break
        for char in chunk:
            if char.isascii() and char.isalnum():
                current.append(char.lower())
            else:
                if len(current) > 0:
                    tokens.append("".join(current))
                    current = []

    if len(current) > 0:
        tokens.append("".join(current))

    return tokens


def _meaningful_tokens_from_text(text):
    # For word-count / longest-page report stats: drop 1-character "words".
    return [t for t in tokenize(text) if len(t) >= MIN_TOKEN_LEN_FOR_ANALYTICS]


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
        # Rule A state: how many times we've crawled each (host, path) regardless
        # of query string. Key format: "<hostname><path>" (e.g. "wiki.ics.uci.edu/doku.php/foo").
        "path_hits": defaultdict(int),
        # Rule D state: fingerprints of normalized page text we have already seen,
        # used to skip propagating links from exact-duplicate pages.
        "content_hashes": set(),
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
        "path_hits": defaultdict(int, saved_state.get("path_hits", {})),
        "content_hashes": set(saved_state.get("content_hashes", [])),
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
        "path_hits": dict(analytics["path_hits"]),
        "content_hashes": sorted(analytics["content_hashes"]),
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

    page_text = soup.get_text(separator=" ")

    # Rule C: pages with very little visible text relative to their HTML
    # payload are UI / navigation pages, not content. Rule D: pages whose
    # normalized text matches a previously-seen page are exact duplicates.
    # Rule E: pages with too few meaningful words are thin caption/list pages.
    # In either case we still want to count the URL as visited (so the
    # unique-page count reflects reality), but we should not propagate the
    # outbound links — that's what fuels combinatorial traps.
    is_content_bearing = _has_sufficient_text_density(page_text, page_bytes)
    meaningful_word_tokens = _meaningful_tokens_from_text(page_text)
    if is_content_bearing and len(meaningful_word_tokens) < MIN_CONTENT_TOKENS:
        is_content_bearing = False
    if is_content_bearing:
        content_hash = _content_fingerprint(page_text)
        if content_hash in analytics["content_hashes"]:
            is_content_bearing = False
        else:
            analytics["content_hashes"].add(content_hash)

    record_page_analytics(base_url, meaningful_word_tokens, is_content_bearing)

    if not is_content_bearing:
        return []

    # Per-page same-(host, path) dedupe: from any single source page we emit
    # at most one URL per (host, path). If a page links to 50 query-string
    # variants of /doku.php/foo, we keep only the first — the others are by
    # definition alternate views of the same underlying page (Lecture 7
    # slide 40's "ever changing URLs" trap signature). This works in concert
    # with Rule A to cap a trap path's hit count near PATH_HIT_LIMIT instead
    # of (N source pages) × (~50 variants each).
    extracted_links = []
    seen_target_path_keys = set()
    for anchor_tag in soup.find_all("a", href=True):
        href_value = anchor_tag["href"].strip()
        if not href_value:
            continue
        # Some pages contain malformed anchors (e.g. href="http://[YOUR_IP]:8080/..."
        # placeholder text in tutorials). Python 3.10's urlparse validates
        # bracketed netlocs as IPv6 addresses and raises ValueError on text
        # like "YOUR_IP", which would otherwise kill the worker thread.
        try:
            absolute_url = urljoin(base_url, href_value)
            defragmented_url, _fragment = urldefrag(absolute_url)
            if not defragmented_url:
                continue
            parsed_target = urlparse(defragmented_url)
        except ValueError:
            continue
        target_path_key = (parsed_target.netloc, parsed_target.path)
        if target_path_key in seen_target_path_keys:
            continue
        seen_target_path_keys.add(target_path_key)
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

        if _looks_like_tilde_photo_gallery(hostname, parsed_url.path):
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
        if _is_non_html_export_query(parsed_url.query):
            return False
        if _has_too_many_query_params(parsed_url.query):
            return False
        if _path_hit_limit_reached(parsed_url, hostname):
            return False

        return True

    except TypeError:
        print("TypeError for ", parsed_url)
        raise
    except ValueError:
        # Python 3.10's urlparse raises ValueError on URLs with malformed
        # bracketed netlocs such as http://[YOUR_IP]:8080/... Treat them as
        # uncrawlable rather than letting the exception propagate.
        return False


# ============================================================
# Helper functions
# ============================================================


def _response_content_type_is_html(raw_response):
    # Only treat body as HTML when the server says so. Stops .ppsx, PDFs,
    # octet-stream binaries, etc. from polluting BeautifulSoup and word stats.
    headers = getattr(raw_response, "headers", None)
    if headers is None:
        return True
    content_type = ""
    if hasattr(headers, "get"):
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    main = str(content_type).split(";")[0].strip().lower()
    if not main:
        return True
    return main in ("text/html", "application/xhtml+xml")


def _looks_like_tilde_photo_gallery(hostname, path):
    # Personal sites often expose thousands of near-identical photo-caption
    # HTML pages under /~user/pix/... — low marginal value for textual IR.
    hostname = (hostname or "").lower()
    path_lower = path.lower()
    if not any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in ALLOWED_DOMAINS
    ):
        return False
    return bool(re.match(r"/~[^/]+/pix(/|$)", path_lower))


def _is_successful_response(resp):
    if resp.status != 200:
        return False
    if resp.raw_response is None:
        return False
    if not resp.raw_response.content:
        return False
    if not _response_content_type_is_html(resp.raw_response):
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
            r"|ps|eps|tex|ppt|pptx|pps|ppsx|ppsm|pptm"
            r"|doc|docx|xls|xlsx|xlsm|names"
            r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            r"|epub|dll|cnf|tgz|sha1"
            r"|thmx|mso|arff|rtf|jar|war|ear|apk|csv"
            r"|sql|sqlite|odb|accdb|mdb"
            r"|odp|ods|odt|odg"
            r"|key|pages|numbers"
            r"|ipynb|nb|wasm|ipa|pkg|deb|rpm|xz|lzma|zst"
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
    # Per CS121 Discussion 3 ("Keep Your Crawler Away From: Calendars, things
    # that end with a date, things that end with events-week..."), this
    # function looks for URL shapes that are almost always calendar/event
    # navigation UIs rather than content. Each branch is a distinct shape
    # we've seen or that the discussion called out.
    path = parsed_url.path
    query = parsed_url.query.lower()

    # Full ISO-style dates as path components: /2024-01-15/... or .../2024-01-15
    if re.search(r"/\d{4}-\d{2}-\d{2}(/|$)", path):
        return True
    # Slash-separated dates as path components: /2024/01/15/... or .../2024/1/15
    if re.search(r"/\d{4}/\d{1,2}/\d{1,2}(/|$)", path):
        return True
    # Year-only or year-month archive paths (WordPress-style /2024/, /2024/01/).
    # The (/|$) anchor on the right keeps this from matching legitimate segments
    # like /research/2019-project-report/ where the year is embedded in text.
    if re.search(r"/(19|20)\d{2}(/\d{1,2})?(/|$)", path):
        return True
    # Event/calendar UI navigation pages: /events/week/..., /calendar/month/...
    if re.search(r"/(events?|calendar)/(week|day|month|year)\b", path):
        return True

    # Date-style query keys typically used to page through calendar views.
    if re.search(
        r"\b(year|month|day|date|when|eventdate|startdate|enddate|from|to|after|before)=\d",
        query,
    ):
        return True

    return False


def _is_non_html_export_query(query):
    # Reject URLs whose query string asks for a non-HTML representation of an
    # already-crawled page (DokuWiki ?do=export_pdf / export_xhtml, MediaWiki
    # ?action=raw / ?action=edit, generic ?format=pdf|xml|json, etc.).
    # These responses are either binary (PDF) — which causes BeautifulSoup to
    # emit "REPLACEMENT CHARACTER" decoding warnings — or non-content UI
    # views, and they never contribute new information beyond the canonical
    # page already in scope.
    if not query:
        return False
    return bool(
        re.search(
            r"\b("
            r"do=export_"
            r"|action=(raw|edit|history|info|delete|protect|purge)"
            r"|format=(pdf|xml|json|atom|rss|raw|csv)"
            r"|output=(pdf|xml|json|raw)"
            r")",
            query.lower(),
        )
    )


def _has_too_many_query_params(query):
    # Rule B: a URL with many query parameters is almost always a dynamically
    # generated view (filter/sort/paginate/faceted-search) rather than a unique
    # content page. We allow up to MAX_QUERY_PARAMS for normal use cases.
    if not query:
        return False
    param_count = sum(1 for pair in query.split("&") if pair)
    return param_count > MAX_QUERY_PARAMS


def _path_hit_limit_reached(parsed_url, hostname):
    # Rule A: combinatorial trap detection. If we have already crawled many
    # variants of this (host, path) under different query strings, refuse any
    # further variant. URLs without a query string are always allowed through,
    # so the canonical page itself is never blocked.
    if not parsed_url.query:
        return False
    path_key = _path_key(hostname, parsed_url.path)
    return analytics["path_hits"].get(path_key, 0) >= PATH_HIT_LIMIT


def _path_key(hostname, path):
    return f"{hostname}{path}"


def _has_sufficient_text_density(page_text, page_bytes):
    # Rule C: ratio of visible text bytes to total HTML bytes. Pages that are
    # mostly markup with little actual prose tend to be UI/action views.
    if not page_bytes:
        return False
    text_bytes = len(page_text.encode("utf-8", errors="ignore"))
    return text_bytes / len(page_bytes) >= MIN_TEXT_DENSITY


def _content_fingerprint(page_text):
    # Rule D: stable hash over normalized text so two pages that render the
    # same prose (modulo whitespace and casing) collide. Used to detect exact
    # near-duplicates such as DokuWiki action variants that all wrap the same
    # underlying article.
    normalized_text = " ".join(page_text.lower().split())
    return hashlib.md5(normalized_text.encode("utf-8", errors="ignore")).hexdigest()


def record_page_analytics(page_url, meaningful_word_tokens, is_content_bearing):
    global _pages_since_last_save

    defragmented_url, _fragment = urldefrag(page_url)
    if not defragmented_url:
        return

    parsed = urlparse(defragmented_url)
    hostname = (parsed.hostname or "").lower()

    # The per-path counter feeds Rule A. We bump it on every successful crawl
    # regardless of content quality, because the trap signal is the number of
    # distinct query variants we have already burned, not whether each variant
    # carried any text.
    analytics["path_hits"][_path_key(hostname, parsed.path)] += 1

    is_new_url = defragmented_url not in analytics["unique_urls"]
    if is_new_url:
        analytics["unique_urls"].add(defragmented_url)
        if hostname == "uci.edu" or hostname.endswith(".uci.edu"):
            analytics["subdomain_pages"][hostname].add(defragmented_url)

    # Only content-bearing pages contribute to the longest-page and word-count
    # analytics. UI / duplicate pages would otherwise drown the report in
    # boilerplate words ("edit", "preview", "namespace", "history", ...).
    if is_content_bearing and is_new_url:
        if len(meaningful_word_tokens) > analytics["longest_page_word_count"]:
            analytics["longest_page_word_count"] = len(meaningful_word_tokens)
            analytics["longest_page_url"] = defragmented_url
        for word in meaningful_word_tokens:
            if word not in STOP_WORDS:
                analytics["word_counts"][word] += 1

    _pages_since_last_save += 1
    if _pages_since_last_save >= SAVE_EVERY_N_PAGES:
        save_analytics()
        _pages_since_last_save = 0


# Allow `python3 scraper.py` to generate the report from saved analytics.
if __name__ == "__main__":
    generate_report()
    print(f"Report written to {REPORT_FILE}")
