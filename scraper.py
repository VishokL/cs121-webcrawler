import atexit
import json
import os
import re
from collections import Counter, defaultdict
from threading import RLock
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from utils import get_logger


# ============================================================
# Configuration
# ============================================================

# The four domains we are allowed to crawl, per the assignment spec.
ALLOWED_DOMAINS = (
    "ics.uci.edu",
    "cs.uci.edu",
    "informatics.uci.edu",
    "stat.uci.edu",
)

# Page-size guards (raw byte length).
MIN_PAGE_BYTES = 500
MAX_PAGE_BYTES = 5_000_000

# Trap heuristics applied in is_valid().
MAX_URL_LENGTH = 250
MAX_PATH_SEGMENTS = 8
MAX_PATH_SEGMENT_REPEATS = 2
MAX_QUERY_PARAMS = 6
MAX_PAGED_VALUE = 50

# When a single host crosses this page count we emit a warning so a human
# can decide whether it's a trap. Not a hard cap.
SOFT_HOST_PAGE_CAP = 5_000

# Persisted state and report file locations.
STOP_WORDS_FILE = "stop_words.txt"
ANALYTICS_FILE = "analytics.json"
REPORT_FILE = "report.txt"

# Flush analytics to disk every N newly-crawled pages.
SAVE_EVERY_N_PAGES = 25
# Log a scraper summary every N newly-crawled pages.
SUMMARY_EVERY_N_PAGES = 50


# ============================================================
# Logger and counters
# ============================================================

logger = get_logger("SCRAPER", "Scraper")

# Single re-entrant lock guards every mutation of the analytics dict and
# the diagnostic counters below. Re-entrant because record_page_analytics
# may call save_analytics / log_summary, which also acquire the lock.
_state_lock = RLock()

# Per-rejection-reason counters. Reset only on process restart.
filter_reason_counts = Counter()
page_skip_reason_counts = Counter()


# ============================================================
# Patterns
# ============================================================

DISALLOWED_EXTENSION_RE = re.compile(
    r".*\.(css|js|bmp|gif|jpe?g|ico"
    r"|png|tiff?|mid|mp2|mp3|mp4"
    r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
    r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
    r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
    r"|epub|dll|cnf|tgz|sha1"
    r"|thmx|mso|arff|rtf|jar|csv"
    r"|rm|smil|wmv|swf|wma|zip|rar|gz"
    r"|svg|woff2?|ttf|eot|otf"
    r"|ipynb|mat|class|odp|ods|odt|pps|ppsx"
    r"|sql|war|apk|deb|rpm|img)$",
    re.IGNORECASE,
)

CALENDAR_PATH_PATTERNS = (
    # "things that end with a date"
    re.compile(r"/\d{4}-\d{2}-\d{2}(/|$)"),
    re.compile(r"/\d{4}/\d{1,2}/\d{1,2}(/|$)"),
    re.compile(r"/\d{4}/\d{1,2}(/|$)"),
    # "events / cal / calendar / archive" sections keyed by year
    re.compile(r"/(events?|cal|calendar|archive)/\d{4}", re.IGNORECASE),
    # "things that end with events week" — also month/day/list/upcoming/past
    re.compile(
        r"/(events?|cal|calendar)/(week|month|day|list|upcoming|past)(/|$)",
        re.IGNORECASE,
    ),
)

CALENDAR_QUERY_KEYS = {
    "year", "month", "day", "date", "when",
    "tribe-bar-date", "eventdisplay", "ical",
    "from", "to", "start_date", "end_date",
}

# Query keys that signal a duplicate/auxiliary view of a page that the
# crawler will already see through some other (canonical) link.
DUPLICATE_VIEW_QUERY_KEYS = {
    "replytocom",     # WordPress comment-reply links — infinite
    "share",
    "like_comment",
    "action",         # MediaWiki edit/history/diff actions
    "diff",
    "oldid",
    "do",             # DokuWiki actions (login/edit/diff/...)
    "rev",            # DokuWiki revision history — creates infinite old-version URLs
    "idx",            # DokuWiki namespace index pages
    "printable",
    "format",         # alternative-format duplicates (json/xml/atom)
    "attachment_id",
    "preview",
    "print",
    "redirect_to",
    "returnurl",
    "sessionid",
    "sid",
    "phpsessid",
    "outlook-ical",
    "ver",
}

# Substrings that mean "this is an admin / auth / feed page, not content."
SKIP_PATH_TOKENS = (
    "/login", "/logout", "/signin", "/signup", "/register",
    "/wp-admin", "/wp-login", "/xmlrpc",
    "/feed", "/rss", "/atom", "/trackback",
    "/zip-attachment/",  # DokuWiki binary attachment downloads
)

# GitLab/Gitea/cgit views with effectively-infinite hash- or branch-based URLs.
GIT_VIEW_PATTERNS = (
    re.compile(r"/-/(commit|blob|tree|raw|blame|compare)/", re.IGNORECASE),
    re.compile(r"/commit/[0-9a-f]{7,}", re.IGNORECASE),
    re.compile(r"/raw/[^/]+/", re.IGNORECASE),
)


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
# URL canonicalization
# ============================================================

def canonicalize_url(url):
    """Return a canonical form of a URL for dedup and frontier consistency.

    Lowercases scheme/host, strips default ports, collapses duplicate slashes,
    removes trailing slash from non-root paths, and drops the fragment.
    Returns "" if the URL cannot be parsed.
    """
    if not url:
        return ""
    try:
        defragged, _ = urldefrag(url.strip())
        parsed = urlparse(defragged)
    except (TypeError, ValueError):
        return ""
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = re.sub(r"/+", "/", parsed.path or "")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


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
            saved = json.load(analytics_file)
    except (json.JSONDecodeError, OSError):
        return _empty_analytics()

    # Re-canonicalize on load so an older non-canonical analytics file dedupes.
    canonical_urls = set()
    for raw in saved.get("unique_urls", []):
        canonical = canonicalize_url(raw)
        if canonical:
            canonical_urls.add(canonical)

    subdomain_pages = defaultdict(set)
    for hostname, urls in saved.get("subdomain_pages", {}).items():
        host = hostname.lower()
        for raw in urls:
            canonical = canonicalize_url(raw)
            if canonical:
                subdomain_pages[host].add(canonical)

    return {
        "unique_urls": canonical_urls,
        "longest_page_url": saved.get("longest_page_url", ""),
        "longest_page_word_count": saved.get("longest_page_word_count", 0),
        "word_counts": Counter(saved.get("word_counts", {})),
        "subdomain_pages": subdomain_pages,
    }


analytics = load_analytics(ANALYTICS_FILE)
_pages_since_last_save = 0
_pages_since_last_summary = 0


def save_analytics(path=ANALYTICS_FILE):
    # Build the snapshot under the lock so concurrent writers can't mutate
    # the dicts/sets/Counters mid-serialization. The file write itself
    # happens outside the lock since it doesn't touch shared state.
    with _state_lock:
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
    lines.append(f"1. Unique pages found: {len(analytics['unique_urls'])}\n\n")
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
    if not _is_successful_response(resp):
        return []

    page_bytes = resp.raw_response.content
    if not _has_acceptable_size(page_bytes):
        _bump_page_skip("too_small" if len(page_bytes) < MIN_PAGE_BYTES else "too_large")
        return []

    raw_base = resp.raw_response.url or resp.url or url
    base_url = canonicalize_url(raw_base) or raw_base

    try:
        soup = BeautifulSoup(page_bytes, "html.parser")
    except Exception as exc:
        logger.warning(f"BeautifulSoup failed for {raw_base}: {exc}")
        _bump_page_skip("bs4_error")
        return []

    record_page_analytics(base_url, soup)

    seen_on_page = set()
    extracted = []
    anchor_count = 0
    for anchor in soup.find_all("a", href=True):
        anchor_count += 1
        href = anchor["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(raw_base, href)
        canonical = canonicalize_url(absolute)
        if not canonical or canonical in seen_on_page:
            continue
        seen_on_page.add(canonical)
        extracted.append(canonical)

    return extracted


def is_valid(url):
    # Decide whether to crawl this url or not.
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return _reject("parse_failed")

    if parsed.scheme not in ("http", "https"):
        return _reject("scheme")

    hostname = (parsed.hostname or "").lower()
    if not _is_in_allowed_domains(hostname):
        return _reject("out_of_domain")

    if _has_disallowed_extension(parsed.path):
        return _reject("bad_extension")

    if len(url) > MAX_URL_LENGTH:
        return _reject("url_too_long")

    if _has_skip_path_token(parsed.path):
        return _reject("admin_or_feed_path")

    if _has_too_many_path_segments(parsed.path):
        return _reject("too_many_path_segments")

    if _has_repeated_path_segments(parsed.path):
        return _reject("repeated_path_segments")

    if _has_adjacent_repeated_segments(parsed.path):
        return _reject("adjacent_repeated_segments")

    if _is_calendar_trap(parsed):
        return _reject("calendar_trap")

    if _has_duplicate_view_query(parsed.query):
        return _reject("duplicate_view_query")

    if _has_too_many_query_params(parsed.query):
        return _reject("too_many_query_params")

    if _has_runaway_pagination(parsed.query):
        return _reject("runaway_pagination")

    if _is_git_view(parsed.path):
        return _reject("git_view")

    return True


def _reject(reason):
    with _state_lock:
        filter_reason_counts[reason] += 1
    return False


# ============================================================
# Helper functions
# ============================================================

def _is_successful_response(resp):
    if resp.status != 200:
        _bump_page_skip(f"status_{resp.status}")
        return False
    if resp.raw_response is None:
        _bump_page_skip("no_raw_response")
        return False
    if not resp.raw_response.content:
        _bump_page_skip("empty_content")
        return False
    return True


def _bump_page_skip(reason):
    with _state_lock:
        page_skip_reason_counts[reason] += 1


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
    return bool(DISALLOWED_EXTENSION_RE.match(path.lower()))


def _has_skip_path_token(path):
    path_lower = path.lower()
    return any(token in path_lower for token in SKIP_PATH_TOKENS)


def _has_too_many_path_segments(path):
    segments = [s for s in path.split("/") if s]
    return len(segments) > MAX_PATH_SEGMENTS


def _has_repeated_path_segments(path):
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    return max(Counter(segments).values()) > MAX_PATH_SEGMENT_REPEATS


def _has_adjacent_repeated_segments(path):
    # Catches /a/b/a/b/, /a/b/c/a/b/c/, ... where a doubling trap is forming.
    # Window starts at 2 to avoid false positives on /cs/cs101/ style paths.
    segments = [s for s in path.split("/") if s]
    n = len(segments)
    for start in range(n):
        max_window = (n - start) // 2
        for window in range(2, max_window + 1):
            left = segments[start:start + window]
            right = segments[start + window:start + 2 * window]
            if left == right:
                return True
    return False


def _is_calendar_trap(parsed):
    path = parsed.path
    for pattern in CALENDAR_PATH_PATTERNS:
        if pattern.search(path):
            return True
    return _query_has_any_key(parsed.query, CALENDAR_QUERY_KEYS)


def _has_duplicate_view_query(query):
    return _query_has_any_key(query, DUPLICATE_VIEW_QUERY_KEYS)


def _query_has_any_key(query, key_set):
    if not query:
        return False
    for pair in query.lower().split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0]
        if key in key_set:
            return True
    return False


def _has_too_many_query_params(query):
    if not query:
        return False
    return len([p for p in query.split("&") if p]) > MAX_QUERY_PARAMS


def _has_runaway_pagination(query):
    if not query:
        return False
    for pair in query.split("&"):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        if key.lower() in ("page", "paged", "start", "offset"):
            try:
                if int(value) > MAX_PAGED_VALUE:
                    return True
            except ValueError:
                continue
    return False


def _is_git_view(path):
    return any(pattern.search(path) for pattern in GIT_VIEW_PATTERNS)


def record_page_analytics(page_url, soup):
    global _pages_since_last_save, _pages_since_last_summary

    canonical = canonicalize_url(page_url)
    if not canonical:
        return
    # Cheap pre-check outside the lock; the authoritative dedup happens
    # again inside the critical section below.
    if canonical in analytics["unique_urls"]:
        return

    page_text = soup.get_text(separator=" ")
    page_words = tokenize(page_text)
    hostname = (urlparse(canonical).hostname or "").lower()

    do_save = False
    do_summary = False
    host_cap_warning = None
    with _state_lock:
        if canonical in analytics["unique_urls"]:
            return
        analytics["unique_urls"].add(canonical)

        if len(page_words) > analytics["longest_page_word_count"]:
            analytics["longest_page_word_count"] = len(page_words)
            analytics["longest_page_url"] = canonical

        for word in page_words:
            if word not in STOP_WORDS:
                analytics["word_counts"][word] += 1

        if hostname == "uci.edu" or hostname.endswith(".uci.edu"):
            analytics["subdomain_pages"][hostname].add(canonical)
            host_count = len(analytics["subdomain_pages"][hostname])
            if host_count and host_count % SOFT_HOST_PAGE_CAP == 0:
                host_cap_warning = (hostname, host_count)

        _pages_since_last_save += 1
        _pages_since_last_summary += 1
        if _pages_since_last_save >= SAVE_EVERY_N_PAGES:
            do_save = True
            _pages_since_last_save = 0
        if _pages_since_last_summary >= SUMMARY_EVERY_N_PAGES:
            do_summary = True
            _pages_since_last_summary = 0

    if host_cap_warning:
        hostname, host_count = host_cap_warning
        logger.warning(
            f"host_page_cap_hit host={hostname} pages={host_count} "
            "(check HOT_PREFIXES below for trap candidates)"
        )
    if do_save:
        save_analytics()
    if do_summary:
        log_summary()


def tokenize(text):
    # Keeps contractions like "don't" together so they match stop_words.txt.
    return [
        match.lower()
        for match in re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", text)
    ]


# ============================================================
# Periodic summary logging — single source of truth for monitoring.
# ============================================================

def log_summary():
    # Snapshot everything we need under the lock so concurrent writers can't
    # mutate the dicts mid-iteration. logger.info() then runs lock-free.
    with _state_lock:
        unique = len(analytics["unique_urls"])
        longest_words = analytics["longest_page_word_count"]
        longest_short = analytics["longest_page_url"][:80]
        top_hosts = sorted(
            ((h, len(u)) for h, u in analytics["subdomain_pages"].items()),
            key=lambda pair: pair[1],
            reverse=True,
        )[:8]
        top_filter_reasons = filter_reason_counts.most_common(10)
        top_page_skips = page_skip_reason_counts.most_common(6)
        urls_snapshot = list(analytics["unique_urls"])

    hosts_str = ", ".join(f"{h}={n}" for h, n in top_hosts) or "(none)"
    logger.info(
        f"SUMMARY unique={unique} longest={longest_words}w@{longest_short} "
        f"top_hosts=[{hosts_str}]"
    )
    if top_filter_reasons:
        reasons = ", ".join(f"{r}={c}" for r, c in top_filter_reasons)
        logger.info(f"FILTER_REASONS {reasons}")
    if top_page_skips:
        skips = ", ".join(f"{r}={c}" for r, c in top_page_skips)
        logger.info(f"PAGE_SKIPS {skips}")

    hot_prefixes = _top_path_prefixes(urls_snapshot)
    if hot_prefixes:
        prefixes_str = ", ".join(f"{p}={n}" for p, n in hot_prefixes)
        logger.info(f"HOT_PREFIXES {prefixes_str}")


def _top_path_prefixes(urls, top_n=6, depth=3):
    counts = Counter()
    for u in urls:
        try:
            parsed = urlparse(u)
        except (TypeError, ValueError):
            continue
        host = parsed.netloc
        segs = [s for s in parsed.path.split("/") if s][:depth]
        prefix = f"{host}/{'/'.join(segs)}" if segs else host
        counts[prefix] += 1
    return counts.most_common(top_n)


# Allow `python3 scraper.py` to generate the report from saved analytics.
if __name__ == "__main__":
    generate_report()
    print(f"Report written to {REPORT_FILE}")
