import logging
import sys
from datetime import datetime

class StructuredFormatter(logging.Formatter):
    def format(self, record):
        timestamp = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')
        level = record.levelname
        
        # Base message parts
        msg_parts = [f"{timestamp}", f"{level:<7}"]
        
        # Context fields
        if hasattr(record, 'job_id') and record.job_id:
            msg_parts.append(f"job_id={record.job_id}")
            
        if hasattr(record, 'step') and record.step:
            msg_parts.append(f"step={record.step}")
            
        if hasattr(record, 'event') and record.event:
            msg_parts.append(f"event={record.event}")
            
        if hasattr(record, 'exception_type') and record.exception_type:
             msg_parts.append(f"exception_type={record.exception_type}")
             
        # Message
        msg_parts.append(f"message={record.getMessage()}")
        
        return " ".join(msg_parts)

class StructuredLogger:
    def __init__(self, name="bot"):
        if sys.platform == "win32":
            try:
                if hasattr(sys.stdout, 'reconfigure'):
                    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        
        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(StructuredFormatter())
            self.logger.addHandler(handler)
            
            # File handler (optional, keeping existing behavior of logging to file)
            # For simplicity matching previous main.py logic could be done here 
            # or in main setup, but let's keep it clean here.
    
    def info(self, message, job_id=None, step=None, event=None, **kwargs):
        extra = {'job_id': job_id, 'step': step, 'event': event}
        extra.update(kwargs)
        self.logger.info(message, extra=extra)
        
    def debug(self, message, job_id=None, step=None, event=None, **kwargs):
        extra = {'job_id': job_id, 'step': step, 'event': event}
        extra.update(kwargs)
        self.logger.debug(message, extra=extra)

    def warning(self, message, job_id=None, step=None, event=None, **kwargs):
        extra = {'job_id': job_id, 'step': step, 'event': event}
        extra.update(kwargs)
        self.logger.warning(message, extra=extra)

    def error(self, message, job_id=None, step=None, event=None, exception=None, **kwargs):
        if exception:
            kwargs['exception_type'] = type(exception).__name__
        extra = {'job_id': job_id, 'step': step, 'event': event}
        extra.update(kwargs)
        self.logger.error(message, extra=extra)

# Singleton instance or factory? Singleton is easier for now.
logger = StructuredLogger()
