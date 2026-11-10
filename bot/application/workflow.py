import os
import time
import logging
import re
import random
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from bot.application.form_filler import FormFiller
from bot.application.smart_form_filler import SmartFormFiller
from bot.persistence.store import Store
from bot.utils.selectors import LOCATORS, get_locator
from bot.utils.logger import logger
from bot.utils.retry import retry
from bot.utils.human_interaction import HumanInteraction


class Workflow:
    def __init__(self, page: Page, uploads, blacklist_titles=None, execution_guard=None, dry_run=None, metrics=None, candidate_profile=None, review_mode=False):
        self.page = page
        self.uploads = uploads
        self.blacklist_titles = blacklist_titles or []
        self.store = Store()
        self.candidate_profile = candidate_profile
        self.review_mode = review_mode
        self.metrics = metrics
        self.dry_run = dry_run
        self.execution_guard = execution_guard
        
        # Use SmartFormFiller if candidate profile provided, otherwise old FormFiller
        if candidate_profile:
            self.form_filler = SmartFormFiller(self.page, candidate_profile)
        else:
            self.form_filler = FormFiller(self.page)
            
        self.locator = LOCATORS
        self.execution_guard = execution_guard
        self.dry_run = dry_run
        self.human = HumanInteraction(self.page)
        self.metrics = metrics

    def _safe_click(self, element, timeout_ms: int = 3000) -> bool:
        """Resilient multi-tier click with hover and force options"""
        if not element:
            return False
        try:
            try:
                element.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            try:
                element.hover(timeout=1000)
            except Exception:
                pass
            try:
                element.click(timeout=timeout_ms)
                return True
            except Exception:
                pass
            try:
                element.click(timeout=timeout_ms, force=True)
                return True
            except Exception:
                pass
            try:
                element.evaluate("el => el.click()")
                return True
            except Exception:
                pass
            return False
        except Exception as e:
            logger.debug(f"Safe click error: {e}")
            return False
    def is_modal_open(self) -> bool:
        """Check if an Easy Apply modal or application dialog is currently visible on screen"""
        modal_content_selectors = [
            "dialog[open][data-testid='dialog']",
            "dialog[open]:has(button)",
            "dialog[open]",
            ".jobs-easy-apply-modal",
            "div[data-test-easy-apply-form-container]",
            "div[role='dialog']:has-text('Easy Apply')",
            "div[role='dialog']:has(button:has-text('Next'), button:has-text('Review'), button:has-text('Submit'))"
        ]
        for sel in modal_content_selectors:
            try:
                loc = self.page.locator(sel)
                for elem in loc.all():
                    try:
                        if elem.is_visible():
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def apply_to_job(self, jobID, phone_number):
        if self.metrics:
            self.metrics.increment("attempted")

        if self.execution_guard and not self.execution_guard.can_apply():
            if self.metrics:
                self.metrics.increment("skipped")
            return False

        # If an application modal is already open on screen, check if finished or continue filling it
        if self.is_modal_open():
            if self._verify_submission(jobID):
                self.close_modal(is_submitted=True)
            else:
                logger.info(f"Existing application modal open - continuing application for job {jobID}", job_id=jobID, step="apply")
                start_time = time.time()
                result = self.send_resume(jobID, start_time)
                self.close_modal(is_submitted=result)
                return result

        self.get_job_page(jobID)
        time.sleep(1)

        button = self.get_easy_apply_button()

        if button is not False:
            if any(word in self.page.title() for word in self.blacklist_titles):
                logger.info('Skipping this application, a blacklisted keyword was found in the job position', 
                           job_id=jobID, step="apply", event="blacklist")
                string_easy = "* Contains blacklisted keyword"
                result = False
            else:
                string_easy = "* has Easy Apply Button"
                logger.info("Clicking the EASY apply button", job_id=jobID, step="apply", event="click_apply")
                
                # Extract job details and record application start
                try:
                    browser_title = self.page.title()
                    # Parse title: "Job Title | Company | LinkedIn"
                    parts = browser_title.split(' | ')
                    job_title = parts[0].strip() if len(parts) > 0 else "Unknown"
                    company = parts[1].strip() if len(parts) > 1 else "Unknown"
                    
                    # Record application start with comprehensive tracking
                    candidate_id = self.candidate_profile.get('name', 'default') if self.candidate_profile else 'default'
                    self.store.start_application(
                        job_id=jobID,
                        job_title=job_title,
                        company=company,
                        candidate_id=candidate_id,
                        job_url=self.page.url
                    )
                except Exception as e:
                    logger.warning(f"Failed to parse job details for tracking: {e}", job_id=jobID)
                
                try:
                    # Record start time for duration tracking
                    start_time = time.time()
                    
                    self._safe_click(button)
                    logger.debug("Waiting for modal to open...", job_id=jobID, step="apply")
                    # Resilient wait for modal to appear
                    modal_opened = False
                    for _ in range(12):
                        time.sleep(0.5)
                        if self.is_modal_open():
                            modal_opened = True
                            break
                    if not modal_opened:
                        logger.debug("Retrying click on Easy Apply button...", job_id=jobID, step="apply")
                        try:
                            button.click(force=True, timeout=2000)
                        except Exception:
                            self._safe_click(button)
                        for _ in range(8):
                            time.sleep(0.5)
                            if self.is_modal_open():
                                modal_opened = True
                                break
                    if not modal_opened:
                        try:
                            self.page.screenshot(path="debug_modal_failed.png")
                            with open("debug_modal_failed.html", "w", encoding="utf-8") as df:
                                df.write(self.page.content())
                            logger.info("Saved debug_modal_failed.png and debug_modal_failed.html", job_id=jobID, step="apply")
                        except Exception:
                            pass
                        logger.warning("⚠️ Easy Apply modal did not appear after clicking button. Skipping job.", job_id=jobID, step="apply")
                        result = False
                        string_easy = "*Modal failed to open"
                    else:
                        logger.info("✅ Easy Apply modal opened successfully", job_id=jobID, step="apply")
                        result = self.send_resume(jobID, start_time)
                        if result:
                            string_easy = "*Applied: Sent Resume"
                        else:
                            string_easy = "*Did not apply: Failed to send Resume"
                except Exception as e:
                    logger.error(f"Error during application: {e}", job_id=jobID, step="apply", exception=e)
                    result = False
                    string_easy = "*Error during application"
        elif "You applied on" in self.page.content():
            logger.info("You have already applied to this position.", job_id=jobID, step="apply", event="already_applied")
            string_easy = "* Already Applied"
            result = False
        else:
            logger.info("The Easy apply button does not exist.", job_id=jobID, step="apply", event="no_button")
            string_easy = "* Doesn't have Easy Apply Button"
            result = False

        logger.info(f"\nPosition {jobID}:\n {self.page.title()} \n {string_easy} \n", 
                   job_id=jobID, step="apply", event="summary")

        self.store.write_to_file(button, jobID, self.page.title(), result)
        
        # CRITICAL: Only dismiss post-submission dialogs if application was successfully submitted
        self.close_modal(is_submitted=result)
        
        return result
    
    def close_modal(self, is_submitted: bool = False):
        """
        Close post-submission dialogs ONLY.
        STRICT ZERO-DISCARD POLICY: Under no circumstances does this click 'Discard' or close an in-progress application.
        """
        try:
            if is_submitted:
                done_selectors = [
                    "dialog[open] button:has-text('Done')",
                    "dialog[open] button:has-text('Dismiss')",
                    "dialog[open] button[aria-label='Dismiss']",
                    ".jobs-easy-apply-modal button:has-text('Done')",
                    ".jobs-easy-apply-modal button:has-text('Dismiss')"
                ]
                for sel in done_selectors:
                    try:
                        btn = self.page.locator(sel).first
                        if btn.count() > 0 and btn.is_visible(timeout=1000):
                            self._safe_click(btn)
                            logger.info("Dismissed post-submission confirmation modal", step="cleanup")
                            time.sleep(1)
                            return
                    except:
                        continue
            else:
                logger.debug("Zero-Discard Policy: Preserving open application modal.", step="cleanup")
        except Exception as e:
            logger.debug(f"Post-submission modal cleanup: {e}", step="cleanup")

    def wait_for_submit_confirmation(self, jobID):
        try:
            logger.info("⏸️ Application paused for manual review before submission.", job_id=jobID, step="confirmation")
            
            # Inject the modal via evaluate
            self.page.evaluate("""() => {
                // Clear any existing overlay
                const existing = document.getElementById('bot-submission-overlay');
                if (existing) existing.remove();

                const overlay = document.createElement('div');
                overlay.id = 'bot-submission-overlay';
                // Use pointer-events: none so user can click/scroll the page behind it
                overlay.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 9999999; pointer-events: none; display: flex; align-items: flex-start; justify-content: flex-end; padding: 30px; transition: opacity 0.3s ease;';
                
                const modal = document.createElement('div');
                // Re-enable pointer events for the modal itself
                modal.style.cssText = 'background: #121212; color: white; padding: 24px; border-radius: 24px; width: 380px; box-shadow: 0 20px 50px rgba(0,0,0,0.6); font-family: "Outfit", "Inter", -apple-system, system-ui, sans-serif; border: 1px solid #333; text-align: left; transform: translateY(-20px); opacity: 0; transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.1); pointer-events: auto;';
                
                const header = document.createElement('div');
                header.style.cssText = 'display: flex; align-items: center; gap: 12px; margin-bottom: 16px;';
                
                const icon = document.createElement('div');
                icon.innerHTML = '🚀';
                icon.style.cssText = 'font-size: 28px;';
                
                const title = document.createElement('h2');
                title.innerText = 'Review & Submit';
                title.style.cssText = 'font-size: 20px; margin: 0; font-weight: 800; background: linear-gradient(135deg, #ff4500, #ff8c00); -webkit-background-clip: text; -webkit-text-fill-color: transparent;';
                
                header.appendChild(icon);
                header.appendChild(title);
                
                const desc = document.createElement('p');
                desc.innerText = 'The bot has finished filling the form. Please review the application on the left and then decide:';
                desc.style.cssText = 'font-size: 14px; margin-bottom: 20px; color: #bbb; line-height: 1.5;';
                
                const btnContainer = document.createElement('div');
                btnContainer.style.cssText = 'display: flex; gap: 12px;';
                
                const proceedBtn = document.createElement('button');
                proceedBtn.innerText = 'Submit Now';
                proceedBtn.style.cssText = 'background: #ff4500; color: white; border: none; padding: 12px 20px; border-radius: 12px; cursor: pointer; font-weight: 700; font-size: 14px; flex: 1.5; transition: all 0.2s;';
                proceedBtn.onmouseenter = () => proceedBtn.style.background = '#ff571a';
                proceedBtn.onmouseleave = () => proceedBtn.style.background = '#ff4500';
                
                const skipBtn = document.createElement('button');
                skipBtn.innerText = 'Skip Job';
                skipBtn.style.cssText = 'background: #2a2a2a; color: #ddd; border: 1px solid #444; padding: 12px 20px; border-radius: 12px; cursor: pointer; font-weight: 600; font-size: 14px; flex: 1; transition: all 0.2s;';
                skipBtn.onmouseenter = () => skipBtn.style.background = '#333';
                skipBtn.onmouseleave = () => skipBtn.style.background = '#2a2a2a';
                
                proceedBtn.onclick = () => { 
                    if (window._botClickListener) document.removeEventListener('click', window._botClickListener, true);
                    window.bot_submit_action = 'proceed'; 
                    modal.style.transform = 'translateY(-20px)';
                    modal.style.opacity = '0';
                    setTimeout(() => overlay.remove(), 400);
                };
                
                skipBtn.onclick = () => { 
                    if (window._botClickListener) document.removeEventListener('click', window._botClickListener, true);
                    window.bot_submit_action = 'skip'; 
                    modal.style.transform = 'translateY(-20px)';
                    modal.style.opacity = '0';
                    setTimeout(() => overlay.remove(), 400);
                };
                
                btnContainer.appendChild(skipBtn);
                btnContainer.appendChild(proceedBtn);
                modal.appendChild(header);
                modal.appendChild(desc);
                modal.appendChild(btnContainer);
                overlay.appendChild(modal);
                document.body.appendChild(overlay);
                
                // Trigger animation
                setTimeout(() => {
                    modal.style.transform = 'translateY(0)';
                    modal.style.opacity = '1';
                }, 50);
                
                // Fallback listener for manual LinkedIn clicks
                if (window._botClickListener) document.removeEventListener('click', window._botClickListener, true);
                window._botClickListener = (e) => {
                    const btn = e.target.closest('button');
                    if (btn && !btn.closest('#bot-submission-overlay')) {
                        if (btn.getAttribute('aria-label') === 'Submit application' || (btn.innerText && btn.innerText.includes('Submit'))) {
                            if (window._botClickListener) document.removeEventListener('click', window._botClickListener, true);
                            window.bot_submit_action = 'proceed';
                            modal.style.transform = 'translateY(-20px)';
                            modal.style.opacity = '0';
                            setTimeout(() => overlay.remove(), 400);
                        }
                    }
                };
                document.addEventListener('click', window._botClickListener, true);
                
                window.bot_submit_action = null;
            }""")
            
            # Wait for user action
            start_time = time.time()
            max_wait = 1800  # 30 minutes
            
            while (time.time() - start_time) < max_wait:
                try:
                    action = self.page.evaluate("() => window.bot_submit_action")
                    if action == 'proceed':
                        logger.info("✅ User confirmed submission. Proceeding...", job_id=jobID, step="confirmation")
                        return True
                    elif action == 'skip':
                        logger.info("⏭️ User skipped submission.", job_id=jobID, step="confirmation")
                        return False
                except Exception as e:
                    # If browser is closed, stop waiting
                    if "closed" in str(e).lower() or "target" in str(e).lower():
                        logger.warning("Browser closed during confirmation wait.", job_id=jobID, step="confirmation")
                        return False
                    raise e
                    
                time.sleep(1)
            
            # Timeout
            logger.warning("⏰ Submission confirmation timed out after 30 mins.", job_id=jobID, step="confirmation")
            self.page.evaluate("() => { const o = document.getElementById('bot-submission-overlay'); if(o) o.remove(); }")
            return False
            
        except Exception as e:
            logger.error(f"Error in submission confirmation: {e}", step="confirmation")
            return True # Fallback to proceed if error occurs in UI injection

    @retry(max_attempts=3, delay=1)
    def get_job_page(self, jobID):
        """Ensure preview panel is loaded for jobID by clicking the job card container"""
        try:
            preview = self.page.locator(".jobs-search__job-details, .job-view-layout, [data-view-name='job-details']").first
            if preview.count() > 0 and preview.locator("button[aria-label*='Easy Apply'], button:has-text('Easy Apply')").count() > 0:
                logger.debug(f'Preview panel already loaded with Easy Apply for job {jobID}', step='get_job_page')
                return

            card_selectors = [
                f"div[componentkey='job-card-component-ref-{jobID}']",
                f"[componentkey*='{jobID}']",
                f"li[data-occludable-job-id*='{jobID}']",
                f"[data-job-id='{jobID}']"
            ]
            for c_sel in card_selectors:
                try:
                    card = self.page.locator(c_sel).first
                    if card.count() > 0:
                        card.scroll_into_view_if_needed(timeout=1000)
                        card.click(timeout=2000)
                        time.sleep(1.5)
                        return
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f'get_job_page note: {e}', step='get_job_page')

    def get_easy_apply_button(self):
        """
        Find and return the Easy Apply button in the preview panel.
        Strictly requires 'Easy Apply' to avoid clicking external apply buttons or nav links.
        """
        try:
            if "/jobs/" not in self.page.url:
                logger.warning("Browser navigated away from jobs page. Returning False to recover.", step="get_button")
                return False

            time.sleep(1.5)  # Let preview panel settle
            
            preview_containers = [
                ".jobs-search__job-details",
                ".job-view-layout",
                "div[data-view-name='job-details']",
                ".jobs-details__main-content",
                ".jobs-details"
            ]
            
            for container_sel in preview_containers:
                try:
                    container = self.page.locator(container_sel).first
                    if container.count() > 0 and container.is_visible():
                        btn = container.locator(
                            "button.jobs-apply-button, "
                            "button[aria-label*='Easy Apply'], "
                            "button:has-text('Easy Apply'), "
                            "#jobs-apply-button-id"
                        ).first
                        if btn.count() > 0 and btn.is_visible():
                            logger.debug("Found Easy Apply button in preview container", step="get_button")
                            return btn
                except Exception:
                    continue

            easy_apply_selectors = [
                "button[aria-label*='Easy Apply']",
                "button.jobs-apply-button:has-text('Easy Apply')",
                "button.jobs-apply-button",
                "button:has-text('Easy Apply')"
            ]
            for sel in easy_apply_selectors:
                try:
                    candidates = self.page.locator(sel).all()
                    for btn in candidates:
                        try:
                            if not btn.is_visible():
                                continue
                            aria_label = (btn.get_attribute("aria-label") or "").lower()
                            text = (btn.text_content() or "").lower()
                            if "filter" in aria_label or "pill" in aria_label:
                                continue
                            if "easy apply" in aria_label or "easy apply" in text:
                                logger.debug(f"Found Easy Apply button: aria='{aria_label}', text='{text}'", step="get_button")
                                return btn
                        except Exception:
                            continue
                except Exception:
                    continue

            logger.info("Easy Apply button not present for this job (may be external apply only).", step="get_button")
            return False
            
        except Exception as e:
            if self.metrics:
                self.metrics.increment("failed")
            logger.debug(f"Easy Apply button check error: {e}", step="get_button", exception=e)
            return False

    def get_elements(self, type) -> list:
        """Get visible elements by type checking primary and fallback locators"""
        for use_fb in [False, True]:
            selector = get_locator(type, use_fallback=use_fb)
            if selector and self.is_present(selector):
                try:
                    found = self.page.locator(selector).all()
                    visible = [el for el in found if el.is_visible()]
                    if visible:
                        return visible
                except Exception:
                    continue
        return []
    def is_present(self, selector):
        """
        Check if element is present on page
        """
        try:
            return self.page.locator(selector).count() > 0
        except:
            return False

    def has_visible_errors(self):
        """Safely check if there are any visible form errors (prevents detach exceptions during redirects)"""
        try:
            for e in self.get_elements("error"):
                try:
                    if e.is_visible():
                        return True
                except Exception:
                    # Ignore individual detached elements
                    continue
        except Exception:
            # If nodes detach mid-check, the page shifted (usually a success state)
            pass
        return False

    @retry(max_attempts=5, delay=1)
    def send_resume(self, jobID=None, start_time=None) -> bool:
        """
        Navigate through multi-step application form
        """
        try:
            submitted = False
            loop = 0
            form_pages = 0
            candidate_id = self.candidate_profile.get('name', 'default') if self.candidate_profile else 'default'
            if not self.is_modal_open():
                logger.warning("⚠️ Application modal is not open, aborting send_resume.", job_id=jobID, step="apply_loop")
                return False
            while loop < 40:  # Multi-page resilient loop
                time.sleep(1)
                
                # Auto-cancel any open "Add education" / "Add experience" blank subforms to prevent form blocking
                try:
                    edu_cancel = self.page.locator(".jobs-easy-apply-form-section__grouping button:has-text('Cancel'), .jobs-easy-apply-modal__content button[aria-label*='Cancel'], .jobs-easy-apply-modal__content button:has-text('Cancel')").first
                    if edu_cancel.count() > 0 and edu_cancel.is_visible():
                        logger.info("Dismissing open Add Education/Experience subform via Cancel button to preserve profile data", job_id=jobID, step="education")
                        self._safe_click(edu_cancel)
                        time.sleep(1)
                except Exception as e:
                    logger.debug(f"Check cancel button note: {e}")

                # Handle Document Upload Redesign Cards
                try:
                    resume_cards = self.page.locator(".jobs-document-upload-redesign-card__container, [data-test-document-upload-card], div[class*='jobs-document-upload-card']").all()
                    if resume_cards:
                        selected_cards = self.page.locator(".jobs-document-upload-redesign-card__container--selected, .jobs-document-upload-redesign-card__container[aria-label='Selected'], [aria-checked='true'], [aria-label*='Selected']").all()
                        if not selected_cards:
                            logger.info("Selecting first available resume card from redesign UI...", job_id=jobID, step="upload_resume")
                            self._safe_click(resume_cards[0])
                            time.sleep(1)
                        else:
                            logger.debug("Resume card already selected in redesign UI", job_id=jobID, step="upload_resume")
                except Exception as e:
                    logger.debug(f"Document upload redesign check: {e}")

                # Upload resume file if file input present
                resume_selector = get_locator("upload_resume")
                if self.is_present(resume_selector):
                    try:
                        resume = self.uploads.get("Resume")
                        if resume:
                            abs_resume = os.path.abspath(resume)
                            self.page.locator(resume_selector).first.set_input_files(abs_resume)
                            logger.info(f"Uploaded resume: {abs_resume}", job_id=jobID, step="upload_resume")
                    except Exception as e:
                        logger.error(f"Resume upload failed: {e}", job_id=jobID, step="upload_resume", exception=e)

                # Upload cover letter
                cv_selector = get_locator("upload_cv")
                if self.is_present(cv_selector):
                    cv = self.uploads.get("Cover Letter")
                    if cv:
                        try:
                            abs_cv = os.path.abspath(cv)
                            self.page.locator(cv_selector).first.set_input_files(abs_cv)
                            logger.info(f"Uploaded cover letter: {abs_cv}", job_id=jobID, step="upload_cv")
                        except Exception as e:
                            pass

                # Click 'Follow Company' if present
                follow_selector = get_locator("follow")
                follow_elements = self.get_elements("follow")
                if follow_elements:
                    for element in follow_elements:
                        try:
                            self._safe_click(element)
                            logger.info("Clicked 'Follow Company' checkbox", job_id=jobID, step="follow")
                        except:
                            pass

                # FILL FORM FIELDS FIRST (before clicking buttons)
                logger.debug("Attempting to fill form fields...", job_id=jobID, step="fill_form")
                if hasattr(self.form_filler, 'fill_all_fields'):
                    # SmartFormFiller
                    self.form_filler.fill_all_fields()
                else:
                    # Old FormFiller (fallback)
                    pass
                
                time.sleep(1)  # Let fields settle

                # Check for submit button
                if len(self.get_elements("submit")) > 0:
                    elements = self.get_elements("submit")
                    for element in elements:
                        element.wait_for(state="visible", timeout=5000)
                        
                        if self.dry_run and not self.dry_run.validate_submit():
                            submitted = True
                            logger.info("Fake success for dry run", job_id=jobID, step="submit", event="dry_run_success")
                            break
                        
                        if self.review_mode:
                            # NEW: Wait for user confirmation before final click
                            user_decision = self.wait_for_submit_confirmation(jobID)
                            
                            if not user_decision:
                                logger.warning("⏭️ User clicked 'Skip Job' - Recording skip", job_id=jobID, step="user_action")
                                self.store.record_user_skip(jobID, candidate_id)
                                submitted = False
                                break
                            
                            logger.info("🎯 User clicked 'Submit Now' - Application WILL be submitted", job_id=jobID, step="user_action")
                            self.store.record_user_confirmation(jobID, candidate_id)
                        else:
                            # Direct submission: add random delay
                            delay = random.uniform(2.0, 5.0)
                            logger.info(f"Direct submission: sleeping {delay:.2f} seconds before submitting...", job_id=jobID, step="submit")
                            time.sleep(delay)
                        
                        # Record start time for this submission attempt
                        submit_start_time = time.time()
                        
                        # Click the native submit button using resilient safe click
                        self._safe_click(element)
                        
                        # Wait for LinkedIn to process
                        logger.info("⏳ Waiting for LinkedIn to process...", job_id=jobID, step="submit")
                        time.sleep(4)
                        
                        # Check if errors appeared after submit
                        has_errors = self.has_visible_errors()
                        if has_errors:
                            logger.warning("⚠️ Form errors after submit button click - Triggering error resolution...", job_id=jobID, step="submit")
                            if hasattr(self.form_filler, 'handle_errored_fields'):
                                self.form_filler.handle_errored_fields()
                            continue
                        
                        # Try to verify LinkedIn accepted it
                        linkedin_verified = False
                        try:
                            if not self.is_present(".jobs-easy-apply-modal") and not self.is_present("dialog[open]"):
                                linkedin_verified = True
                            elif not self.is_present("button[aria-label='Submit application']") and not self.is_present("dialog button:has-text('Submit')"):
                                linkedin_verified = True
                            elif self.is_present("dialog") or self.is_present(".artdeco-modal__content"):
                                content = (self.page.locator("dialog, .artdeco-modal__content").first.text_content(timeout=2000) or "").lower()
                                if any(w in content for w in ["sent", "submitted", "received", "thank"]):
                                    linkedin_verified = True
                        except:
                            pass
                        
                        duration = round(time.time() - start_time, 2) if start_time else None
                        
                        if linkedin_verified:
                            logger.info(f"✅ LinkedIn confirmed submission! (Duration: {duration}s)", job_id=jobID, step="submit", event="success")
                        else:
                            logger.info(f"✅ Application submitted (Duration: {duration}s)", job_id=jobID, step="submit")
                        
                        self.store.record_submission_success(
                            job_id=jobID,
                            candidate_id=candidate_id,
                            duration_seconds=duration,
                            form_pages=form_pages,
                            fields_filled=0
                        )
                        
                        submitted = True
                        time.sleep(2)
                        break
                    
                    if submitted:
                        break

                # Check for review button (before next button)
                elif len(self.get_elements("review")) > 0:
                    needs_res = self.has_visible_errors() or (hasattr(self.form_filler, 'has_unfilled_fields') and self.form_filler.has_unfilled_fields())
                    if needs_res:
                        logger.warning("⚠️ Form errors or unfilled fields before Review click - Resolving...", job_id=jobID, step="review")
                        if hasattr(self.form_filler, 'handle_errored_fields'):
                            self.form_filler.handle_errored_fields()

                    elements = self.get_elements("review")
                    for element in elements:
                        # Safety filter: NEVER click "Add" buttons
                        try:
                            btn_text = (element.text_content() or "").strip().lower()
                            aria_label = (element.get_attribute("aria-label") or "").strip().lower()
                            if "add" in btn_text or "add" in aria_label:
                                logger.debug(f"Skipping Add button: text='{btn_text}', aria='{aria_label}'", step="review")
                                continue
                        except Exception:
                            pass
                            
                        element.wait_for(state="visible", timeout=5000)
                        self._safe_click(element)
                        time.sleep(1.5)

                        # Check if LinkedIn highlighted unfilled required fields after clicking Review
                        if self.has_visible_errors() or (hasattr(self.form_filler, 'has_unfilled_fields') and self.form_filler.has_unfilled_fields()):
                            logger.warning("⚠️ Validation errors or unfilled fields detected after Review click - Resolving...", job_id=jobID, step="review")
                            if hasattr(self.form_filler, 'handle_errored_fields'):
                                self.form_filler.handle_errored_fields()
                        else:
                            form_pages += 1
                            logger.info(f"Clicked Review button (page {form_pages})", job_id=jobID, step="review")
                        break

                # Check for next button
                elif len(self.get_elements("next")) > 0:
                    needs_res = self.has_visible_errors() or (hasattr(self.form_filler, 'has_unfilled_fields') and self.form_filler.has_unfilled_fields())
                    if needs_res:
                        logger.warning("⚠️ Form errors or unfilled fields before Next click - Resolving...", job_id=jobID, step="next")
                        if hasattr(self.form_filler, 'handle_errored_fields'):
                            self.form_filler.handle_errored_fields()

                    elements = self.get_elements("next")
                    for element in elements:
                        # Safety filter: NEVER click "Add", "Add more", "Add education", or "Add degree" buttons
                        try:
                            btn_text = (element.text_content() or "").strip().lower()
                            aria_label = (element.get_attribute("aria-label") or "").strip().lower()
                            if "add" in btn_text or "add" in aria_label:
                                logger.debug(f"Skipping Add button: text='{btn_text}', aria='{aria_label}'", step="next")
                                continue
                        except Exception:
                            pass
                            
                        element.wait_for(state="visible", timeout=5000)
                        self._safe_click(element)
                        form_pages += 1
                        logger.info(f"Clicked Next button (page {form_pages})", job_id=jobID, step="next")
                        time.sleep(1.5)

                        # Check if LinkedIn highlighted unfilled required fields after clicking Next
                        if self.has_visible_errors() or (hasattr(self.form_filler, 'has_unfilled_fields') and self.form_filler.has_unfilled_fields()):
                            logger.warning("⚠️ Validation errors or unfilled fields detected after Next click - Resolving...", job_id=jobID, step="next")
                            if hasattr(self.form_filler, 'handle_errored_fields'):
                                self.form_filler.handle_errored_fields()
                        break

                # Check for follow button
                elif len(self.get_elements("follow")) > 0:
                    elements = self.get_elements("follow")
                    for element in elements:
                        element.wait_for(state="visible", timeout=5000)
                        self._safe_click(element)
                        logger.info("Clicked Follow button", job_id=jobID, step="follow")
                        break
                
                else:
                    # Check direct text fallback for Submit / Review / Next buttons inside modal
                    try:
                        modal_box = self.page.locator("dialog[open], dialog[data-testid='dialog'], dialog, .jobs-easy-apply-modal, .artdeco-modal, div[role='dialog']").first
                        modal_submit = modal_box.locator("button").filter(has_text=re.compile(r"Submit", re.I)).first
                        modal_review = modal_box.locator("button").filter(has_text=re.compile(r"Review", re.I)).first
                        modal_next = modal_box.locator("button").filter(has_text=re.compile(r"Next|Continue", re.I)).first
                        
                        if modal_submit.count() > 0 and modal_submit.is_visible():
                            logger.info("Found Submit button via modal fallback, clicking...", job_id=jobID, step="submit")
                            self._safe_click(modal_submit)
                            submitted = True
                            time.sleep(2)
                            break
                        elif modal_review.count() > 0 and modal_review.is_visible():
                            self._safe_click(modal_review)
                            form_pages += 1
                            logger.info(f"Clicked Review button via modal text fallback (page {form_pages})", job_id=jobID, step="review")
                            time.sleep(1.5)
                            continue
                        elif modal_next.count() > 0 and modal_next.is_visible():
                            self._safe_click(modal_next)
                            form_pages += 1
                            logger.info(f"Clicked Next button via modal text fallback (page {form_pages})", job_id=jobID, step="next")
                            time.sleep(1.5)
                            continue
                    except Exception:
                        pass

                    # No buttons found - check if complete or stuck
                    logger.warning(f"⚠️ No Next/Review/Submit buttons found (loop {loop})", job_id=jobID, step="no_action")
                    if not self.is_modal_open():
                        if self._verify_submission(jobID):
                            logger.info("Modal closed with verified submission", job_id=jobID, step="complete")
                            submitted = True
                        else:
                            logger.warning("Modal closed or dismissed without confirmed submission", job_id=jobID, step="incomplete")
                            submitted = False
                        break
                    
                    if self.is_present(".jobs-easy-apply-form-section__grouping, fieldset, div[data-test-form-builder-component]"):
                        logger.warning("Form fields present but progression stuck - attempting field refill...", job_id=jobID)
                        if hasattr(self.form_filler, 'fill_all_fields'):
                            self.form_filler.fill_all_fields(force_refill=True)
                        if hasattr(self.form_filler, 'handle_errored_fields'):
                            self.form_filler.handle_errored_fields()
                
                loop += 1
                
                if loop % 5 == 0:
                    logger.info(f"Application loop iteration {loop}/40", job_id=jobID, step="progress")

            # Final check before concluding: If modal is still open, prompt human instead of failing/discarding
            if not submitted and self.is_modal_open():
                logger.warning("⚠️ Application modal still open - Zero-Discard Policy: Prompting user to complete in browser...", job_id=jobID)
                from bot.utils.human_popup import prompt_human_with_popup
                prompt_human_with_popup(
                    question_text=f"Job Application ({jobID}) requires manual review/completion in the browser. Zero-Discard Guarantee is active (bot will never discard).",
                    field_type="MANUAL_SUBMISSION",
                    data_type="ACTION",
                    timeout_seconds=600,
                    suggested_answer="Completed"
                )
                time.sleep(3)
                if self._verify_submission(jobID) or not self.is_modal_open():
                    submitted = True

            if not submitted:
                logger.warning(f"⚠️ Application loop finished for job {jobID} without confirmed submission (modal preserved)", job_id=jobID, step="incomplete")
                if self.metrics:
                    self.metrics.increment("failed")

        except Exception as e:
            logger.error(f"Error during application process: {e}", job_id=jobID, step="apply_loop", exception=e)
            if self.metrics:
                self.metrics.increment("failed")

        if submitted and self.execution_guard:
            self.execution_guard.on_success()
            
        if submitted and self.metrics:
            self.metrics.increment("submitted")

        return submitted

    def _click_submit_button(self, jobID=None) -> bool:
        """Robustly find and click the LinkedIn 'Submit application' button"""
        try:
            selectors = [
                get_locator("submit"),                         
                get_locator("submit", use_fallback=True),      
                "button:has-text('Submit application')",      
                "button[type='submit']",                       
                "footer button.artdeco-button--primary"        
            ]

            for selector in selectors:
                try:
                    btn = self.page.locator(selector).first
                    if btn.count() > 0 and btn.is_visible():
                        if self._safe_click(btn):
                            logger.info(f"Clicked Submit button using selector: {selector}", job_id=jobID)
                            if self.metrics:
                                self.metrics.increment("submit_clicks")
                            return True
                except:
                    continue

            try:
                btn = self.page.get_by_role("button", name=re.compile("Submit application|Submit", re.I)).first
                if btn.count() > 0 and btn.is_visible():
                    if self._safe_click(btn):
                        logger.info("Clicked Submit button using Role API", job_id=jobID)
                        if self.metrics:
                            self.metrics.increment("submit_clicks")
                        return True
            except:
                pass

            return False
        except Exception as e:
            logger.error(f"Error clicking submit: {e}", job_id=jobID)
            return False

    def _verify_submission(self, jobID=None) -> bool:
        """Thoroughly check if the application was actually sent"""
        try:
            modal = self.page.locator("dialog[open], .jobs-easy-apply-modal, .artdeco-modal").first
            if modal.count() > 0 and modal.is_visible():
                content = (modal.text_content(timeout=1000) or "").lower()
                success_phrases = ["your application was sent", "application sent", "application submitted"]
                if any(phrase in content for phrase in success_phrases):
                    return True

            if self.page.locator("h2:has-text('Application sent'), h3:has-text('Application sent')").count() > 0:
                return True

            return False
        except:
            return False

    def _wait_for_submit_confirmation(self, jobID) -> bool:
        """Display a sleek top-bar banner to review and confirm submission, matching question style."""
        try:
            self.page.evaluate("""(jobID) => { 
                // Reset flags
                window.bot_submit_confirmed = false;
                window.bot_submit_skipped = false;

                // Create Modern Floating Banner (Sleek Top Bar)
                const banner = document.createElement('div');
                banner.id = 'bot-review-banner';
                banner.style.cssText = 'position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: #1a1a1a; color: white; padding: 12px 25px; border-radius: 12px; z-index: 999999; box-shadow: 0 10px 30px rgba(0,0,0,0.5); font-family: -apple-system, system-ui, sans-serif; display: flex; align-items: center; gap: 20px; border: 1px solid #ff4500; min-width: 400px; animation: slideDown 0.4s ease-out;';
                
                const style = document.createElement('style');
                style.innerHTML = `@keyframes slideDown { from { transform: translate(-50%, -100%); opacity: 0; } to { transform: translate(-50%, 0); opacity: 1; } }`;
                document.head.appendChild(style);

                const text = document.createElement('div');
                text.innerHTML = `<span style="color: #ff4500; font-weight: bold; font-size: 14px; display: block;">🚀 READY TO SUBMIT?</span> <span style="font-size: 13px; color: #ccc;">Please review details for Job ${jobID}</span>`;
                text.style.flex = '1';
                
                const btnContainer = document.createElement('div');
                btnContainer.style.display = 'flex';
                btnContainer.style.gap = '10px';

                // Proceed Button
                const proceedBtn = document.createElement('button');
                proceedBtn.textContent = '✅ Proceed';
                proceedBtn.style.cssText = 'background: #ff4500; color: white; border: none; padding: 8px 18px; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px; transition: 0.2s;';
                proceedBtn.onclick = () => { window.bot_submit_confirmed = true; banner.style.opacity = '0.5'; banner.innerText = '⌛ Submitting...'; };

                // Skip Button
                const skipBtn = document.createElement('button');
                skipBtn.textContent = '⏭️ Skip';
                skipBtn.style.cssText = 'background: #333; color: white; border: none; padding: 8px 15px; border-radius: 8px; cursor: pointer; font-size: 12px;';
                skipBtn.onclick = () => { window.bot_submit_skipped = true; banner.remove(); };

                btnContainer.appendChild(skipBtn);
                btnContainer.appendChild(proceedBtn);
                banner.appendChild(text);
                banner.appendChild(btnContainer);
                document.body.appendChild(banner);
            }""", jobID)

            print(f"\n" + "!"*60)
            print(f"🚀 REVIEW REQUIRED for Job {jobID}")
            print(f"👉 Application is filled. Please review and click 'Proceed' in the browser.")
            print("!"*60 + "\n")

            while True: 
                is_confirmed = self.page.evaluate("() => window.bot_submit_confirmed")
                is_skipped = self.page.evaluate("() => window.bot_submit_skipped")

                if is_confirmed:
                    self.page.evaluate("() => { const b = document.getElementById('bot-review-banner'); if(b) b.remove(); }")
                    return True
                if is_skipped:
                    return False
                time.sleep(0.5)
            return False

        except Exception as e:
            logger.error(f"Error in review banner: {e}")
            return True