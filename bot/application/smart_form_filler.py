"""
Smart Form Filler with Human-in-Loop for Unknown Questions
Supports profile data, answer learning, and manual intervention
"""

import time
import re
import json
import os
from playwright.sync_api import Page, Locator
from bot.utils.logger import logger
from bot.utils.selectors import LOCATORS
from bot.application.llm_filler import LLMFiller


class SmartFormFiller:
    def __init__(self, page: Page, candidate_profile: dict):
        self.page = page
        self.candidate_profile = candidate_profile
        self.profile_data = candidate_profile.get('profile_data', {})
        # Assuming LOCATORS is defined globally or imported
        self.locator = LOCATORS 
        self.execution_guard = None
        self.dry_run = None
        self.metrics = None
        
        # Initialize LLM Auto-filler
        self.llm_filler = LLMFiller(candidate_profile)
        
        # Profile-specific learned answers file
        candidate_id = candidate_profile.get('id', 'default')
        self.learned_answers_file = f'./profiles/{candidate_id}/learned_answers.json'
        
        # Load previously learned answers for this candidate
        self.learned_answers = self._load_learned_answers()
        logger.info(f"Loaded {len(self.learned_answers)} learned answers for {candidate_id}", step="init")
        
        # GLiNER for smart question matching (optional)
        self.gliner = None
        # Uncomment to enable GLiNER (requires: pip install gliner)
        # try:
        #     from gliner import GLiNER
        #     self.gliner = GLiNER.from_pretrained("urchade/gliner_base")
        #     logger.info("GLiNER loaded successfully", step="init")
        # except:
        #     logger.warning("GLiNER not available, using keyword matching", step="init")
    
    def _load_learned_answers(self):
        """Load previously learned answers for this candidate"""
        
        if os.path.exists(self.learned_answers_file):
            try:
                with open(self.learned_answers_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load learned answers: {e}", step="init")
        return {}
    
    def _save_learned_answers(self):
        """Save learned answers for this candidate"""
        
        try:
            os.makedirs(os.path.dirname(self.learned_answers_file), exist_ok=True)
            with open(self.learned_answers_file, 'w') as f:
                json.dump(self.learned_answers, f, indent=2)
            logger.debug(f"Saved {len(self.learned_answers)} learned answers", step="save_answers")
        except Exception as e:
            logger.warning(f"Could not save learned answers: {e}", step="save_answers")
        
    def fill_all_fields(self):
        """
        Fill all form fields on the current page
        Returns True if successful, False if user cancelled
        """
        try:
            # Find all form fields
            fields = self.page.locator(".jobs-easy-apply-form-section__grouping, .fb-dash-form-element").all()
            
            if not fields:
                logger.debug("No form fields found on current page", step="fill_fields")
                return True
                
            logger.info(f"Found {len(fields)} form fields to process", step="fill_fields")
            
            human_intervention_occurred = False
            
            for i, field in enumerate(fields):
                try:
                    intervention_result = self._process_field(field, i+1)
                    if intervention_result == "human_input":
                        human_intervention_occurred = True
                except Exception as e:
                    logger.warning(f"Error processing field {i+1}: {e}", step="fill_fields")
                    continue
            
            # AUTO-PROCEED: Removed the manual "ready" prompt to reduce manual work
            if human_intervention_occurred:
                logger.info("Human intervention occurred, but proceeding automatically...", step="fill_fields")
                time.sleep(2) # Brief pause to allow user to see what happened
                
            return True
            
        except Exception as e:
            logger.error(f"Error filling fields: {e}", step="fill_fields", exception=e)
            return False
    
    def _process_field(self, field: Locator, field_num: int):
        """Process a single form field. Returns 'human_input' if human intervention was needed."""
        try:
            # Get field label/question
            question_text = self._extract_question(field)
            if not question_text:
                return None
            
            # 1. Skip if already filled (e.g., auto-filled by LinkedIn or browser)
            existing_value = self._extract_filled_value(field)
            if existing_value and str(existing_value).strip():
                logger.info(f"Field {field_num}: '{question_text}' already has value. Skipping.", step="process_field")
                return "skipped_filled"

            # Check if required
            is_required = self._is_required_field(field)
            required_marker = "⚠️ REQUIRED" if is_required else ""
            
            logger.debug(f"Field {field_num}: {question_text} {required_marker}", step="process_field")
            
            # Determine field type
            field_type = self._detect_field_type(field)
            
            # Extract HTML data type (INTEGER, SELECT_OPTION, RADIO_OPTION, BOOLEAN, SHORT_TEXT, LONG_TEXT)
            data_type = self._extract_html_data_type(field, field_type, question_text)
            
            # Get options if applicable
            options = self._extract_options(field, field_type) if field_type in ["select", "radio"] else None
            
            # Get answer from profile data / LLM
            answer = self._get_answer(question_text, field_type, options, data_type)
            
            # If no answer found, ask human
            if answer is None:
                answer = self._ask_human(question_text, field, is_required)
                if answer is None:  # User skipped
                    if is_required:
                        logger.warning(f"⚠️ REQUIRED FIELD SKIPPED: {question_text}", step="process_field")
                        print(f"\n⚠️ WARNING: You skipped a REQUIRED field: {question_text}\n")
                    return None
                return "human_input"  # Signal that human intervention occurred
            
            # Fill the field
            self._fill_field(field, answer, field_type, data_type)
            return "auto_filled"
            
        except Exception as e:
            logger.debug(f"Field processing error: {e}", step="process_field")
            return None
    
    def _is_required_field(self, field: Locator) -> bool:
        """Check if field is marked as required"""
        try:
            # Check for required attribute
            if field.locator("[required], [aria-required='true']").count() > 0:
                return True
            
            # Check for "Required" text in label
            text = field.text_content(timeout=1000)
            if text and "required" in text.lower():
                return True
        except:
            pass
        return False
    
    def _extract_question(self, field: Locator) -> str:
        """Extract question text from field (cleanly)"""
        try:
            # Try specific label elements first for cleaner keys
            label_selectors = [
                "span.fb-dash-form-element__label",
                ".fb-dash-form-element__label",
                "label",
                "legend",
                ".jobs-easy-apply-form-section__grouping h3",
                ".fb-dash-form-element__label-text"
            ]
            
            for selector in label_selectors:
                label_el = field.locator(selector).first
                if label_el.count() > 0:
                    text = label_el.inner_text(timeout=500).strip()
                    if text:
                        # Remove "Required" and extra whitespace
                        text = re.sub(r'\s*Required\s*', '', text, flags=re.IGNORECASE)
                        text = re.sub(r'\s*\*+\s*$', '', text) # Remove trailing asterisks
                        text = " ".join(text.split())
                        if len(text) > 3: # Ignore very short labels
                            return text[:200]

            # Fallback to full text but filter out common noise
            text = field.text_content(timeout=1000)
            if text:
                text = text.strip()
                text = re.sub(r'\s*Required\s*', '', text, flags=re.IGNORECASE)
                # Take first line that looks like a question
                lines = [line.strip() for line in text.split('\n') if len(line.strip()) > 5]
                if lines:
                    return lines[0][:200]
        except:
            pass
        return "Unknown Question"
    
    def _detect_field_type(self, field: Locator) -> str:
        """Detect the type of form field"""
        try:
            # Check for select dropdown
            if field.locator("select").count() > 0:
                return "select"
            
            # Check for radio buttons
            if field.locator("input[type='radio']").count() > 0:
                return "radio"
            
            # Check for checkbox
            if field.locator("input[type='checkbox']").count() > 0:
                return "checkbox"
            
            # Check for textarea
            if field.locator("textarea").count() > 0:
                return "textarea"
            
            # Check for numeric input fields (such as years of experience)
            if field.locator("input[type='number']").count() > 0:
                return "number"
            
            # Check for text/number/url inputs (any other visible input)
            if field.locator("input:not([type='hidden'])").count() > 0:
                return "text"
                
        except:
            pass
        
        return "unknown"
    
    def _extract_html_data_type(self, field: Locator, field_type: str, question_text: str = "") -> str:
        """Extract the exact HTML accepting data type and format constraints"""
        try:
            if field_type == "select":
                return "SELECT_OPTION"
            if field_type == "radio":
                return "RADIO_OPTION"
            if field_type == "checkbox":
                return "BOOLEAN"
            if field_type == "textarea":
                return "LONG_TEXT"
            
            # Inspect input element attributes
            input_elem = field.locator("input:not([type='hidden'])").first
            if input_elem.count() > 0:
                attr_type = (input_elem.get_attribute("type") or "").lower()
                inputmode = (input_elem.get_attribute("inputmode") or "").lower()
                pattern = (input_elem.get_attribute("pattern") or "").lower()
                
                if attr_type == "number" or "numeric" in inputmode or "decimal" in inputmode or "[0-9]" in pattern:
                    return "INTEGER"
                if attr_type == "email" or "email" in inputmode:
                    return "EMAIL_ADDRESS"
                if attr_type == "tel" or "tel" in inputmode:
                    return "PHONE_NUMBER"
                if attr_type == "url" or "url" in inputmode:
                    return "URL_LINK"

            # Check question text for numeric keywords
            q_lower = question_text.lower()
            numeric_keywords = ["years", "how many", "count", "number of", "experience in years", "rating", "scale of", "salary", "compensation", "notice period"]
            if any(kw in q_lower for kw in numeric_keywords):
                return "INTEGER"

        except Exception as e:
            logger.debug(f"Error extracting HTML data type: {e}", step="extract_data_type")
            
        return "SHORT_TEXT" if field_type == "text" else field_type.upper()

    def _extract_options(self, field: Locator, field_type: str) -> list:
        """Extract available options for select/radio fields"""
        options = []
        try:
            if field_type == "select":
                option_elements = field.locator("select option").all()
                for opt in option_elements:
                    text = opt.inner_text().strip()
                    # Skip placeholders
                    if text and "select" not in text.lower() and len(text) > 0:
                        options.append(text)
            elif field_type == "radio":
                labels = field.locator("label").all()
                for label in labels:
                    text = label.inner_text().strip()
                    if text:
                        options.append(text)
        except Exception as e:
            logger.debug(f"Failed to extract options: {e}", step="extract_options")
        return options

    def _get_answer(self, question_text: str, field_type: str, options: list = None, data_type: str = None):
        """
        Get answer from profile data using smart matching
        Priority: 1. Learned answers, 2. Profile data, 3. None
        """
        question_lower = question_text.lower()
        
        # 0. Check learned answers first (exact match)
        if question_text in self.learned_answers:
            return self.learned_answers[question_text]
        
        # 0.1 Check legacy Store (fallback)
        from bot.persistence.store import Store
        legacy_store = Store()
        legacy_answer = legacy_store.get_answer(question_lower)
        if legacy_answer:
            logger.debug(f"Found answer in legacy store: {legacy_answer}", step="get_answer")
            return legacy_answer
        
        # 1. Smart keyword matching for common questions
        answer = self._match_keywords(question_lower)
        if answer:
            return answer
        
        # 2. Try LLM auto-filler if enabled
        if self.llm_filler.is_enabled():
            llm_answer = self.llm_filler.get_answer(question_text, field_type, options, data_type)
            if llm_answer is not None:
                # Cache it in the main learned answers file as well so it's persisted/available
                self.learned_answers[question_text] = llm_answer
                self._save_learned_answers()
                logger.info(f"🤖 LLM auto-filled '{question_text}' [{data_type}] with: '{llm_answer}'", step="get_answer")
                return llm_answer
        
        return None
    
    def _match_keywords(self, question_lower: str):
        """Match question to profile data using keywords"""
        
        # 1. Identity
        if 'first name' in question_lower or 'first_name' in question_lower:
            full_name = self.profile_data.get('full_name', '')
            return full_name.split()[0] if full_name.split() else ''
        
        if 'last name' in question_lower or 'last_name' in question_lower:
            full_name = self.profile_data.get('full_name', '')
            parts = full_name.split()
            return parts[-1] if len(parts) > 1 else ''
        
        if 'full name' in question_lower or ('name' in question_lower and 'first' not in question_lower and 'last' not in question_lower):
            return self.profile_data.get('full_name')
        
        # 2. Contact
        if 'email' in question_lower:
            return self.profile_data.get('email')
        
        if 'phone' in question_lower or 'mobile' in question_lower:
            if 'country' in question_lower or 'code' in question_lower:
                return self.profile_data.get('country_code')
            return self.profile_data.get('phone')
        
        # 3. Experience & Skills
        if 'years' in question_lower:
            if 'python' in question_lower:
                return self.profile_data.get('years_python')
            if 'javascript' in question_lower or 'js' in question_lower:
                return self.profile_data.get('years_javascript')
            if 'react' in question_lower:
                return self.profile_data.get('years_react')
            if 'ml' in question_lower or 'machine learning' in question_lower:
                return self.profile_data.get('years_ml')
            return self.profile_data.get('years_experience')
        
        # 4. Work Auth & Sponsorship (CRITICAL)
        if 'sponsor' in question_lower or 'visa' in question_lower:
            return self.profile_data.get('sponsorship_required')
        
        if 'authorized' in question_lower and 'work' in question_lower:
            return self.profile_data.get('authorized_to_work')
            
        if 'legally' in question_lower and 'eligible' in question_lower:
            return self.profile_data.get('authorized_to_work')

        # 5. Preferences
        if 'relocate' in question_lower:
            return self.profile_data.get('willing_to_relocate')
        
        if 'remote' in question_lower:
            return self.profile_data.get('willing_to_work_remote')
        
        # 6. Salary
        if 'salary' in question_lower or 'compensation' in question_lower:
            if 'current' in question_lower:
                return self.profile_data.get('current_salary')
            return self.profile_data.get('expected_salary')
        
        # 7. Broad "Yes/No" Patterns
        # REMOVED: No more blind "Yes" defaults to avoid "wrong answers".
        # We now rely on the 4-6s wait window for unknown questions.
            
        # 8. Demographic Defaults (often grouped at end)
        if 'gender' in question_lower:
            return self.profile_data.get('gender')
        if 'race' in question_lower or 'ethnicity' in question_lower:
            return self.profile_data.get('race_ethnicity')
        if 'lgbtq' in question_lower or 'disability' in question_lower or 'veteran' in question_lower:
            return self.profile_data.get('diverse_background')

        return None
    
    def _ask_human(self, question_text: str, field: Locator, is_required: bool = False):
        """
        Ask human to answer unknown question
        Highlights field and waits for EXPLICIT button click in browser
        """
        try:
            # Highlight the field in browser and add interactive banner
            highlight_color = 'rgba(255, 0, 0, 0.4)' if is_required else 'rgba(255, 165, 0, 0.4)'
            field.evaluate(f"""(el, color) => {{ 
                el.style.backgroundColor = color;
                el.style.border = '4px solid #ff4500';
                el.style.borderRadius = '8px';
                el.scrollIntoView({{block: 'center', behavior: 'smooth'}});
                
                // Clear any existing banner
                const existing = document.getElementById('bot-manual-banner');
                if (existing) existing.remove();

                // Reset flags
                window.bot_confirm = false;
                window.bot_skip = false;

                // Create Modern Floating Banner
                const banner = document.createElement('div');
                banner.id = 'bot-manual-banner';
                banner.style.cssText = 'position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: #1a1a1a; color: white; padding: 20px 30px; border-radius: 16px; z-index: 999999; box-shadow: 0 10px 40px rgba(0,0,0,0.5); font-family: -apple-system, system-ui, sans-serif; display: flex; align-items: center; gap: 20px; border: 1px solid #ff4500; min-width: 400px;';
                
                const text = document.createElement('div');
                text.innerHTML = `<span style="color: #ff4500; font-weight: bold; display: block; margin-bottom: 4px;">🤔 QUESTION:</span> <span style="font-size: 16px;">{question_text}</span>`;
                text.style.flex = '1';
                
                const btnContainer = document.createElement('div');
                btnContainer.style.display = 'flex';
                btnContainer.style.gap = '10px';

                // Confirm Button
                const confirmBtn = document.createElement('button');
                confirmBtn.textContent = '✅ Confirm & Next';
                confirmBtn.style.cssText = 'background: #ff4500; color: white; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; transition: 0.2s;';
                confirmBtn.onmouseenter = () => confirmBtn.style.transform = 'scale(1.05)';
                confirmBtn.onmouseleave = () => confirmBtn.style.transform = 'scale(1)';
                confirmBtn.onclick = () => {{ window.bot_confirm = true; banner.style.opacity = '0.5'; banner.innerText = '⌛ Processing...'; }};

                // Skip Button
                const skipBtn = document.createElement('button');
                skipBtn.textContent = '⏭️ Skip';
                skipBtn.style.cssText = 'background: #444; color: white; border: none; padding: 10px 15px; border-radius: 8px; cursor: pointer; font-size: 13px;';
                skipBtn.onclick = () => {{ window.bot_skip = true; banner.remove(); }};

                btnContainer.appendChild(skipBtn);
                btnContainer.appendChild(confirmBtn);
                banner.appendChild(text);
                banner.appendChild(btnContainer);
                document.body.appendChild(banner);
            }}""", highlight_color)
            
            logger.warning(f"🤔 WAITING FOR CONFIRMATION: {question_text}", step="human_input")
            print(f"\n" + "="*50)
            print(f"🤔 ACTION REQUIRED: {question_text}")
            print(f"👉 Please answer and CLICK 'Confirm & Next' in the browser")
            print("="*50 + "\n")
            
            # Indefinite wait loop (with safety timeout of 5 minutes)
            start_time = time.time()
            while (time.time() - start_time) < 300: # 5 minute limit
                # Check for browser flags
                is_confirmed = self.page.evaluate("() => window.bot_confirm")
                is_skipped = self.page.evaluate("() => window.bot_skip")

                if is_confirmed:
                    # Final extract and save
                    final_answer = self._extract_filled_value(field)
                    if final_answer:
                        self.learned_answers[question_text] = final_answer
                        self._save_learned_answers()
                        logger.info(f"✅ Learned answer: {final_answer}", step="human_input")
                    
                    self._cleanup_highlights(field)
                    return final_answer
                
                if is_skipped:
                    logger.info("Field skipped by user", step="human_input")
                    self._cleanup_highlights(field)
                    return None

                time.sleep(0.5)
            
            # Timeout
            logger.warning("Human input timed out (5 mins), skipping field", step="human_input")
            self._cleanup_highlights(field)
            return None
                
        except Exception as e:
            logger.error(f"Error in human input: {e}", step="human_input")
            self._cleanup_highlights(field)
            return None

    def _cleanup_highlights(self, field: Locator):
        """Remove highlights and browser messages"""
        try:
            field.evaluate("""el => {
                el.style.backgroundColor = '';
                el.style.border = '';
                el.style.borderRadius = '';
                const msg = document.getElementById('bot-manual-banner');
                if (msg) msg.remove();
            }""")
        except:
            pass
    
    def _extract_filled_value(self, field: Locator) -> str:
        """Extract the value that was filled in the field"""
        try:
            # 1. Try Text Input/Email/Tel
            input_elem = field.locator("input[type='text'], input[type='email'], input[type='tel'], input[type='number']").first
            if input_elem.count() > 0:
                val = input_elem.input_value()
                if val and val.strip(): return val.strip()
            
            # 2. Try Textarea
            textarea = field.locator("textarea").first
            if textarea.count() > 0:
                val = textarea.input_value()
                if val and val.strip(): return val.strip()
            
            # 3. Try Select Dropdown
            select = field.locator("select").first
            if select.count() > 0:
                val = select.input_value()
                if val and val.strip() and val != "Select an option": 
                    # Try to get the text label instead of internal value
                    try:
                        return self.page.evaluate("el => el.options[el.selectedIndex].text", select.element_handle())
                    except:
                        return val
            
            # 4. Try Radio (get checked label)
            checked_radio = field.locator("input[type='radio']:checked").first
            if checked_radio.count() > 0:
                # Try to find associated label text
                radio_id = checked_radio.get_attribute("id")
                if radio_id:
                    label = self.page.locator(f"label[for='{radio_id}']").first
                    if label.count() > 0:
                        return label.inner_text().strip()
                return "Yes" # Default fallback for checked radio
            
            # 5. Try Checkbox
            checkbox = field.locator("input[type='checkbox']").first
            if checkbox.count() > 0:
                return "Checked" if checkbox.is_checked() else None
                
        except:
            pass
        
        return None
    
    def _clean_integer_answer(self, answer: str) -> str:
        """Sanitize LLM output to a clean whole number (integer)"""
        if not answer:
            return "0"
        
        answer_str = str(answer).strip()
        answer_lower = answer_str.lower()
        
        # Map boolean/yes/no answers to digits if expected field is an integer
        if answer_lower in ["yes", "true"]:
            return "1"
        if answer_lower in ["no", "false"]:
            return "0"

        # Map common text numbers to digits
        word_to_num = {
            "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
            "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"
        }
        if answer_lower in word_to_num:
            return word_to_num[answer_lower]

        # Extract digits or decimals (like "1.4")
        numbers = re.findall(r'\d+\.\d+|\d+', answer_str)
        if not numbers:
            return "0"
        
        first_num = numbers[0]
        try:
            # Convert to float and round to nearest integer (e.g. 1.4 -> 1, 1.5 -> 2)
            return str(int(round(float(first_num))))
        except Exception as e:
            # Fallback to only digits
            only_digits = re.sub(r'\D', '', first_num)
            return only_digits if only_digits else "0"

    def _fill_field(self, field: Locator, answer: str, field_type: str, data_type: str = None):
        """Fill the field with the answer"""
        try:
            if field_type == "select":
                select = field.locator("select").first
                # Try by label first, then by value
                try:
                    select.select_option(label=answer)
                except:
                    try:
                        select.select_option(value=answer)
                    except:
                        pass
                logger.debug(f"Selected: {answer}", step="fill_field")
                
            elif field_type == "radio":
                # Find label containing or matching the answer case-insensitively
                label = field.locator("label").filter(has_text=re.compile(rf"^\s*{re.escape(str(answer))}\s*$", re.I)).first
                if label.count() > 0:
                    label.click()
                    logger.debug(f"Radio selected via label: {answer}", step="fill_field")
                else:
                    # Fallback to value-based input selector
                    radio = field.locator(f"input[type='radio'][value='{answer}']").first
                    if radio.count() > 0:
                        radio.click()
                        logger.debug(f"Radio selected via input value: {answer}", step="fill_field")
                    
            elif field_type == "checkbox":
                checkbox = field.locator("input[type='checkbox']").first
                if str(answer).lower() in ['yes', 'true', '1', 'checked']:
                    checkbox.check()
                else:
                    checkbox.uncheck()
                logger.debug(f"Checkbox: {answer}", step="fill_field")
                
            elif field_type in ["number", "text", "textarea"] or data_type == "INTEGER":
                input_elem = field.locator("input:not([type='hidden']), textarea").first
                if input_elem.count() > 0:
                    try:
                        input_elem.click()
                    except:
                        pass
                    
                    final_value = str(answer)
                    if field_type == "number" or data_type == "INTEGER":
                        final_value = self._clean_integer_answer(answer)
                        
                    input_elem.fill("")  # Clear first
                    input_elem.fill(final_value)
                    input_elem.dispatch_event("input")
                    input_elem.dispatch_event("change")
                    logger.debug(f"Filled field ({field_type}/{data_type}): {final_value} (raw answer: {answer})", step="fill_field")
                
        except Exception as e:
            logger.debug(f"Error filling field: {e}", step="fill_field")