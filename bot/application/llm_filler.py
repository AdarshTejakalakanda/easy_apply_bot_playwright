import os
import json
import logging
import requests
import time
from dotenv import load_dotenv

# Ensure environment variables are loaded
load_dotenv()

logger = logging.getLogger("bot.application.llm_filler")

class LLMFiller:
    def __init__(self, candidate_profile: dict):
        self.candidate_profile = candidate_profile
        self.profile_data = candidate_profile.get('profile_data', {})
        self.candidate_id = candidate_profile.get('id', 'default')
        
        # Cache file for answers
        self.cache_file = f'./profiles/{self.candidate_id}/llm_cache.json'
        self.cache = self._load_cache()
        
        # API Keys
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.openai_key = os.getenv("OPENAI_API_KEY")
        
        # Primary Gemini Model
        self.primary_model = "gemini-2.5-flash"
        
        # Load resume text from system_prompt_txt (highest priority) or PDF
        self.resume_text = self._load_resume_text()
        
        # Gemini Context Caching ID
        self.cache_id = None
        
        # Initialize Gemini Context Cache if Gemini is used
        if self.gemini_key:
            self._init_gemini_cache()
            
        if self.is_enabled():
            provider = "Gemini" if self.gemini_key else "OpenAI"
            status = f"with context caching (ID: {self.cache_id})" if self.cache_id else "without context caching (fallback mode)"
            logger.info(f"🤖 LLM auto-filler enabled using {provider} {status}.", extra={"step": "llm_init"})

    def is_enabled(self) -> bool:
        """Returns True if any API key is configured"""
        return bool(self.gemini_key or self.openai_key)

    def _load_cache(self) -> dict:
        """Load cached answers from file"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load LLM cache: {e}", extra={"step": "llm_init"})
        return {}

    def _save_cache(self):
        """Save cached answers to file"""
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save LLM cache: {e}", extra={"step": "llm_cache"})

    def _load_resume_text(self) -> str:
        """Load resume text from system_prompt_txt or PDF"""
        # Try system_prompt_txt first
        system_prompt_path = './system_prompt_txt'
        if os.path.exists(system_prompt_path):
            try:
                with open(system_prompt_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        logger.info("Loaded resume content from system_prompt_txt", extra={"step": "llm_init"})
                        return content
            except Exception as e:
                logger.warning(f"Failed to read system_prompt_txt: {e}", extra={"step": "llm_init"})

        # Fallback to configured resume PDF/TXT
        uploads = self.candidate_profile.get('uploads', {})
        resume_path = uploads.get('Resume')
        if not resume_path or not os.path.exists(resume_path):
            return ""

        ext = os.path.splitext(resume_path)[1].lower()
        if ext == '.txt':
            try:
                with open(resume_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except:
                pass
        elif ext == '.pdf':
            try:
                import pypdf
                text = ""
                with open(resume_path, 'rb') as f:
                    reader = pypdf.PdfReader(f)
                    for page in reader.pages:
                        page_text = page.extract_text()
                        if page_text:
                            text += page_text + "\n"
                return text
            except ImportError:
                logger.warning("pypdf is not installed. PDF resume text extraction skipped. Run: pip install pypdf", extra={"step": "llm_init"})
            except Exception as e:
                logger.warning(f"Failed to read PDF resume: {e}", extra={"step": "llm_init"})
        return ""

    def _init_gemini_cache(self):
        """Create a context cache resource using Gemini API"""
        if not self.gemini_key or not self.resume_text:
            return
            
        url = f"https://generativelanguage.googleapis.com/v1beta/cachedContents?key={self.gemini_key}"
        headers = {"Content-Type": "application/json"}
        
        # Combine System Prompt + Resume as the cached context
        system_instructions = self._get_system_instructions()
        cached_content_payload = f"{system_instructions}\n\n--- RESUME DATA ---\n{self.resume_text}"
        
        payload = {
            "model": f"models/{self.primary_model}",
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": cached_content_payload
                        }
                    ]
                }
            ],
            "ttl": "3600s" # Cache context for 1 hour
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=20)
            if response.status_code in [200, 201]:
                result = response.json()
                self.cache_id = result.get("name")
                logger.info(f"✅ Gemini context cache created successfully: {self.cache_id}", extra={"step": "llm_cache"})
            else:
                # Often fails with INVALID_ARGUMENT if text is too small (needs >32k tokens).
                # We log this as debug/info and fall back to standard non-cached calls.
                logger.debug(f"Gemini Context Caching not active (likely payload too small, which is normal). Fallback mode active.", extra={"step": "llm_cache"})
        except Exception as e:
            logger.warning(f"Error initializing Gemini context cache: {e}. Fallback active.", extra={"step": "llm_cache"})

    def _get_system_instructions(self) -> str:
        """Detailed system instructions for grounding the LLM's responses"""
        profile_json = json.dumps(self.profile_data, indent=2)
        return f"""You are an elite AI Job Application Assistant. Your goal is to help the candidate, {self.profile_data.get('full_name', 'the candidate')}, auto-fill forms for job applications on LinkedIn with 100% accuracy, matching their profile and resume details.

Here is the candidate's structured profile details:
{profile_json}

Follow these strict guidelines when answering questions:

1. WORK AUTHORIZATION & SPONSORSHIP (CRITICAL):
   - Check the profile data for work authorization status.
   - If the question asks if the candidate is authorized to work in the target country, answer based on 'authorized_to_work' (usually "Yes").
   - If the question asks if they require sponsorship now or in the future, answer strictly based on 'sponsorship_required' (usually "No").
   - Do not hallucinate or guess differently; respect these flags explicitly as they directly affect candidate eligibility.

2. EXPERIENCE YEARS & SKILLS:
   - When asked for "Years of experience" with a specific tool, language, or concept, check the resume.
   - If the resume explicitly mentions the skill, calculate the duration based on employment timeline (or use 'years_experience' from profile data).
   - If a specific tool is not mentioned, but is a subset of their work, estimate conservatively (e.g. 1-2 years). If completely unmentioned and unrelated, return "0".
   - Always return numbers as digits (e.g. "2" instead of "two") unless options dictate otherwise.

3. WRITTEN RESPONSES (Cover letters, "Why this role?", "Describe your experience"):
   - Keep answers professional, concise, and impact-driven.
   - Keep the length to 1-3 sentences maximum.
   - Highlight achievements, metrics, or tools matching the job question.
   - Write in first-person active voice ("I designed...", "I developed...") matching the candidate's professional style.

4. SELECTION CONSTRAINTS:
   - For dropdowns (select) or radio buttons, your answer MUST match one of the available choices exactly.
   - Choose the closest positive match representing the candidate's profile truthfully.
   - For demographics/voluntary self-identification (Gender, Race, Disability, Veteran status), select the candidate's preferences or "Decline to self-identify"/"I do not wish to answer" if not specified.

5. FORMATTING:
   - Respond strictly in JSON format as instructed. No markdown fences (like ```json), no preambles, no explanation prefix.
"""

    def get_answer(self, question_text: str, field_type: str, options: list = None, data_type: str = None) -> str or None:
        """Get answer from LLM (using local cache first)"""
        cache_key = f"{question_text}|{field_type}|{data_type}|{json.dumps(options or [])}"
        
        if cache_key in self.cache:
            logger.debug(f"LLM Cache Hit for: '{question_text}'", extra={"step": "llm_get_answer"})
            return self.cache[cache_key]

        # Get answer from API
        answer = self._query_llm(question_text, field_type, options, data_type)
        if answer is not None:
            self.cache[cache_key] = answer
            self._save_cache()
        return answer

    def _query_llm(self, question_text: str, field_type: str, options: list = None, data_type: str = None) -> str or None:
        """Query LLM API to get the answer"""
        try:
            if self.gemini_key:
                return self._call_gemini(question_text, field_type, options, data_type)
            elif self.openai_key:
                return self._call_openai(question_text, field_type, options, data_type)
        except Exception as e:
            logger.error(f"LLM API Call failed: {e}", extra={"step": "llm_api_call"})
        return None

    def _call_gemini(self, question_text: str, field_type: str, options: list = None, data_type: str = None) -> str or None:
        """Call Gemini API via REST with fallback models on 429 Rate Limit"""
        headers = {"Content-Type": "application/json"}
        options_str = json.dumps(options) if options else "None"
        accepting_type = data_type or field_type.upper()
        
        user_prompt = f"""--- FORM FIELD TO FILL ---
Question: "{question_text}"
Field Type: "{field_type}"
HTML Accepting Data Type: "{accepting_type}"
Available Options: {options_str}

--- STRICT OUTPUT FORMAT RULES FOR DATA TYPE "{accepting_type}" ---
1. If Data Type is "INTEGER" or field asks for numbers/years:
   - Your answer MUST BE ONLY A WHOLE NUMBER / DIGIT (e.g. "0", "1", "2", "3", "5").
   - DO NOT include words like "years", "months", decimals like "1.4", or extra text!
2. If Data Type is "RADIO_OPTION" or "SELECT_OPTION":
   - Your answer MUST EXACTLY match one of the values in the "Available Options" list.
   - Choose the option that is most accurate/truthful based on the candidate's profile and resume.
3. If Data Type is "BOOLEAN":
   - Respond with "Yes" or "No".
4. If Data Type is "SHORT_TEXT" or "LONG_TEXT":
   - Provide a direct, factual answer matching the candidate's resume.
5. Return your response ONLY as a valid JSON object matching this schema:
{{
  "answer": "your_answer_value_here",
  "explanation": "brief reasoning"
}}
Do not return any other text, markdown formatting, or prefix. Return ONLY the JSON object.
"""
        
        # Candidate models to try in sequence to bypass 429 limitations
        models_to_try = [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3-flash",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash"
        ]
        
        for model in models_to_try:
            # Context caching is only compatible/supported with the primary model
            use_cache = (self.cache_id and model == self.primary_model)
            
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.gemini_key}"
            
            if use_cache:
                payload = {
                    "contents": [{
                        "role": "user",
                        "parts": [{"text": user_prompt}]
                    }],
                    "cachedContent": self.cache_id,
                    "generationConfig": {
                        "responseMimeType": "application/json"
                    }
                }
            else:
                system_instructions = self._get_system_instructions()
                full_prompt = f"{system_instructions}\n\n--- RESUME DATA ---\n{self.resume_text}\n\n{user_prompt}"
                payload = {
                    "contents": [{
                        "role": "user",
                        "parts": [{"text": full_prompt}]
                    }],
                    "generationConfig": {
                        "responseMimeType": "application/json"
                    }
                }
                
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=30)
                if response.status_code == 200:
                    result = response.json()
                    text_response = result['candidates'][0]['content']['parts'][0]['text']
                    data = json.loads(text_response.strip())
                    return str(data.get("answer", ""))
                elif response.status_code == 429:
                    logger.warning(f"⚠️ Rate limit (429) hit on model {model}. Swapping to next fallback model...", extra={"step": "llm_fallback"})
                    # Brief sleep to clear burst rate limits before requesting next candidate model
                    time.sleep(1.5)
                    continue
                else:
                    logger.warning(f"Gemini API ({model}) returned error status {response.status_code}: {response.text}", extra={"step": "llm_call"})
                    # Try next model for non-429 failures too (robustness)
                    continue
            except Exception as e:
                logger.error(f"Error querying Gemini model {model}: {e}", extra={"step": "llm_call"})
                continue
                
        return None

    def _call_openai(self, question_text: str, field_type: str, options: list = None, data_type: str = None) -> str or None:
        """Call OpenAI API via REST"""
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.openai_key}"
        }
        
        system_instructions = self._get_system_instructions()
        options_str = json.dumps(options) if options else "None"
        accepting_type = data_type or field_type.upper()
        
        full_prompt = f"""{system_instructions}

--- CANDIDATE RESUME TEXT ---
{self.resume_text or "No resume text available."}

--- FORM FIELD TO FILL ---
Question: "{question_text}"
Field Type: "{field_type}"
HTML Accepting Data Type: "{accepting_type}"
Available Options: {options_str}

--- STRICT OUTPUT FORMAT RULES FOR DATA TYPE "{accepting_type}" ---
1. If Data Type is "INTEGER" or field asks for numbers/years:
   - Your answer MUST BE ONLY A WHOLE NUMBER / DIGIT (e.g. "0", "1", "2", "3", "5").
   - DO NOT include words like "years", "months", decimals like "1.4", or extra text!
2. If Data Type is "RADIO_OPTION" or "SELECT_OPTION":
   - Your answer MUST EXACTLY match one of the values in the "Available Options" list.
   - Choose the option that is most accurate/truthful based on the candidate's profile and resume.
3. If Data Type is "BOOLEAN":
   - Respond with "Yes" or "No".
4. If Data Type is "SHORT_TEXT" or "LONG_TEXT":
   - Provide a direct, factual answer matching the candidate's resume.
5. Return your response ONLY as a valid JSON object matching this schema:
{{
  "answer": "your_answer_value_here",
  "explanation": "brief reasoning"
}}
Do not return any other text, markdown formatting, or prefix. Return ONLY the JSON object.
"""
        
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "user", "content": full_prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            result = response.json()
            try:
                text_response = result['choices'][0]['message']['content']
                data = json.loads(text_response.strip())
                return str(data.get("answer", ""))
            except Exception as e:
                logger.warning(f"Failed to parse OpenAI response: {e}. Raw response: {response.text}")
        else:
            logger.warning(f"OpenAI API returned error status {response.status_code}: {response.text}")
        return None
