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

    # only do stuff if response is okay
    if resp.status != 200 or not resp.raw_response:
        return []

    content = resp.raw_response.content
    if not content or len(content) < 100:
        return []

    # get links html and turn it into a soup object used for searching
    soup = BeautifulSoup(content, "html.parser")

    # search the soup object for links (which are a tags)
    links = []
    # find all the links which have 'a' as tag
    # set max links for a page to be 50 for now:
    for a in soup.find_all('a', href=True)[:50]:
        link = a['href']
        # relative link
        full_link = urljoin(resp.url, link)
        # defrag link
        defrag_url, _ = urldefrag(full_link)
        # append to our list of links
        links.append(defrag_url)

    return list(set(links))

def is_valid(url):
    # Decide whether to crawl this url or not. 
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.
    try:
        parsed = urlparse(url)
        if parsed.scheme not in set(["http", "https"]):
            return False

        # get domain
        host = parsed.netloc.lower()

        if not (
                host == "ics.uci.edu" or host.endswith(".ics.uci.edu") or
                host == "cs.uci.edu" or host.endswith(".cs.uci.edu") or
                host == "informatics.uci.edu" or host.endswith(".informatics.uci.edu") or
                host == "stat.uci.edu" or host.endswith(".stat.uci.edu")
        ):
            return False

        if re.search(r"calendar", url.lower()):  # should add more here, this is just a test
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
        print ("TypeError for ", parsed)
        raise
