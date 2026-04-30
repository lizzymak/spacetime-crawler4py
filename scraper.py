import re
import atexit
import threading
from urllib.parse import urlparse, urldefrag, urljoin
from bs4 import BeautifulSoup


# analytics for the report
analytics_lock = threading.Lock()
unique_pages = set()
word_frequencies = {}
subdomain_counts = {}
longest_page_url = ""
longest_page_word_count = 0
STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "as", "at", "be", "because", "been", "before", "being", "below",
    "between", "both", "but", "by", "can", "cannot", "could", "did", "do", "does",
    "doing", "down", "during", "each", "few", "for", "from", "further", "had",
    "has", "have", "having", "he", "her", "here", "hers", "herself", "him",
    "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "itself", "just", "me", "more", "most", "my", "myself", "no", "nor", "not",
    "now", "of", "off", "on", "once", "only", "or", "other", "our", "ours",
    "ourselves", "out", "over", "own", "same", "she", "should", "so", "some",
    "such", "than", "that", "the", "their", "theirs", "them", "themselves",
    "then", "there", "these", "they", "this", "those", "through", "to", "too",
    "under", "until", "up", "very", "was", "we", "were", "what", "when",
    "where", "which", "while", "who", "whom", "why", "with", "would", "you",
    "your", "yours", "yourself", "yourselves"
}


# tokenize (code logic from Assignment 1)
def tokenize_text(text):
    current_token = ""

    for char in text:
        if char.isascii() and char.isalnum():
            current_token += char.lower()
        else:
            if current_token != "":
                yield current_token
                current_token = ""

    if current_token != "":
        yield current_token


def add_tokens_to_frequencies(tokens):
    for token in tokens:
        if token in STOP_WORDS:
            continue

        if token.isdigit():  # don't want to count numbers
            continue

        if len(token) <= 1:
            continue

        if token in word_frequencies:
            word_frequencies[token] += 1
        else:
            word_frequencies[token] = 1


def scraper(url, resp):
    update_analytics(url, resp)

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

    # filter out malformed responses and potentially not useful HTML
    if resp is None or resp.status != 200 or resp.raw_response is None:
        return []
    content = resp.raw_response.content
    if not content or len(content) < 100:
        return []

    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception:
        return []

    links = []
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
            defrag_url, _ = urldefrag(full_link)
        except Exception:
            continue

        if defrag_url:
            links.append(defrag_url)

    return list(set(links))


def is_valid(url):
    # Decide whether to crawl this url or not.
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.

    trap_patterns = [
        r"calendar",
        r"/events/",
        r"tribe-bar-date",
        r"eventdisplay",
        r"ical=1",
        r"outlook-ical",
        r"action=history",
        r"action=diff",
        r"version=",
        r"/timeline",
        r"\?c=[mnds];o=[ad]",
        r"&c=[mnds];o=[ad]",
        r"auth",
        r"login",
        r"logout",
        r"register",
        r"signin",
        r"signup",
        r"password",
        r"oauth",
        r"datasets\?search=",
        r"keywords=",
        r"orderby=",
        r"sort=",
        r"order=",
    ]

    try:
        parsed = urlparse(url)

        if parsed.scheme not in set(["http", "https"]):
            return False

        host = parsed.netloc.lower()

        if not (
            host == "ics.uci.edu" or host.endswith(".ics.uci.edu") or
            host == "cs.uci.edu" or host.endswith(".cs.uci.edu") or
            host == "informatics.uci.edu" or host.endswith(".informatics.uci.edu") or
            host == "stat.uci.edu" or host.endswith(".stat.uci.edu")
        ):
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

    except Exception:
        return False


def update_analytics(url, resp):
    """
    Gather statistics when crawling and updates global counters along the way
    """
    global longest_page_url
    global longest_page_word_count

    if resp is None or resp.status != 200 or resp.raw_response is None:
        return

    content = resp.raw_response.content

    if not content:
        return

    page_url = resp.url if getattr(resp, "url", None) else url

    try:
        page_url, _ = urldefrag(page_url)
    except Exception:
        return

    if not is_valid(page_url):
        return

    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception:
        return

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    tokens = list(tokenize_text(text))

    parsed = urlparse(page_url)
    subdomain = parsed.netloc.lower()

    with analytics_lock:
        if page_url in unique_pages:
            return

        unique_pages.add(page_url)
        if subdomain in subdomain_counts:
            subdomain_counts[subdomain] += 1
        else:
            subdomain_counts[subdomain] = 1

        if len(tokens) > longest_page_word_count:
            longest_page_word_count = len(tokens)
            longest_page_url = page_url

        add_tokens_to_frequencies(tokens)


def write_report():
    try:
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
                file.write(subdomain + ", " + str(subdomain_counts[subdomain]) + "\n")

    except Exception as e:
        print("Error writing report:", e)


atexit.register(write_report)
