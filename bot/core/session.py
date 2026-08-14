import time
import random
from bot.utils.logger import logger
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

class Session:
    def __init__(self, page: Page):
        self.page = page

    def login(self, username, password):
        logger.info("Logging in.....Please wait :)  ", step="login", event="start")
        
        try:
            self.page.goto("https://www.linkedin.com/login?trk=guest_homepage-basic_nav-header-signin", 
                          wait_until="domcontentloaded")
            
            # Check if we are already logged in
            if "feed" in self.page.url:
                logger.info("Already logged in.", step="login", event="success")
                return

            # Wait for login form
            self.page.wait_for_selector("#username", timeout=10000)
            
            # Fill username
            self.page.fill("#username", username)
            time.sleep(random.uniform(1, 2))
            
            # Fill password
            self.page.fill("#password", password)
            time.sleep(random.uniform(1, 2))
            
            # Click login button
            self.page.click('button[type="submit"]')
            
            # Wait for navigation or 2FA/verification to complete (checking URL for up to 60 seconds)
            logger.info("Waiting for login verification/2FA (up to 60 seconds)...", step="login")
            for _ in range(60):
                if "feed" in self.page.url:
                    break
                time.sleep(1)
            
            # Check if login was successful
            if "feed" in self.page.url or "checkpoint" in self.page.url:
                logger.info("Login successful (or 2FA/verification active)", step="login", event="success")
            else:
                logger.warning("Login may have failed - unexpected URL", step="login", event="warning")
                
        except PlaywrightTimeoutError:
            logger.error("TimeoutException! Username/password field or login button not found", 
                        step="login", event="failure", exception_type="TimeoutException")
        except Exception as e:
            logger.error(f"Login failed: {e}", step="login", event="failure", exception=e)

