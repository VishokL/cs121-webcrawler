import os
import shelve
import time
from collections import defaultdict, deque
from threading import Condition, RLock
from urllib.parse import urlparse

from utils import get_logger, get_urlhash, normalize
from scraper import is_valid


def _domain_of(url):
    return (urlparse(url).hostname or "").lower()


class Frontier(object):
    """Thread-safe frontier with per-domain politeness.

    Maintains one queue per host. get_tbd_url() returns (url, wait_seconds)
    where wait_seconds is how long the worker must sleep before issuing the
    request. The slot is reserved at pick time so two concurrent workers
    never violate the per-host delay.
    """

    def __init__(self, config, restart):
        self.logger = get_logger("FRONTIER")
        self.config = config
        self.lock = RLock()
        self.cond = Condition(self.lock)
        self.domain_queues = defaultdict(deque)
        self.next_allowed = defaultdict(float)
        self.in_flight = 0

        if not os.path.exists(self.config.save_file) and not restart:
            self.logger.info(
                f"Did not find save file {self.config.save_file}, "
                f"starting from seed.")
        elif os.path.exists(self.config.save_file) and restart:
            self.logger.info(
                f"Found save file {self.config.save_file}, deleting it.")
            os.remove(self.config.save_file)

        self.save = shelve.open(self.config.save_file)
        if restart:
            for url in self.config.seed_urls:
                self.add_url(url)
        else:
            self._parse_save_file()
            if not self.save:
                for url in self.config.seed_urls:
                    self.add_url(url)

    def _parse_save_file(self):
        total_count = len(self.save)
        tbd_count = 0
        for url, completed in self.save.values():
            if not completed and is_valid(url):
                self.domain_queues[_domain_of(url)].append(url)
                tbd_count += 1
        self.logger.info(
            f"Found {tbd_count} urls to be downloaded from {total_count} "
            f"total urls discovered.")

    def _pending_total_unlocked(self):
        return sum(len(q) for q in self.domain_queues.values())

    def pending_total(self):
        with self.lock:
            return self._pending_total_unlocked()

    def get_tbd_url(self):
        """Returns (url, wait_seconds) or (None, 0.0) when the crawl is done.

        Reserves the next per-host politeness slot before returning, so the
        caller MUST sleep wait_seconds before issuing the request and MUST
        call mark_url_complete when finished (even on download failure).
        """
        with self.cond:
            while True:
                now = time.time()
                best_domain = None
                best_wait = None
                for domain, queue in self.domain_queues.items():
                    if not queue:
                        continue
                    wait = max(0.0, self.next_allowed[domain] - now)
                    if best_wait is None or wait < best_wait:
                        best_domain = domain
                        best_wait = wait

                if best_domain is not None:
                    url = self.domain_queues[best_domain].popleft()
                    # Reserve this domain's next slot.
                    self.next_allowed[best_domain] = (
                        now + best_wait + self.config.time_delay
                    )
                    self.in_flight += 1
                    return url, best_wait

                # No URLs queued. If no one is in flight, the crawl is done.
                if self.in_flight == 0:
                    return None, 0.0

                # Other workers may still add URLs; wait to be notified.
                self.cond.wait(timeout=1.0)

    def add_url(self, url):
        with self.cond:
            url = normalize(url)
            urlhash = get_urlhash(url)
            if urlhash not in self.save:
                self.save[urlhash] = (url, False)
                self.save.sync()
                self.domain_queues[_domain_of(url)].append(url)
                self.cond.notify()

    def mark_url_complete(self, url):
        with self.cond:
            urlhash = get_urlhash(url)
            if urlhash not in self.save:
                self.logger.error(
                    f"Completed url {url}, but have not seen it before.")
            self.save[urlhash] = (url, True)
            self.save.sync()
            self.in_flight -= 1
            if self.in_flight == 0 and self._pending_total_unlocked() == 0:
                self.cond.notify_all()
