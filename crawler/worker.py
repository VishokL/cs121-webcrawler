from threading import Thread

from inspect import getsource
from utils.download import download
from utils import get_logger
import scraper
import time


class Worker(Thread):
    def __init__(self, worker_id, config, frontier):
        self.logger = get_logger(f"Worker-{worker_id}", "Worker")
        self.config = config
        self.frontier = frontier
        self.iterations = 0
        # basic check for requests in scraper
        assert {getsource(scraper).find(req) for req in {"from requests import", "import requests"}} == {-1}, "Do not use requests in scraper.py"
        assert {getsource(scraper).find(req) for req in {"from urllib.request import", "import urllib.request"}} == {-1}, "Do not use urllib.request in scraper.py"
        super().__init__(daemon=True)

    def run(self):
        while True:
            tbd_url, wait_seconds = self.frontier.get_tbd_url()
            if tbd_url is None:
                self.logger.info(
                    f"Frontier is empty. Stopping. "
                    f"Processed {self.iterations} URLs in this thread."
                )
                break

            # Politeness sleep happens OUTSIDE the frontier lock so other
            # workers can pick from other domains while we wait.
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            resp = download(tbd_url, self.config, self.logger)
            try:
                scraped_urls = scraper.scraper(tbd_url, resp)
            except Exception as exc:
                self.logger.exception(f"scraper raised on {tbd_url}: {exc}")
                scraped_urls = []

            for scraped_url in scraped_urls:
                self.frontier.add_url(scraped_url)
            self.frontier.mark_url_complete(tbd_url)
            self.iterations += 1
            self.logger.info(
                f"[#{self.iterations}] {tbd_url} "
                f"status={resp.status} new_links={len(scraped_urls)} "
                f"waited={wait_seconds:.2f}s "
                f"frontier_tbd={self.frontier.pending_total()}"
            )
