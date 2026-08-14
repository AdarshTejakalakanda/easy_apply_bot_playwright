import duckdb
import pandas as pd
from datetime import datetime, timedelta
import logging
from pathlib import Path
import os
import re

log = logging.getLogger(__name__)

class Store:
    def __init__(self, db_file='data/bot_data.duckdb'):
        self.db_file = db_file
        # Ensure data directory exists
        Path(self.db_file).parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database schema and migrate (one-time setup)
        self._init_db()
        self._migrate_legacy_data()
    
    def _get_connection(self):
        """Get a fresh database connection for each operation"""
        return duckdb.connect(self.db_file)

    def _init_db(self):
        """Initialize database schema (if not exists)"""
        con = self._get_connection()
        try:
            # Main applications table with comprehensive tracking
            con.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    -- Core identification
                    job_id VARCHAR PRIMARY KEY,
                    job_title VARCHAR,
                    company VARCHAR,
                    location VARCHAR,
                    work_type VARCHAR,  -- Remote, Hybrid, On-site
                    
                    -- Timestamps
                    started_at TIMESTAMP,
                    user_confirmed_at TIMESTAMP,  -- When user clicked "Submit Now"
                    submitted_at TIMESTAMP,        -- When LinkedIn confirmed submission
                    
                    -- Status tracking
                    status VARCHAR,  -- 'user_confirmed', 'submitted', 'failed', 'skipped', 'error'
                    attempted BOOLEAN DEFAULT FALSE,
                    success BOOLEAN DEFAULT FALSE,
                    
                    -- User interaction
                    user_submitted BOOLEAN DEFAULT FALSE,  -- Did user click "Submit Now"?
                    user_skipped BOOLEAN DEFAULT FALSE,     -- Did user click "Skip Job"?
                    
                    -- Form details
                    form_pages INTEGER DEFAULT 0,
                    fields_filled INTEGER DEFAULT 0,
                    errors_encountered INTEGER DEFAULT 0,
                    error_message VARCHAR,
                    
                    -- Timing
                    duration_seconds REAL,  -- Total time from start to submission
                    
                    -- Context
                    candidate_id VARCHAR DEFAULT 'default',
                    proxy_used VARCHAR DEFAULT NULL,
                    run_id VARCHAR,
                    
                    -- Metadata
                    job_url VARCHAR,
                    salary_range VARCHAR,
                    seniority_level VARCHAR,
                    employment_type VARCHAR,  -- Full-time, Part-time, Contract
                    
                    -- Extras
                    notes VARCHAR
                )
            """)
            
            con.execute("""
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id VARCHAR PRIMARY KEY,
                    name VARCHAR,
                    email VARCHAR,
                    phone VARCHAR,
                    linkedin_profile VARCHAR,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_applications INTEGER DEFAULT 0,
                    total_user_confirmations INTEGER DEFAULT 0,
                    total_successful_submissions INTEGER DEFAULT 0
                )
            """)
            
            con.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id VARCHAR PRIMARY KEY,
                    candidate_id VARCHAR,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    
                    -- Detailed metrics
                    jobs_viewed INTEGER DEFAULT 0,
                    applications_attempted INTEGER DEFAULT 0,
                    user_confirmations INTEGER DEFAULT 0,  -- Count of "Submit Now" clicks
                    applications_submitted INTEGER DEFAULT 0,  -- Actual LinkedIn submissions
                    applications_failed INTEGER DEFAULT 0,
                    applications_skipped INTEGER DEFAULT 0,
                    
                    -- Context
                    proxy_used VARCHAR,
                    system_id VARCHAR DEFAULT 'local',
                    
                    -- Session info
                    search_keywords VARCHAR,
                    location_filter VARCHAR,
                    notes VARCHAR
                )
            """)
            
            con.execute("""
                CREATE TABLE IF NOT EXISTS qa (
                    question VARCHAR UNIQUE,
                    answer VARCHAR,
                    candidate_id VARCHAR DEFAULT 'default',
                    times_used INTEGER DEFAULT 0,
                    last_used_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create sequence for autoincrement if not exists
            con.execute("CREATE SEQUENCE IF NOT EXISTS seq_submission_events_id")
            
            # Ensure table has default nextval by checking constraint behavior
            try:
                con.execute("INSERT INTO submission_events (job_id, event_type, details) VALUES ('test_id', 'test_type', 'test_details')")
                con.execute("DELETE FROM submission_events WHERE job_id = 'test_id'")
            except Exception as test_err:
                if "constraint" in str(test_err).lower() or "not null" in str(test_err).lower():
                    log.info("Migrating submission_events table to support autoincrement...")
                    con.execute("DROP TABLE IF EXISTS submission_events")
            
            # Create submission events table for granular tracking
            con.execute("""
                CREATE TABLE IF NOT EXISTS submission_events (
                    event_id INTEGER DEFAULT nextval('seq_submission_events_id') PRIMARY KEY,
                    job_id VARCHAR,
                    event_type VARCHAR,  -- 'user_confirmed', 'submit_clicked', 'success', 'error'
                    event_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    details VARCHAR,
                    candidate_id VARCHAR DEFAULT 'default'
                )
            """)
            
        finally:
            con.close()

    def _migrate_legacy_data(self):
        # Migrate CSVs if they exist and haven't been migrated
        # We check if tables are empty to decide (simplification, but safe for first run)
        
        con = self._get_connection()
        try:
            # QA Migration
            count_qa = con.execute("SELECT count(*) FROM qa").fetchone()[0]
            qa_csv = Path("data/qa.csv")
            if count_qa == 0 and qa_csv.exists():
                log.info("Migrating QA CSV to DuckDB...")
                try:
                    # DuckDB can read CSV directly. 
                    # Handling potential schema mismatch robustly:
                    con.execute(f"INSERT OR IGNORE INTO qa SELECT Question, Answer FROM read_csv_auto('{qa_csv}')")
                    qa_csv.rename("data/qa.csv.bak") # Rename after successful migration
                except Exception as e:
                    log.warning(f"QA migration failed: {e}")

            # Applications Migration
            count_apps = con.execute("SELECT count(*) FROM applications").fetchone()[0]
            out_csv = Path("data/out.csv")
            if count_apps == 0 and out_csv.exists():
                log.info("Migrating Applications CSV to DuckDB...")
                try:
                    # The CSV had no headers usually, or we need to be careful.
                    # Previous code read it with names=['timestamp', 'jobID', 'job', 'company', 'attempted', 'result']
                    # read_csv_auto might infer headers if they exist or columns.
                    # Let's specify columns to be safe if it was headerless. 
                    # Actually legacy writer didn't write headers if file existed, but might have created them?
                    # The old code: df.read_csv(header=None) implies no headers.
                    
                    con.execute(f"""
                        INSERT INTO applications (timestamp, job_id, job, company, attempted, result)
                        SELECT column0, column1, column2, column3, column4, column5 
                        FROM read_csv('{out_csv}', header=False, columns={{'column0': 'TIMESTAMP', 'column1': 'VARCHAR', 'column2': 'VARCHAR', 'column3': 'VARCHAR', 'column4': 'BOOLEAN', 'column5': 'BOOLEAN'}})
                    """)
                    out_csv.rename("data/out.csv.bak")
                except Exception as e:
                    log.warning(f"Applications migration failed: {e}")
        finally:
            con.close()



    def get_appliedIDs(self) -> list | None:
        con = self._get_connection()
        try:
            # Get successful applications from last 2 days? Or attempts? 
            # Original code: df = df[df['timestamp'] > (datetime.now() - timedelta(days=2))]
            # jobIDs = list(df.jobID)
            
            two_days_ago = datetime.now() - timedelta(days=2)
            results = con.execute("SELECT job_id FROM applications WHERE timestamp > ?", [two_days_ago]).fetchall()
            jobIDs = [row[0] for row in results]
            log.info(f"{len(jobIDs)} jobIDs found (last 48h)")
            return jobIDs
        except Exception as e:
            log.error(f"Failed to fetch jobIDs: {e}")
            return []
        finally:
            con.close()

    
    def start_application(self, job_id, job_title, company, candidate_id='default', job_url=None, location=None):
        """Record when an application starts"""
        con = self._get_connection()
        try:
            timestamp = datetime.now()
            # Use INSERT OR REPLACE to handle retries/re-attempts
            con.execute("""
                INSERT OR REPLACE INTO applications 
                (job_id, job_title, company, location, started_at, status, attempted, candidate_id, job_url)
                VALUES (?, ?, ?, ?, ?, 'started', TRUE, ?, ?)
            """, [job_id, job_title, company, location, timestamp, candidate_id, job_url])
            
            # Log event
            self.log_submission_event(job_id, 'application_started', f'Started application for {job_title} at {company}', candidate_id)
            
        except Exception as e:
            log.error(f"Failed to record application start: {e}")
        finally:
            con.close()
    
    def record_user_confirmation(self, job_id, candidate_id='default'):
        """Record when user clicks 'Submit Now' button"""
        con = self._get_connection()
        try:
            timestamp = datetime.now()
            con.execute("""
                UPDATE applications 
                SET user_confirmed_at = ?,
                    user_submitted = TRUE,
                    status = 'user_confirmed'
                WHERE job_id = ?
            """, [timestamp, job_id])
            
            # Log event
            self.log_submission_event(job_id, 'user_confirmed', 'User clicked Submit Now button', candidate_id)
            log.info(f"📊 Recorded user confirmation for job {job_id}")
            
        except Exception as e:
            log.error(f"Failed to record user confirmation: {e}")
        finally:
            con.close()
    
    def record_user_skip(self, job_id, candidate_id='default'):
        """Record when user clicks 'Skip Job' button"""
        con = self._get_connection()
        try:
            timestamp = datetime.now()
            con.execute("""
                UPDATE applications 
                SET user_skipped = TRUE,
                    status = 'skipped'
                WHERE job_id = ?
            """, [timestamp, job_id])
            
            # Log event
            self.log_submission_event(job_id, 'user_skipped', 'User clicked Skip Job button', candidate_id)
            log.info(f"📊 Recorded user skip for job {job_id}")
            
        except Exception as e:
            log.error(f"Failed to record user skip: {e}")
        finally:
            con.close()
    
    def record_submission_success(self, job_id, candidate_id='default', duration_seconds=None, form_pages=0, fields_filled=0):
        """Record successful LinkedIn submission"""
        con = self._get_connection()
        try:
            timestamp = datetime.now()
            con.execute("""
                UPDATE applications 
                SET submitted_at = ?,
                    success = TRUE,
                    status = 'submitted',
                    duration_seconds = ?,
                    form_pages = ?,
                    fields_filled = ?
                WHERE job_id = ?
            """, [timestamp, duration_seconds, form_pages, fields_filled, job_id])
            
            # Log event
            self.log_submission_event(job_id, 'submission_success', f'Application successfully submitted (duration: {duration_seconds}s)', candidate_id)
            log.info(f"✅ Recorded successful submission for job {job_id}")
            
        except Exception as e:
            log.error(f"Failed to record submission success: {e}")
        finally:
            con.close()
    
    def record_submission_failure(self, job_id, error_message, candidate_id='default', errors_encountered=0):
        """Record failed submission attempt"""
        con = self._get_connection()
        try:
            con.execute("""
                UPDATE applications 
                SET success = FALSE,
                    status = 'failed',
                    error_message = ?,
                    errors_encountered = ?
                WHERE job_id = ?
            """, [error_message, errors_encountered, job_id])
            
            # Log event
            self.log_submission_event(job_id, 'submission_failed', f'Error: {error_message}', candidate_id)
            log.info(f"❌ Recorded submission failure for job {job_id}")
            
        except Exception as e:
            log.error(f"Failed to record submission failure: {e}")
        finally:
            con.close()
    
    def log_submission_event(self, job_id, event_type, details, candidate_id='default'):
        """Log granular submission events for debugging and analytics"""
        con = self._get_connection()
        try:
            con.execute("""
                INSERT INTO submission_events (job_id, event_type, details, candidate_id)
                VALUES (?, ?, ?, ?)
            """, [job_id, event_type, details, candidate_id])
        except Exception as e:
            log.error(f"Failed to log submission event: {e}")
        finally:
            con.close()
    
    def write_to_file(self, button, jobID, browserTitle, result, candidate_id='default', proxy_used=None):
        """Legacy method for backward compatibility - maps to new schema"""
        def re_extract(text, pattern):
            target = re.search(pattern, text)
            if target:
                target = target.group(1)
            return target
            
        timestamp = datetime.now()
        attempted = True if button else False
        
        job = re_extract(browserTitle.split(' | ')[0], r"\(?\d?\)?\s?(\w.*)")
        company = re_extract(browserTitle.split(' | ')[1], r"(\w.*)")
        
        # Use new schema structure
        con = self._get_connection()
        try:
            # Check if application already exists
            existing = con.execute("SELECT job_id FROM applications WHERE job_id = ?", [jobID]).fetchone()
            
            if existing:
                # Update existing record
                con.execute("""
                    UPDATE applications 
                    SET success = ?,
                        status = ?,
                        proxy_used = ?
                    WHERE job_id = ?
                """, [result, 'submitted' if result else 'failed', proxy_used, jobID])
            else:
                # Insert new record (legacy path)
                con.execute("""
                    INSERT INTO applications 
                    (job_id, job_title, company, started_at, attempted, success, status, candidate_id, proxy_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [jobID, job, company, timestamp, attempted, result, 'submitted' if result else 'failed', candidate_id, proxy_used])
                
        except Exception as e:
            log.error(f"Failed to write application to DB: {e}")
        finally:
            con.close()


    def save_answer(self, question, answer):
        con = self._get_connection()
        try:
            con.execute("INSERT OR REPLACE INTO qa VALUES (?, ?)", [question, answer])
            log.info(f"Saved answer for: '{question}'")
        except Exception as e:
             log.error(f"Failed to save QA: {e}")
        finally:
            con.close()

    def get_answer(self, question):
        con = self._get_connection()
        try:
            res = con.execute("SELECT answer FROM qa WHERE question = ?", [question]).fetchone()
            return res[0] if res else None
        except Exception as e:
            log.error(f"Failed to get answer: {e}")
            return None
        finally:
            con.close()
