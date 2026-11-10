# Playwright locator strings
# Optimized for Playwright's unified selector syntax
#project-bot-easy-apply-python-playwright\bot\utils\selectors.py
LOCATORS = {
    "next": {
        "primary": "dialog[open] button:has-text('Next'), dialog[data-testid='dialog'] button:has-text('Next'), dialog button:has-text('Next'), div[role='dialog'] button:has-text('Next'), .jobs-easy-apply-modal button:has-text('Next'), .artdeco-modal button:has-text('Next'), .artdeco-modal__actionbar button:has-text('Next'), button[aria-label*='next step'], button[aria-label*='Continue to next step'], button.artdeco-button--primary:has-text('Next'), button.artdeco-button--primary:has-text('Continue')",
        "fallback": "dialog button:has-text('Next'):not(:has-text('Add')), button:has-text('Next'):not(:has-text('Add')), button:has-text('Continue'):not(:has-text('Add')), button[aria-label*='Next']:not(:has-text('Add'))"
    },
    
    "review": {
        "primary": "dialog[open] button:has-text('Review'), dialog[data-testid='dialog'] button:has-text('Review'), dialog button:has-text('Review'), div[role='dialog'] button:has-text('Review'), .jobs-easy-apply-modal button:has-text('Review'), .artdeco-modal button:has-text('Review'), .artdeco-modal__actionbar button:has-text('Review'), button[aria-label*='Review your application'], button.artdeco-button--primary:has-text('Review')",
        "fallback": "dialog button:has-text('Review'):not(:has-text('Add')), button:has-text('Review'):not(:has-text('Add')), button[aria-label*='Review']:not(:has-text('Add'))"
    },
    
    "submit": {
        "primary": "dialog[open] button:has-text('Submit'), dialog[data-testid='dialog'] button:has-text('Submit'), dialog button:has-text('Submit'), dialog button[aria-label*='Submit'], button[aria-label*='Submit application'], button:has-text('Submit application'), div[role='dialog'] button:has-text('Submit'), .jobs-easy-apply-modal button:has-text('Submit'), .artdeco-modal button:has-text('Submit'), .artdeco-modal__actionbar button:has-text('Submit'), button.artdeco-button--primary:has-text('Submit')",
        "fallback": "dialog button:has-text('Submit'):not(:has-text('Add')), button:has-text('Submit application'):not(:has-text('Add')), button:has-text('Submit'):not(:has-text('Add')), button[type='submit']:has-text('Submit'):not(:has-text('Add'))"
    },
    "error": {
        "primary": ".artdeco-inline-feedback--error, [data-testid='text-input-helper-text'], [aria-invalid='true'], p:has-text('Invalid input'), p:has-text('Please enter a valid'), .jobs-easy-apply-form-element--error",
        "fallback": "[data-test-form-element-error-message], .fb-form-element--error, span:has-text('Invalid input')"
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
        "primary": "div[data-testid='lazy-column'], .jobs-search-results-list, .scaffold-layout__list, .scaffold-layout__list-detail-inner",
        "fallback": ".jobs-search-results__list, ul.scaffold-layout__list-container"
    },
    
    "links": {
        "primary": "div[role='button'][componentkey^='job-card-component-ref-'], div[componentkey^='job-card-component-ref-'], li[data-occludable-job-id], div.job-card-container",
        "fallback": "li.jobs-search-results__list-item, li.scaffold-layout__list-item, div.scaffold-layout__list-item, ul.scaffold-layout__list-container > li"
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
        "primary": ".jobs-search__job-details button.jobs-apply-button, .jobs-search__job-details button[aria-label*='Easy Apply'], button[aria-label='Easy Apply to this job'], button[aria-label*='Easy Apply'], #jobs-apply-button-id, button#jobs-apply-button-id, button.jobs-apply-button, button:has-text('Easy Apply')",
        "fallback": "button[aria-label*='LinkedIn Apply to'], button[aria-label*='Apply to'], button[aria-label*='Continue applying'], button:has-text('Apply')"
    },

    "modal": {
        "primary": "dialog[open], dialog[data-testid='dialog'], dialog, .jobs-easy-apply-modal, .jobs-easy-apply-modal__content, .artdeco-modal__content, div[data-test-easy-apply-form-container], div[data-test-modal], div.artdeco-modal[role='dialog'], div[role='dialog']:has(.jobs-easy-apply-form-section__grouping), div[role='dialog']:has-text('Easy Apply')",
        "fallback": "dialog[open], dialog[data-testid='dialog'], dialog, div.artdeco-modal, div[role='dialog']"
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
        
    if isinstance(locator, dict):
        if use_fallback and "fallback" in locator:
            return locator["fallback"]
        return locator.get("primary", "")
    
    return locator


def has_fallback(key: str) -> bool:
    """Check if a selector has a fallback defined"""
    locator = LOCATORS.get(key)
    return isinstance(locator, dict) and "fallback" in locator
