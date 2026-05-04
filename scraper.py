import re
import threading
import atexit
from urllib.parse import urlparse, urldefrag, urljoin
from bs4 import BeautifulSoup
from collections import defaultdict, Counter
import urllib.robotparser
import string

from text_processing import tokenize_text, filter_tokens
from similarity import stable_hash_64, compute_simhash, hamming_distance

ALLOWED_DOMAINS = re.compile(
    r"^(.+\.)?(ics|cs|informatics|stat)\.uci\.edu$"
)

analytics_lock = threading.Lock()
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


seen_simhashes = set()
SIMHASH_THRESHOLD = 3  
SIMHASH_NEAR_DUP_THRESHOLD = 5
MIN_FILTERED_WORDS = 50
MAX_CONTENT_BYTES = 1_000_000
MAX_VISITS = 500

domain_visits = defaultdict(int)
robot_parsers = {}


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

    # GitLab / mailman sinks
    r"^https?://gitlab\.ics\.uci\.edu/.*(/-/|/tags|/branches|/commits|/starrers|/forks|/activity|/users).*",
    r"^https?://mailman\.ics\.uci\.edu/.*",

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


def add_tokens_to_frequencies(tokens):
    word_frequencies.update(tokens)


def is_similar_to_seen(simhash):
    for seen in seen_simhashes:
        if hamming_distance(simhash, seen) <= SIMHASH_THRESHOLD:
            return True
    return False


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


def is_trap(parsed_url) -> bool:
    domain = parsed_url.hostname
 
    with domain_lock:
        if domain_visits[domain] >= MAX_VISITS:
            return True
    
    # repeated path segments (e.g. /a/b/a/b/…)
    segments = [s for s in parsed_url.path.split("/") if s]

    if len(segments) > 15:
        return True

    seen = set()
    for seg in segments:
        if seg in seen:
            return True
        seen.add(seg)
        
 
    # excessively long query string
    if len(parsed_url.query) > 200:
        return True
    
    if re.search(r'[?&](date|page|start|offset|from|to)=', parsed_url.query):
        param_values = parsed_url.query.split('&')
        if any(re.search(r'\d{4}-\d{2}-\d{2}', v) for v in param_values):
            return True
 
    return False

def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]


def extract_next_links(url, resp):
    # url: the URL that was used to get the page
    # resp.url: the actual url of the page
    # resp.status: the status code returned by the server. 200 is OK, you got the page. Other numbers mean that there was some kind of problem.
    # resp.error: when status is not 200, you can check the error here, if needed.
    # resp.raw_response: this is where the page actually is. More specifically, the raw_response has two parts:
    #         resp.raw_response.url: the url, again
    #         resp.raw_response.content: the content of the page!
    # Return a list with the hyperlinks (as strings) scrapped from resp.raw_response.content
 
    # update_analytics is the gatekeeper — if it returns False, skip this page
    if not update_analytics(url, resp):
        return []
 
    # increment domain visit counter only for pages that passed all checks
    with domain_lock:
        domain_visits[urlparse(url).hostname] += 1
 
    content  = resp.raw_response.content
    soup     = BeautifulSoup(content, "html.parser")
    base_url = resp.url if getattr(resp, "url", None) else url
    links    = []
 
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
            links.append(defrag_url)
 
    return list(set(links))

def is_valid(url) -> bool:
    # Decide whether to crawl this url or not.
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.
    try:
        parsed = urlparse(url)
 
        if parsed.scheme not in {"http", "https"}:
            return False
 
        if not parsed.hostname or not ALLOWED_DOMAINS.match(parsed.hostname):
            return False
 
        if is_trap(parsed):
            return False
 
        if not can_fetch(parsed, url):
            return False
 
        lower_url = url.lower()
        for pattern in trap_patterns:
            if re.search(pattern, lower_url):
                return False
 
        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz)$",
            parsed.path.lower()
        )
 
    except TypeError:
        print("TypeError for ", parsed)
        raise
    except Exception:
        return False

def update_analytics(url, resp) -> bool:
    """
    Gather statistics when crawling and update global counters.
    Returns True  if the page is valid, unique, and not a duplicate
                  (i.e. the caller should extract outgoing links).
    Returns False if the page is invalid, low-quality, already seen,
                  exact duplicate, or near-duplicate.
    """
    global longest_page_url, longest_page_word_count
    global exact_duplicate_count, near_duplicate_count
 
    if resp is None or resp.status != 200 or resp.raw_response is None:
        return False
 
    content = resp.raw_response.content
    if not content:
        return False
 
    if len(content) > MAX_CONTENT_BYTES:
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
    filtered_tokens = filter_tokens(tokens)
 
    # low-information page filter
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
 
        # subdomain tracking — store unique URLs, keyed by hostname
        if subdomain.endswith(".ics.uci.edu"):
            subdomain_counts[subdomain].add(page_url)
 
        if len(filtered_tokens) > longest_page_word_count:
            longest_page_word_count = len(filtered_tokens)
            longest_page_url = page_url
 
        add_tokens_to_frequencies(filtered_tokens)
 
    return True

def write_report():
    """
    report.txt will be written in the current working directory.
    probably this: ~/cs121/spacetime-crawler4py/report.txt
    """
    try:
        with analytics_lock:
            with open("report.txt", "w", encoding="utf-8") as file:
                file.write("Report\n\n")
 
                file.write("1. Number of unique pages found:\n")
                file.write(str(len(unique_pages)) + "\n\n")
 
                file.write("2. Longest page by word count:\n")
                file.write(longest_page_url + "\n")
                file.write(str(longest_page_word_count) + " words\n\n")
 
                file.write("3. Top 50 most common words:\n")
                for token, count in word_frequencies.most_common(50):
                    file.write(token + ", " + str(count) + "\n")
 
                file.write("\n4. Subdomains found in ics.uci.edu:\n")
                for subdomain in sorted(subdomain_counts.keys()):
                    file.write(
                        subdomain + ", " + str(len(subdomain_counts[subdomain])) + "\n"
                    )
 
                file.write("\n5. Duplicate detection:\n")
                file.write(
                    "Exact duplicate pages skipped: "
                    + str(exact_duplicate_count) + "\n"
                )
                file.write(
                    "Near duplicate pages skipped: "
                    + str(near_duplicate_count) + "\n"
                )
 
    except Exception as e:
        print("Error writing report:", e)
 
 
atexit.register(write_report)
# the report only gets created when the crawler exits normally