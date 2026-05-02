import re
import atexit
import threading
from urllib.parse import urlparse, urldefrag, urljoin, parse_qs
from bs4 import BeautifulSoup

from text_processing import tokenize_text, filter_tokens
from similarity import stable_hash_64, compute_simhash, hamming_distance


# analytics for the report
analytics_lock = threading.Lock()
unique_pages = set()
word_frequencies = {}
subdomain_counts = {}
longest_page_url = ""
longest_page_word_count = 0

# tracks repeated URL patterns like /page/1, /page/2, /page/3
url_pattern_counts = {}

# duplicate detection
exact_page_hashes = set()
near_page_simhashes = []
exact_duplicate_count = 0
near_duplicate_count = 0
SIMHASH_NEAR_DUP_THRESHOLD = 5
# arbitrary-ish threshold; 4-8 is usually reasonable for 64-bit SimHash near duplicates

# page quality thresholds
MIN_FILTERED_WORDS = 50
MAX_CONTENT_BYTES = 1_000_000

ALLOWED_DOMAINS = [
    ".ics.uci.edu",
    ".cs.uci.edu",
    ".informatics.uci.edu",
    ".stat.uci.edu",
]


def add_tokens_to_frequencies(tokens):
    for token in tokens:
        if token in word_frequencies:
            word_frequencies[token] += 1
        else:
            word_frequencies[token] = 1


def scraper(url, resp):
    if resp is None or resp.status != 200 or resp.raw_response is None:
        return []

    if not resp.raw_response.content:
        return []

    # don't let huge files/pages waste time or pollute analytics
    if len(resp.raw_response.content) > MAX_CONTENT_BYTES:
        return []

    # duplicate detection + analytics happen before link extraction
    should_extract_links = update_analytics(url, resp)

    if not should_extract_links:
        return []

    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]


def extract_next_links(url, resp):
    # Implementation required.
    # url: the URL that was used to get the page
    # resp.url: the actual url of the page
    # resp.status: the status code returned by the server. 200 is OK, you got the page.
    # resp.error: when status is not 200, you can check the error here, if needed.
    # resp.raw_response.content: the actual page content.
    # Return a list with the hyperlinks scraped from resp.raw_response.content.

    if resp is None or resp.status != 200 or resp.raw_response is None:
        return []

    content = resp.raw_response.content

    # filter out malformed responses and potentially not useful HTML
    if not content or len(content) < 250:
        return []

    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception:
        return []

    links = set()
    base_url = resp.url if getattr(resp, "url", None) else url

    for a in soup.find_all("a", href=True):
        link = a.get("href")

        if not link:
            continue

        link = link.strip()

        if not link:
            continue

        if link.startswith("#"):
            continue

        if link.lower().startswith(("mailto:", "javascript:", "tel:")):
            continue

        try:
            full_link = urljoin(base_url, link)
            clean_url, _ = urldefrag(full_link)
        except Exception:
            continue

        if not clean_url:
            continue

        parsed = urlparse(clean_url)
        host = parsed.hostname.lower() if parsed.hostname else ""

        # early domain check so we do not even return off-domain links
        if is_allowed_domain(host):
            links.add(clean_url)

    return list(links)


def is_allowed_domain(host):
    if not host:
        return False

    for domain in ALLOWED_DOMAINS:
        if host == domain.strip(".") or host.endswith(domain):
            return True

    return False


def is_valid(url):
    global url_pattern_counts

    try:
        if not url:
            return False

        url = url.strip()

        if url in {"-", "#"}:
            return False

        if len(url) > 300:
            return False

        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            return False

        host = parsed.hostname.lower() if parsed.hostname else ""

        if not is_allowed_domain(host):
            return False

        full_url_low = url.lower()
        path_low = parsed.path.lower()
        query_low = parsed.query.lower()

        # block unwanted file extensions
        if re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico|png|tiff?|mid|mp2|mp3|mp4"
            r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            r"|epub|dll|cnf|tgz|sha1|thmx|mso|arff|rtf|jar|csv"
            r"|rm|smil|wmv|swf|wma|zip|rar|gz"
            r"|patch|diff|git|ipynb|emx|mpg|scm|ss|rkt|nb|nbp|bib"
            r"|odp|db|war|dtd|sql|img|ics|ical|xml|json)$",
            path_low
        ):
            return False

        trap_patterns = [
            # slide / presentation traps
            r".*/~[a-zA-Z0-9]+/.*(sld|tsld)[0-9]+\.htm.*",
            r".*/~[a-zA-Z0-9]+/(presentations|slides)/.*",

            # DokuWiki maintenance / generated views
            r".*/doku\.php/projects:maint-.*",
            r".*/doku\.php/.*(\?do=diff|\&rev=).*",

            # old course archives
            r".*/~[a-zA-Z0-9]+/(courses|teaching|class|assignments|homeworks|grad/courses)/(19|20[0-2])[0-9].*",

            # recursive publication / technical silos
            r".*/~[a-zA-Z0-9]+/publications/[ar][0-9]+[A-Z]?\.html.*",
            r".*/~[a-zA-Z0-9]+/(papers|softwares|benchmarks|bibs|junkyard)/.*",

            # calendar / event loops
            r".*(\?tribe|eventdisplay|ical|outlook-ical|eventdate=).*",
            r".*/events/.*(month|list|tag|page/|today|week).*",
            r".*/events/20[0-2][0-9]-[0-9]{2}.*",
            r"calendar",
            r"/events/",

            # GitLab / mailman sinks
            r"^https?://gitlab\.ics\.uci\.edu/.*(/-/|/tags|/branches|/commits|/starrers|/forks|/activity|/users).*",
            r"^https?://mailman\.ics\.uci\.edu/.*",

            # photo galleries and image browsing
            r".*/gallery/.*(\?|&)(page|image|photo)=.*",
            r".*/photos/.*[0-9]{3,}.*",
            r".*/images?/.*[0-9]{3,}.*",

            # pagination traps
            r".*/page/[0-9]{2,}.*",
            r".*[\?&]page=[0-9]{2,}.*",

            # date archives
            r".*/[0-9]{4}/[0-9]{2}/[0-9]{2}.*",
            r".*/archive/[0-9]{4}.*",

            # search / filter / sorting
            r".*[\?&](filter|sort|order|search|query|keywords|orderby)=.*",

            # session IDs / auth / tracking
            r".*[\?&](session|sid|token|key|phpsessid)=.*",
            r"auth",
            r"login",
            r"logout",
            r"register",
            r"signin",
            r"signup",
            r"password",
            r"oauth",

            # PDF viewers / document processors
            r".*/pdfviewer.*",
            r".*[\?&](view|viewer|display)=.*",

            # printer-friendly / share versions
            r".*[\?&](print|share|format)=.*",

            # comment and reply chains
            r".*[\?&](replytocom|comment)=.*",

            # version control / diff pages
            r".*/diff/.*",
            r".*/compare/.*",
            r"action=history",
            r"action=diff",
            r"version=",

            # dataset / generated technical pages
            r"datasets\?search=",
            r"/dataset",
            r"/datasets",
            r"smiles",
            r"molecule",
            r"chemical",
            r"compound",
        ]

        for pattern in trap_patterns:
            if re.search(pattern, full_url_low):
                return False

        # our crawler lowkey gets trapped in wiki if we allow generated DokuWiki actions
        if host == "wiki.ics.uci.edu":
            query = parse_qs(query_low)

            bad_doku_actions = {
                "media",
                "edit",
                "export_pdf",
                "index",
                "recent",
                "revisions",
                "diff",
                "backlink",
                "login",
                "logout",
                "admin",
                "profile",
                "subscribe",
                "unsubscribe",
            }

            if "do" in query:
                for action in query["do"]:
                    if action in bad_doku_actions or action == "":
                        return False

            if "rev" in query:
                return False

            if "tab_files" in query or "tab_details" in query or "image" in query:
                return False

        # very strict: blocks all doku.php pages
        # comment this out if you still want normal wiki content pages
        if "/doku.php" in path_low:
            return False

        # known trouble spots
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

        for trouble_path in trouble_paths:
            if trouble_path in path_low:
                return False

        # broad trap query parameters
        trap_params = [
            "action=",
            "do=",
            "rev=",
            "format=",
            "timeline=",
            "image=",
            "tab_details=",
            "tab_files=",
            "ns=",
            "share=",
            "diff=",
            "view=",
            "day=",
            "month=",
            "year=",
            "idx=",
            "c=",
            "o=",
            "sort=",
            "order=",
        ]

        for param in trap_params:
            if param in full_url_low:
                return False

        # avoid extremely deep or repetitive paths
        path_segments = [segment for segment in parsed.path.split("/") if segment]

        if len(path_segments) > 10:
            return False

        if len(path_segments) > len(set(path_segments)) + 3:
            return False

        if re.search(r"(/.+?)\1{2,}", path_low):
            return False

        if re.search(r"(/[^/]+)\1{2,}", path_low):
            return False

        # dynamic URL-pattern trap detection
        # example: /page/1, /page/2, /page/3 become /page/N
        pattern = re.sub(r"\d+", "N", full_url_low)

        with analytics_lock:
            url_pattern_counts[pattern] = url_pattern_counts.get(pattern, 0) + 1

            if url_pattern_counts[pattern] > 30:
                return False

        return True

    except Exception:
        return False


def update_analytics(url, resp):
    """
    Gather statistics when crawling and update global counters.
    Returns True if the page should contribute outgoing links.
    Returns False if the page is invalid, low-quality, already seen,
    exact duplicate, or near duplicate.
    """
    global longest_page_url
    global longest_page_word_count
    global exact_duplicate_count
    global near_duplicate_count

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

    exact_hash = stable_hash_64(" ".join(filtered_tokens))
    simhash_value = compute_simhash(filtered_tokens)

    parsed = urlparse(page_url)
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

        if subdomain in subdomain_counts:
            subdomain_counts[subdomain] += 1
        else:
            subdomain_counts[subdomain] = 1

        if len(filtered_tokens) > longest_page_word_count:
            longest_page_word_count = len(filtered_tokens)
            longest_page_url = page_url

        add_tokens_to_frequencies(filtered_tokens)

    return True


def write_report():
    """
    report.txt will be written in the current working directory
    probably this: ~/cs121/spacetime-crawler4py/report.txt
    """
    try:
        with analytics_lock:
            sorted_words = sorted(
                word_frequencies.items(),
                key=lambda item: (-item[1], item[0])
            )

            with open("report.txt", "w", encoding="utf-8") as file:
                file.write("Report\n")

                file.write("1. Number of unique pages found:\n")
                file.write(str(len(unique_pages)) + "\n\n")

                file.write("2. Longest page by word count:\n")
                file.write(longest_page_url + "\n")
                file.write(str(longest_page_word_count) + " words\n\n")

                file.write("3. Top 50 most common words:\n")
                for token, count in sorted_words[:50]:
                    file.write(token + ", " + str(count) + "\n")

                file.write("\n4. Subdomains found in uci.edu:\n")
                for subdomain in sorted(subdomain_counts.keys()):
                    file.write(
                        subdomain + ", " + str(subdomain_counts[subdomain]) + "\n"
                    )

                file.write("\n5. Duplicate detection:\n")
                file.write(
                    "Exact duplicate pages skipped: "
                    + str(exact_duplicate_count)
                    + "\n"
                )
                file.write(
                    "Near duplicate pages skipped: "
                    + str(near_duplicate_count)
                    + "\n"
                )

    except Exception as e:
        print("Error writing report:", e)


atexit.register(write_report)
# the report only gets created when the crawler exits normally