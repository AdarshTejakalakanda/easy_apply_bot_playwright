import re
from bot.utils.logger import logger

class JobIdentity:
    @staticmethod
    def extract_job_id(element):
        """
        Extracts job ID from a job card element (Playwright locator).
        Tries multiple methods supporting classic and modern Horizon UI.
        """
        try:
            # Method 0: Modern Horizon UI componentkey (e.g., job-card-component-ref-4486845045)
            try:
                comp_key = element.get_attribute("componentkey", timeout=1000)
                if comp_key:
                    m = re.search(r"job-card-component-ref-(\d+)", comp_key)
                    if m:
                        return m.group(1)
            except:
                pass

            # Method 1: Try data-job-id attribute
            try:
                job_id = element.get_attribute("data-job-id", timeout=1000)
                if job_id and str(job_id).isdigit():
                    return str(job_id)
            except:
                pass
            
            # Method 2: Try data-occludable-job-id attribute
            try:
                job_id = element.get_attribute("data-occludable-job-id", timeout=1000)
                if job_id:
                    if ":" in job_id:
                        job_id = job_id.split(":")[-1]
                    if str(job_id).isdigit():
                        return str(job_id)
            except:
                pass
            
            # Method 3: Try to find job ID in child links (href with /jobs/view/ or currentJobId=)
            try:
                links = element.locator("a[href*='/jobs/view/'], a[href*='currentJobId='], a").all()
                for link in links[:3]:
                    href = link.get_attribute("href", timeout=1000)
                    if not href:
                        continue
                    if "/jobs/view/" in href:
                        parts = href.split("/jobs/view/")
                        if len(parts) > 1:
                            jid = parts[1].split("/")[0].split("?")[0]
                            if jid.isdigit():
                                return jid
                    m = re.search(r"currentJobId=(\d+)", href)
                    if m:
                        return m.group(1)
            except:
                pass
            
            # Method 4: Try data-entity-urn
            try:
                urn = element.get_attribute("data-entity-urn", timeout=1000)
                if urn and ":" in urn:
                    job_id = urn.split(":")[-1]
                    if job_id.isdigit():
                        return job_id
            except:
                pass

            # Method 5: Check nested element componentkey
            try:
                nested = element.locator("[componentkey^='job-card-component-ref-']").first
                if nested.count() > 0:
                    ck = nested.get_attribute("componentkey", timeout=1000)
                    m = re.search(r"job-card-component-ref-(\d+)", ck or "")
                    if m:
                        return m.group(1)
            except:
                pass
            
            logger.debug("Could not extract job ID from element", step="extract_job_id")
            return None
            
        except Exception as e:
            logger.debug(f"Failed to extract job ID: {e}", step="extract_job_id")
            return None
