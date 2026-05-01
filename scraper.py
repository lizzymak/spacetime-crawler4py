import re
from urllib.parse import urlparse, urldefrag, urljoin
from bs4 import BeautifulSoup
from collections import defaultdict
import urllib.robotparser
from collections import Counter
import string

ALLOWED_DOMAINS = re.compile(
    r"^(.+\.)?(ics|cs|informatics|stat)\.uci\.edu$"
)

STOP_WORDS = set("""a about above after again against all am an and any are aren't as at be 
because been before being below between both but by can't cannot could couldn't did didn't do 
does doesn't doing don't down during each few for from further had hadn't has hasn't have haven't 
having he he'd he'll he's her here here's hers herself him himself his how how's i i'd i'll i'm 
i've if in into is isn't it it's its itself let's me more most mustn't my myself no nor not of 
off on once only or other ought our ours ourselves out over own same shan't she she'd she'll she's 
should shouldn't so some such than that that's the their theirs them themselves then there there's 
these they they'd they'll they're they've this those through to too under until up very was wasn't 
we we'd we'll we're we've were weren't what what's when when's where where's which while who who's 
whom why why's with won't would wouldn't you you'd you'll you're you've your yours yourself 
yourselves""".split())

word_counts = Counter()  

domain_visits = defaultdict(int)
MAX_VISITS = 500

max_words = {"url": "", "count": 0}
unique_pages = set()
longest_page_words = 0


seen_simhashes = set()
SIMHASH_THRESHOLD = 3  

subdomain_counts = defaultdict(set)

robot_parsers = {}


trap_patterns = [
    r"tribe-bar-date",
    r"eventdisplay",
    r"ical=1",
    r"outlook-ical",
    r"action=history",
    r"action=diff",
    r"/timeline",
    r"\?c=[mnds];o=[ad]",
    r"&c=[mnds];o=[ad]",
    r"[?&]auth=",
    r"[?&]login=",
    r"[?&]logout=",
    r"[?&]signin=",
    r"[?&]signup=",
    r"[?&]password=",
    r"[?&]oauth=",
    r"datasets\?search=",
    r"[?&]keywords=",
    r"[?&]orderby=",
    r"[?&]sort=",
    r"[?&]order=",
    r"[?&]do=media",
    r"[?&]tab_details=",
    r"[?&]tab_files=",
    r"[?&]image=",
    r"doku\.php.*\?.*image=",
]

def tokenize(text: str):
    tokens = []
    valid_chars = set(string.ascii_letters + string.digits)
    token = []
    for char in text:
        if char.isalnum() and char in valid_chars:
            token.append(char.casefold())
        else:
            if token:
                tokens.append("".join(token))
                token.clear()
    if token:
        tokens.append("".join(token))
    return tokens


def get_simhash(page_content):
    words = page_content.lower().split()
    v = [0] * 64
    for word in words:
        h = hash(word)
        for i in range(64):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1
    simhash = 0
    for i in range(64):
        if v[i] > 0:
            simhash |= (1 << i)
    return simhash

def hamming_distance(h1, h2):
    return bin(h1 ^ h2).count('1')

def is_similar_to_seen(simhash):
    for seen in seen_simhashes:
        if hamming_distance(simhash, seen) <= SIMHASH_THRESHOLD:
            return True
    return False


def can_fetch(parsed_url, raw_url, user_agent):
    base = f"{parsed_url.scheme}://{parsed_url.hostname}"

    if base not in robot_parsers:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{base}/robots.txt")
        try:
            rp.read()
        except Exception:
            robot_parsers[base] = None
            return True
        robot_parsers[base] = rp

    rp = robot_parsers[base]
    if rp is None:
        return True
    return rp.can_fetch(user_agent, raw_url)


def is_trap(parsed_url):
    domain = parsed_url.hostname
    if domain_visits[domain] >= MAX_VISITS:
        return True

    segments = [s for s in parsed_url.path.split('/') if s]
    seen = set()
    for seg in segments:
        if seg in seen:
            return True
        seen.add(seg)

    if len(parsed_url.query) > 200:
        return True

    return False

def scraper(url, resp):
    defrag_url, _ = urldefrag(url)
    unique_pages.add(defrag_url)

    parsed = urlparse(defrag_url)
    if parsed.hostname and parsed.hostname.endswith(".ics.uci.edu"):
        subdomain_counts[parsed.hostname].add(defrag_url)


    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]


def extract_next_links(url, resp):
    if resp is None or resp.status != 200 or resp.raw_response is None:
        return []

    content = resp.raw_response.content

    # nothing in the URL
    if not content or len(content) < 100:
        return []
    
    # too big of a file (5MB)
    if len(content) > 5 * 1024 * 1024:  
        return []

    # the actual content
    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception:
        return []

    # high text content check
    text = soup.get_text(strip=True)
    if not text or len(text) < 200: # or len(text) / len(content) < 0.1:
        return []

    # dead URL check (200 but no real content)
    if not text.strip():
        return []

    tokens = tokenize(text)
    filtered = [t for t in tokens if t not in STOP_WORDS]
    word_counts.update(filtered)

    # similar page check
    page_simhash = get_simhash(text)
    if is_similar_to_seen(page_simhash):
        return []
    seen_simhashes.add(page_simhash)

    # word count tracking
    word_count = len(text.split())
    if word_count > max_words["count"]:
        max_words["count"] = word_count
        max_words["url"] = url

    # only count pages that actually have content
    domain_visits[urlparse(url).hostname] += 1

    links = []
    base_url = resp.url if getattr(resp, "url", None) else url

    for a in soup.find_all('a', href=True):
        link = a.get("href")

        if not link:
            continue

        link = link.strip()

        if not link or link.startswith("#"):
            continue

        if link.lower().startswith(("mailto:", "javascript:", "tel:")):
            continue

        try:
            full_link = urljoin(base_url, link)
            defrag_url, _ = urldefrag(full_link)
        except Exception:
            continue

        if defrag_url:
            links.append(defrag_url)

    return list(set(links))


def is_valid(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False

        if not parsed.hostname or not ALLOWED_DOMAINS.match(parsed.hostname):
            return False

        if is_trap(parsed):
            return False

        if not can_fetch(parsed, url, "*"): 
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
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz)$", parsed.path.lower())

    except TypeError:
        print("TypeError for ", parsed)
        raise

def get_report():
    with open("report.txt", "w") as f:
        # Q1
        f.write(f"1. Unique pages: {len(unique_pages)}\n\n")
        
        # Q2
        f.write(f"2. Longest page: {max_words['url']} with {max_words['count']} words\n\n")
        
        # Q3
        f.write("3. Top 50 words:\n")
        for word, count in word_counts.most_common(50):
            f.write(f"  {word}: {count}\n")
        f.write("\n")
        
        # Q4
        f.write("4. Subdomains in ics.uci.edu:\n")
        for subdomain in sorted(subdomain_counts.keys()):
            f.write(f"  {subdomain}, {len(subdomain_counts[subdomain])}\n")