import requests
from bs4 import BeautifulSoup
import re
from collections import Counter
from text_processing import tokenize_text, filter_tokens

class Posting:
    def __init__(self, docid, tfidf, fields=None):
        self.docid = docid
        self.tfidf = tfidf # use freq counts for now
        self.fields = fields if fields else []
    def __repr__(self):
        return f"Post(ID: {self.docid}, Freq: {self.tfidf})"

def buildIndex(D):
    inverted_index = {}
    doc_id = {}
    doc_count = 0
    for url in D:
        doc_count += 1
        doc_id[doc_count] = url

        try:
            response = requests.get(url)
        except Exception:
            raise Exception
        html_content = response.text


        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=' ')
        # fetch text, tokenize, and count
        tokens = re.findall(r'\b[a-z0-9]+\b', text.lower())
        filtered_tokens = filter_tokens(tokens)
        term_frequencies = Counter(filtered_tokens)

        for word, freq in term_frequencies.items():
            if word not in inverted_index:
                inverted_index[word] = []
            new_post = Posting(doc_count, freq)
            inverted_index[word].append(new_post)
            
    return inverted_index, doc_id


if __name__ == "__main__":
    
    try:
        inverted_index, doc_id = buildIndex(["https://catalogue.uci.edu/"])
        print(inverted_index, doc_id)
        
    except Exception as e:
        print(f"Error: {e}")
