"""
Amazon Jobs (UK) new-posting alert bot.

What this does each time it runs:
1. Opens the jobsatamazon.co.uk search page for a given postcode using a real
   (headless) browser, because the site is a JavaScript app with bot protection.
2. Listens in on the background data requests the page makes to load job
   results, and pulls the job listings out of that data.
3. Compares the listings to the ones seen on the previous run (stored in
   seen_jobs.json in this repo).
4. Emails you only the new ones. On the very first run, it just saves a
   baseline (no alert), so you don't get a flood of every existing job.

NOTE: This site does not publish a documented public API, so job extraction
uses a best-effort generic search through whatever JSON the page loads. If
the site changes its structure, this may need small adjustments -- the
debug_last_run.json artifact (saved each run) helps with that.
"""

import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---- Configuration (from environment / GitHub Secrets) --------------------
POSTAL_CODE = os.environ.get("POSTAL_CODE", "EN3 7PZ")
SEARCH_QUERY = os.environ.get("SEARCH_QUERY", "")  # empty = all jobs
SEARCH_URL = (
    "https://www.jobsatamazon.co.uk/app#/jobSearch"
    f"?locale=en-GB&query={SEARCH_QUERY}&postal={POSTAL_CODE.replace(' ', '+')}"
)

GMAIL_SENDER = os.environ.get("GMAIL_SENDER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_RECIPIENT = os.environ.get("ALERT_RECIPIENT")

SEEN_FILE = Path("seen_jobs.json")
DEBUG_FILE = Path("debug_last_run.json")

# Keys that, if present on a JSON object, suggest "this object is a job posting"
TITLE_KEYS = ["jobTitle", "title", "job_title", "name"]
ID_KEYS = ["jobId", "id", "job_id", "requisitionId"]
LOCATION_KEYS = ["city", "location", "locationName", "address"]


def collect_job_like_objects(data, found):
    """Recursively walk arbitrary JSON and pull out objects that look like job postings."""
    if isinstance(data, dict):
        title = next((data[k] for k in TITLE_KEYS if k in data and isinstance(data[k], str)), None)
        if title:
            job_id = next((str(data[k]) for k in ID_KEYS if k in data), title)
            location = next((data[k] for k in LOCATION_KEYS if k in data and isinstance(data[k], str)), "")
            found[job_id] = {"title": title, "location": location}
        for v in data.values():
            collect_job_like_objects(v, found)
    elif isinstance(data, list):
        for item in data:
            collect_job_like_objects(item, found)


def fetch_current_jobs():
    captured_payloads = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )

        def handle_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype:
                    body = response.json()
                    captured_payloads.append(body)
            except Exception:
                pass  # non-JSON or unreadable response, ignore

        page.on("response", handle_response)

        page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)  # give the SPA extra time to finish loading results

        browser.close()

    found = {}
    for payload in captured_payloads:
        collect_job_like_objects(payload, found)

    # Save everything captured this run for troubleshooting
    DEBUG_FILE.write_text(json.dumps(captured_payloads, indent=2)[:2_000_000])

    return found


def send_email(subject, body):
    if not (GMAIL_SENDER and GMAIL_APP_PASSWORD and ALERT_RECIPIENT):
        print("Email not configured (missing secrets) -- skipping send.")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_SENDER
    msg["To"] = ALERT_RECIPIENT

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_SENDER, [ALERT_RECIPIENT], msg.as_string())


def main():
    current_jobs = fetch_current_jobs()
    print(f"Found {len(current_jobs)} job-like listings this run.")

    if not SEEN_FILE.exists():
        # First ever run: just save the baseline, don't alert on everything at once
        SEEN_FILE.write_text(json.dumps(current_jobs, indent=2))
        send_email(
            "Amazon Job Alert Bot - Setup Complete",
            f"Setup complete. Now tracking {len(current_jobs)} current listings "
            f"for postcode {POSTAL_CODE}. You'll be emailed when new ones appear.",
        )
        print("First run -- baseline saved, no alert sent for existing jobs.")
        return

    previous_jobs = json.loads(SEEN_FILE.read_text())
    new_ids = set(current_jobs) - set(previous_jobs)

    if new_ids:
        lines = [f"- {current_jobs[jid]['title']} ({current_jobs[jid]['location']})" for jid in new_ids]
        body = "New Amazon job postings found:\n\n" + "\n".join(lines)
        body += f"\n\nSearch page: {SEARCH_URL}"
        send_email(f"{len(new_ids)} New Amazon Job Posting(s)", body)
        print(f"Sent alert for {len(new_ids)} new posting(s).")
    else:
        print("No new postings this run.")

    SEEN_FILE.write_text(json.dumps(current_jobs, indent=2))


if __name__ == "__main__":
    main()
