import re
import atexit
import threading
from urllib.parse import urlparse, urldefrag, urljoin, parse_qs
from bs4 import BeautifulSoup
from collections import defaultdict

import urllib.robotparser

from text_processing import tokenize_text, filter_tokens
from similarity import stable_hash_64, compute_simhash, hamming_distance


# ── Domain allowlist ──────────────────────────────────────────────────────────

ALLOWED_DOMAINS = re.compile(
    r"^(.+\.)?(ics|cs|informatics|stat)\.uci\.edu$"
)


# ── Analytics globals ─────────────────────────────────────────────────────────

analytics_lock  = threading.Lock()
domain_lock = threading.Lock()
robots_lock = threading.Lock()

unique_pages = set()
word_frequencies = {}
subdomain_counts = defaultdict(set)
longest_page_url = ""
longest_page_word_count = 0

exact_page_hashes = set()
near_page_simhashes = []
exact_duplicate_count = 0
near_duplicate_count = 0

url_pattern_counts    = {}   # normalised pattern → visit count (trap detection)
domain_visits         = defaultdict(int)
robot_parsers         = {}


# limits
SIMHASH_NEAR_DUP_THRESHOLD = 5
MIN_FILTERED_WORDS         = 50
MAX_CONTENT_BYTES          = 1_000_000
MAX_VISITS                 = 500


# patterns
trap_patterns = [
    # Slide / presentation traps
    r".*/~[a-zA-Z0-9]+/.*(sld|tsld)[0-9]+\.htm.*",
    r".*/~[a-zA-Z0-9]+/(presentations|slides)/.*",

    # DokuWiki traps
    r".*/doku\.php/projects:maint-.*",
    r"doku\.php.*[?&](do|rev|image)=.*",

    # Old course archives
    r".*/~[a-zA-Z0-9]+/(courses|teaching|class|assignments|homeworks|grad/courses)/(19|20[0-2])[0-9].*",

    # Publication / technical silos
    r".*/~[a-zA-Z0-9]+/publications/[ar][0-9]+[A-Z]?\.html.*",
    r".*/~[a-zA-Z0-9]+/(papers|softwares|benchmarks|bibs|junkyard)/.*",

    # Calendar / event loops
    r".*[?&](tribe[^&]*|tribe-bar-date|eventdisplay|ical=1|outlook-ical|eventdate=).*",
    r".*/events/.*(month|list|tag|page/|today|week).*",
    r".*/events/20[0-2][0-9]-[0-9]{2}.*",
    r"isg\.ics\.uci\.edu/events/",
    r"calendar",
    r"/events/",
    r"/timeline",

    # Known sinks
    r"^https?://gitlab\.ics\.uci\.edu/.*(/-/|/tags|/branches|/commits|/starrers|/forks|/activity|/users).*",
    r"^https?://mailman\.ics\.uci\.edu/.*",
    r"^https?://ngs\.ics\.uci\.edu.*",
    r"^https?://grape\.ics\.uci\.edu/wiki/.*/raw-attachment/.*",
    r"^https?://grape\.ics\.uci\.edu/wiki/.*/attachment/.*",
    r"^https?://fano\.ics\.uci\.edu/ca/.*",

    # Photo galleries / image browsing
    r".*/gallery/.*[?&](page|image|photo)=.*",
    r".*/photos/.*[0-9]{3,}.*",
    r".*/images?/.*[0-9]{3,}.*",

    # Pagination
    r".*/page/[0-9]{2,}.*",
    r".*[?&]page=[0-9]{2,}.*",

    # Date archives
    r".*/[0-9]{4}/[0-9]{2}/[0-9]{2}.*",
    r".*/archive/[0-9]{4}.*",

    # Search / filter / sorting
    r".*[?&](filter|sort|order|search|query|keywords|orderby)=.*",
    r"\?c=[mnds];o=[ad]",
    r"&c=[mnds];o=[ad]",

    # Auth / session tracking
    r".*[?&](session|sid|token|key|phpsessid|auth|login|logout|signin|signup|password|oauth)=.*",
    r"auth",
    r"login",
    r"logout",
    r"register",
    r"signin",
    r"signup",
    r"password",
    r"oauth",

    # PDF viewers / document display
    r".*/pdfviewer.*",
    r".*[?&](view|viewer|display|tab_details|tab_files)=.*",

    # Printer / share versions
    r".*[?&](print|share|format)=.*",

    # Comment / reply chains
    r".*[?&](replytocom|comment)=.*",

    # Version control / diff pages
    r".*/diff/.*",
    r".*/compare/.*",
    r"action=(history|diff)",
    r"version=",

    # Datasets / chemical pages
    r"datasets\?search=",
    r"/datasets?",
    r"(smiles|molecule|chemical|compound)",
]

# Known trouble path segments (file-2 specific)
trouble_paths = [
    "/calendar/",
    "/wp-content/",
    "/login",
    "/events/page/",
    "/~eppstein/pix/",
    "/commit/",
    "/tree/",
    "/blob/",
    "/raw/",
    "/src/",
    "/pix/",
]

# Broad trap query parameters (file-2 specific)
trap_params = [
    "action=", "do=", "rev=", "format=", "timeline=", "image=",
    "tab_details=", "tab_files=", "ns=", "share=", "diff=", "view=",
    "day=", "month=", "year=", "idx=", "c=", "o=", "sort=", "order=",
]

# DokuWiki actions to block on wiki.ics.uci.edu (file-2 specific)
bad_doku_actions = {
    "media", "edit", "export_pdf", "index", "recent", "revisions",
    "diff", "backlink", "login", "logout", "admin", "profile",
    "subscribe", "unsubscribe",
}


# Checking Robots.txt

def can_fetch(parsed_url, raw_url):
    base = f"{parsed_url.scheme}://{parsed_url.hostname}"

    with robots_lock:
        if base not in robot_parsers:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{base}/robots.txt")
            try:
                rp.read()
                robot_parsers[base] = rp
            except Exception:
                robot_parsers[base] = None

        rp = robot_parsers[base]

    if rp is None:
        return True
    return rp.can_fetch("*", raw_url)


# Trap Helper

def is_trap(parsed_url) -> bool:
    if not parsed_url.hostname:
        return True
    # per-domain visit cap
    with domain_lock:
        if domain_visits[parsed_url.hostname] >= MAX_VISITS:
            return True

    # date values in query string (path-based date patterns don't cover this)
    if re.search(r"[?&](date|page|start|offset|from|to)=", parsed_url.query):
        if any(re.search(r"\d{4}-\d{2}-\d{2}", v) for v in parsed_url.query.split("&")):
            return True

    # excessively long query not caught by trap_params
    if len(parsed_url.query) > 200:
        return True

    return False

def scraper(url, resp):
    if not update_analytics(url, resp):
        return []

    # increment domain visit counter only for pages that passed all checks
    with domain_lock:
        domain_visits[urlparse(url).hostname] += 1

    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    content  = resp.raw_response.content
    base_url = resp.url if getattr(resp, "url", None) else url

    # filter out very short / likely non-HTML responses (file-2)
    if len(content) < 250:
        return []

    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception:
        return []

    links = set()

    for a in soup.find_all("a", href=True):
        link = a.get("href")

        if not link:
            continue

        link = link.strip()

        if not link or link.startswith("#"):
            continue

        if link.lower().startswith(("mailto:", "javascript:", "tel:")):
            continue

        try:
            full_link     = urljoin(base_url, link)
            defrag_url, _ = urldefrag(full_link)
        except Exception:
            continue

        if defrag_url:
            links.add(defrag_url)

    return list(links)

def is_valid(url) -> bool:
    try:
        if not url or len(url.strip()) > 300:
            return False

        if url in {"-", "#"}:
            return False

        parsed = urlparse(url)
        path_low = parsed.path.lower()
        host = parsed.hostname.lower() if parsed.hostname else ""

        if parsed.scheme not in {"http", "https"}:
            return False

        if not host or not ALLOWED_DOMAINS.match(host):
            return False

        if is_trap(parsed):
            return False

        if not can_fetch(parsed, url):
            return False

        # blocked file extensions 
        if re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico|png|tiff?|mid|mp2|mp3|mp4"
            r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            r"|epub|dll|cnf|tgz|sha1|thmx|mso|arff|rtf|jar|csv"
            r"|rm|smil|wmv|swf|wma|zip|rar|gz"
            r"|patch|diff|git|ipynb|emx|mpg|scm|ss|rkt|nb|nbp|bib"
            r"|odp|db|war|dtd|sql|img|ics|ical|xml|json"
            r"|txt|log|cfg|conf)$",
            path_low
        ):
            return False

        full_url_low = url.lower()
        query_low = parsed.query.lower()

        # regex trap patterns
        for pattern in trap_patterns:
            if re.search(pattern, full_url_low):
                return False

        # wiki.ics.uci.edu DokuWiki action filter (file-2)
        if host == "wiki.ics.uci.edu":
            query = parse_qs(query_low)

            if "do" in query:
                for action in query["do"]:
                    if action in bad_doku_actions or action == "":
                        return False

            if "rev" in query:
                return False

            if "tab_files" in query or "tab_details" in query or "image" in query:
                return False

        # block all doku.php pages (file-2)
        if "/doku.php" in path_low:
            return False

        # known trouble path segments (file-2)
        for trouble_path in trouble_paths:
            if trouble_path in path_low:
                return False

        # broad trap query parameters (file-2)
        for param in trap_params:
            if param in full_url_low:
                return False

        # path depth and repetition checks (file-2)
        path_segments = [s for s in parsed.path.split("/") if s]

        if len(path_segments) > 10:
            return False

        if len(path_segments) > len(set(path_segments)) + 3:
            return False

        if re.search(r"(/.+?)\1{2,}", path_low):
            return False

        if re.search(r"(/[^/]+)\1{2,}", path_low):
            return False

        # dynamic URL-pattern trap detection (file-2)
        pattern = re.sub(r"\d+", "N", full_url_low)

        with analytics_lock:
            url_pattern_counts[pattern] = url_pattern_counts.get(pattern, 0) + 1
            if url_pattern_counts[pattern] > 30:
                return False

        return True

    except TypeError:
        print("TypeError for", urlparse(url))
        raise
    except Exception:
        return False


# Reporting our finds

def add_tokens_to_frequencies(tokens):
    for token in tokens:
        word_frequencies[token] = word_frequencies.get(token, 0) + 1


def update_analytics(url, resp) -> bool:
    """
    Gatekeeper for each crawled page.
    Returns True  → page is unique and non-duplicate; caller should extract links.
    Returns False → page is invalid, low-quality, or a duplicate; skip it.
    """
    global longest_page_url, longest_page_word_count
    global exact_duplicate_count, near_duplicate_count

    if resp is None or resp.status != 200 or resp.raw_response is None:
        return False

    content = resp.raw_response.content
    if not content or len(content) > MAX_CONTENT_BYTES:
        return False

    page_url = resp.url if getattr(resp, "url", None) else url
    try:
        page_url, _ = urldefrag(page_url)
    except Exception:
        return False

    if not is_valid(page_url):
        return False

    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception:
        return False

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    tokens = list(tokenize_text(text))
    filtered_tokens = list(filter_tokens(tokens))

    if len(filtered_tokens) < MIN_FILTERED_WORDS:
        return False

    exact_hash    = stable_hash_64(" ".join(filtered_tokens))
    simhash_value = compute_simhash(filtered_tokens)

    parsed    = urlparse(page_url)
    subdomain = parsed.hostname.lower() if parsed.hostname else ""

    with analytics_lock:
        if page_url in unique_pages:
            return False

        if exact_hash in exact_page_hashes:
            exact_duplicate_count += 1
            return False

        for old_simhash in near_page_simhashes:
            if hamming_distance(simhash_value, old_simhash) <= SIMHASH_NEAR_DUP_THRESHOLD:
                near_duplicate_count += 1
                return False

        unique_pages.add(page_url)
        exact_page_hashes.add(exact_hash)
        near_page_simhashes.append(simhash_value)

        # track unique URLs per subdomain (file-1 approach: set of URLs)
        if subdomain.endswith(".ics.uci.edu"):
            subdomain_counts[subdomain].add(page_url)

        if len(filtered_tokens) > longest_page_word_count:
            longest_page_word_count = len(filtered_tokens)
            longest_page_url        = page_url

        add_tokens_to_frequencies(filtered_tokens)

    return True

def write_report():
    """
    Writes report.txt to the current working directory on crawler exit.
    """
    try:
        with analytics_lock:
            sorted_words = sorted(
                word_frequencies.items(),
                key=lambda item: (-item[1], item[0])
            )

            with open("report.txt", "w", encoding="utf-8") as f:
                f.write("Report\n\n")

                f.write("1. Number of unique pages found:\n")
                f.write(str(len(unique_pages)) + "\n\n")

                f.write("2. Longest page by word count:\n")
                f.write(longest_page_url + "\n")
                f.write(str(longest_page_word_count) + " words\n\n")

                f.write("3. Top 50 most common words:\n")
                for token, count in sorted_words[:50]:
                    f.write(token + ", " + str(count) + "\n")

                f.write("\n4. Subdomains found in ics.uci.edu:\n")
                for subdomain in sorted(subdomain_counts.keys()):
                    f.write(
                        subdomain + ", " + str(len(subdomain_counts[subdomain])) + "\n"
                    )

                f.write("\n5. Duplicate detection:\n")
                f.write("Exact duplicate pages skipped: " + str(exact_duplicate_count) + "\n")
                f.write("Near duplicate pages skipped: "  + str(near_duplicate_count)  + "\n")

    except Exception as e:
        print("Error writing report:", e)


atexit.register(write_report)
# the report only gets created when the crawler exits normally