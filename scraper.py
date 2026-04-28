import re
from urllib.parse import urlparse, urldefrag, urljoin
from bs4 import BeautifulSoup


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

    for a in soup.find_all("a", href=True)[:50]:  # only takes the first 50 for now! 
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