import re
from bot.utils.logger import logger

class JobIdentity:
    @staticmethod
    def extract_job_id(element):
        """
        Extracts job ID from a job card element (Playwright locator).
        Supports Horizon UI componentkey, currentJobId URL parameter, data attributes, and child anchors.
        """
        try:
            # Method 1: Try componentkey attribute (Horizon UI e.g. "job-card-component-ref-4486830495")
            try:
                ck = element.get_attribute("componentkey", timeout=1000)
                if ck:
                    m = re.search(r'\d{7,14}', ck)
                    if m:
                        return m.group()
            except:
                pass

            # Method 2: Try currentJobId in child links
            try:
                link = element.locator("a[href*='currentJobId=']").first
                if link.count() > 0:
                    href = link.get_attribute("href", timeout=1000)
                    if href:
                        m = re.search(r'currentJobId=(\d+)', href)
                        if m:
                            return m.group(1)
            except:
                pass

            # Method 3: Try data-job-id attribute on element or children
            try:
                job_id = element.get_attribute("data-job-id", timeout=1000)
                if job_id and job_id.isdigit():
                    return job_id
                child = element.locator("[data-job-id]").first
                if child.count() > 0:
                    job_id = child.get_attribute("data-job-id", timeout=1000)
                    if job_id and job_id.isdigit():
                        return job_id
            except:
                pass
            
            # Method 4: Try data-occludable-job-id attribute
            try:
                job_id = element.get_attribute("data-occludable-job-id", timeout=1000)
                if not job_id:
                    child = element.locator("[data-occludable-job-id]").first
                    if child.count() > 0:
                        job_id = child.get_attribute("data-occludable-job-id", timeout=1000)
                if job_id:
                    if ":" in job_id:
                        job_id = job_id.split(":")[-1]
                    if job_id.isdigit():
                        return job_id
            except:
                pass
            
            # Method 5: Try to find job ID in href of child link (/jobs/view/1234567890/)
            try:
                link = element.locator("a[href*='/jobs/view/']").first
                if link.count() > 0:
                    href = link.get_attribute("href", timeout=1000)
                    if href:
                        m = re.search(r'/jobs/view/(\d+)', href)
                        if m:
                            return m.group(1)
            except:
                pass
            
            # Method 6: Try data-entity-urn
            try:
                urn = element.get_attribute("data-entity-urn", timeout=1000)
                if not urn:
                    child = element.locator("[data-entity-urn]").first
                    if child.count() > 0:
                        urn = child.get_attribute("data-entity-urn", timeout=1000)
                if urn and ":" in urn:
                    job_id = urn.split(":")[-1]
                    if job_id.isdigit():
                        return job_id
            except:
                pass

            # Method 7: Search all child anchors for numeric job IDs
            try:
                for a in element.locator("a[href]").all()[:4]:
                    href = a.get_attribute("href") or ""
                    m = re.search(r'(?:jobId|view|currentJobId)[=/](\d{7,12})', href)
                    if m:
                        return m.group(1)
            except:
                pass
            
            logger.debug("Could not extract job ID from element", step="extract_job_id")
            return None
            
        except Exception as e:
            logger.debug(f"Failed to extract job ID: {e}", step="extract_job_id")
            return None

