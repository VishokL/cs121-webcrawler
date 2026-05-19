import atexit
import hashlib
import io
import json
import os
import re
from urllib.parse import urldefrag, urljoin, urlparse
from collections import Counter
from bs4 import BeautifulSoup

# < GLOBAL VARIABLES >

ALLOWED_DOMAINS = (
    "ics.uci.edu",
    "cs.uci.edu",
    "informatics.uci.edu",
    "stat.uci.edu",
)

# Filters for exceedingly small/large files.
MIN_PAGE_BYTES = 500
MAX_PAGE_BYTES = 5000000

# Trap detection thresholds for is_valid.
MAX_URL_LENGTH = 300
MAX_SEGMENTS = 8
MAX_SEGMENT_REPEATS = 2

# Caps query params before treating URL.
MAX_QUERY_PARAMS = 3

# Caps distinct query-string variants.
PATH_HIT_LIMIT = 10

# Caps sequential-named files per directory.
SEQ_PAGE_LIMIT = 30

# Caps links to the same target directory.
MAX_DIR_PER_PAGE = 5

# Regex for sequential/numeric filenames.
SEQ_FILENAME_RE = re.compile(r"^(?:[a-z]{0,10})?(\d+)(\.html?)?$", re.IGNORECASE)

# Minimum text/HTML ratio for pages.
MIN_TEXT_DENSITY = 0.05

# Minimum meaningful word tokens for pages.
MIN_CONTENT_TOKENS = 18

# Minimum token length for report's word counts.
MIN_REPORT_WORD_LEN = 3

# Month names/abbrevs to exclude from word counts (English).
MONTH_STOPWORDS = {
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
    "january",
    "february",
    "march",
    "april",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}

# Persisted state and report file locations.
STOP_WORDS_FILE = "stop_words.txt"
ANALYTICS_FILE = "analytics.json"
REPORT_FILE = "report.txt"

# Flushes analytics to disk every N pages.
SAVE_INTERVAL = 25

# < STOP WORDS >

# Loads stop words from disk.
def load_stop_words(path):
    if not os.path.exists(path):
        return set()

    with open(path, encoding="utf-8") as stop_words_file:
        return {line.strip().lower() for line in stop_words_file if line.strip()}

# < TOKENIZATION >

# Tokenizes text into alphanumeric ASCII tokens.
# Same tokeninzer from HW1
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
def meaningful_tokens(text):
    eligible = []
    for token in tokenize(text):
        if len(token) < MIN_REPORT_WORD_LEN:
            continue
        if not token.isalpha():
            continue
        if token in MONTH_STOPWORDS:
            continue
        eligible.append(token)
    return eligible

# < ANALYTICS STATE >

class Crawler:
    def __init__(self, analytics_file=ANALYTICS_FILE, stop_words_file=STOP_WORDS_FILE):
        self.analytics_file = analytics_file
        self.stop_words = load_stop_words(stop_words_file)
        self.unique_urls = set()
        self.longest_page_url = ""
        self.longest_word_count = 0
        self.word_counts = Counter()
        self.subdomain_pages = {}
        self.path_hits = {}
        self.seq_dir_hits = {}
        self.content_hashes = set()
        self.pages_since_save = 0
        self.load(analytics_file)

    # Loads persisted analytics state from disk.
    def load(self, path):
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as analytics_file:
                saved_state = json.load(analytics_file)
        except (json.JSONDecodeError, OSError):
            return
        self.unique_urls = set(saved_state.get("unique_urls", []))
        self.longest_page_url = saved_state.get("longest_page_url", "")
        self.longest_word_count = saved_state.get("longest_word_count", 0)
        self.word_counts = Counter(saved_state.get("word_counts", {}))
        self.subdomain_pages = {
            hostname: set(urls) for hostname, urls in saved_state.get("subdomain_pages", {}).items()
        }
        self.path_hits = dict(saved_state.get("path_hits", {}))
        self.seq_dir_hits = dict(saved_state.get("seq_dir_hits", {}))
        self.content_hashes = set(saved_state.get("content_hashes", []))

    # Saves analytics state to disk.
    def save(self):
        snapshot = {
            "unique_urls": sorted(self.unique_urls),
            "longest_page_url": self.longest_page_url,
            "longest_word_count": self.longest_word_count,
            "word_counts": dict(self.word_counts),
            "subdomain_pages": {hostname: sorted(urls) for hostname, urls in self.subdomain_pages.items()},
            "path_hits": dict(self.path_hits),
            "seq_dir_hits": dict(self.seq_dir_hits),
            "content_hashes": sorted(self.content_hashes),
        }
        with open(self.analytics_file, "w", encoding="utf-8") as analytics_file:
            json.dump(snapshot, analytics_file, indent=2)

    # Writes report answers to disk.
    def generate_report(self, path=REPORT_FILE):
        lines = []

        lines.append(f"1. Unique pages found: {len(self.unique_urls)}\n\n")
        lines.append(f"2. Longest page (by word count): {self.longest_page_url} ({self.longest_word_count} words)\n\n")

        lines.append("3. 50 most common words (word, count):\n")
        for word, count in self.word_counts.most_common(50):
            lines.append(f"{word}, {count}\n")
        lines.append("\n")

        lines.append("4. Subdomains under uci.edu (subdomain, unique pages):\n")
        for hostname in sorted(self.subdomain_pages):
            page_count = len(self.subdomain_pages[hostname])
            lines.append(f"{hostname}, {page_count}\n")

        with open(path, "w", encoding="utf-8") as report_file:
            report_file.writelines(lines)

    # Updates analytics state for crawled page.
    def record_page(self, page_url, tokens, is_content):
        clean_url = urldefrag(page_url)[0]
        if not clean_url:
            return

        parsed = urlparse(clean_url)
        hostname = (parsed.hostname or "").lower()

        is_new = clean_url not in self.unique_urls
        if is_new:
            self.unique_urls.add(clean_url)

            if hostname == "uci.edu" or hostname.endswith(".uci.edu"):
                if hostname not in self.subdomain_pages:
                    self.subdomain_pages[hostname] = set()
                self.subdomain_pages[hostname].add(clean_url)

        if is_content and is_new:
            if len(tokens) > self.longest_word_count:
                self.longest_word_count = len(tokens)
                self.longest_page_url = clean_url

            for word in tokens:
                if word not in self.stop_words:
                    self.word_counts[word] += 1

        self.pages_since_save += 1
        if self.pages_since_save >= SAVE_INTERVAL:
            self.save()
            self.pages_since_save = 0

crawler = Crawler()
atexit.register(crawler.save)

# < STARTING CODE >

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
    if not is_successful_response(resp):
        return []

    page_bytes = resp.raw_response.content
    if not has_acceptable_size(page_bytes):
        return []

    base_url = resp.raw_response.url or resp.url or url

    try:
        soup = BeautifulSoup(page_bytes, "html.parser")
    except Exception:
        return []

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    page_text = soup.get_text(separator=" ")

    is_content = has_text_density(page_text, page_bytes)
    tokens = meaningful_tokens(page_text)

    if is_content and len(tokens) < MIN_CONTENT_TOKENS:
        is_content = False

    if is_content:
        content_hash = hash_normalized_page_text(page_text)
        if content_hash in crawler.content_hashes:
            is_content = False

        else:
            crawler.content_hashes.add(content_hash)

    crawler.record_page(base_url, tokens, is_content)

    if not is_content:
        return []

    links = []
    seen_keys = set()
    dir_counts = Counter()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href:
            continue

        try:
            abs_url = urljoin(base_url, href)
            clean_url = urldefrag(abs_url)[0]
            if not clean_url:
                continue

            target = urlparse(clean_url)

        except ValueError:
            continue

        key = (target.netloc, target.path)
        if key in seen_keys:
            continue

        seen_keys.add(key)

        if is_seq_filename(target.path):
            host = (target.hostname or "").lower()
            cache_key = dir_key(host, target.path)
            if dir_counts[cache_key] >= MAX_DIR_PER_PAGE:
                continue

            dir_counts[cache_key] += 1

        links.append(clean_url)

    return links

def is_valid(url):
    # Decide whether to crawl this url or not. 
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.
    try:
        parsed = urlparse(url)
        if parsed.scheme not in set(["http", "https"]):
            return False

        hostname = (parsed.hostname or "").lower()
        if not is_allowed_domain(hostname):
            return False

        if has_disallowed_extension(parsed.path):
            return False

        if is_pagination_path(parsed.path):
            return False

        if is_low_info_path(parsed.path):
            return False

        if len(url) > MAX_URL_LENGTH:
            return False
        if has_too_many_segments(parsed.path):
            return False
        if has_repeated_segments(parsed.path):
            return False
        if is_calendar_trap(parsed):
            return False
        if is_export_query(parsed.query):
            return False
        if has_too_many_params(parsed.query):
            return False
        if path_hit_limit_reached(parsed, hostname):
            return False
        if seq_dir_limit_reached(parsed, hostname):
            return False

        if is_seq_filename(parsed.path):
            sk = dir_key(hostname, parsed.path)
            crawler.seq_dir_hits[sk] = crawler.seq_dir_hits.get(sk, 0) + 1

        if parsed.query:
            pk = path_key(hostname, parsed.path)
            crawler.path_hits[pk] = crawler.path_hits.get(pk, 0) + 1

        return True

    except TypeError:
        print ("TypeError for ", parsed)
        return False

    except ValueError:
        return False

# < HELPER FUNCTIONS >

# Checks for usable HTTP 200 HTML response.
def is_successful_response(resp):
    if resp.status != 200:
        return False
    if resp.raw_response is None:
        return False
    if not resp.raw_response.content:
        return False
    if not is_html_response(resp.raw_response):
        return False
    return True

# Checks Content-Type header for HTML.
def is_html_response(raw_response):
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
def has_acceptable_size(page_bytes):
    return MIN_PAGE_BYTES <= len(page_bytes) <= MAX_PAGE_BYTES

# Checks hostname against allowed domains.
def is_allowed_domain(hostname):
    if not hostname:
        return False

    for domain in ALLOWED_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True

    return False

# Checks for disallowed file extension.
def has_disallowed_extension(path):
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
def has_too_many_segments(path):
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) > MAX_SEGMENTS

# Checks for repeated path segments.
def has_repeated_segments(path):
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return False

    segment_counts = Counter(segments)
    return max(segment_counts.values()) > MAX_SEGMENT_REPEATS

# Detects calendar / date trap URLs.
def is_calendar_trap(parsed):
    path = parsed.path
    query = parsed.query.lower()

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
def is_export_query(query):
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
def is_low_info_path(path):
    return bool(re.search(
            r"/(pix|photos?|gallery|galleries|albums?"
            r"|genealogy|family[-_]?tree|ancestry|surnames?"
            r"|zip[-_]?attachment|raw[-_]?attachment)(/|$)",
            path,
            re.IGNORECASE,
        )
    )

# Detects /page/N pagination archives.
def is_pagination_path(path):
    return bool(re.search(r"/page/\d+(/|$)", path, re.IGNORECASE))

# Checks for too many query parameters.
def has_too_many_params(query):
    if not query:
        return False

    param_count = sum(1 for pair in query.split("&") if pair)
    return param_count > MAX_QUERY_PARAMS

# Checks query-variant limit per (host, path).
def path_hit_limit_reached(parsed, hostname):
    if not parsed.query:
        return False

    key = path_key(hostname, parsed.path)
    return crawler.path_hits.get(key, 0) >= PATH_HIT_LIMIT

# Builds (host, path) cache key.
def path_key(hostname, path):
    return f"{hostname}{path}"

# Builds (host, directory) cache key.
def dir_key(hostname, path):
    dir_prefix = path.rsplit("/", 1)[0] + "/"
    return f"{hostname}{dir_prefix}"

# Detects sequential / numeric filenames.
def is_seq_filename(path):
    filename = path.rsplit("/", 1)[-1]
    return bool(SEQ_FILENAME_RE.match(filename))

# Checks sequential-file limit per directory.
def seq_dir_limit_reached(parsed, hostname):
    if not is_seq_filename(parsed.path):
        return False

    key = dir_key(hostname, parsed.path)
    return crawler.seq_dir_hits.get(key, 0) >= SEQ_PAGE_LIMIT

# Checks visible-text density of page.
def has_text_density(page_text, page_bytes):
    if not page_bytes:
        return False

    text_bytes = len(page_text.encode("utf-8", errors="ignore"))
    return text_bytes / len(page_bytes) >= MIN_TEXT_DENSITY

# Hash normalized page text.
def hash_normalized_page_text(page_text):
    normalized_text = " ".join(page_text.lower().split())
    return hashlib.md5(normalized_text.encode("utf-8", errors="ignore")).hexdigest()

if __name__ == "__main__":
    crawler.generate_report()
    print(f"Report written to {REPORT_FILE}")
