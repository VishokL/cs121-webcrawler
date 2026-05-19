import atexit
import calendar
import hashlib
import io
import json
import os
import re
from collections import Counter, defaultdict
from urllib.parse import urldefrag, urljoin, urlparse
from bs4 import BeautifulSoup

# < GLOBAL VARIABLES >

ALLOWED_DOMAINS = (
    "ics.uci.edu",
    "cs.uci.edu",
    "informatics.uci.edu",
    "stat.uci.edu",
)

# Filters for exceedingly small/large files.
MIN_PAGE_BYTES, MAX_PAGE_BYTES = 500, 5000000

# Trap detection thresholds for is_valid.
MAX_URL_LENGTH = 300
MAX_PATH_SEGMENTS = 8
MAX_PATH_SEGMENT_REPEATS = 2

# Caps query params before treating URL.
MAX_QUERY_PARAMS = 3

# Caps distinct query-string variants.
PATH_HIT_LIMIT = 10

# Caps sequential-named files per directory.
SEQUENTIAL_PAGE_LIMIT = 30

# Caps links to the same target directory.
MAX_SAME_DIR_PER_PAGE = 5

# Regex for sequential/numeric filenames.
_SEQUENTIAL_FILENAME_RE = re.compile(r"^(?:[a-z]{0,10})?(\d+)(\.html?)?$", re.IGNORECASE)

# Minimum text/HTML ratio for pages.
MIN_TEXT_DENSITY = 0.05

# Minimum meaningful word tokens for pages.
MIN_CONTENT_TOKENS = 18

# Minimum token length for report's word counts.
MIN_REPORT_WORD_LEN = 3

# Month names/abbrevs to exclude from word counts.
REPORT_MONTH_STOPWORDS = frozenset(calendar.month_abbr[i].lower() for i in range(1, 13)) | frozenset(calendar.month_name[i].lower() for i in range(1, 13)) | frozenset({"sept"})

# Persisted state and report file locations.
STOP_WORDS_FILE = "stop_words.txt"
ANALYTICS_FILE = "analytics.json"
REPORT_FILE = "report.txt"

# Flushes analytics to disk every N pages.
SAVE_EVERY_N_PAGES = 25


# < STOP WORDS >

# Loads stop words from disk.
def load_stop_words(path):
    if not os.path.exists(path):
        return set()

    with open(path, encoding="utf-8") as stop_words_file:
        return {line.strip().lower() for line in stop_words_file if line.strip()}


STOP_WORDS = load_stop_words(STOP_WORDS_FILE)


# < TOKENIZATION >

# Tokenizes text into alphanumeric ASCII tokens.
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


# Filters tokens for report eligibility.
def _meaningful_tokens_from_text(text):
    eligible = []
    for token in tokenize(text):
        if len(token) < MIN_REPORT_WORD_LEN:
            continue
        if not token.isalpha():
            continue
        if token in REPORT_MONTH_STOPWORDS:
            continue
        eligible.append(token)
    return eligible


# < ANALYTICS STATE >

# Returns empty analytics state.
def _empty_analytics():
    return {
        "unique_urls": set(),
        "longest_page_url": "",
        "longest_page_word_count": 0,
        "word_counts": Counter(),
        "subdomain_pages": defaultdict(set),
        "path_hits": defaultdict(int),
        "seq_dir_hits": defaultdict(int),
        "content_hashes": set(),
    }


# Loads persisted analytics state from disk.
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
        "subdomain_pages": defaultdict(set, {hostname: set(urls) for hostname, urls in saved_state.get("subdomain_pages", {}).items()}),
        "path_hits": defaultdict(int, saved_state.get("path_hits", {})),
        "seq_dir_hits": defaultdict(int, saved_state.get("seq_dir_hits", {})),
        "content_hashes": set(saved_state.get("content_hashes", [])),
    }


analytics = load_analytics(ANALYTICS_FILE)
_pages_since_last_save = 0


# Saves analytics state to disk.
def save_analytics(path=ANALYTICS_FILE):
    snapshot = {
        "unique_urls": sorted(analytics["unique_urls"]),
        "longest_page_url": analytics["longest_page_url"],
        "longest_page_word_count": analytics["longest_page_word_count"],
        "word_counts": dict(analytics["word_counts"]),
        "subdomain_pages": {hostname: sorted(urls) for hostname, urls in analytics["subdomain_pages"].items()},
        "path_hits": dict(analytics["path_hits"]),
        "seq_dir_hits": dict(analytics["seq_dir_hits"]),
        "content_hashes": sorted(analytics["content_hashes"]),
    }
    with open(path, "w", encoding="utf-8") as analytics_file:
        json.dump(snapshot, analytics_file, indent=2)


atexit.register(save_analytics)


# Writes report answers to disk.
def generate_report(path=REPORT_FILE):
    lines = []

    lines.append(f"1. Unique pages found: {len(analytics['unique_urls'])}\n\n")
    lines.append(f"2. Longest page (by word count): {analytics['longest_page_url']} ({analytics['longest_page_word_count']} words)\n\n")

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


# < REQUIRED CRAWLER INTERFACE >

def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]


def extract_next_links(url, resp):
    # Implementation required.
    # url: the URL that was used to get the page
    # resp.url: the actual url of the page
    # resp.status: the status code returned by the server. 200 is OK, you got the page. Other numbers mean that there was some kind of problem.
    # resp.error: when status is not 200, you can check the error here, if needed.
    # resp.raw_response: this is where the page actually is. More specifically, the raw_response has two parts:
    #         resp.raw_response.url: the url, again
    #         resp.raw_response.content: the content of the page!
    # Return a list with the hyperlinks (as strings) scrapped from resp.raw_response.content
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

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    page_text = soup.get_text(separator=" ")

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

    extracted_links = []
    seen_target_path_keys = set()
    target_dir_counts = Counter()
    for anchor_tag in soup.find_all("a", href=True):
        href_value = anchor_tag["href"].strip()
        if not href_value:
            continue

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

        if _is_sequential_filename(parsed_target.path):
            target_hostname = (parsed_target.hostname or "").lower()
            target_dir_key = _dir_key(target_hostname, parsed_target.path)
            if target_dir_counts[target_dir_key] >= MAX_SAME_DIR_PER_PAGE:
                continue

            target_dir_counts[target_dir_key] += 1

        extracted_links.append(defragmented_url)

    return extracted_links


def is_valid(url):
    # Decide whether to crawl this url or not. 
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.
    try:
        parsed = urlparse(url)
        if parsed.scheme not in set(["http", "https"]):
            return False

        hostname = (parsed.hostname or "").lower()
        if not _is_in_allowed_domains(hostname):
            return False

        if _has_disallowed_extension(parsed.path):
            return False

        if _is_pagination_archive(parsed.path):
            return False

        if _is_low_info_path(parsed.path):
            return False

        if len(url) > MAX_URL_LENGTH:
            return False
        if _has_too_many_path_segments(parsed.path):
            return False
        if _has_repeated_path_segments(parsed.path):
            return False
        if _looks_like_calendar_trap(parsed):
            return False
        if _is_non_html_export_query(parsed.query):
            return False
        if _has_too_many_query_params(parsed.query):
            return False
        if _path_hit_limit_reached(parsed, hostname):
            return False
        if _seq_dir_limit_reached(parsed, hostname):
            return False

        if _is_sequential_filename(parsed.path):
            analytics["seq_dir_hits"][_dir_key(hostname, parsed.path)] += 1

        if parsed.query:
            analytics["path_hits"][_path_key(hostname, parsed.path)] += 1

        return True

    except TypeError:
        print ("TypeError for ", parsed)
        return False

    except ValueError:
        return False


# < HELPER FUNCTIONS >

# Checks for usable HTTP 200 HTML response.
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


# Checks Content-Type header for HTML.
def _response_content_type_is_html(raw_response):
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


# Checks page bytes within size bounds.
def _has_acceptable_size(page_bytes):
    return MIN_PAGE_BYTES <= len(page_bytes) <= MAX_PAGE_BYTES


# Checks hostname against allowed domains.
def _is_in_allowed_domains(hostname):
    if not hostname:
        return False

    for domain in ALLOWED_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True

    return False


# Checks for disallowed file extension.
def _has_disallowed_extension(path):
    return bool(
        re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|pps|ppsx|pptm|ppsm"
            + r"|doc|docx|docm|xls|xlsx|xlsm|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1|ipynb|nb"
            + r"|txt|md|log|yaml|yml|conf|cfg|ini"
            + r"|sql|sqlite|odb|accdb|mdb"
            + r"|odp|ods|odt|odg|key|pages|numbers"
            + r"|c|cc|cpp|cxx|h|hpp|hxx|java|py|rb|pl|go|rs|swift|kt"
            + r"|ff|bib|sty|bst|cls"
            + r"|thmx|mso|arff|rtf|jar|war|ear|apk|csv"
            + r"|wasm|ipa|pkg|deb|rpm|xz|lzma|zst"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz)$",
            path.lower(),
        )
    )


# Checks for too many path segments.
def _has_too_many_path_segments(path):
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) > MAX_PATH_SEGMENTS


# Checks for repeated path segments.
def _has_repeated_path_segments(path):
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return False

    segment_counts = Counter(segments)
    return max(segment_counts.values()) > MAX_PATH_SEGMENT_REPEATS


# Detects calendar / date trap URLs.
def _looks_like_calendar_trap(parsed_url):
    path = parsed_url.path
    query = parsed_url.query.lower()

    if re.search(r"/\d{4}-\d{2}-\d{2}/?$", path):
        return True

    if re.search(r"/(19|20)\d{2}(/\d{1,2}){0,2}/?$", path):
        return True

    if re.search(r"/(events?|calendar)/(week|day|month|year)\b", path):
        return True

    if re.search(r"\b(year|month|day|date|when|eventdate|startdate|enddate|from|to|after|before)=\d", query):
        return True

    return False


# Detects non-HTML export query strings.
def _is_non_html_export_query(query):
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


# Detects low-information path patterns.
def _is_low_info_path(path):
    return bool(re.search(
            r"/(pix|photos?|gallery|galleries|albums?"
            r"|genealogy|family[-_]?tree|ancestry|surnames?"
            r"|zip[-_]?attachment|raw[-_]?attachment)(/|$)",
            path,
            re.IGNORECASE,
        )
    )


# Detects /page/N pagination archives.
def _is_pagination_archive(path):
    return bool(re.search(r"/page/\d+(/|$)", path, re.IGNORECASE))


# Checks for too many query parameters.
def _has_too_many_query_params(query):
    if not query:
        return False

    param_count = sum(1 for pair in query.split("&") if pair)
    return param_count > MAX_QUERY_PARAMS


# Checks query-variant limit per (host, path).
def _path_hit_limit_reached(parsed_url, hostname):
    if not parsed_url.query:
        return False

    path_key = _path_key(hostname, parsed_url.path)
    return analytics["path_hits"].get(path_key, 0) >= PATH_HIT_LIMIT


# Builds (host, path) cache key.
def _path_key(hostname, path):
    return f"{hostname}{path}"


# Builds (host, directory) cache key.
def _dir_key(hostname, path):
    dir_prefix = path.rsplit("/", 1)[0] + "/"
    return f"{hostname}{dir_prefix}"


# Detects sequential / numeric filenames.
def _is_sequential_filename(path):
    filename = path.rsplit("/", 1)[-1]
    return bool(_SEQUENTIAL_FILENAME_RE.match(filename))


# Checks sequential-file limit per directory.
def _seq_dir_limit_reached(parsed_url, hostname):
    if not _is_sequential_filename(parsed_url.path):
        return False

    dir_key = _dir_key(hostname, parsed_url.path)
    return analytics["seq_dir_hits"].get(dir_key, 0) >= SEQUENTIAL_PAGE_LIMIT


# Checks visible-text density of page.
def _has_sufficient_text_density(page_text, page_bytes):
    if not page_bytes:
        return False

    text_bytes = len(page_text.encode("utf-8", errors="ignore"))
    return text_bytes / len(page_bytes) >= MIN_TEXT_DENSITY


# Hashes normalized page text.
def _content_fingerprint(page_text):
    normalized_text = " ".join(page_text.lower().split())
    return hashlib.md5(normalized_text.encode("utf-8", errors="ignore")).hexdigest()


# Updates analytics state for crawled page.
def record_page_analytics(page_url, meaningful_word_tokens, is_content_bearing):
    global _pages_since_last_save

    defragmented_url, _fragment = urldefrag(page_url)
    if not defragmented_url:
        return

    parsed = urlparse(defragmented_url)
    hostname = (parsed.hostname or "").lower()

    is_new_url = defragmented_url not in analytics["unique_urls"]
    if is_new_url:
        analytics["unique_urls"].add(defragmented_url)

        if hostname == "uci.edu" or hostname.endswith(".uci.edu"):
            analytics["subdomain_pages"][hostname].add(defragmented_url)

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


if __name__ == "__main__":
    generate_report()
    print(f"Report written to {REPORT_FILE}")
