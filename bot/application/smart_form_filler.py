"""
Smart Form Filler with Human-in-Loop for Unknown Questions
Supports profile data, answer learning, and manual intervention
"""

import time
import re
import json
import os
import difflib
from playwright.sync_api import Page, Locator
from bot.utils.logger import logger
from bot.utils.selectors import LOCATORS
from bot.application.llm_filler import LLMFiller
from bot.utils.human_popup import prompt_human_with_popup


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
        
    def _scroll_modal_content(self):
        """Scroll down and up the Easy Apply modal dialog to trigger lazy-rendering of all field components"""
        try:
            modal_content = self.page.locator(".jobs-easy-apply-modal__content, .artdeco-modal__content, div[data-test-easy-apply-form-container]").first
            if modal_content.count() > 0:
                # Scroll to bottom
                modal_content.evaluate("el => el.scrollTo({top: el.scrollHeight, behavior: 'smooth'})")
                time.sleep(0.4)
                # Scroll back to top
                modal_content.evaluate("el => el.scrollTo({top: 0, behavior: 'smooth'})")
                time.sleep(0.3)
        except Exception:
            pass

    def _get_form_fields(self):
        """Find all form field elements on current page"""
        try:
            return self.page.locator(".jobs-easy-apply-form-section__grouping, .fb-dash-form-element, div[data-test-form-builder-component], .jobs-easy-apply-form-element, fieldset.fb-form-element").all()
        except Exception as e:
            logger.debug(f"Error querying form fields: {e}", step="fill_fields")
            return []

    def has_unfilled_fields(self) -> bool:
        """Check if any fields on current page are still empty or marked with validation error"""
        try:
            fields = self._get_form_fields()
            for field in fields:
                val = self._extract_filled_value(field)
                if not val or not str(val).strip():
                    is_req = self._is_required_field(field)
                    if is_req:
                        return True
                has_err = field.locator(".artdeco-inline-feedback--error, [aria-invalid='true'], .fb-form-element--error, [data-test-form-element-error]").count() > 0
                if has_err:
                    return True
        except:
            pass
        return False

    def _get_local_answer(self, question_text: str, field_type: str, options: list = None, data_type: str = None):
        """Get answer exclusively from local sources (exact learned answers, semantic cache, profile keywords)"""
        question_lower = question_text.lower()
        is_numeric_type = (data_type in ["INTEGER", "DECIMAL", "NUMBER"]) or (field_type == "number")
        
        # 0. Check exact learned answers
        if question_text in self.learned_answers:
            cached_val = self.learned_answers[question_text]
            if is_numeric_type:
                return self._clean_numeric_answer(cached_val, data_type)
            return cached_val
        
        # 0.1 Check local semantic cache match
        semantic_match = self._find_semantic_match(question_text, field_type, options, data_type)
        if semantic_match:
            if is_numeric_type:
                return self._clean_numeric_answer(semantic_match, data_type)
            return semantic_match
            
        # 0.2 Check legacy Store (fallback)
        from bot.persistence.store import Store
        legacy_store = Store()
        legacy_answer = legacy_store.get_answer(question_lower)
        if legacy_answer:
            if is_numeric_type:
                return self._clean_numeric_answer(legacy_answer, data_type)
            return legacy_answer
        
        # 0.3 Smart keyword matching for common profile questions
        answer = self._match_keywords(question_lower)
        if answer:
            if is_numeric_type:
                return self._clean_numeric_answer(answer, data_type)
            return answer
            
        return None

    def fill_all_fields(self, force_refill: bool = False):
        """
        Fill all form fields on the current page using Page-Level Batching:
        1. Modal Smooth Scrolling to ensure dynamic fields are rendered.
        2. Phase 1 (Local Cache Sweep): Sweep all fields and resolve from local cache/profile data.
        3. Phase 2 (Single LLM Batch Call): Collect all unresolved fields and query LLM in ONE single API call.
        4. Phase 3 (DOM Injection): Apply all resolved answers into the page inputs.
        5. Phase 4 (Human Popup Fallback): For any low-confidence or awkward fields, display the centered topmost popup.
        6. Pass 2 (Verification Re-Sweep): Catch and fill any missed or errored fields.
        """
        try:
            self._scroll_modal_content()
            fields = self._get_form_fields()
            
            if not fields:
                logger.debug("No form fields found on current page", step="fill_fields")
                return True
                
            logger.info(f"Found {len(fields)} form fields on current page", step="fill_fields")
            
            resolved_actions = {}       # index -> {"field": field, "answer": str, "field_type": str, "data_type": str, "question": str, "is_required": bool, "html_spec": dict}
            unresolved_batch = []       # list of field metadata dicts to send to LLM
            human_intervention_occurred = False

            # Phase 1: Local Cache Sweep for all fields on page
            for i, field in enumerate(fields):
                try:
                    question_text = self._extract_question(field)
                    if not question_text:
                        continue
                    
                    has_error = False
                    try:
                        has_error = field.locator(".artdeco-inline-feedback--error, [aria-invalid='true'], .fb-form-element--error, [data-test-form-element-error]").count() > 0
                    except:
                        pass
                    
                    existing_value = self._extract_filled_value(field)
                    if existing_value and str(existing_value).strip() and not force_refill and not has_error:
                        logger.info(f"Field {i+1}: '{question_text}' already has value. Skipping.", step="process_field")
                        continue
                    
                    is_required = self._is_required_field(field)
                    field_type = self._detect_field_type(field)
                    html_spec = self._inspect_html_element_spec(field, field_type, question_text)
                    data_type = html_spec.get("expected_data_type") or self._extract_html_data_type(field, field_type, question_text)
                    options = html_spec.get("options") or (self._extract_options(field, field_type) if field_type in ["select", "radio"] else [])
                    
                    # Check local cache first (exact learned answers, semantic cache, keyword rules)
                    local_answer = self._get_local_answer(question_text, field_type, options, data_type)
                    if local_answer is not None:
                        logger.debug(f"Field {i+1}: '{question_text}' resolved via local cache -> '{local_answer}'", step="fill_fields")
                        resolved_actions[i] = {
                            "field": field,
                            "answer": local_answer,
                            "field_type": field_type,
                            "data_type": data_type,
                            "question": question_text,
                            "is_required": is_required,
                            "html_spec": html_spec,
                            "source": "local_cache"
                        }
                    else:
                        # Push to unresolved batch list
                        unresolved_batch.append({
                            "id": i,
                            "field_obj": field,
                            "question": question_text,
                            "field_type": field_type,
                            "data_type": data_type,
                            "options": options,
                            "details": html_spec.get("details", ""),
                            "is_required": is_required,
                            "html_spec": html_spec
                        })
                except Exception as e:
                    logger.warning(f"Error evaluating field {i+1}: {e}", step="fill_fields")
                    continue

            # Phase 2: Single Batched LLM Call for all cache misses
            human_fields = []
            if unresolved_batch:
                if self.llm_filler.is_enabled():
                    logger.info(f"🚀 Querying LLM with batch of {len(unresolved_batch)} unresolved form fields in 1 call...", step="fill_fields")
                    batch_results = self.llm_filler.get_batch_answers(unresolved_batch)
                    
                    for item in unresolved_batch:
                        item_id = item["id"]
                        q_text = item["question"]
                        f_type = item["field_type"]
                        d_type = item["data_type"]
                        opts = item["options"]
                        is_req = item["is_required"]
                        field = item["field_obj"]
                        html_spec = item["html_spec"]
                        
                        llm_res = batch_results.get(item_id, {})
                        ans = llm_res.get("answer")
                        conf = llm_res.get("confidence", 0.0)
                        
                        if ans and str(ans).strip().upper() != "HUMAN_INTERVENTION_REQUIRED" and conf >= 0.75:
                            # Sanitize answer for data type
                            if d_type in ["INTEGER", "DECIMAL", "NUMBER"] or f_type == "number":
                                ans = self._clean_numeric_answer(ans, d_type)
                            
                            # Cache answer in learned answers and persist
                            self.learned_answers[q_text] = ans
                            self._save_learned_answers()
                            logger.info(f"🤖 LLM batched answer for '{q_text}' [{d_type}] (conf {conf:.2f}): '{ans}'", step="fill_fields")
                            
                            resolved_actions[item_id] = {
                                "field": field,
                                "answer": ans,
                                "field_type": f_type,
                                "data_type": d_type,
                                "question": q_text,
                                "is_required": is_req,
                                "html_spec": html_spec,
                                "source": "llm_batch"
                            }
                        else:
                            # Low confidence or human intervention requested
                            human_fields.append((item, ans))
                else:
                    for item in unresolved_batch:
                        human_fields.append((item, None))

            # Phase 3: Fill all resolved fields into the DOM
            for idx, act in resolved_actions.items():
                try:
                    field = act["field"]
                    try:
                        field.scroll_into_view_if_needed(timeout=1000)
                    except:
                        pass
                    self._fill_field(field, act["answer"], act["field_type"], act["data_type"])
                except Exception as e:
                    logger.warning(f"Error filling field '{act.get('question')}': {e}", step="fill_fields")

            # Phase 4: Handle any human popup interventions
            for item, tentative_ans in human_fields:
                try:
                    q_text = item["question"]
                    field = item["field_obj"]
                    f_type = item["field_type"]
                    d_type = item["data_type"]
                    opts = item["options"]
                    is_req = item["is_required"]
                    html_spec = item["html_spec"]
                    
                    try:
                        field.scroll_into_view_if_needed(timeout=1000)
                    except:
                        pass
                        
                    human_ans = self._ask_human(
                        question_text=q_text,
                        field=field,
                        is_required=is_req,
                        suggested_answer=tentative_ans if tentative_ans and tentative_ans.upper() != "HUMAN_INTERVENTION_REQUIRED" else None,
                        field_type=f_type,
                        options=opts,
                        data_type=d_type
                    )
                    if human_ans is not None:
                        self._fill_field(field, human_ans, f_type, d_type)
                        human_intervention_occurred = True
                except Exception as e:
                    logger.warning(f"Error during human prompt: {e}", step="fill_fields")

            # Phase 5: Pass 2 Verification Re-Sweep (guarantee zero missed or errored fields)
            time.sleep(1)
            self._scroll_modal_content()
            fields_pass2 = self._get_form_fields()
            
            unfilled_count = 0
            for i, field in enumerate(fields_pass2):
                val = self._extract_filled_value(field)
                has_error = False
                try:
                    has_error = field.locator(".artdeco-inline-feedback--error, [aria-invalid='true'], .fb-form-element--error, [data-test-form-element-error]").count() > 0
                except:
                    pass

                # If field remains empty or in error state
                if not val or has_error:
                    unfilled_count += 1
                    q_text = self._extract_question(field)
                    logger.info(f"🔍 Re-sweep filling missed/empty field ({unfilled_count}): '{q_text}'", step="fill_fields")
                    try:
                        field.scroll_into_view_if_needed(timeout=1000)
                    except:
                        pass
                    intervention_result = self._process_field(field, i+1, force_refill=True)
                    if intervention_result == "human_input":
                        human_intervention_occurred = True
            
            if human_intervention_occurred:
                logger.info("Human intervention occurred, proceeding automatically...", step="fill_fields")
                time.sleep(1)
                
            return True
            
        except Exception as e:
            logger.error(f"Error filling fields: {e}", step="fill_fields", exception=e)
            return False
    
    def _process_field(self, field: Locator, field_num: int, force_refill: bool = False):
        """Process a single form field. Returns 'human_input' if human intervention was needed."""
        try:
            # Get field label/question
            question_text = self._extract_question(field)
            if not question_text:
                return None
            
            # Check if field has visible error states
            has_error = False
            try:
                has_error = field.locator(".artdeco-inline-feedback--error, [aria-invalid='true'], .fb-form-element--error, [data-test-form-element-error]").count() > 0
            except:
                pass

            # 1. Skip if already filled, UNLESS force_refill is True or field has an error
            existing_value = self._extract_filled_value(field)
            if existing_value and str(existing_value).strip() and not force_refill and not has_error:
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
            
            # If no answer found, ask human via On-Screen Popup Window (10 min timer)
            if answer is None:
                suggested_answer = None
                if self.llm_filler and hasattr(self.llm_filler, 'get_last_tentative_answer'):
                    suggested_answer = self.llm_filler.get_last_tentative_answer()

                answer = self._ask_human(
                    question_text=question_text, 
                    field=field, 
                    is_required=is_required, 
                    suggested_answer=suggested_answer,
                    field_type=field_type,
                    options=options,
                    data_type=data_type
                )
                
                if answer is None:  # User skipped and no fallback answer
                    if is_required:
                        logger.warning(f"⚠️ REQUIRED FIELD SKIPPED: {question_text}", step="process_field")
                        print(f"\n⚠️ WARNING: You skipped a REQUIRED field: {question_text}\n")
                    return None
                
                # Fill the field with the human answer
                self._fill_field(field, answer, field_type, data_type)
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
    
    def _inspect_html_element_spec(self, field: Locator, field_type: str, question_text: str = "") -> dict:
        """Inspect the exact live HTML element in the DOM and return detailed specifications"""
        try:
            dom_info = field.evaluate("""el => {
                // 0. Check for visible inline error messages to detect exact page validation rules
                const errorEl = el.querySelector('.artdeco-inline-feedback--error, [data-test-form-element-error], .fb-form-element--error');
                const errorText = errorEl ? errorEl.innerText.trim() : '';

                // 1. Select Dropdown
                const select = el.querySelector('select');
                if (select) {
                    const options = Array.from(select.options).map(o => o.text.trim()).filter(t => t && !t.toLowerCase().includes('select an option'));
                    return {
                        tag: '<select>',
                        html_type: 'select-one',
                        expected_data_type: 'SELECT_OPTION',
                        description: 'Dropdown Menu Option',
                        options: options,
                        placeholder: '',
                        error_text: errorText,
                        details: `HTML <select> with ${options.length} options`
                    };
                }
                
                // 2. Radio Button Group
                const radios = el.querySelectorAll('input[type="radio"]');
                if (radios.length > 0) {
                    const labels = Array.from(el.querySelectorAll('label')).map(l => l.innerText.trim()).filter(Boolean);
                    return {
                        tag: '<input type="radio">',
                        html_type: 'radio',
                        expected_data_type: 'RADIO_OPTION',
                        description: 'Radio Button Choice',
                        options: labels,
                        placeholder: '',
                        error_text: errorText,
                        details: `Select one option from: [${labels.join(', ')}]`
                    };
                }
                
                // 3. Checkbox
                const checkbox = el.querySelector('input[type="checkbox"]');
                if (checkbox) {
                    return {
                        tag: '<input type="checkbox">',
                        html_type: 'checkbox',
                        expected_data_type: 'BOOLEAN',
                        description: 'Single Checkbox (Yes / No)',
                        options: ['Yes', 'No'],
                        placeholder: '',
                        error_text: errorText,
                        details: 'Checkbox (Checked / Unchecked)'
                    };
                }
                
                // 4. Textarea
                const textarea = el.querySelector('textarea');
                if (textarea) {
                    const maxlen = textarea.getAttribute('maxlength');
                    const ph = textarea.getAttribute('placeholder') || '';
                    return {
                        tag: '<textarea>',
                        html_type: 'textarea',
                        expected_data_type: 'LONG_TEXT',
                        description: 'Multi-line Paragraph Text',
                        options: [],
                        placeholder: ph,
                        error_text: errorText,
                        details: `Multi-line paragraph text${maxlen ? ' (Max ' + maxlen + ' chars)' : ''}`
                    };
                }
                
                // 5. Input element
                const input = el.querySelector('input:not([type="hidden"])');
                if (input) {
                    const type = (input.getAttribute('type') || 'text').toLowerCase();
                    const inputmode = (input.getAttribute('inputmode') || '').toLowerCase();
                    const pattern = input.getAttribute('pattern') || '';
                    const min = input.getAttribute('min');
                    const max = input.getAttribute('max');
                    const placeholder = input.getAttribute('placeholder') || '';
                    
                    let expected_type = 'SHORT_TEXT';
                    let desc = 'Single-line Text';
                    let details = `<input type="${type}">`;
                    
                    // Check if error text dictates numeric/decimal constraints
                    const errLower = errorText.toLowerCase();
                    const isDecimalErr = errLower.includes('decimal') || errLower.includes('larger than 0') || errLower.includes('0.0');
                    const isNumericErr = errLower.includes('number') || errLower.includes('numeric') || errLower.includes('digits') || errLower.includes('whole number');

                    if (isDecimalErr) {
                        expected_type = 'DECIMAL';
                        desc = 'Decimal Number (e.g. 0.0, 0, 1.5)';
                        details = `Decimal requirement (Page validation: "${errorText}")`;
                    } else if (type === 'number' || inputmode === 'numeric' || inputmode === 'decimal' || pattern.includes('0-9') || pattern.includes('\\\\d') || min !== null || isNumericErr) {
                        expected_type = 'INTEGER';
                        desc = 'Whole Number / Digits Only';
                        details = `Numeric input (HTML type="${type}"${inputmode ? ' inputmode="' + inputmode + '"' : ''}${min !== null ? ' min="' + min + '"' : ''})`;
                    } else if (type === 'email' || inputmode === 'email') {
                        expected_type = 'EMAIL_ADDRESS';
                        desc = 'Email Address';
                        details = 'Email field (HTML type="email")';
                    } else if (type === 'tel' || inputmode === 'tel') {
                        expected_type = 'PHONE_NUMBER';
                        desc = 'Phone Number';
                        details = 'Phone field (HTML type="tel")';
                    } else if (type === 'url' || inputmode === 'url') {
                        expected_type = 'URL_LINK';
                        desc = 'Website URL Link';
                        details = 'URL field (HTML type="url")';
                    }
                    
                    return {
                        tag: `<input type="${type}">`,
                        html_type: type,
                        inputmode: inputmode,
                        pattern: pattern,
                        placeholder: placeholder,
                        error_text: errorText,
                        expected_data_type: expected_type,
                        description: desc,
                        options: [],
                        details: details
                    };
                }
                
                return {
                    tag: '<input type="text">',
                    html_type: 'text',
                    expected_data_type: 'SHORT_TEXT',
                    description: 'Text Input',
                    options: [],
                    placeholder: '',
                    error_text: '',
                    details: 'Standard text input'
                };
            }""")
            
            # Check question keywords if tag was plain text input
            if dom_info.get("expected_data_type") == "SHORT_TEXT" and question_text:
                q_lower = question_text.lower()
                numeric_keywords = ["years", "how many", "count", "number of", "experience in years", "rating", "scale of", "salary", "compensation", "notice period", "offer", "hold any offer", "current offer", "expected ctc", "current ctc"]
                if any(kw in q_lower for kw in numeric_keywords):
                    dom_info["expected_data_type"] = "INTEGER"
                    dom_info["description"] = "Whole Number (Integer Digits Only)"
                    dom_info["details"] = "Numeric question (Expected digits only e.g. 0, 1, 2, 5 - no text)"

            return dom_info
        except Exception as e:
            logger.debug(f"Error inspecting live HTML element: {e}", step="inspect_element")
            return {
                "tag": f"<{field_type}>",
                "html_type": field_type,
                "expected_data_type": field_type.upper(),
                "description": field_type.upper(),
                "options": [],
                "placeholder": "",
                "details": f"Field type: {field_type}"
            }

    def _extract_html_data_type(self, field: Locator, field_type: str, question_text: str = "") -> str:
        """Extract the exact HTML accepting data type and format constraints"""
        spec = self._inspect_html_element_spec(field, field_type, question_text)
        return spec.get("expected_data_type", "SHORT_TEXT")

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

    def _calculate_semantic_similarity(self, q1: str, q2: str) -> float:
        """Calculate semantic similarity score between two question strings (0.0 to 1.0)"""
        if not q1 or not q2:
            return 0.0
            
        def _normalize(t):
            t = str(t).lower()
            t = re.sub(r'[^\w\s]', '', t)
            stop_words = {'do', 'you', 'have', 'in', 'a', 'the', 'of', 'with', 'for', 'to', 'is', 'are', 'your', 'how', 'many', 'please', 'specify', 'select'}
            return [w for w in t.split() if w not in stop_words]

        words1 = _normalize(q1)
        words2 = _normalize(q2)
        
        if not words1 or not words2:
            return 0.0
            
        # Tech / Domain term guard: prevent cross-matching different technologies (e.g. Python vs Java)
        tech_terms = {
  'python', 'java', 'javascript', 'typescript', 'c++', 'c#', 'sql',
  'react', 'aws', 'docker', 'kubernetes', 'gcp', 'azure',
  'fastapi', 'django', 'flask', 'html', 'css', 'git', 'linux',
  'node', 'express', 'vue', 'angular', 'pytorch', 'tensorflow',
  'pandas', 'numpy', 'rag', 'llm', 'agentic', 'dspy',
  'mongodb', 'tailwindcss', 'oracle', 'postgresql',

  # AI / Generative AI
  'artificial intelligence', 'machine learning', 'deep learning',
  'generative ai', 'genai', 'natural language processing', 'nlp',
  'computer vision', 'speech ai', 'multimodal ai',

  # LLMs
  'large language models', 'llm', 'transformers', 'foundation models',
  'gpt', 'gemini', 'claude', 'llama', 'mistral', 'qwen',
  'embedding models', 'encoder-decoder models', 'vision language models',
  'small language models', 'slm',

  # LLM Application Development
  'langchain', 'langgraph', 'llamaindex', 'semantic kernel',
  'haystack', 'crewai', 'autogen', 'openai api', 'anthropic api',
  'gemini api', 'hugging face', 'huggingface transformers',
  'vllm', 'ollama', 'litellm',

  # RAG / Knowledge Systems
  'rag', 'advanced rag', 'agentic rag', 'retrieval augmented generation',
  'vector databases', 'vector search', 'semantic search',
  'hybrid search', 'reranking', 'chunking', 'document parsing',
  'knowledge graphs', 'graph rag', 'embeddings',

  # Vector Databases
  'pinecone', 'weaviate', 'milvus', 'qdrant', 'chroma',
  'faiss', 'pgvector', 'elasticsearch', 'opensearch',

  # AI Agents
  'ai agents', 'agentic ai', 'agentic workflows', 'multi-agent systems',
  'autonomous agents', 'tool calling', 'function calling',
  'model context protocol', 'mcp', 'agent memory',
  'planning agents', 'reasoning agents',

  # Prompt Engineering
  'prompt engineering', 'prompt design', 'system prompts',
  'few-shot prompting', 'zero-shot prompting', 'chain of thought',
  'structured outputs', 'json mode', 'function calling',
  'prompt optimization', 'dspy',

  # Model Training / Fine-tuning
  'fine-tuning', 'supervised fine-tuning', 'sft',
  'parameter efficient fine-tuning', 'peft', 'lora', 'qlora',
  'reinforcement learning', 'rlhf', 'dpo', 'distillation',
  'transfer learning', 'self-supervised learning',

  # ML / Deep Learning
  'scikit-learn', 'xgboost', 'lightgbm', 'catboost',
  'cnn', 'rnn', 'lstm', 'gru', 'gan', 'vae',
  'diffusion models', 'reinforcement learning',
  'self-supervised learning', 'federated learning',

  # AI Infrastructure / MLOps
  'mlops', 'llmops', 'modelops', 'mlflow', 'kubeflow',
  'weights and biases', 'wandb', 'tensorboard',
  'model serving', 'model deployment', 'inference',
  'distributed training', 'gpu computing', 'cuda',
  'triton inference server', 'ray',

  # AI Evaluation / Safety
  'llm evaluation', 'model evaluation', 'ai evaluation',
  'rag evaluation', 'hallucination detection',
  'guardrails', 'ai safety', 'responsible ai',
  'ai alignment', 'red teaming', 'toxicity detection',
  'bias detection', 'observability', 'llm observability',

  # AI APIs / Platforms
  'openai', 'anthropic', 'google vertex ai', 'aws bedrock',
  'azure openai', 'google ai studio', 'hugging face',
  'groq', 'together ai', 'fireworks ai',

  # Generative Media
  'text to image', 'text to video', 'text to speech',
  'speech to text', 'image generation', 'video generation',
  'stable diffusion', 'flux', 'dall-e', 'whisper',
  'tts', 'stt',

  # AI Data / Search
  'data labeling', 'synthetic data', 'data augmentation',
  'feature engineering', 'feature stores', 'data pipelines',
  'semantic retrieval', 'information retrieval',

  # AI Architecture
  'transformer architecture', 'attention mechanism',
  'self-attention', 'mixture of experts', 'moe',
  'quantization', 'pruning', 'knowledge distillation',
  'context window', 'long context', 'speculative decoding'
}
        tech1 = set(words1) & tech_terms
        tech2 = set(words2) & tech_terms
        if tech1 and tech2 and tech1 != tech2:
            return 0.0

        s1 = " ".join(words1)
        s2 = " ".join(words2)
        if s1 == s2:
            return 1.0

        # Sequence matcher ratio
        seq_ratio = difflib.SequenceMatcher(None, s1, s2).ratio()

        # Token set overlap ratio (Jaccard)
        set1 = set(words1)
        set2 = set(words2)
        jaccard = len(set1 & set2) / max(len(set1 | set2), 1)

        return (seq_ratio * 0.5) + (jaccard * 0.5)

    def _find_semantic_match(self, question_text: str, field_type: str = None, options: list = None, data_type: str = None) -> str or None:
        """Find semantically matching answer from learned_answers or llm_cache"""
        try:
            best_score = 0.0
            best_answer = None
            best_matched_question = None

            # Candidate sources: 1. learned_answers, 2. llm_cache keys
            candidates = list(self.learned_answers.items())
            
            # Also pull from LLMFiller cache if available
            if self.llm_filler and hasattr(self.llm_filler, 'cache'):
                for key, val in self.llm_filler.cache.items():
                    # LLM cache key format: "question|field_type|data_type|options_json"
                    q_part = key.split('|')[0]
                    if q_part not in self.learned_answers:
                        candidates.append((q_part, val))

            for cached_q, cached_a in candidates:
                score = self._calculate_semantic_similarity(question_text, cached_q)
                if score > best_score and score >= 0.82:  # 82% similarity threshold
                    cand_ans_str = str(cached_a).strip()
                    
                    # If select/radio, ensure the answer is a valid option
                    if field_type in ["select", "radio"] and options:
                        matched_option = None
                        for opt in options:
                            if cand_ans_str.lower() == str(opt).strip().lower():
                                matched_option = opt
                                break
                        if not matched_option:
                            continue  # Skip this cached answer as it's not in current options
                        cand_ans_str = matched_option

                    best_score = score
                    best_answer = cand_ans_str
                    best_matched_question = cached_q

            if best_answer is not None:
                logger.info(f"🎯 Semantic Cache Hit ({int(best_score*100)}% match): '{question_text}' matched '{best_matched_question}' -> '{best_answer}'", step="semantic_cache")
                return best_answer

        except Exception as e:
            logger.debug(f"Semantic match lookup error: {e}", step="semantic_cache")

        return None

    def _get_answer(self, question_text: str, field_type: str, options: list = None, data_type: str = None):
        """
        Get answer from profile data using smart matching
        Priority: 1. Local Cache/Keywords, 2. LLM
        """
        # 1. Try local sources first
        local_ans = self._get_local_answer(question_text, field_type, options, data_type)
        if local_ans is not None:
            return local_ans
        
        # 2. Try LLM auto-filler if enabled
        if self.llm_filler.is_enabled():
            is_numeric_type = (data_type in ["INTEGER", "DECIMAL", "NUMBER"]) or (field_type == "number")
            llm_answer = self.llm_filler.get_answer(question_text, field_type, options, data_type)
            if llm_answer is not None:
                if is_numeric_type:
                    llm_answer = self._clean_numeric_answer(llm_answer, data_type)
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
    
    def _ask_human(self, question_text: str, field: Locator, is_required: bool = False, 
                   suggested_answer: str = None, field_type: str = "text", options: list = None, data_type: str = None):
        """
        Ask human to answer unknown question via On-Screen Popup Window
        - Displays in the center of the screen (TopMost / on top of all other windows)
        - Pre-fills the LLM's suggested fallback answer (if available)
        - Includes a 10-minute live countdown timer before auto-submitting the LLM fallback
        """
        try:
            # Highlight field in browser for visual reference
            highlight_color = 'rgba(255, 0, 0, 0.4)' if is_required else 'rgba(255, 165, 0, 0.4)'
            try:
                field.evaluate(f"""(el, color) => {{ 
                    el.style.backgroundColor = color;
                    el.style.border = '4px solid #ff4500';
                    el.style.borderRadius = '8px';
                    el.scrollIntoView({{block: 'center', behavior: 'smooth'}});
                }}""", highlight_color)
            except Exception:
                pass
            
            logger.warning(f"🤔 ACTION REQUIRED: Unknown field '{question_text}' - Showing On-Screen Popup (10 min timer)...", step="human_input")
            print(f"\n" + "="*60)
            print(f"🤔 ACTION REQUIRED (ON-SCREEN POPUP): {question_text}")
            if suggested_answer:
                print(f"💡 AI Suggestion: '{suggested_answer}' (Will auto-submit in 10 minutes if unanswered)")
            print(f"👉 Please answer in the on-screen popup window...")
            print("="*60 + "\n")
            
            # Inspect exact live HTML DOM specification
            html_spec = self._inspect_html_element_spec(field, field_type, question_text)
            if not data_type:
                data_type = html_spec.get("expected_data_type", "SHORT_TEXT")

            # Launch on-screen popup (topmost, 10-min countdown, live HTML spec card)
            final_answer = prompt_human_with_popup(
                question_text=question_text,
                field_type=field_type,
                options=options,
                suggested_answer=suggested_answer,
                is_required=is_required,
                timeout_seconds=600,  # 10 minutes
                data_type=data_type,
                html_spec=html_spec
            )
            
            self._cleanup_highlights(field)
            
            if final_answer is not None and str(final_answer).strip():
                final_answer_str = str(final_answer).strip()
                # Save answer to learned answers for future applications
                self.learned_answers[question_text] = final_answer_str
                self._save_learned_answers()
                logger.info(f"✅ Learned answer saved from popup: '{final_answer_str}'", step="human_input")
                return final_answer_str
            else:
                logger.info(f"Field skipped or no answer provided for: '{question_text}'", step="human_input")
                return None
                
        except Exception as e:
            logger.error(f"Error in human input popup: {e}", step="human_input")
            self._cleanup_highlights(field)
            return suggested_answer if suggested_answer else None

    def handle_errored_fields(self):
        """Find and prompt human to resolve any fields that currently have validation errors or are left unfilled"""
        try:
            fields = self._get_form_fields()
            for i, field in enumerate(fields):
                val = self._extract_filled_value(field)
                has_error = False
                try:
                    has_error = field.locator(".artdeco-inline-feedback--error, [aria-invalid='true'], .fb-form-element--error, [data-test-form-element-error]").count() > 0
                except:
                    pass

                is_req = self._is_required_field(field)

                # If field has error or is an empty required field
                if has_error or (is_req and not val):
                    question_text = self._extract_question(field)
                    field_type = self._detect_field_type(field)
                    data_type = self._extract_html_data_type(field, field_type, question_text)
                    options = self._extract_options(field, field_type) if field_type in ["select", "radio"] else None
                    
                    try:
                        field.scroll_into_view_if_needed(timeout=1000)
                    except:
                        pass

                    logger.warning(f"🚨 Missing/Errored field: '{question_text}' - Triggering On-Screen Popup for resolution...", step="handle_error")
                    
                    suggested_answer = None
                    if self.llm_filler and hasattr(self.llm_filler, 'get_last_tentative_answer'):
                        suggested_answer = self.llm_filler.get_last_tentative_answer()

                    answer = self._ask_human(
                        question_text=question_text,
                        field=field,
                        is_required=True,
                        suggested_answer=suggested_answer,
                        field_type=field_type,
                        options=options,
                        data_type=data_type
                    )
                    if answer is not None:
                        self._fill_field(field, answer, field_type, data_type)
        except Exception as e:
            logger.error(f"Error handling errored fields: {e}", step="handle_error")

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
    
    def _clean_numeric_answer(self, answer: str, data_type: str = "INTEGER") -> str:
        """Sanitize any answer (including cached strings like 'No'/'Yes') to a clean numeric/decimal value"""
        if answer is None:
            return "0"
        
        answer_str = str(answer).strip()
        answer_lower = answer_str.lower()
        
        # Map boolean/yes/no answers to digits if expected field is numeric
        if answer_lower in ["no", "none", "false", "n/a", "na", "no offer", "nil", "zero", "0"]:
            return "0"
        if answer_lower in ["yes", "true", "one", "1"]:
            return "1"

        # Map common text numbers to digits
        word_to_num = {
            "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
            "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"
        }
        if answer_lower in word_to_num:
            return word_to_num[answer_lower]

        # Extract digits or decimals (like "1.4" or "0.0")
        numbers = re.findall(r'\d+\.\d+|\d+', answer_str)
        if not numbers:
            return "0"
        
        first_num = numbers[0]
        if data_type == "DECIMAL":
            return first_num
        try:
            return str(int(round(float(first_num))))
        except Exception:
            only_digits = re.sub(r'\D', '', first_num)
            return only_digits if only_digits else "0"

    def _clean_integer_answer(self, answer: str) -> str:
        """Sanitize LLM output to a clean whole number (integer)"""
        return self._clean_numeric_answer(answer, data_type="INTEGER")

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
                
            elif field_type in ["number", "text", "textarea"] or (data_type in ["INTEGER", "DECIMAL", "NUMBER"]):
                input_elem = field.locator("input:not([type='hidden']), textarea").first
                if input_elem.count() > 0:
                    try:
                        input_elem.click()
                    except:
                        pass
                    
                    final_value = str(answer)
                    if field_type == "number" or (data_type in ["INTEGER", "DECIMAL", "NUMBER"]):
                        final_value = self._clean_numeric_answer(answer, data_type or "INTEGER")
                        
                    input_elem.fill("")  # Clear first
                    input_elem.fill(final_value)
                    input_elem.dispatch_event("input")
                    input_elem.dispatch_event("change")
                    logger.debug(f"Filled field ({field_type}/{data_type}): {final_value} (raw answer: {answer})", step="fill_field")
                
        except Exception as e:
            logger.debug(f"Error filling field: {e}", step="fill_field")