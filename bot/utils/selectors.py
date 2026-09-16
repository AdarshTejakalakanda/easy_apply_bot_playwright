# Playwright locator strings
# Optimized for Playwright's unified selector syntax
#project-bot-easy-apply-python-playwright\bot\utils\selectors.py
LOCATORS = {
    "next": {
        "primary": "button[aria-label='Continue to next step'], button[aria-label*='next step'], button[aria-label*='Continue'], footer button.artdeco-button--primary:has-text('Next'), footer button.artdeco-button--primary:has-text('Continue')",
        "fallback": "button.artdeco-button--primary:has-text('Next'), button.artdeco-button--primary:has-text('Continue'), //button[contains(., 'Next') or contains(., 'Continue')]"
    },
    
    "review": {
        "primary": "button[aria-label='Review your application'], button[aria-label*='Review'], footer button.artdeco-button--primary:has-text('Review')",
        "fallback": "button.artdeco-button--primary:has-text('Review'), //button[contains(., 'Review')]"
    },
    
    "submit": {
        "primary": "button[aria-label='Submit application'], button[aria-label*='Submit application'], button[aria-label*='Submit your application'], footer button.artdeco-button--primary:has-text('Submit')",
        "fallback": "button.artdeco-button--primary:has-text('Submit'), button[type='submit']:has-text('Submit'), //button[contains(., 'Submit')]"
    },
    "error": {
        "primary": ".artdeco-inline-feedback--error",
        "fallback": ".jobs-easy-apply-form-element--error, [data-test-form-element-error-message]"
    },
    
    "upload_resume": {
        "primary": "input[type='file'][id*='resume']",
        "fallback": "input[type='file'][id*='upload-resume']"
    },
    
    "upload_cv": {
        "primary": "input[type='file'][id*='cover']",
        "fallback": "input[type='file'][id*='upload-cover-letter']"
    },
    
    "follow": {
        "primary": "label[for='follow-company-checkbox']",
        "fallback": "//label[contains(text(), 'Follow')]"
    },
    
    "upload": {
        "primary": "input[name='file']",
        "fallback": "input[type='file']"
    },
    
    "search": {
        "primary": ".jobs-search-results-list",
        "fallback": ".jobs-search-results__list"
    },
    
    "links": {
        "primary": "div.job-card-container",
        "fallback": "li.jobs-search-results__list-item, div.scaffold-layout__list-item"
    },
    
    "fields": {
        "primary": ".jobs-easy-apply-form-section__grouping",
        "fallback": ".jobs-easy-apply-form-element"
    },
    
    "radio_select": {
        "primary": "input[type='radio']",
        "fallback": "//input[@type='radio']"
    },
    
    "multi_select": {
        "primary": "[id*='text-entity-list-form-component']",
        "fallback": "[id*='text-entity-list']"
    },
    
    "text_select": {
        "primary": ".artdeco-text-input--input",
        "fallback": "input[type='text']"
    },
    
    "2fa_oneClick": {
        "primary": "#reset-password-submit-button",
        "fallback": "button[type='submit']"
    },
    
    "easy_apply_button": {
        "primary": "#jobs-apply-button-id, button#jobs-apply-button-id, button.jobs-apply-button",
        "fallback": "button.jobs-apply-button, button[aria-label*='LinkedIn Apply to'], button[aria-label*='Apply to'], button[aria-label*='Easy Apply'], button[aria-label*='Continue applying']"
    }
}


def get_locator(key: str, use_fallback: bool = False):
    """
    Get a locator by key, optionally returning the fallback.
    
    Args:
        key: The selector key
        use_fallback: If True, return fallback locator if available
    
    Returns:
        String selector for Playwright or the original value if not dict
    """
    locator = LOCATORS.get(key)
    
    if not locator:
        return None
    
    # If it's a dict with primary/fallback
    if isinstance(locator, dict):
        if use_fallback and "fallback" in locator:
            return locator["fallback"]
        return locator.get("primary", locator.get("fallback"))
    
    # Legacy format (direct string)
    return locator


def has_fallback(key: str) -> bool:
    """Check if a selector has a fallback defined"""
    locator = LOCATORS.get(key)
    return isinstance(locator, dict) and "fallback" in locator
