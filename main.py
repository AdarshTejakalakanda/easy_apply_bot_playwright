import yaml
import logging
import time
from bot.utils.logger import logger

from bot.core.browser import Browser
from bot.core.session import Session
from bot.application.workflow import Workflow
from bot.discovery.search import Search
from bot.core.execution_guard import ExecutionGuard
from bot.core.dry_run import DryRun
from bot.core.metrics import Metrics
from bot.core.proxy_manager import ProxyConfig, ProxyRotator, load_advanced_proxy_config
import atexit
from dotenv import load_dotenv
import os



load_dotenv()


def load_candidates():
    """Load candidate profiles from candidates.yaml"""
    try:
        with open("config/candidates.yaml", 'r') as f:
            data = yaml.safe_load(f)
        return data.get('candidates', [])
    except FileNotFoundError:
        logger.warning("candidates.yaml not found, falling back to config.yaml", step="init")
        return None


def select_candidate(candidates):
    """Let user select which candidate profile to use"""
    enabled_candidates = [c for c in candidates if c.get('enabled', False)]
    
    if not enabled_candidates:
        raise Exception("No enabled candidates found in candidates.yaml")
    
    if len(enabled_candidates) == 1:
        selected = enabled_candidates[0]
        logger.info(f"Auto-selected only enabled candidate: {selected['name']}", step="init")
        return selected
    
    print("\n" + "=" * 70)
    print("SELECT CANDIDATE PROFILE")
    print("=" * 70)
    for i, candidate in enumerate(enabled_candidates):
        print(f"{i+1}. {candidate['name']} ({candidate['id']})")
    print("=" * 70)
    
    while True:
        try:
            choice = input("Enter number: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(enabled_candidates):
                selected = enabled_candidates[idx]
                print(f"✅ Selected: {selected['name']}\n")
                return selected
            else:
                print("Invalid choice. Try again.")
        except ValueError:
            print("Please enter a number.")
        except KeyboardInterrupt:
            raise Exception("User cancelled profile selection")


def save_profile_metadata(profile_path, email):
    """Save metadata to profile folder to track which email it belongs to"""
    import json
    import os
    
    os.makedirs(profile_path, exist_ok=True)
    metadata_file = os.path.join(profile_path, 'profile_metadata.json')
    
    metadata = {
        'email': email,
        'last_updated': str(logger.info.__self__.__class__.__name__ if hasattr(logger, 'info') else 'unknown')
    }
    
    try:
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save profile metadata: {e}", step="save_metadata")


def verify_profile_metadata(profile_path, expected_email):
    """
    Verify profile metadata matches expected email
    Non-intrusive - doesn't navigate to LinkedIn
    """
    import json
    import os
    
    metadata_file = os.path.join(profile_path, 'profile_metadata.json')
    
    # If metadata file doesn't exist, this is first run - create it
    if not os.path.exists(metadata_file):
        logger.info(f"First run for this profile, creating metadata", step="verify_profile")
        save_profile_metadata(profile_path, expected_email)
        print(f"\n✅ New profile created for: {expected_email}")
        return True
    
    # Load and verify metadata
    try:
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
        
        saved_email = metadata.get('email', '').lower()
        expected_email_lower = expected_email.lower()
        
        if saved_email == expected_email_lower:
            logger.info(f"✅ Profile metadata verified: {saved_email}", step="verify_profile")
            print(f"\n✅ Using saved profile for: {saved_email}")
            return True
        else:
            # MISMATCH!
            print("\n" + "=" * 70)
            print("⛔ PROFILE MISMATCH DETECTED!")
            print("=" * 70)
            print(f"This browser profile belongs to: {saved_email}")
            print(f"But you selected candidate:      {expected_email_lower}")
            print("=" * 70)
            print("The profile folder contains session data for a different account!")
            print(f"Profile path: {profile_path}")
            print("=" * 70)
            print("Options:")
            print("  1. Select the correct candidate matching this profile")
            print(f"  2. Delete the profile folder to start fresh")
            print("=" * 70)
            raise Exception(f"Profile mismatch: profile has {saved_email}, selected {expected_email_lower}")
    
    except json.JSONDecodeError:
        logger.warning("Corrupted metadata file, recreating", step="verify_profile")
        save_profile_metadata(profile_path, expected_email)
        return True
    except Exception as e:
        if "mismatch" in str(e).lower():
            raise
        logger.warning(f"Metadata verification warning: {e}", step="verify_profile")
        return True


if __name__ == '__main__':
    # Load candidates from new YAML structure
    candidates = load_candidates()
    
    if candidates:
        # NEW FLOW: Use candidates.yaml
        logger.info("Using candidates.yaml configuration", step="init")
        
        # Let user select candidate
        selected_candidate = select_candidate(candidates)
        candidate_id = selected_candidate['id']
        candidate_name = selected_candidate['name']
        
        logger.info(f"Starting bot for: {candidate_name}", step="init", candidate_id=candidate_id)
        
        # Extract config from candidate profile
        credentials = selected_candidate.get('credentials', {})
        username = os.getenv(f"{candidate_id.upper()}_USERNAME", credentials.get('email'))
        password = os.getenv(f"{candidate_id.upper()}_PASSWORD", credentials.get('password'))
        
        # Fallback to generic env vars
        if not password:
            password = os.getenv('LINKEDIN_PASSWORD')
        
        phone_number = credentials.get('phone')
        
        search_config = selected_candidate.get('search', {})
        positions = search_config.get('positions', [])
        locations = search_config.get('locations', [])
        experience_level = search_config.get('experience_level', [])
        
        preferences = selected_candidate.get('preferences', {})
        uploads = selected_candidate.get('uploads', {})
        blacklist = preferences.get('blacklist', [])
        blacklist_titles = preferences.get('blacklist_titles', [])
        
        max_apps = preferences.get('max_applications_per_run', 50)
        cooldown = preferences.get('cooldown_seconds', 5)
        is_dry_run = preferences.get('dry_run', False)
        
        # Proxy Setup (Advanced)
        proxy_config = None
        # First check candidate-specific proxy pool
        if 'proxy' in selected_candidate:
            rotator = ProxyRotator(selected_candidate['proxy'])
            proxy_config = rotator.get_proxy(candidate_id=candidate_id)
        
        # If no candidate proxy, check global proxy pool (if defined at root of YAML)
        if not proxy_config:
            with open("config/candidates.yaml", 'r') as f:
                root_data = yaml.safe_load(f)
                proxy_rotator = load_advanced_proxy_config(root_data)
                if proxy_rotator:
                    proxy_config = proxy_rotator.get_proxy(candidate_id=candidate_id)
        
        # Auto-assign profile path if empty (each candidate gets their own)
        profile_path = selected_candidate.get('profile_path', '')
        if not profile_path:
            profile_path = f'./profiles/{candidate_id}'
            logger.info(f"Using auto-generated profile path: {profile_path}", step="init")
        
    else:
        # FALLBACK: Use old config.yaml
        logger.warning("Falling back to config.yaml (legacy mode)", step="init")
        
        with open("config.yaml", 'r') as stream:
            parameters = yaml.safe_load(stream)
        
        selected_candidate = None  # No profile
        username = os.getenv('LINKEDIN_USERNAME', parameters.get('username'))
        password = os.getenv('LINKEDIN_PASSWORD', parameters.get('password'))
        phone_number = os.getenv('PHONE_NUMBER', parameters.get('phone_number'))
        
        positions = parameters['positions']
        locations = parameters['locations']
        experience_level = parameters.get('experience_level', [])
        
        uploads = parameters.get('uploads', {})
        blacklist = parameters.get('blacklist', [])
        blacklist_titles = parameters.get('blackListTitles', [])
        
        execution_config = parameters.get('execution', {})
        max_apps = execution_config.get('max_applications_per_run', 10)
        cooldown = execution_config.get('cooldown_seconds', 90)
        is_dry_run = execution_config.get('dry_run', True)
        
        profile_path = parameters.get('profile_path', '')
        
        # Proxy Setup (Legacy)
        proxy_rotator = load_advanced_proxy_config(parameters)
        proxy_config = proxy_rotator.get_proxy() if proxy_rotator else None

    # Validate
    assert len(positions) > 0, "At least one position must be specified"
    assert len(locations) > 0, "At least one location must be specified"
    assert username is not None, "Username/Email must be provided"
    assert password is not None, "Password must be provided"
    assert phone_number is not None, "Phone number must be provided"

    logger.info("Starting Easy Apply Bot (Playwright Version)", step="init")
    
    execution_guard = ExecutionGuard(max_apps, cooldown)
    dry_run = DryRun(is_dry_run)
    metrics = Metrics()
    
    # Register summary on exit
    atexit.register(metrics.print_summary)

    # Initialize Core Components
    browser = Browser(
        profile_path=profile_path if profile_path else None,
        proxy_config=proxy_config,
        headless=False # Keep visible for now
    )
    page = browser.get_page()
    
    # Click center of screen after browser opens (dismiss popups/activate page)
    logger.info("Waiting 4 seconds before clicking center screen...", step="init")
    time.sleep(4)
    viewport_size = page.viewport_size
    center_x = viewport_size['width'] // 2
    center_y = viewport_size['height'] // 2
    page.mouse.click(center_x, center_y)
    logger.info(f"Clicked center of screen at ({center_x}, {center_y})", step="init")
    
    # Login
    session = Session(page)
    session.login(username, password)
    
    # Verify profile metadata if using candidate profile
    if selected_candidate:
        expected_email = selected_candidate.get('credentials', {}).get('email')
        if expected_email and profile_path:
            verify_profile_metadata(profile_path, expected_email)

    # Application & Search Components
    workflow = Workflow(
        page, 
        uploads, 
        blacklist_titles, 
        execution_guard=execution_guard, 
        dry_run=dry_run, 
        metrics=metrics,
        candidate_profile=selected_candidate  # Pass the profile!
    )
    
    search = Search(page, workflow, blacklist, experience_level, phone_number)
    
    locations = [l for l in locations if l is not None]
    positions = [p for p in positions if p is not None]

    try:
        search.start_apply(positions, locations)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user", step="shutdown", event="keyboard_interrupt")
    except Exception as e:
        logger.error(f"Bot encountered an error: {e}", step="shutdown", event="error", exception=e)
    finally:
        logger.info("Closing browser...", step="shutdown", event="cleanup")
        
        # Save learned answers back to YAML if using candidate profile
        if selected_candidate and hasattr(workflow.form_filler, 'learned_answers'):
            learned = workflow.form_filler.learned_answers
            if learned:
                logger.info(f"Saving {len(learned)} learned answers to profile", step="shutdown")
                try:
                    import yaml
                    with open("config/candidates.yaml", 'r') as f:
                        data = yaml.safe_load(f)
                    
                    # Update the specific candidate's learned_answers
                    for candidate in data.get('candidates', []):
                        if candidate['id'] == selected_candidate['id']:
                            if 'profile_data' not in candidate:
                                candidate['profile_data'] = {}
                            if 'learned_answers' not in candidate['profile_data']:
                                candidate['profile_data']['learned_answers'] = {}
                            
                            # Merge new answers
                            candidate['profile_data']['learned_answers'].update(learned)
                            break
                    
                    with open("config/candidates.yaml", 'w') as f:
                        yaml.dump(data, f, sort_keys=False, default_flow_style=False)
                    logger.info("✅ config/candidates.yaml updated with new learned answers", step="shutdown")
                except Exception as e:
                    logger.warning(f"Failed to save learned answers to YAML: {e}", step="shutdown")
        
        # --- WBL API Job Activity Logging ---
        wbl_candidate_id = selected_candidate.get('wbl_candidate_id') if selected_candidate else None
        if wbl_candidate_id:
            logger.info("Sending job activity log to Whitebox Learning API...", step="wbl_api")
            notes = (
                f"Automated bot run completed. "
                f"Attempted: {metrics.attempted}, "
                f"Submitted (Applied): {metrics.submitted}, "
                f"Skipped: {metrics.skipped}, "
                f"Failed: {metrics.failed}."
            )
            from bot.utils.wbl_api import send_job_activity_log
            send_job_activity_log(
                candidate_id=wbl_candidate_id,
                notes=notes,
                activity_count=metrics.submitted,
                job_id=146
            )
        else:
            logger.warning("No 'wbl_candidate_id' defined in candidate profile. Skipping WBL log.", step="wbl_api")
            
        browser.close()
        logger.info("Bot shutdown complete", step="shutdown", event="complete")
