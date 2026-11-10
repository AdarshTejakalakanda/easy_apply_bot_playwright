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
                    data = json.load(f)
                    # Filter out contaminated or invalid entries
                    cleaned = {
                        k: v for k, v in data.items()
                        if str(v).strip().lower() not in ["on", "off", "none", "null", "undefined"]
                        and str(k).strip().lower() != str(v).strip().lower()
                    }
                    return cleaned
            except Exception as e:
                logger.warning(f"Could not load learned answers: {e}", step="init")
        return {}
    
    def _save_learned_answers(self):
        """Save learned answers for this candidate"""
        try:
            os.makedirs(os.path.dirname(self.learned_answers_file), exist_ok=True)
            # Filter out contaminated or invalid entries before saving
            self.learned_answers = {
                k: v for k, v in self.learned_answers.items()
                if str(v).strip().lower() not in ["on", "off", "none", "null", "undefined"]
                and str(k).strip().lower() != str(v).strip().lower()
            }
            with open(self.learned_answers_file, 'w') as f:
                json.dump(self.learned_answers, f, indent=2)
            logger.debug(f"Saved {len(self.learned_answers)} learned answers", step="save_answers")
        except Exception as e:
            logger.warning(f"Could not save learned answers: {e}", step="save_answers")
        
    def _scroll_modal_content(self):
        """Scroll down and up the Easy Apply modal dialog to trigger lazy-rendering of all field components"""
        try:
            modal_content = self.page.locator("dialog[open], dialog[data-testid='dialog'], dialog, .jobs-easy-apply-modal__content, .artdeco-modal__content, div[data-test-easy-apply-form-container]").first
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
        """Find all form field elements on current page (strictly scoped to active modal dialog)"""
        try:
            modal = self.page.locator("dialog[open], dialog[data-testid='dialog'], dialog, .jobs-easy-apply-modal, .artdeco-modal, div[role='dialog'], div[data-test-easy-apply-form-container]").first
            if modal.count() == 0:
                logger.debug("No modal dialog container present on page", step="form_fields")
                return []

            search_root = modal

            grouping_selectors = (
                "div[componentkey^='easyApplyFieldFocus'], "
                ".jobs-easy-apply-form-section__grouping, "
                ".fb-dash-form-element, "
                "div[data-test-form-builder-component], "
                ".jobs-easy-apply-form-element, "
                "fieldset.fb-form-element, "
                "fieldset, "
                "div[data-test-form-element], "
                "div[data-test-single-typeahead-entity-form-component], "
                "div[data-test-text-entity-list-form-component], "
                "div[data-test-checkbox-form-component], "
                "div[data-test-radio-form-component]"
            )
            raw_fields = search_root.locator(grouping_selectors).all()

            # Guaranteed Control-Driven Discovery: Walk up from all visible, editable inputs
            controls = search_root.locator("input:not([type='hidden']):not([type='file']), select, textarea").all()
            if controls:
                control_fields = []
                for ctrl in controls:
                    try:
                        if not ctrl.is_visible():
                            continue
                        # Never touch background search bars or navigation inputs
                        comp_key = (ctrl.get_attribute("componentkey") or "").lower()
                        placeholder = (ctrl.get_attribute("placeholder") or "").lower()
                        if "semanticsearchbox" in comp_key or "describe the job you want" in placeholder:
                            continue
                        # Find the enclosing question container that holds label/question text
                        container = ctrl.locator("xpath=ancestor::div[p or label or legend or h3 or h4 or @componentkey][last()]").first
                        if container.count() > 0:
                            control_fields.append(container)
                        else:
                            control_fields.append(ctrl.locator("xpath=..").first)
                    except Exception:
                        control_fields.append(ctrl)
                raw_fields = raw_fields + control_fields
            filtered = []
            seen_ctrl_ids = set()
            for f in raw_fields:
                try:
                    # Skip if it is a Resume / Document upload card or file section (handled by workflow)
                    if self._is_resume_or_file_section(f):
                        continue

                    # An element MUST have at least one editable control
                    ctrl = f.locator("input:not([type='file']), select, textarea").first
                    if ctrl.count() == 0:
                        continue
                        
                    # Skip if it is an Education or Work Experience card section
                    if self._is_education_section(f) or self._is_work_experience_section(f):
                        continue
                        
                    # Skip any element that contains an Add button and has no real inputs
                    if f.locator("button:has-text('Add'), button[aria-label*='Add'], button:has-text('add')").count() > 0:
                        if f.locator("input:not([type='hidden']), select, textarea").count() == 0:
                            continue

                    ctrl_key = ctrl.get_attribute("id") or ctrl.get_attribute("name") or ctrl.get_attribute("aria-label") or f.element_handle()
                    if ctrl_key:
                        if ctrl_key in seen_ctrl_ids:
                            continue
                        seen_ctrl_ids.add(ctrl_key)

                    filtered.append(f)
                except Exception:
                    filtered.append(f)
            return filtered
        except Exception as e:
            logger.debug(f"Error querying form fields: {e}", step="fill_fields")
            return []

    def _is_placeholder_or_empty(self, val: str, question_text: str = "") -> bool:
        """Return True if val is None, empty, whitespace, or a common dropdown/field placeholder prompt"""
        if val is None:
            return True
        v = str(val).strip().lower()
        if not v:
            return True
            
        # Reject browser default values or raw boolean flags
        if v in ["on", "off", "yes/no", "true/false"]:
            return True
            
        if question_text and (v == question_text.strip().lower() or question_text.strip().lower() in v):
            return True
        
        # Remove trailing/leading punctuation
        v_clean = re.sub(r'^[^\w]+|[^\w]+$', '', v)
        if not v_clean:
            return True

        placeholder_exact = {
            "select", "select an option", "select option", "please select", 
            "please select an option", "choose", "choose an option", "choose one", 
            "select one", "select a value", "select an answer", "select qualification", 
            "select your qualification", "select country", "select state", "select location",
            "none", "null", "undefined", "n/a", "na", "--", "-- select --", "--select--"
        }
        if v in placeholder_exact or v_clean in placeholder_exact:
            return True
            
        if v.startswith("select an option") or v.startswith("please select") or v.startswith("choose an option"):
            return True
            
        return False

    def _field_has_error(self, field: Locator) -> bool:
        """Check if field has visible error states across classic and modern LinkedIn UIs"""
        try:
            # 1. Explicit HTML invalid attribute
            if field.locator("input[aria-invalid='true'], select[aria-invalid='true'], textarea[aria-invalid='true'], [aria-invalid='true']").count() > 0:
                return True

            # 2. Classic LinkedIn error containers
            if field.locator(".artdeco-inline-feedback--error, .fb-form-element--error, [data-test-form-element-error]").count() > 0:
                return True

            # 3. Explicit error text
            error_text_selectors = [
                "p:has-text('Invalid input')",
                "p:has-text('Please enter a valid')",
                "p:has-text('Please enter a whole number')",
                "p:has-text('Enter a whole number')",
                "p:has-text('Please enter a decimal number')",
                "p:has-text('Please make a selection')",
                "p:has-text('Make a selection')",
                "p:has-text('Please select')",
                "p:has-text('Select an option')",
                "p:has-text('Please enter between')",
                "[data-testid='text-input-helper-text']:has-text('Invalid')",
                "[data-testid='text-input-helper-text']:has-text('valid')",
                "[data-testid='text-input-helper-text']:has-text('required')",
                "[data-testid='text-input-helper-text']:has-text('select')",
                "[data-testid='text-input-helper-text']:has-text('whole number')",
                "[data-testid='text-input-helper-text']:has-text('enter')",
                "span:has-text('Invalid input')",
                ".artdeco-inline-feedback--error",
                "[role='alert']"
            ]
            for sel in error_text_selectors:
                try:
                    if field.locator(sel).count() > 0:
                        return True
                except:
                    continue

            return False
        except:
            return False

    def has_unfilled_fields(self) -> bool:
        """Check if any fields on current page are still empty or marked with validation error (strictly required fields only)"""
        try:
            fields = self._get_form_fields()
            for field in fields:
                field_type = self._detect_field_type(field)
                if field_type in ["file", "education_section", "work_experience_section", "button_container"]:
                    continue
                # If an element has no editable controls (inputs/selects/textareas), it cannot be unfilled
                if field.locator("input, select, textarea").count() == 0:
                    continue
                
                # Check for explicit validation errors first
                if self._field_has_error(field):
                    return True

                # Check if strictly required field is empty
                is_req = self._is_required_field(field)
                if is_req:
                    q_text = self._extract_question(field)
                    val = self._extract_filled_value(field, q_text)
                    if not val or not str(val).strip() or self._is_placeholder_or_empty(val, q_text):
                        return True
        except:
            pass
        return False

    def _is_answer_valid_for_field(self, ans: str, field_type: str, options: list = None, data_type: str = None) -> bool:
        """Validate if an answer matches the field constraints and options before accepting it from cache"""
        if ans is None:
            return False
        s = str(ans).strip()
        d_type = (data_type or field_type or "").upper()
        
        # 0. Checkbox / Boolean fields
        if field_type == "checkbox" or d_type == "BOOLEAN":
            return s.lower() in ["yes", "true", "1", "on", "checked", "agree", "no", "false", "0", "off"]

        if not s or self._is_placeholder_or_empty(s):
            return False
        
        # 1. Radio / Select fields with explicit options
        if field_type in ["radio", "select"] or d_type in ["RADIO_OPTION", "SELECT_OPTION"]:
            if options and len(options) > 0:
                s_lower = s.lower()
                clean_opts = [str(o).strip().lower() for o in options]
                # Direct match
                if s_lower in clean_opts:
                    return True
                # Boolean options ['yes', 'no']
                if s_lower in ["yes", "true", "1"] and any("yes" in o for o in clean_opts):
                    return True
                if s_lower in ["no", "false", "0"] and any("no" in o for o in clean_opts):
                    return True
                # Substring match
                if any(s_lower in o or o in s_lower for o in clean_opts):
                    return True
                return False
                
        # 2. Integer / Numeric fields
        if d_type in ["INTEGER", "NUMBER"] or field_type == "number":
            clean_digits = re.sub(r'\D', '', s)
            if not clean_digits:
                return False
                
        # 3. URL fields
        if d_type == "URL_LINK" or "url" in field_type:
            if not (s.startswith("http://") or s.startswith("https://") or "www." in s or "linkedin.com" in s):
                return False
                
        return True

    def _get_local_answer(self, question_text: str, field_type: str, options: list = None, data_type: str = None):
        """Get answer exclusively from local sources and adapt dynamically to target live HTML data type"""
        question_lower = question_text.lower()
        
        raw_ans = None
        # 0. Check exact learned answers
        if question_text in self.learned_answers:
            raw_ans = self.learned_answers[question_text]
        
        # 0.1 Check local semantic cache match
        if raw_ans is None:
            raw_ans = self._find_semantic_match(question_text, field_type, options, data_type)
            
        # 0.2 Check legacy Store (fallback)
        if raw_ans is None:
            from bot.persistence.store import Store
            legacy_store = Store()
            raw_ans = legacy_store.get_answer(question_lower)

        # Discard invalid raw answers (e.g. 'on', 'off' for non-checkbox, or question text)
        if raw_ans is not None:
            raw_str = str(raw_ans).strip()
            if raw_str.lower() in ["on", "off"]:
                if field_type != "checkbox":
                    raw_ans = None
            elif raw_str.lower() in ["none", "null", "undefined"] or raw_str.lower() == question_lower or question_lower in raw_str.lower():
                raw_ans = None
        
        # 0.3 Smart keyword matching for common profile questions
        if raw_ans is None:
            raw_ans = self._match_keywords(question_lower)
            
        if raw_ans is not None:
            # Dynamically adapt stored/cached answer to the live HTML element's format and constraints
            adapted_ans = self._adapt_answer_to_target_field(raw_ans, field_type, data_type, options, question_text)
            
            # SCHEMA VALIDATION: Discard if adapted answer does not match field type and available options
            if not self._is_answer_valid_for_field(adapted_ans, field_type, options, data_type):
                logger.warning(f"⚠️ Cache answer '{adapted_ans}' does not match schema for '{question_text}' (type={field_type}, opts={options}). Discarding cache and sending to LLM.", step="get_local_answer")
                return None
                
            return adapted_ans
            
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
                time.sleep(1.5)
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
                    
                    has_error = self._field_has_error(field)
                    
                    existing_value = self._extract_filled_value(field, question_text)
                    if existing_value and str(existing_value).strip() and not self._is_placeholder_or_empty(existing_value, question_text) and not force_refill and not has_error:
                        logger.info(f"Field {i+1}: '{question_text}' already has value '{existing_value}'. Skipping.", step="process_field")
                        continue
                    
                    is_required = self._is_required_field(field)
                    field_type = self._detect_field_type(field)
                    if field_type in ["file", "education_section", "work_experience_section", "button_container"] or field.locator("input, select, textarea").count() == 0:
                        if field_type == "education_section":
                            logger.info(f"Field {i+1}: Education section - skipping 'Add more' button as candidate education is on profile.", step="fill_fields")
                        elif field_type == "work_experience_section":
                            logger.info(f"Field {i+1}: Work experience section - skipping 'Add more' button as experience is on profile.", step="fill_fields")
                        elif field_type == "file":
                            logger.debug(f"Field {i+1}: File/Resume upload field. Handled by workflow.", step="fill_fields")
                        continue
                        
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
                field_type = self._detect_field_type(field)
                if field_type in ["file", "education_section", "work_experience_section", "button_container"] or field.locator("input, select, textarea").count() == 0:
                    continue
                q_text = self._extract_question(field)
                val = self._extract_filled_value(field, q_text)
                has_error = self._field_has_error(field)

                # If field remains empty or in error state
                if not val or self._is_placeholder_or_empty(val, q_text) or has_error:
                    unfilled_count += 1
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
            has_error = self._field_has_error(field)

            # 1. Skip if already filled, UNLESS field has an active error
            existing_value = self._extract_filled_value(field, question_text)
            if existing_value and str(existing_value).strip() and not self._is_placeholder_or_empty(existing_value, question_text) and not has_error:
                logger.info(f"Field {field_num}: '{question_text}' already has value '{existing_value}'. Skipping.", step="process_field")
                return "skipped_filled"

            # Check if required
            is_required = self._is_required_field(field)
            required_marker = "⚠️ REQUIRED" if is_required else ""
            
            logger.debug(f"Field {field_num}: {question_text} {required_marker}", step="process_field")
            
            # Determine field type
            field_type = self._detect_field_type(field)
            if field_type in ["file", "education_section", "work_experience_section"]:
                if field_type == "education_section":
                    logger.info(f"Field {field_num}: Education section - skipping 'Add more' button as candidate education is on profile.", step="process_field")
                    return "skipped_education"
                elif field_type == "work_experience_section":
                    logger.info(f"Field {field_num}: Work experience section - skipping 'Add more' button.", step="process_field")
                    return "skipped_work_experience"
                return "skipped_file"
            
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
        """Extract question text from field cleanly using labels, ARIA attributes, and semantic cues"""
        try:
            # Skip file / resume sections completely
            if self._is_resume_or_file_section(field):
                return ""

            # 1. Check aria-label attribute on inputs/selects/textareas inside this field (cleanest accessible label)
            for ctrl in field.locator("input:not([type='hidden']), select, textarea").all():
                c_type = (ctrl.get_attribute("type") or "").lower()
                al = ctrl.get_attribute("aria-label")
                if al:
                    al_clean = re.sub(r'\s*Required\s*', '', al, flags=re.IGNORECASE).strip()
                    al_clean = re.sub(r'\s*\*+\s*$', '', al_clean).strip()
                    # Skip option-level radio/checkbox aria-labels like "Yes" or "No"
                    if c_type in ["radio", "checkbox"] and al_clean.lower() in ["yes", "no", "true", "false", "agree", "disagree"]:
                        continue
                    if len(al_clean) > 2:
                        return al_clean[:200]

            # 2. Try specific label/question elements
            label_selectors = [
                "span.fb-dash-form-element__label",
                ".fb-dash-form-element__label",
                "[data-test-form-builder-component-label]",
                ".artdeco-text-input--label",
                "label",
                "legend",
                ".jobs-easy-apply-form-section__grouping h3",
                ".fb-dash-form-element__label-text",
                "p:not([data-testid*='helper']):not(:has-text('Invalid input'))"
            ]
            
            for selector in label_selectors:
                label_el = field.locator(selector).first
                if label_el.count() > 0:
                    text = label_el.inner_text(timeout=500).strip()
                    if text and not any(err in text.lower() for err in ["invalid input", "characters", "helper"]):
                        # Remove "Required" and extra whitespace
                        text = re.sub(r'\s*Required\s*', '', text, flags=re.IGNORECASE)
                        text = re.sub(r'\s*\*+\s*$', '', text) # Remove trailing asterisks
                        text = " ".join(text.split())
                        if len(text) > 3: # Ignore very short labels
                            return text[:200]

            # 3. Check aria-labelledby attribute
            for ctrl in field.locator("input:not([type='hidden']), select, textarea").all():
                lbl_ids = ctrl.get_attribute("aria-labelledby")
                if lbl_ids:
                    for single_id in lbl_ids.split():
                        try:
                            target = self.page.locator(f"#{single_id}").first
                            if target.count() > 0:
                                t_text = target.text_content(timeout=500).strip()
                                t_text = re.sub(r'\s*Required\s*', '', t_text, flags=re.IGNORECASE).strip()
                                if len(t_text) > 2:
                                    return t_text[:200]
                        except Exception:
                            pass

            # 4. Check semantic cues from id or name attributes
            for ctrl in field.locator("input:not([type='hidden']), select").all():
                cid = (ctrl.get_attribute("id") or "").lower()
                cname = (ctrl.get_attribute("name") or "").lower()
                cattr = cid + " " + cname
                if "countrycode" in cattr or "country-code" in cattr:
                    return "Phone country code"
                if "phonenumber" in cattr or "phone-number" in cattr or "mobilephone" in cattr:
                    return "Mobile phone number"
                if "email" in cattr:
                    return "Email address"
                if "firstname" in cattr or "first-name" in cattr:
                    return "First name"
                if "lastname" in cattr or "last-name" in cattr:
                    return "Last name"
                if "city" in cattr or "location" in cattr:
                    return "Location (city)"

            # 5. Check placeholder attribute
            for ctrl in field.locator("input:not([type='hidden']), textarea").all():
                ph = ctrl.get_attribute("placeholder")
                if ph:
                    ph_clean = re.sub(r'\s*Required\s*', '', ph, flags=re.IGNORECASE).strip()
                    if len(ph_clean) > 3 and not any(skip in ph_clean.lower() for skip in ["select", "type your", "enter your", "choose"]):
                        return ph_clean[:200]

            # 6. Fallback to full text of the field element
            text = field.text_content(timeout=1000)
            if text:
                text = text.strip()
                text = re.sub(r'\s*Required\s*', '', text, flags=re.IGNORECASE)
                lines = [line.strip() for line in text.split('\n') if len(line.strip()) > 3]
                if lines:
                    return lines[0][:200]
        except:
            pass
        return "Unknown Question"
    
    def _is_education_section(self, field: Locator) -> bool:
        """
        Check if field grouping represents the card-based Education section
        (where the candidate's degrees are listed and an 'Add'/'Add more' button is shown).
        Excludes standard screening questions like 'Have you completed Bachelor's Degree? (Yes/No)' or dropdowns.
        """
        try:
            # If the field contains radio inputs or a select dropdown, it is a screening question, NOT the card section
            if field.locator("input[type='radio'], select").count() > 0:
                return False
                
            # Check for header/h3/legend/title with 'education'
            header = field.locator("h3, legend, .jobs-easy-apply-form-section__grouping-header, .fb-dash-form-element__label").first
            if header.count() > 0:
                h_text = header.inner_text().strip().lower()
                if "education" in h_text:
                    return True
            
            # Check for any button with 'add' inside the field in education context
            for btn in field.locator("button").all():
                try:
                    btn_text = (btn.text_content() or "").strip().lower()
                    aria_label = (btn.get_attribute("aria-label") or "").strip().lower()
                    if "add" in btn_text or "add" in aria_label:
                        all_text = (field.text_content() or "").lower()
                        if any(k in all_text for k in ["education", "degree", "school", "institution", "college", "university"]):
                            return True
                except Exception:
                    pass
                
            # Check text content starting with or containing Education
            text = field.text_content(timeout=500)
            if text:
                t_lower = text.strip().lower()
                if ("education" in t_lower or "degree" in t_lower or "school" in t_lower) and any(k in t_lower for k in ["add", "institution", "bachelor", "master", "college"]):
                    # Ensure no normal text/select inputs exist (i.e. it's the section container)
                    if field.locator("input:not([type='hidden']), select, textarea").count() == 0:
                        return True
        except:
            pass
        return False

    def _is_work_experience_section(self, field: Locator) -> bool:
        """
        Check if field grouping represents the card-based Work Experience section.
        Excludes questions like 'How many years of work experience do you have?'.
        """
        try:
            if field.locator("input[type='radio'], select, input[type='number']").count() > 0:
                return False
                
            header = field.locator("h3, legend, .jobs-easy-apply-form-section__grouping-header").first
            if header.count() > 0:
                h_text = header.inner_text().strip().lower()
                if "work experience" in h_text:
                    return True
                    
            if field.locator("button[aria-label*='Add work experience'], button[aria-label*='Add experience'], button:has-text('Add work experience'), button:has-text('Add experience')").count() > 0:
                return True
        except:
            pass
        return False

    def _is_resume_or_file_section(self, field: Locator) -> bool:
        """
        Check if element represents a resume card, document upload, or CV file section.
        These are managed automatically by the workflow resume manager and should never be treated as questions.
        """
        try:
            # Check class attributes for document upload / resume cards
            cls = (field.get_attribute("class") or "").lower()
            if any(k in cls for k in ["jobs-document-upload", "document-upload", "resume-card", "file-upload"]):
                return True

            # Check child elements
            if field.locator("input[type='file'], [class*='jobs-document-upload'], [class*='document-upload'], [data-test-document-upload-card]").count() > 0:
                return True

            # Check if element text is a filename (e.g. Adarsh_Teja_aiml.pdf)
            text = (field.text_content(timeout=300) or "").lower()
            if any(ext in text for ext in [".pdf", ".docx", ".doc", ".rtf"]):
                return True

            if ("resume" in text or "cover letter" in text) and any(w in text for w in ["upload", "choose", "file", "document"]):
                if field.locator("select, textarea").count() == 0:
                    return True
        except Exception:
            pass
        return False

    def _detect_field_type(self, field: Locator) -> str:
        """Detect the type of form field"""
        try:
            # Check for Resume or File upload card
            if self._is_resume_or_file_section(field):
                return "file"

            # Check for standalone button container (e.g. Add education / Add more)
            if field.locator("button:has-text('Add'), button[aria-label*='Add']").count() > 0 and field.locator("input, select, textarea").count() == 0:
                return "education_section"

            # Check for Education section (do not click Add more button)
            if self._is_education_section(field):
                return "education_section"
                
            # Check for Work Experience section (do not click Add more button)
            if self._is_work_experience_section(field):
                return "work_experience_section"

            # Check for file upload / document upload redesign cards
            if field.locator("input[type='file']").count() > 0 or field.locator(".jobs-document-upload-redesign-card__container").count() > 0:
                return "file"
            
            # Check for select dropdown
            if field.locator("select").count() > 0:
                return "select"
            
            # Check for radio buttons
            if field.locator("input[type='radio'], [role='radio'], [role='radiogroup']").count() > 0:
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
            if field.locator("input:not([type='hidden']):not([type='file'])").count() > 0:
                return "text"
                
        except:
            pass
        
        return "unknown"
    
    def _inspect_html_element_spec(self, field: Locator, field_type: str, question_text: str = "") -> dict:
        """Inspect the exact live HTML element in the DOM and return detailed specifications"""
        try:
            if field_type in ["education_section", "work_experience_section"]:
                section_name = "Education" if field_type == "education_section" else "Work Experience"
                return {
                    "tag": "<section>",
                    "html_type": field_type,
                    "expected_data_type": "SECTION",
                    "description": f"{section_name} Section",
                    "options": [],
                    "placeholder": "",
                    "error_text": "",
                    "details": f"{section_name} section (Profile data preserved; Add more button bypassed)"
                }

            dom_info = field.evaluate("""el => {
                // 0. Check for visible inline error messages to detect exact page validation rules
                const errorEl = el.querySelector('.artdeco-inline-feedback--error, [data-test-form-element-error], .fb-form-element--error');
                const errorText = errorEl ? errorEl.innerText.trim() : '';

                // Check for file input / resume cards
                const fileInput = el.querySelector('input[type="file"]');
                if (fileInput || el.querySelector('.jobs-document-upload-redesign-card__container')) {
                    return {
                        tag: '<input type="file">',
                        html_type: 'file',
                        expected_data_type: 'FILE',
                        description: 'Document / Resume Upload',
                        options: [],
                        placeholder: '',
                        error_text: errorText,
                        details: 'File upload (handled by bot resume manager)'
                    };
                }

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
                const radios = el.querySelectorAll('input[type="radio"], [role="radio"]');
                if (radios.length > 0) {
                    let opts = [];
                    radios.forEach(r => {
                        let t = "";
                        const id = r.id;
                        if (id) {
                            const lbl = el.querySelector(`label[for="${id}"]`);
                            if (lbl) t = lbl.innerText.trim();
                        }
                        if (!t) {
                            const parentLbl = r.closest('label');
                            if (parentLbl) t = parentLbl.innerText.trim();
                        }
                        if (!t && r.parentElement) {
                            t = r.parentElement.innerText.trim();
                        }
                        if (!t) {
                            t = r.value || r.getAttribute('aria-label') || "";
                        }
                        if (t && !opts.includes(t)) opts.push(t);
                    });
                    if (opts.length === 0) {
                        const allLabels = Array.from(el.querySelectorAll('label')).map(l => l.innerText.trim()).filter(Boolean);
                        opts = allLabels.length > 0 ? allLabels : ["Yes", "No"];
                    }
                    return {
                        tag: '<input type="radio">',
                        html_type: 'radio',
                        expected_data_type: 'RADIO_OPTION',
                        description: 'Radio Button Choice',
                        options: opts,
                        placeholder: '',
                        error_text: errorText,
                        details: `Select one option from: [${opts.join(', ')}]`
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
                numeric_keywords = [
                    "years", "how many", "count", "number of", "experience in years", 
                    "rating", "scale of", "salary", "compensation", "notice period", 
                    "offer", "hold any offer", "current offer", "expected ctc", "current ctc",
                    "how soon", "when can you join", "how soon can you", "joining time", "days"
                ]
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
                # 1. Extract from radio inputs and associated label/wrapper elements
                radios = field.locator("input[type='radio'], [role='radio']").all()
                for r in radios:
                    opt_text = ""
                    r_id = r.get_attribute("id")
                    if r_id:
                        lbl = field.locator(f"label[for='{r_id}']").first
                        if lbl.count() > 0:
                            opt_text = lbl.inner_text().strip()
                    if not opt_text:
                        parent_lbl = r.locator("xpath=ancestor::label[1]").first
                        if parent_lbl.count() > 0:
                            opt_text = parent_lbl.inner_text().strip()
                    if not opt_text:
                        parent_wrap = r.locator("xpath=..").first
                        if parent_wrap.count() > 0:
                            opt_text = parent_wrap.inner_text().strip()
                    if not opt_text:
                        opt_text = r.get_attribute("aria-label") or r.get_attribute("value") or ""
                    
                    if opt_text:
                        # Clean asterisks or required tags
                        opt_text = re.sub(r'\s*Required\s*', '', opt_text, flags=re.IGNORECASE).strip()
                        opt_text = re.sub(r'\s*\*+\s*$', '', opt_text).strip()
                        if opt_text and opt_text not in options:
                            options.append(opt_text)

                # 2. Fallback to labels
                if not options:
                    labels = field.locator("label, [role='radio']").all()
                    for label in labels:
                        text = label.inner_text().strip()
                        text = re.sub(r'\s*Required\s*', '', text, flags=re.IGNORECASE).strip()
                        if text and text not in options:
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
            return self.profile_data.get('email') or self.candidate_profile.get('credentials', {}).get('email')
        
        if 'phone' in question_lower or 'mobile' in question_lower:
            if 'country' in question_lower or 'code' in question_lower:
                return self.profile_data.get('country_code', '+91')
            return self.profile_data.get('phone') or self.candidate_profile.get('credentials', {}).get('phone')

        # Commute & In-Person Interview (must be checked before city/location)
        if any(k in question_lower for k in ['commute', 'commuting', 'in person', 'in-person', 'attend in person', 'willing to travel', 'hybrid']):
            return "Yes"

        # Relocation (must be checked before city/location)
        if 'relocate' in question_lower or 'relocation' in question_lower:
            return self.profile_data.get('willing_to_relocate') or "Yes"

        # Prior / Past Employment with the Company
        if any(k in question_lower for k in [
            'previously been employed', 'worked before', 'worked at', 'worked for', 
            'former employee', 'previously worked', 'prior employee', 'engaged with',
            'have you ever worked', 'have you previously worked'
        ]):
            return "No"

        # Location / City (strictly questions asking for candidate's location/city - avoid 'capacity', 'commute', etc.)
        if re.search(r'\bcity\b', question_lower) or (re.search(r'\blocation\b', question_lower) and not any(w in question_lower for w in ['comfortable', 'willing', 'commute', 'relocate', 'agree', 'able to', 'open to', 'capacity', 'travel'])):
            return self.profile_data.get('city') or self.learned_answers.get('Location (city)') or self.learned_answers.get('City') or 'Hyderabad'

        # 3. Experience & Skills
        if 'years' in question_lower or 'experience' in question_lower:
            if 'python' in question_lower:
                return self.profile_data.get('years_python') or self.profile_data.get('years_experience', '1')
            if 'javascript' in question_lower or 'js' in question_lower:
                return self.profile_data.get('years_javascript') or '1'
            if 'react' in question_lower:
                return self.profile_data.get('years_react') or '1'
            if 'ml' in question_lower or 'machine learning' in question_lower or 'ai' in question_lower:
                return self.profile_data.get('years_ml') or self.profile_data.get('years_experience', '1')
            return self.profile_data.get('years_experience', '1')
        
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

        # 6.1 Degree / Qualification / Education level
        if any(k in question_lower for k in ['qualification', 'highest education', 'degree', 'level of education', 'highest level']):
            return self.profile_data.get('degree') or self.profile_data.get('education') or self.learned_answers.get('Highest Qualification Held') or "Bachelor's"
        
        # 7. Availability / Start Date / Notice Period
        if any(k in question_lower for k in ['start', 'notice period', 'immediate', 'available to start', 'within the next', '1-2 weeks', '1–2 weeks', 'start date']):
            return "Yes"

        # 7.1 Prior / Past Employment with the Company
        if any(k in question_lower for k in [
            'previously been employed', 'worked before', 'worked at', 'worked for', 
            'former employee', 'previously worked', 'prior employee', 'engaged with',
            'have you ever worked', 'have you previously worked'
        ]):
            return "No"

        # 7.2 Background check / Screening consent
        if any(k in question_lower for k in ['background check', 'drug test', 'screening']):
            if 'convicted' in question_lower or 'criminal' in question_lower or 'felony' in question_lower:
                return "No"
            return "Yes"

        # 7.3 Commute & In-Person Interview
        if any(k in question_lower for k in ['commute', 'commuting', 'in person', 'in-person', 'attend in person', 'willing to travel', 'hybrid']):
            return "Yes"

        # 7.4 Consent / Privacy Policy / Future Job Opportunities / Terms / Agreement
        if any(k in question_lower for k in [
            'privacy policy', 'privacy notice', 'terms of service', 'terms and conditions', 
            'contact me about future', 'future job opportunities', 'allow', 'consent',
            'agree to', 'i agree', 'i acknowledge', 'terms of use'
        ]):
            return "Yes"
            
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
            # Absolute guard: Never ask human if field is already filled and has no error
            existing = self._extract_filled_value(field, question_text)
            has_error = self._field_has_error(field)
            if existing and str(existing).strip() and not self._is_placeholder_or_empty(existing, question_text) and not has_error:
                logger.info(f"Field '{question_text}' already has valid value '{existing}' and no errors. Skipping human popup.", step="human_input")
                return str(existing).strip()

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
        """Resolve any fields that currently have validation errors or are left unfilled via Local Cache -> LLM Batch -> Human fallback"""
        try:
            fields = self._get_form_fields()
            unresolved_errored = []
            
            for i, field in enumerate(fields):
                field_type = self._detect_field_type(field)
                if field_type in ["file", "education_section", "work_experience_section", "button_container"] or field.locator("input, select, textarea").count() == 0:
                    continue
                question_text = self._extract_question(field)
                if not question_text:
                    continue
                val = self._extract_filled_value(field, question_text)
                has_error = self._field_has_error(field)
                is_req = self._is_required_field(field)

                # If field has error or is an empty/placeholder required field
                if has_error or (is_req and (val is None or self._is_placeholder_or_empty(val, question_text))):
                    field_type = self._detect_field_type(field)
                    data_type = self._extract_html_data_type(field, field_type, question_text)
                    options = self._extract_options(field, field_type) if field_type in ["select", "radio"] else None
                    html_spec = self._inspect_html_element_spec(field, field_type, question_text)
                    
                    try:
                        field.scroll_into_view_if_needed(timeout=1000)
                    except:
                        pass

                    # Step 1: Try Local Cache & Profile Match first
                    resolved_answer = self._get_local_answer(question_text, field_type, options, data_type)
                    
                    # Anti-Loop Guard: If the field already has an active error and the local answer is the EXACT failing value in DOM:
                    if has_error and val and resolved_answer and str(resolved_answer).strip().lower() == str(val).strip().lower():
                        logger.warning(f"⚠️ Local cache answer '{resolved_answer}' for '{question_text}' is currently failing validation in DOM. Evicting bad cache entry and adapting...", step="handle_error")
                        if question_text in self.learned_answers:
                            del self.learned_answers[question_text]
                            self._save_learned_answers()
                        
                        # If the failing value was non-numeric text, try converting to numeric:
                        numeric_alt = self._clean_numeric_answer(str(val), data_type="INTEGER")
                        if numeric_alt and numeric_alt != str(val):
                            logger.info(f"🔄 Retrying errored field '{question_text}' with numeric adaptation: '{numeric_alt}'", step="handle_error")
                            resolved_answer = numeric_alt
                            data_type = "INTEGER"
                        else:
                            resolved_answer = None

                    if resolved_answer is not None:
                        logger.info(f"✅ Errored/empty field '{question_text}' resolved via profile/cache -> '{resolved_answer}'", step="handle_error")
                        self._fill_field(field, resolved_answer, field_type, data_type)
                        time.sleep(0.3)
                        continue

                    # Queue for batch LLM resolution
                    unresolved_errored.append({
                        "id": i,
                        "field_obj": field,
                        "question": question_text,
                        "field_type": field_type,
                        "data_type": data_type,
                        "options": options,
                        "details": html_spec.get("details", ""),
                        "is_required": is_req,
                        "html_spec": html_spec
                    })

            # Step 2: Batch LLM call for all unresolved errored fields
            remaining_for_human = []
            if unresolved_errored:
                if self.llm_filler and self.llm_filler.is_enabled():
                    logger.info(f"🚀 Batching {len(unresolved_errored)} unresolved errored/empty field(s) to LLM with system_prompt_txt in 1 call...", step="handle_error")
                    batch_results = self.llm_filler.get_batch_answers(unresolved_errored)
                    
                    for item in unresolved_errored:
                        item_id = item["id"]
                        q_text = item["question"]
                        f_type = item["field_type"]
                        d_type = item["data_type"]
                        field = item["field_obj"]
                        
                        llm_res = batch_results.get(item_id, {})
                        ans = llm_res.get("answer")
                        conf = llm_res.get("confidence", 0.0)
                        
                        if ans and str(ans).strip().upper() != "HUMAN_INTERVENTION_REQUIRED" and conf >= 0.70:
                            if d_type in ["INTEGER", "DECIMAL", "NUMBER"] or f_type == "number":
                                ans = self._clean_numeric_answer(ans, d_type)
                            logger.info(f"✅ LLM answered errored field '{q_text}' [{d_type}] (conf {conf:.2f}): '{ans}'", step="handle_error")
                            self.learned_answers[q_text] = ans
                            self._save_learned_answers()
                            self._fill_field(field, ans, f_type, d_type)
                            time.sleep(0.3)
                        else:
                            remaining_for_human.append((item, ans))
                else:
                    remaining_for_human = [(item, None) for item in unresolved_errored]

            # Step 3: Human popup ONLY if local cache AND LLM cannot resolve
            for item, tentative_ans in remaining_for_human:
                field = item["field_obj"]
                q_text = item["question"]
                f_type = item["field_type"]
                d_type = item["data_type"]
                opts = item["options"]
                is_req = item["is_required"]
                
                # Check if already resolved
                val_now = self._extract_filled_value(field, q_text)
                err_now = self._field_has_error(field)
                if val_now and not self._is_placeholder_or_empty(val_now, q_text) and not err_now:
                    logger.info(f"Field '{q_text}' already resolved with '{val_now}'. Skipping human popup.", step="handle_error")
                    continue

                logger.warning(f"🚨 Missing/Errored field: '{q_text}' - Triggering On-Screen Popup for resolution...", step="handle_error")
                ans = self._ask_human(
                    question_text=q_text,
                    field=field,
                    is_required=is_req,
                    suggested_answer=tentative_ans if tentative_ans and tentative_ans.upper() != "HUMAN_INTERVENTION_REQUIRED" else None,
                    field_type=f_type,
                    options=opts,
                    data_type=d_type
                )
                if ans is not None:
                    self._fill_field(field, ans, f_type, d_type)
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
    
    def _extract_filled_value(self, field: Locator, question_text: str = "") -> str:
        """Extract the value that was filled in the field"""
        try:
            # 0. Check for Education section (profile already contains education)
            if self._is_education_section(field):
                return "Education Provided (Profile)"
                
            # 0.1 Check for Work Experience section
            if self._is_work_experience_section(field):
                return "Work Experience Provided (Profile)"

            # 0.2 Check for File / Document upload
            if field.locator("input[type='file']").count() > 0 or field.locator(".jobs-document-upload-redesign-card__container").count() > 0:
                if field.locator(".jobs-document-upload-redesign-card__container--selected, [aria-label='Selected']").count() > 0:
                    return "Resume Selected"
                return "File Field"

            # 1. Try any text/email/tel/number input
            input_elem = field.locator("input:not([type='hidden']):not([type='file']):not([type='checkbox']):not([type='radio'])").first
            if input_elem.count() > 0:
                val = None
                try:
                    val = input_elem.input_value()
                except:
                    pass
                if not val or not str(val).strip():
                    try:
                        val = input_elem.get_attribute("value")
                    except:
                        pass
                if val and str(val).strip():
                    val_str = str(val).strip()
                    if not self._is_placeholder_or_empty(val_str, question_text):
                        return val_str
                return None
            
            # 2. Try Textarea
            textarea = field.locator("textarea").first
            if textarea.count() > 0:
                val = textarea.input_value()
                if val and val.strip() and not self._is_placeholder_or_empty(val.strip(), question_text):
                    return val.strip()
                return None
            
            # 3. Try Select Dropdown
            select = field.locator("select").first
            if select.count() > 0:
                selected_text = ""
                try:
                    selected_text = self.page.evaluate("el => el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : ''", select.element_handle())
                except:
                    pass
                if not selected_text:
                    try:
                        selected_text = select.input_value() or ""
                    except:
                        pass
                
                selected_text = (selected_text or "").strip()
                if selected_text and not self._is_placeholder_or_empty(selected_text, question_text):
                    return selected_text
                return None
            
            # 4. Try Radio (get checked label)
            checked_radio = field.locator("input[type='radio']:checked, [role='radio'][aria-checked='true']").first
            if checked_radio.count() == 0:
                # Fallback: check each radio input's property
                for r in field.locator("input[type='radio']").all():
                    try:
                        if r.is_checked():
                            checked_radio = r
                            break
                    except:
                        pass

            if checked_radio.count() > 0:
                q_clean = (question_text or "").strip().lower()

                def _valid_radio_label(candidate: str) -> str:
                    if not candidate:
                        return None
                    clean = re.sub(r'\s*\*+\s*$', '', candidate).strip()
                    c_lower = clean.lower()
                    if c_lower in ["on", "off", "none", "null", "undefined"]:
                        return None
                    if q_clean and (c_lower == q_clean or q_clean in c_lower):
                        return None
                    if len(clean) > 80:
                        return None
                    return clean

                # 4.1 Try associated label via id
                radio_id = checked_radio.get_attribute("id")
                if radio_id:
                    try:
                        label = field.locator(f"label[for='{radio_id}']").first
                        if label.count() > 0:
                            ans = _valid_radio_label(label.inner_text())
                            if ans: return ans
                    except:
                        pass
                # 4.2 Try ancestor label
                try:
                    parent_lbl = checked_radio.locator("xpath=ancestor::label[1]").first
                    if parent_lbl.count() > 0:
                        ans = _valid_radio_label(parent_lbl.inner_text())
                        if ans: return ans
                except:
                    pass
                # 4.3 Try sibling label or span
                try:
                    sibling = checked_radio.locator("xpath=following-sibling::label | following-sibling::span | preceding-sibling::label").first
                    if sibling.count() > 0:
                        ans = _valid_radio_label(sibling.inner_text())
                        if ans: return ans
                except:
                    pass
                # 4.4 Try parent container text
                try:
                    parent_wrap = checked_radio.locator("xpath=..").first
                    if parent_wrap.count() > 0:
                        ans = _valid_radio_label(parent_wrap.inner_text())
                        if ans: return ans
                except:
                    pass
                # 4.5 Try aria-label or value
                val = checked_radio.get_attribute("aria-label") or checked_radio.get_attribute("value")
                ans = _valid_radio_label(val)
                if ans:
                    return ans

                # 4.6 Fallback: Infer from radio index in 2-option group (Yes / No)
                try:
                    all_radios = field.locator("input[type='radio'], [role='radio']").all()
                    if len(all_radios) == 2:
                        for i_r, r in enumerate(all_radios):
                            if r.is_checked() or r.get_attribute("aria-checked") == "true":
                                return "Yes" if i_r == 0 else "No"
                except:
                    pass

                return "Checked"
            
            # 5. Try Checkbox
            checkbox = field.locator("input[type='checkbox'], [role='checkbox']").first
            if checkbox.count() > 0:
                is_chk = False
                try:
                    is_chk = checkbox.is_checked() or checkbox.get_attribute("aria-checked") == "true"
                except:
                    pass
                return "Checked" if is_chk else None
                
        except:
            pass
        
        return None
    
    def _match_best_option(self, raw_answer: str, options: list) -> str:
        """
        Intelligently map any raw answer (numeric '0'/'1', string 'Yes'/'No', range '2', text)
        to the best matching option from the target dropdown/radio options list.
        """
        if not options or not raw_answer:
            return raw_answer if raw_answer is not None else ""
            
        ans_str = str(raw_answer).strip()
        ans_lower = ans_str.lower()
        
        # 1. Exact match (case-insensitive)
        for opt in options:
            if str(opt).strip().lower() == ans_lower:
                return str(opt).strip()

        # Country Dial Code Matching (e.g. "+91", "(+91)", "India", "Your Country (+91)")
        dial_match = re.search(r'\+?\d{1,4}', ans_str)
        if dial_match:
            digits = dial_match.group().lstrip('+')
            for opt in options:
                opt_str = str(opt).strip()
                if f"(+{digits})" in opt_str or f"+{digits}" in opt_str or f"({digits})" in opt_str:
                    return opt_str
        for country in ["india", "united states", "canada", "united kingdom", "australia", "germany", "france"]:
            if country in ans_lower:
                for opt in options:
                    if country in str(opt).strip().lower():
                        return str(opt).strip()
                
        # 2. Boolean / Numeric Semantic Equivalence
        if ans_lower in ["0", "no", "false", "none", "nil", "n/a", "no offer", "not applicable", "off"]:
            for opt in options:
                opt_l = str(opt).strip().lower()
                if opt_l in ["no", "0", "false", "none", "n/a", "i do not", "no, i do not", "0 years", "none of the above", "no offer", "0.0"]:
                    return str(opt).strip()
        elif ans_lower in ["1", "yes", "true", "on", "checked", "agree", "i agree"]:
            for opt in options:
                opt_l = str(opt).strip().lower()
                if opt_l in ["yes", "1", "true", "i do", "yes, i do", "1 year", "1+ year", "1.0", "i agree"]:
                    return str(opt).strip()

        # 3. Numeric Range Matching (e.g. answer is "2" or "2 years", options: ["0-1 years", "1-3 years", "3-5 years", "5+ years"])
        num_match = re.search(r'\d+', ans_str)
        if num_match:
            val = int(num_match.group())
            for opt in options:
                opt_str = str(opt).strip()
                # Check ranges like "1-3", "1 to 3", "1 - 3"
                range_match = re.search(r'(\d+)\s*(?:-|to)\s*(\d+)', opt_str, re.I)
                if range_match:
                    low = int(range_match.group(1))
                    high = int(range_match.group(2))
                    if low <= val <= high:
                        return opt_str
                # Check "X+" or "more than X"
                plus_match = re.search(r'(\d+)\s*\+|more than\s*(\d+)|over\s*(\d+)', opt_str, re.I)
                if plus_match:
                    thresh = int(next(m for m in plus_match.groups() if m is not None))
                    if val >= thresh:
                        return opt_str
                # Check "less than X" or "under X"
                under_match = re.search(r'less than\s*(\d+)|under\s*(\d+)|<\s*(\d+)', opt_str, re.I)
                if under_match:
                    thresh = int(next(m for m in under_match.groups() if m is not None))
                    if val < thresh:
                        return opt_str

        # 4. Prefix / Substring matching (e.g. answer "Yes" matches "Yes, I am authorized" or "Male" matches "Male (he/him)")
        for opt in options:
            opt_l = str(opt).strip().lower()
            if opt_l.startswith(ans_lower) or ans_lower.startswith(opt_l):
                return str(opt).strip()
                
        # 5. Fuzzy containment
        for opt in options:
            opt_l = str(opt).strip().lower()
            if ans_lower in opt_l:
                return str(opt).strip()

        return str(options[0]).strip() if options else ans_str

    def _adapt_answer_to_target_field(self, raw_answer: str, field_type: str, data_type: str = None, options: list = None, question_text: str = "") -> str:
        """
        Universal Live Data-Type Adapter:
        Converts ANY saved, cached, or LLM answer into the exact data type and option schema required by the live DOM.
        """
        if raw_answer is None:
            return ""
            
        ans_str = str(raw_answer).strip()
        data_type_upper = (data_type or field_type or "SHORT_TEXT").upper()
        
        # 1. Numeric / Integer Fields (Whole numbers only)
        if data_type_upper in ["INTEGER", "NUMBER"] or field_type == "number":
            return self._clean_numeric_answer(ans_str, data_type="INTEGER")
            
        # 2. Decimal Fields (e.g. "0.0", "1.5", "50.0")
        if data_type_upper == "DECIMAL":
            return self._clean_numeric_answer(ans_str, data_type="DECIMAL")
            
        # 3. Radio / Select / Dropdown Fields (Must match target available options)
        if field_type in ["radio", "select"] or data_type_upper in ["RADIO_OPTION", "SELECT_OPTION"]:
            if options:
                return self._match_best_option(ans_str, options)
            return ans_str
            
        # 4. Boolean Fields
        if data_type_upper == "BOOLEAN" or field_type == "checkbox":
            ans_l = ans_str.lower()
            if ans_l in ["0", "no", "false", "none", "nil", "n/a", "no offer", "off"]:
                return "No" if field_type != "checkbox" else "false"
            elif ans_l in ["1", "yes", "true", "on", "checked", "agree", "i agree"]:
                return "Yes" if field_type != "checkbox" else "true"
            return "Yes" if "yes" in ans_l else ("No" if "no" in ans_l else ans_str)
            
        # 5. Text / Long Text (Clean quotes/markdown wrappers if any)
        if ans_str.startswith('"') and ans_str.endswith('"') and len(ans_str) > 1:
            ans_str = ans_str[1:-1].strip()
            
        return ans_str

    def _clean_numeric_answer(self, answer: str, data_type: str = "INTEGER") -> str:
        """Sanitize any answer (including cached strings like 'No'/'Yes') to a clean numeric/decimal value"""
        if answer is None:
            return "0"
        
        answer_str = str(answer).strip()
        answer_lower = answer_str.lower()
        
        # Map boolean/yes/no and immediate joining answers to digits if expected field is numeric
        if answer_lower in ["no", "none", "false", "n/a", "na", "no offer", "nil", "zero", "0", "not applicable", "immediately", "immediate", "immediate joiner", "asap", "now", "0 days"]:
            return "0" if data_type != "DECIMAL" else "0.0"
        if answer_lower in ["yes", "true", "one", "1"]:
            return "1" if data_type != "DECIMAL" else "1.0"

        # Map common time periods to number of days
        time_to_days = {
            "1 month": "30", "2 months": "60", "3 months": "90",
            "1 week": "7", "2 weeks": "14", "3 weeks": "21", "4 weeks": "28",
            "15 days": "15", "30 days": "30", "60 days": "60", "90 days": "90"
        }
        if answer_lower in time_to_days:
            val = time_to_days[answer_lower]
            return val if data_type != "DECIMAL" else f"{val}.0"

        # Map common text numbers to digits
        word_to_num = {
            "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
            "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"
        }
        if answer_lower in word_to_num:
            val = word_to_num[answer_lower]
            return val if data_type != "DECIMAL" else f"{val}.0"

        # Extract digits or decimals (like "1.4" or "0.0")
        numbers = re.findall(r'\d+\.\d+|\d+', answer_str)
        if not numbers:
            return "0" if data_type != "DECIMAL" else "0.0"
        
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

    def _handle_typeahead_input(self, input_elem: Locator, value: str) -> bool:
        """
        Handle LinkedIn typeahead/autocomplete inputs (e.g. City/Location, Company, School):
        Types the value, waits for the floating dropdown list, and clicks the best matching suggestion.
        """
        try:
            val_str = str(value).strip()
            if not val_str:
                return False

            input_elem.click()
            time.sleep(0.2)
            input_elem.fill("")
            time.sleep(0.2)

            # Type value to trigger LinkedIn's async auto-suggest
            type_text = val_str.split(',')[0].strip() if ',' in val_str else val_str
            try:
                input_elem.press_sequentially(type_text, delay=40)
            except:
                input_elem.fill(type_text)
                input_elem.dispatch_event("input")
                
            time.sleep(1.0)  # Wait for dropdown to populate

            # Typeahead suggestion selectors across LinkedIn Easy Apply forms
            suggestion_selectors = [
                ".basic-typeahead__selectable-list li",
                "div[role='listbox'] div[role='option']",
                "div[role='listbox'] li",
                "ul[role='listbox'] li",
                ".basic-typeahead__results-list li",
                ".artdeco-typeahead__results-list li",
                "div[data-test-typeahead-result]",
                ".search-typeahead-v2__hit",
                ".basic-typeahead__selectable",
                ".type-ahead-results li"
            ]

            # Try locating the active dropdown list in page or parent
            for sel in suggestion_selectors:
                try:
                    suggestions = self.page.locator(sel).all()
                    if suggestions:
                        # 1. Try to find best match containing val_str or city name
                        best_match = None
                        for s in suggestions:
                            try:
                                text = s.text_content(timeout=500).strip()
                                if text:
                                    if val_str.lower() in text.lower() or text.lower() in val_str.lower():
                                        best_match = s
                                        break
                                    if type_text.lower() in text.lower():
                                        best_match = s
                            except:
                                continue

                        # If no exact match, select the first suggestion
                        target_to_click = best_match or suggestions[0]
                        target_text = ""
                        try:
                            target_text = target_to_click.text_content(timeout=500).strip()
                        except:
                            pass
                            
                        target_to_click.click(timeout=2000, force=True)
                        logger.info(f"✅ Clicked typeahead dropdown suggestion: '{target_text}' for input '{val_str}'", step="fill_typeahead")
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue

            # Keyboard navigation fallback: ArrowDown + Enter
            try:
                input_elem.press("ArrowDown")
                time.sleep(0.3)
                input_elem.press("Enter")
                time.sleep(0.3)
                logger.info(f"Selected typeahead suggestion via ArrowDown+Enter for: '{val_str}'", step="fill_typeahead")
                return True
            except:
                pass

        except Exception as e:
            logger.debug(f"Typeahead handler note: {e}", step="fill_typeahead")
            
        return False

    def _fill_field(self, field: Locator, answer: str, field_type: str, data_type: str = None):
        """Fill the field with the answer, guaranteeing schema, typeahead, and data type compliance"""
        try:
            if field_type in ["file", "education_section", "work_experience_section"]:
                return
                
            # Extract live options for select / radio fields to ensure valid option selection
            options = self._extract_options(field, field_type) if field_type in ["select", "radio"] else []
            final_value = self._adapt_answer_to_target_field(answer, field_type, data_type, options)
            
            if field_type == "select":
                select = field.locator("select").first
                try:
                    select.select_option(label=final_value)
                except:
                    try:
                        select.select_option(value=final_value)
                    except:
                        if options:
                            best = self._match_best_option(final_value, options)
                            try:
                                select.select_option(label=best)
                            except:
                                pass
                logger.debug(f"Selected: {final_value}", step="fill_field")
                
            elif field_type == "radio":
                target_str = str(final_value).strip().lower()
                is_target_yes = target_str in ["yes", "true", "1", "y"]
                is_target_no = target_str in ["no", "false", "0", "n"]
                
                radios = field.locator("input[type='radio'], [role='radio']").all()
                selected = False
                
                # Check each radio element
                for r in radios:
                    r_val = (r.get_attribute("value") or "").strip().lower()
                    r_id = r.get_attribute("id") or ""
                    r_aria = (r.get_attribute("aria-label") or "").strip().lower()
                    
                    candidate_texts = []
                    if r_val:
                        candidate_texts.append(r_val)
                    if r_aria:
                        candidate_texts.append(r_aria)
                        
                    if r_id:
                        try:
                            lbl = field.locator(f"label[for='{r_id}']").first
                            if lbl.count() > 0:
                                t = lbl.inner_text().strip().lower()
                                if t: candidate_texts.append(t)
                        except:
                            pass
                            
                    try:
                        parent_lbl = r.locator("xpath=ancestor::label[1]").first
                        if parent_lbl.count() > 0:
                            t = parent_lbl.inner_text().strip().lower()
                            if t: candidate_texts.append(t)
                    except:
                        pass
                        
                    try:
                        parent_wrap = r.locator("xpath=..").first
                        if parent_wrap.count() > 0:
                            t = parent_wrap.inner_text().strip().lower()
                            if t: candidate_texts.append(t)
                    except:
                        pass

                    # Determine match
                    matched = False
                    for txt in candidate_texts:
                        if txt == target_str:
                            matched = True
                            break
                        if is_target_yes and (txt in ["yes", "true", "1"] or re.search(r'\byes\b', txt)):
                            matched = True
                            break
                        if is_target_no and (txt in ["no", "false", "0"] or re.search(r'\bno\b', txt)):
                            matched = True
                            break
                        if target_str in txt or txt in target_str:
                            matched = True
                            break
                            
                    if matched:
                        try:
                            r.scroll_into_view_if_needed(timeout=1000)
                        except:
                            pass
                            
                        # 1. Playwright direct check
                        try:
                            r.check(force=True, timeout=1500)
                            selected = True
                        except:
                            pass
                            
                        # 2. DOM Event evaluation (crucial for modern React & Horizon UI)
                        try:
                            r.evaluate("""el => {
                                el.checked = true;
                                if (el.setAttribute) el.setAttribute('aria-checked', 'true');
                                el.dispatchEvent(new Event('click', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                            }""")
                            selected = True
                        except:
                            pass
                            
                        # 3. Click associated label if present
                        if r_id:
                            try:
                                lbl = field.locator(f"label[for='{r_id}']").first
                                if lbl.count() > 0:
                                    lbl.click(force=True, timeout=1000)
                                    selected = True
                            except:
                                pass
                                
                        # 4. Click parent wrapper if present
                        try:
                            parent_wrap = r.locator("xpath=ancestor::*[self::label or self::div][1]").first
                            if parent_wrap.count() > 0:
                                parent_wrap.click(force=True, timeout=1000)
                                selected = True
                        except:
                            pass
                            
                        # 5. Verify checked property
                        try:
                            if not r.is_checked():
                                r.evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); }")
                        except:
                            pass
                            
                        logger.info(f"✅ Radio selected for '{target_str}': matched candidate {candidate_texts}", step="fill_field")
                        break

                # Fallback: if no radio input matched, search labels and clickable spans/divs
                if not selected:
                    labels = field.locator("label, div[role='radio'], span[role='radio']").all()
                    for lbl in labels:
                        lbl_text = (lbl.inner_text() or "").strip().lower()
                        lbl_match = False
                        if lbl_text == target_str:
                            lbl_match = True
                        elif is_target_yes and (lbl_text in ["yes", "true", "1"] or re.search(r'\byes\b', lbl_text)):
                            lbl_match = True
                        elif is_target_no and (lbl_text in ["no", "false", "0"] or re.search(r'\bno\b', lbl_text)):
                            lbl_match = True
                        elif target_str in lbl_text:
                            lbl_match = True
                            
                        if lbl_match:
                            try:
                                lbl.scroll_into_view_if_needed(timeout=1000)
                                lbl.click(force=True, timeout=1500)
                                selected = True
                                logger.info(f"✅ Radio selected via fallback element click: '{lbl_text}'", step="fill_field")
                                break
                            except:
                                continue
                    
            elif field_type == "checkbox":
                checkbox = field.locator("input[type='checkbox'], [role='checkbox']").first
                if checkbox.count() > 0:
                    target_str = str(final_value).strip().lower()
                    should_check = target_str in ['yes', 'true', '1', 'checked', 'on', 'agree', 'i agree']
                    try:
                        checkbox.scroll_into_view_if_needed(timeout=1000)
                    except:
                        pass
                        
                    try:
                        if should_check:
                            try:
                                checkbox.check(force=True, timeout=1500)
                            except:
                                pass
                            checkbox.evaluate("""el => {
                                el.checked = true;
                                if (el.setAttribute) el.setAttribute('aria-checked', 'true');
                                el.dispatchEvent(new Event('click', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                            }""")
                            if not checkbox.is_checked():
                                parent_lbl = checkbox.locator("xpath=ancestor::label[1] | following-sibling::label | preceding-sibling::label").first
                                if parent_lbl.count() > 0:
                                    parent_lbl.click(force=True, timeout=1000)
                        else:
                            try:
                                checkbox.uncheck(force=True, timeout=1500)
                            except:
                                pass
                            checkbox.evaluate("""el => {
                                el.checked = false;
                                if (el.setAttribute) el.setAttribute('aria-checked', 'false');
                                el.dispatchEvent(new Event('click', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                            }""")
                    except Exception as e:
                        logger.debug(f"Error checking checkbox: {e}", step="fill_field")
                    logger.info(f"✅ Checkbox {'checked' if should_check else 'unchecked'} for '{target_str}'", step="fill_field")
                
            elif field_type in ["number", "text", "textarea"] or (data_type in ["INTEGER", "DECIMAL", "NUMBER"]):
                input_elem = field.locator("input:not([type='hidden']), textarea").first
                if input_elem.count() > 0:
                    try:
                        input_elem.click()
                    except:
                        pass
                        
                    final_value_str = str(final_value)
                    
                    # 1. Check if this is a typeahead / combobox auto-suggest field (e.g. City/Location, Company, School)
                    is_typeahead = False
                    try:
                        role = input_elem.get_attribute("role") or ""
                        aria_auto = input_elem.get_attribute("aria-autocomplete") or ""
                        aria_ctrl = input_elem.get_attribute("aria-controls") or ""
                        cls = input_elem.get_attribute("class") or ""
                        parent_cls = field.get_attribute("class") or ""
                        q_text = self._extract_question(field).lower()
                        
                        if role == "combobox" or aria_auto in ["list", "both"] or aria_ctrl or "typeahead" in cls or "typeahead" in parent_cls or any(k in q_text for k in ["city", "location", "school", "university", "company", "postal", "zip"]):
                            is_typeahead = True
                    except:
                        pass
                        
                    if is_typeahead:
                        handled = self._handle_typeahead_input(input_elem, final_value_str)
                        if handled:
                            return
                    
                    # 2. Standard Text/Number Input Fill
                    input_elem.fill("")  # Clear first
                    input_elem.fill(final_value_str)
                    input_elem.dispatch_event("input")
                    input_elem.dispatch_event("change")
                    
                    # 3. Check if a floating typeahead dropdown popped up after standard fill
                    time.sleep(0.5)
                    if self.page.locator(".basic-typeahead__selectable-list, div[role='listbox'] div[role='option'], .artdeco-typeahead__results-list").count() > 0:
                        self._handle_typeahead_input(input_elem, final_value_str)
                        
                    logger.debug(f"Filled field ({field_type}/{data_type}): {final_value_str} (raw answer: {answer})", step="fill_field")
                
        except Exception as e:
            logger.debug(f"Error filling field: {e}", step="fill_field")