import time
import random
import logging
from bs4 import BeautifulSoup

from bot.application.workflow import Workflow
from bot.utils.delays import sleep_random
from bot.utils.selectors import LOCATORS, get_locator
from bot.utils.logger import logger
from bot.utils.retry import retry
from bot.utils.stale_guard import safe_action
from bot.discovery.job_identity import JobIdentity
from bot.discovery.scroll_tracker import ScrollTracker
from bot.utils.human_interaction import HumanInteraction
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


class Search:
    def __init__(self, page: Page, workflow: Workflow, blacklist=None, experience_level=None, phone_number=None):
        self.page = page
        self.workflow = workflow
        self.blacklist = blacklist or []
        self.experience_level = experience_level or []
        self.locator = LOCATORS
        self.MAX_SEARCH_TIME = 60 * 60
        self.phone_number = phone_number

    def start_apply(self, positions, locations):
        combos = []
        while len(combos) < len(positions) * len(locations):
            position = positions[random.randint(0, len(positions) - 1)]
            location = locations[random.randint(0, len(locations) - 1)]
            combo = (position, location)
            if combo not in combos:
                combos.append(combo)
                logger.info(f"Applying to {position}: {location}", step="search_init")
                self.applications_loop(position, location)
            if len(combos) > 500:
                break

    def applications_loop(self, position, location):
        jobs_per_page = 0
        start_time = time.time()
        scroll_tracker = ScrollTracker(self.page)
        human = HumanInteraction(self.page)

        logger.info("Looking for jobs.. Please wait..", step="job_search", event="start")

        jobs_per_page = self.next_jobs_page(position, location, jobs_per_page)
        logger.info("Looking for jobs.. Please wait..", step="job_search", event="page_loaded")

        while time.time() - start_time < self.MAX_SEARCH_TIME:
            try:
                logger.info(f"{(self.MAX_SEARCH_TIME - (time.time() - start_time)) // 60} minutes left in this search", 
                           step="job_search", event="timer")
                sleep_random()

                # Check if search results container is present
                search_selector = get_locator("search")
                if self.is_present(search_selector):
                    scrollresults = self.page.locator(search_selector).first
                    
                    # Get current scroll height
                    current_height = scrollresults.evaluate("el => el.scrollHeight")
                    
                    # Scroll down with human-like behavior
                    for i in range(300, int(current_height), 300):
                        scrollresults.evaluate(f"el => el.scrollTo(0, {i})")
                        time.sleep(random.uniform(0.1, 0.3))

                    if not scroll_tracker.update_scroll(current_height):
                        if scroll_tracker.should_stop():
                            logger.warning("Scroll limits reached, moving to next page", step="job_search", event="scroll_stop")
                            jobs_per_page = self.next_jobs_page(position, location, jobs_per_page)
                            continue
                
                
                # Extract jobs
                links_selector = get_locator("links")
                logger.debug(f"Using job card selector: {links_selector}", step="job_search")
                
                if self.is_present(links_selector):
                    # Wait a bit for job cards to fully load
                    time.sleep(random.uniform(1, 2))
                    
                    links = self.page.locator(links_selector).all()
                    logger.info(f"Found {len(links)} job cards on page", step="job_search", event="cards_found")
                    
                    if len(links) == 0:
                        # Try fallback selector
                        fallback_selector = get_locator("links", use_fallback=True)
                        logger.warning(f"No cards with primary selector, trying fallback: {fallback_selector}", step="job_search")
                        if fallback_selector:
                            links = self.page.locator(fallback_selector).all()
                            logger.info(f"Fallback found {len(links)} job cards", step="job_search", event="cards_found_fallback")
                    
                    for i, link in enumerate(links):
                        try:
                            # Check if element is visible before processing
                            if not link.is_visible():
                                continue
                            
                            job_id = JobIdentity.extract_job_id(link)
                            if job_id and not scroll_tracker.is_processed(job_id):
                                try:
                                    link_text = link.text_content(timeout=3000)
                                except:
                                    link_text = ""
                                
                                if 'Applied' not in link_text:
                                    if link_text not in self.blacklist:
                                        logger.info(f"Found new job: {job_id}", step="job_search", event="found_job")
                                        
                                        # CRITICAL: Click the job card to show preview panel
                                        # Don't click the title link, click the card container
                                        try:
                                            # Click on the job card div, NOT the title link
                                            # This shows the preview panel with Easy Apply button
                                            link.click(timeout=5000, force=False)
                                            logger.debug(f"Clicked job card {job_id} to show preview", step="job_search")
                                            time.sleep(1.5)  # Wait for preview panel to load
                                        except Exception as click_err:
                                            logger.warning(f"Could not click job card: {click_err}", step="job_search")
                                        
                                        self.workflow.apply_to_job(job_id, self.phone_number)
                                        scroll_tracker.add_job(job_id)
                                    else:
                                        logger.info(f"Skipping blacklisted job: {job_id}", step="job_search", event="blacklisted")
                                        scroll_tracker.add_job(job_id)  # Blacklisted but processed
                                else:
                                    logger.debug(f"Already applied to job: {job_id}", step="job_search", event="already_applied")
                                    scroll_tracker.add_job(job_id)  # Already applied
                            elif job_id:
                                logger.debug(f"Job {job_id} already processed", step="job_search", event="duplicate")
                        except Exception as e:
                            logger.warning(f"Error processing job card {i}: {e}", step="job_search", event="card_error")
                            continue
                    
                    if scroll_tracker.should_stop():
                        jobs_per_page = self.next_jobs_page(position, location, jobs_per_page)

                else:
                    logger.warning(f"No job cards found with selector: {links_selector}", step="job_search", event="no_cards")
                    # Debug: print what elements are on the page
                    try:
                        all_divs = self.page.locator("div").count()
                        all_lis = self.page.locator("li").count()
                        logger.debug(f"Page has {all_divs} divs and {all_lis} list items", step="job_search")
                    except:
                        pass
                    jobs_per_page = self.next_jobs_page(position, location, jobs_per_page)

            except Exception as e:
                logger.error(f"Search loop error: {e}", step="job_search", event="error", exception=e)
                break

    def ensure_easy_apply_filter(self):
        """Ensure the Easy Apply filter pill button is visibly toggled ON in the search results UI"""
        try:
            time.sleep(1.5)
            # Find Easy Apply button in top filter bar
            filter_buttons = [
                self.page.locator("button[aria-label*='Easy Apply filter']").first,
                self.page.locator("button[aria-label*='Easy Apply']").first,
                self.page.locator("#searchFilter_applyWithLinkedin").first,
                self.page.locator("button.artdeco-pill").filter(has_text="Easy Apply").first,
                self.page.locator("button").filter(has_text="Easy Apply").first
            ]
            
            for btn in filter_buttons:
                if btn.count() > 0 and btn.is_visible():
                    aria_checked = btn.get_attribute("aria-checked")
                    aria_pressed = btn.get_attribute("aria-pressed")
                    class_attr = btn.get_attribute("class") or ""
                    
                    is_active = (aria_checked == "true") or (aria_pressed == "true") or ("artdeco-pill--selected" in class_attr) or ("artdeco-button--primary" in class_attr)
                    
                    if not is_active:
                        logger.info("🔘 Enabling Easy Apply filter pill on LinkedIn UI...", step="job_search")
                        btn.click()
                        time.sleep(2.5)
                        return True
                    else:
                        logger.debug("Easy Apply filter pill is active", step="job_search")
                        return True
        except Exception as e:
            logger.debug(f"Could not verify Easy Apply filter pill: {e}", step="job_search")
        return False

    @retry(max_attempts=3, delay=1)
    def next_jobs_page(self, position, location, jobs_per_page):
        import urllib.parse
        
        # Build query parameters with both modern and legacy Easy Apply flags
        params = {
            "f_AL": "true",
            "f_LF": "f_AL",
            "keywords": position,
            "start": str(jobs_per_page)
        }
        
        # Experience level filter (1=Internship, 2=Entry, 3=Associate, 4=Mid-Senior, 5=Director, 6=Executive)
        if self.experience_level:
            params["f_E"] = ",".join(map(str, self.experience_level))
            
        # Remote Workplace Type filter (f_WT=2)
        if location and str(location).strip().lower() == "remote":
            params["f_WT"] = "2"
        elif location:
            params["location"] = str(location).strip()

        query_str = urllib.parse.urlencode(params)
        url = f"https://www.linkedin.com/jobs/search/?{query_str}"
        
        logger.info(f"Loading jobs page: {url}", step="next_jobs_page")
        self.page.goto(url, wait_until="domcontentloaded")
        self.load_page()
        
        # Verify and toggle Easy Apply filter pill on page if not active
        self.ensure_easy_apply_filter()
        
        return jobs_per_page + 25

    @retry(max_attempts=3, delay=1)
    def load_page(self, sleep=1):
        """
        Scroll the page to load all content
        """
        # Wait for initial page load
        time.sleep(2)
        
        scroll_page = 0
        while scroll_page < 4000:
            self.page.evaluate(f"window.scrollTo(0, {scroll_page})")
            scroll_page += 500
            time.sleep(sleep)

        if sleep != 1:
            self.page.evaluate("window.scrollTo(0, 0)")
            time.sleep(sleep)
        
        # Extra wait for job cards to render
        time.sleep(2)

        return BeautifulSoup(self.page.content(), "lxml")

    def get_elements(self, type) -> list:
        """
        Get elements by type from locators
        """
        selector = get_locator(type)
        if selector and self.is_present(selector):
            return self.page.locator(selector).all()
        return []

    def is_present(self, selector):
        """
        Check if element is present on page
        """
        try:
            return self.page.locator(selector).count() > 0
        except:
            return False
