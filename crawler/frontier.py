import os
import shelve
import time

from threading import Thread, RLock
# from queue import Queue, Empty
from urllib.parse import urlparse
from utils import get_logger, get_urlhash, normalize
from scraper import is_valid


class Frontier(object):
    def __init__(self, config, restart):
        self.logger = get_logger("FRONTIER")
        self.config = config
        self.to_be_downloaded = list()
        self.lock = RLock()  # multithread test
        self.domain_last_access_time = {}  # track last request time for politeness
        
        if not os.path.exists(self.config.save_file) and not restart:
            # Save file does not exist, but request to load save.
            self.logger.info(
                f"Did not find save file {self.config.save_file}, "
                f"starting from seed.")
        elif os.path.exists(self.config.save_file) and restart:
            # Save file does exists, but request to start from seed.
            self.logger.info(
                f"Found save file {self.config.save_file}, deleting it.")
            os.remove(self.config.save_file)
        # Load existing save file, or create one if it does not exist.
        self.save = shelve.open(self.config.save_file)
        if restart:
            for url in self.config.seed_urls:
                self.add_url(url)
        else:
            # Set the frontier state with contents of save file.
            self._parse_save_file()
            if not self.save:
                for url in self.config.seed_urls:
                    self.add_url(url)

    def _parse_save_file(self):
        ''' This function can be overridden for alternate saving techniques. '''
        total_count = len(self.save)
        tbd_count = 0
        with self.lock:
            for url, completed in self.save.values():
                if not completed and is_valid(url):
                    self.to_be_downloaded.append(url)
                    tbd_count += 1
        self.logger.info(
            f"Found {tbd_count} urls to be downloaded from {total_count} "
            f"total urls discovered.")

    def get_tbd_url(self):  # updated to support multithreading
        with self.lock:
            try:
                return self.to_be_downloaded.pop()
            except IndexError:
                return None

    def add_url(self, url):
        url = normalize(url)
        urlhash = get_urlhash(url)
        with self.lock:
            if urlhash not in self.save:
                self.save[urlhash] = (url, False)
                self.save.sync()
                self.to_be_downloaded.append(url)
    
    def mark_url_complete(self, url):
        url = normalize(url)
        urlhash = get_urlhash(url)
        with self.lock:
            if urlhash not in self.save:
                self.logger.error(
                    f"Completed url {url}, but have not seen it before.")
                return
            self.save[urlhash] = (url, True)
            self.save.sync()

    def wait_if_not_polite(self, url):
        """
        Enforce per-domain politeness across all worker threads.
        """
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if not domain:
            return

        while True:
            with self.lock:
                current_time = time.time()
                last_access_time = self.domain_last_access_time.get(domain, 0.0)

                elapsed_time = current_time - last_access_time
                wait_time = self.config.time_delay - elapsed_time

                if wait_time <= 0:
                    self.domain_last_access_time[domain] = current_time
                    return

            time.sleep(wait_time)
