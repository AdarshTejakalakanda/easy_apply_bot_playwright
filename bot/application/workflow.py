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

    def apply_to_job(self, jobID, phone_number):
        if self.metrics:
            self.metrics.increment("attempted")

        if self.execution_guard and not self.execution_guard.can_apply():
            if self.metrics:
                self.metrics.increment("skipped")
            return False

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
                    
                    button.click()
                    logger.debug("Waiting for modal to open...", job_id=jobID, step="apply")
                    time.sleep(2)  # Wait for modal to fully open
                    
                    # Form filling happens inside send_resume now
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
            # ONLY dismiss confirmation modals if the application was ALREADY successfully submitted
            if is_submitted:
                done_selectors = [
                    "button:has-text('Done')",
                    "button:has-text('Dismiss')",
                    "button[aria-label='Dismiss']",
                    "button.artdeco-modal__dismiss"
                ]
                for sel in done_selectors:
                    try:
                        btn = self.page.locator(sel).first
                        if btn.count() > 0 and btn.is_visible(timeout=1000):
                            btn.click()
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
        """
        Don't navigate to full job page - LinkedIn hides Easy Apply button there!
        Instead, just ensure the job card is clicked to show preview panel on right.
        The job card should already be clicked by search.py, so we just wait a bit.
        """
        import time
        logger.debug(f"Waiting for job preview panel to load for job {jobID}", step="get_job_page")
        time.sleep(0.5)  # Minimal wait - card already clicked in search.py
        # Stay on search results page - Easy Apply button is in the preview panel!

    @retry(max_attempts=3, delay=1)
    def get_easy_apply_button(self):
        """
        Find and return the Easy Apply button in the preview panel
        (NOT the full job page - LinkedIn hides it there!)
        """
        try:
            # Wait for preview panel to load
            import time
            logger.debug("Waiting for preview panel with Easy Apply button...", step="get_button")
            time.sleep(5)  # Wait for preview panel to fully load
            
            # Debug: Count all buttons on page
            try:
                all_buttons = self.page.locator("button").count()
                logger.debug(f"Total buttons on page: {all_buttons}", step="get_button")
            except:
                pass
            
            # Try primary selector first (ID)
            button_selector = get_locator("easy_apply_button")
            logger.debug(f"Looking for Easy Apply button with selector: {button_selector}", step="get_button")
            
            try:
                # Wait for button to appear with longer timeout
                self.page.wait_for_selector(button_selector, timeout=10000, state="visible")
                buttons = self.page.locator(button_selector).all()
                logger.debug(f"Primary selector found {len(buttons)} buttons", step="get_button")
                
                for button in buttons:
                    try:
                        text = button.text_content(timeout=2000)
                        if text and ("Apply" in text or "Continue" in text):
                            logger.debug(f"Found Apply button: {text.strip()}", step="get_button")
                            return button
                    except:
                        continue
            except Exception as e:
                logger.debug(f"Primary selector didn't find button: {e}", step="get_button")
            
            # Try fallback selector
            fallback_selector = get_locator("easy_apply_button", use_fallback=True)
            if fallback_selector and fallback_selector != button_selector:
                logger.debug(f"Trying fallback selector: {fallback_selector}", step="get_button")
                try:
                    self.page.wait_for_selector(fallback_selector, timeout=10000, state="visible")
                    buttons = self.page.locator(fallback_selector).all()
                    logger.debug(f"Fallback selector found {len(buttons)} buttons", step="get_button")
                    
                    for button in buttons:
                        try:
                            text = button.text_content(timeout=2000)
                            if text and ("Apply" in text or "Continue" in text):
                                logger.debug(f"Found Apply button with fallback: {text.strip()}", step="get_button")
                                return button
                        except:
                            continue
                except Exception as e:
                    logger.debug(f"Fallback selector didn't find button: {e}", step="get_button")
            
            # Try text-based selector as last resort
            try:
                logger.debug("Trying text-based selector", step="get_button")
                button = self.page.get_by_role("button", name=re.compile("Apply|Continue", re.I)).first
                if button.is_visible(timeout=2000):
                    logger.debug("Found apply button with text selector", step="get_button")
                    return button
            except Exception as e:
                logger.debug(f"Text-based selector didn't find button: {e}", step="get_button")
            
            # Debug: Try to find ANY button with "Easy Apply" in text
            try:
                logger.debug("Searching for any button containing 'Easy Apply' text...", step="get_button")
                all_buttons = self.page.locator("button").all()
                for i, btn in enumerate(all_buttons[:30]):  # Check first 30 buttons
                    try:
                        text = btn.text_content(timeout=1000)
                        if text and ("Easy Apply" in text or "Continue" in text):
                            logger.debug(f"Found button with text at index {i}: {text.strip()}", step="get_button")
                            # Get its attributes
                            btn_id = btn.get_attribute("id", timeout=1000)
                            btn_class = btn.get_attribute("class", timeout=1000)
                            logger.debug(f"  ID: {btn_id}, Class: {btn_class}", step="get_button")
                            return btn
                    except:
                        continue
            except Exception as e:
                logger.debug(f"Manual search failed: {e}", step="get_button")
            
            logger.debug("Easy Apply button not found after all attempts", step="get_button")
            return False
            
        except Exception as e:
            if self.metrics:
                self.metrics.increment("failed")
            logger.debug(f"Easy Apply button not found: {e}", step="get_button", exception=e)
            return False

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
            while loop < 40:  # Multi-page resilient loop
                time.sleep(1)
                
                # Upload resume
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
                            element.click()
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
                        
                        # Click the native submit button
                        element.click()
                        
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
                            if not self.is_present(".jobs-easy-apply-modal"):
                                linkedin_verified = True
                            elif not self.is_present("button[aria-label='Submit application']"):
                                linkedin_verified = True
                            elif self.is_present(".artdeco-modal__content"):
                                content = self.page.locator(".artdeco-modal__content").first.text_content(timeout=2000)
                                if "application" in content.lower() and "sent" in content.lower():
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

                # Check for errors or unfilled required fields
                elif self.has_visible_errors() or (hasattr(self.form_filler, 'has_unfilled_fields') and self.form_filler.has_unfilled_fields()):
                    logger.warning("⚠️ Form contains unfilled fields or validation errors. Running auto-fill sweep...", job_id=jobID, step="form_error")
                    
                    if hasattr(self.form_filler, 'fill_all_fields'):
                        self.form_filler.fill_all_fields(force_refill=True)
                    
                    time.sleep(1)
                    
                    if self.has_visible_errors() or (hasattr(self.form_filler, 'has_unfilled_fields') and self.form_filler.has_unfilled_fields()):
                        logger.warning("🤔 Form still has missing/errored fields - Triggering On-Screen Popup for resolution...", 
                                       job_id=jobID, step="human_fix")
                        if hasattr(self.form_filler, 'handle_errored_fields'):
                            self.form_filler.handle_errored_fields()
                        time.sleep(1)
                    
                    continue

                # Check for next button
                elif len(self.get_elements("next")) > 0:
                    elements = self.get_elements("next")
                    for element in elements:
                        element.wait_for(state="visible", timeout=5000)
                        element.click()
                        form_pages += 1
                        logger.info(f"Clicked Next button (page {form_pages})", job_id=jobID, step="next")
                        break

                # Check for review button
                elif len(self.get_elements("review")) > 0:
                    elements = self.get_elements("review")
                    for element in elements:
                        element.wait_for(state="visible", timeout=5000)
                        element.click()
                        form_pages += 1
                        logger.info(f"Clicked Review button (page {form_pages})", job_id=jobID, step="review")
                        break

                # Check for follow button
                elif len(self.get_elements("follow")) > 0:
                    elements = self.get_elements("follow")
                    for element in elements:
                        element.wait_for(state="visible", timeout=5000)
                        element.click()
                        logger.info("Clicked Follow button", job_id=jobID, step="follow")
                        break
                
                else:
                    # No buttons found - check if complete or stuck
                    logger.warning(f"⚠️ No Next/Review/Submit buttons found (loop {loop})", job_id=jobID, step="no_action")
                    
                    if not self.is_present(".jobs-easy-apply-modal"):
                        logger.info("Modal closed - application complete", job_id=jobID, step="complete")
                        submitted = True
                        break
                    
                    if self.is_present(".jobs-easy-apply-form-section__grouping") or self.is_present("fieldset.fb-form-element"):
                        logger.warning("Form fields present but progression stuck - attempting field refill...", job_id=jobID)
                        if hasattr(self.form_filler, 'fill_all_fields'):
                            self.form_filler.fill_all_fields(force_refill=True)
                        if hasattr(self.form_filler, 'handle_errored_fields'):
                            self.form_filler.handle_errored_fields()
                
                loop += 1
                
                if loop % 5 == 0:
                    logger.info(f"Application loop iteration {loop}/40", job_id=jobID, step="progress")

            # Final check before concluding: If modal is still open, prompt human instead of failing/discarding
            if not submitted and self.is_present(".jobs-easy-apply-modal"):
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
                if not self.is_present(".jobs-easy-apply-modal") or self._verify_submission(jobID):
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

    def _click_submit_button(self, jobID) -> bool:
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
                    btn = self.page.locator(selector).visible().first
                    if btn.count() > 0:
                        btn.click()
                        logger.info(f"Clicked Submit button using selector: {selector}", job_id=jobID)
                        if self.metrics:
                            self.metrics.increment("submit_clicks")
                        return True
                except:
                    continue

            try:
                btn = self.page.get_by_role("button", name=re.compile("Submit application|Submit", re.I)).visible().first
                if btn.count() > 0:
                    btn.click()
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

    def _verify_submission(self, jobID) -> bool:
        """Thoroughly check if the application was actually sent"""
        try:
            success_confirmations = ["sent", "successfully", "done", "submitted", "received", "thanks"]
            if self.is_present(".artdeco-modal__content"):
                content = self.page.locator(".artdeco-modal__content").first.text_content(timeout=3000).lower()
                if any(word in content for word in success_confirmations):
                    return True

            if self.is_present("h2:has-text('Application sent')") or self.is_present("h2:has-text('successfully')"):
                return True

            if not self.is_present(".jobs-easy-apply-modal") and not self.is_present(get_locator("error")):
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