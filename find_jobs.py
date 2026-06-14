#!/usr/bin/env python3
"""
Remote React Native / Mobile job finder.
Pulls from several stable public job APIs, filters by keywords,
de-duplicates against previously seen jobs, and emails any new matches.
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# A job must contain at least one of these (case-insensitive) in its title or
# tags/description to be considered a match.
MUST_MATCH = [
    "react native",
    "react-native",
    "mobile developer",
    "mobile engineer",
    "ios developer",
    "android developer",
    "expo",
]

# If any of these appear we treat it as a strong mobile signal even alone.
STRONG_SIGNALS = ["react native", "react-native", "expo"]

# Jobs containing these are skipped (avoid senior-only mismatch is optional —
# leave empty if you want everything). Example: ["principal", "staff"]
EXCLUDE = []

SEEN_FILE = Path(__file__).parent / "seen_jobs.json"
USER_AGENT = "Mozilla/5.0 (job-finder; +https://github.com)"
REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def fetch(url, is_json=True):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if is_json else raw
    except (URLError, HTTPError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  ! failed to fetch {url}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Source adapters — each returns a list of normalized job dicts:
#   {id, title, company, url, location, source, tags}
# ---------------------------------------------------------------------------

def from_remoteok():
    data = fetch("https://remoteok.com/api")
    if not data or not isinstance(data, list):
        return []
    jobs = []
    for item in data:
        # first element is metadata/legal notice
        if not isinstance(item, dict) or "id" not in item:
            continue
        jobs.append({
            "id": f"remoteok-{item.get('id')}",
            "title": item.get("position", "") or item.get("title", ""),
            "company": item.get("company", ""),
            "url": item.get("url", ""),
            "location": item.get("location", "") or "Remote",
            "source": "RemoteOK",
            "tags": " ".join(item.get("tags", []) or []),
        })
    return jobs


def from_remotive():
    data = fetch("https://remotive.com/api/remote-jobs?category=software-dev")
    if not data or "jobs" not in data:
        return []
    jobs = []
    for item in data["jobs"]:
        jobs.append({
            "id": f"remotive-{item.get('id')}",
            "title": item.get("title", ""),
            "company": item.get("company_name", ""),
            "url": item.get("url", ""),
            "location": item.get("candidate_required_location", "") or "Remote",
            "source": "Remotive",
            "tags": " ".join(item.get("tags", []) or []),
        })
    return jobs


def from_arbeitnow():
    data = fetch("https://www.arbeitnow.com/api/job-board-api")
    if not data or "data" not in data:
        return []
    jobs = []
    for item in data["data"]:
        jobs.append({
            "id": f"arbeitnow-{item.get('slug')}",
            "title": item.get("title", ""),
            "company": item.get("company_name", ""),
            "url": item.get("url", ""),
            "location": item.get("location", "") or "Remote",
            "source": "Arbeitnow",
            "tags": " ".join(item.get("tags", []) or []),
        })
    return jobs


def from_himalayas():
    data = fetch("https://himalayas.app/jobs/api?limit=100")
    if not data or "jobs" not in data:
        return []
    jobs = []
    for item in data["jobs"]:
        locs = item.get("locationRestrictions") or []
        jobs.append({
            "id": f"himalayas-{item.get('guid', item.get('title',''))}",
            "title": item.get("title", ""),
            "company": item.get("companyName", ""),
            "url": item.get("applicationLink", "") or item.get("guid", ""),
            "location": ", ".join(locs) if locs else "Remote",
            "source": "Himalayas",
            "tags": " ".join(item.get("categories", []) or []),
        })
    return jobs


def from_weworkremotely():
    # We Work Remotely exposes RSS feeds per category (no JSON API).
    # The programming feed covers dev roles; we filter for mobile downstream.
    feed_url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
    raw = fetch(feed_url, is_json=False)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ! WWR feed parse error: {e}", file=sys.stderr)
        return []

    jobs = []
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        guid_el = item.find("guid")
        desc_el = item.find("description")
        region_el = item.find("region")  # WWR custom tag, may be absent

        raw_title = title_el.text if title_el is not None and title_el.text else ""
        # WWR titles look like "Company Name: Job Title"
        if ":" in raw_title:
            company, _, title = raw_title.partition(":")
            company, title = company.strip(), title.strip()
        else:
            company, title = "", raw_title.strip()

        link = link_el.text if link_el is not None and link_el.text else ""
        guid = guid_el.text if guid_el is not None and guid_el.text else link
        desc = desc_el.text if desc_el is not None and desc_el.text else ""
        # strip HTML tags from description for cleaner keyword matching
        desc = re.sub(r"<[^>]+>", " ", desc)
        region = region_el.text if region_el is not None and region_el.text else "Remote"

        jobs.append({
            "id": f"wwr-{guid}",
            "title": title,
            "company": company,
            "url": link,
            "location": region or "Remote",
            "source": "WeWorkRemotely",
            "tags": desc[:500],
        })
    return jobs


SOURCES = [from_remoteok, from_remotive, from_arbeitnow, from_himalayas, from_weworkremotely]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def matches(job):
    haystack = f"{job['title']} {job['tags']}".lower()

    for bad in EXCLUDE:
        if bad.lower() in haystack:
            return False

    # strong signal anywhere = match
    for sig in STRONG_SIGNALS:
        if sig in haystack:
            return True

    # otherwise require a mobile keyword in the TITLE specifically,
    # so we don't catch "we also use react native somewhere" noise
    title = job["title"].lower()
    for kw in MUST_MATCH:
        if kw in title:
            return True
    return False


# ---------------------------------------------------------------------------
# Seen-state persistence
# ---------------------------------------------------------------------------

def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen):
    # keep it from growing forever: cap at most recent 2000 ids
    SEEN_FILE.write_text(json.dumps(list(seen)[-2000:]))


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email_html(new_jobs):
    rows = []
    for j in new_jobs:
        rows.append(f"""
          <tr>
            <td style="padding:10px 12px;border-bottom:1px solid #eee;">
              <a href="{j['url']}" style="color:#0B5260;font-weight:600;text-decoration:none;font-size:15px;">{j['title']}</a><br>
              <span style="color:#555;font-size:13px;">{j['company']}</span>
              <span style="color:#999;font-size:12px;"> · {j['location']}</span>
              <span style="color:#0CA3C1;font-size:11px;"> · {j['source']}</span>
            </td>
          </tr>""")
    return f"""\
<html><body style="font-family:Helvetica,Arial,sans-serif;background:#f5f5f5;padding:20px;">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;border:1px solid #e5e5e5;">
    <div style="background:#0B5260;color:#fff;padding:16px 20px;">
      <h2 style="margin:0;font-size:18px;">🚀 {len(new_jobs)} nuevos trabajos React Native / Mobile</h2>
      <p style="margin:4px 0 0;font-size:12px;opacity:.85;">{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
    </div>
    <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
    <div style="padding:12px 20px;color:#999;font-size:11px;">
      Fuentes: RemoteOK · Remotive · Arbeitnow · Himalayas · We Work Remotely
    </div>
  </div>
</body></html>"""


def send_email(new_jobs):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("EMAIL_TO", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚀 {len(new_jobs)} nuevos trabajos React Native/Mobile"
    msg["From"] = user
    msg["To"] = to_addr

    text = "\n".join(f"{j['title']} — {j['company']} ({j['location']}) [{j['source']}]\n{j['url']}\n" for j in new_jobs)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(build_email_html(new_jobs), "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    print(f"  ✓ email sent to {to_addr}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Run started {datetime.now(timezone.utc).isoformat()}")
    all_jobs = []
    for src in SOURCES:
        name = src.__name__.replace("from_", "")
        print(f"- fetching {name} ...")
        jobs = src()
        print(f"    got {len(jobs)} raw jobs")
        all_jobs.extend(jobs)
        time.sleep(1)  # be polite

    matched = [j for j in all_jobs if matches(j)]
    print(f"\n{len(matched)} jobs matched filters (out of {len(all_jobs)} total)")

    seen = load_seen()
    new_jobs = [j for j in matched if j["id"] not in seen]
    print(f"{len(new_jobs)} are NEW since last run")

    if new_jobs:
        try:
            send_email(new_jobs)
        except KeyError as e:
            print(f"  ! missing email env var {e} — skipping email, still recording seen")
        except Exception as e:
            print(f"  ! email failed: {e}")
            # don't mark as seen if the email didn't go out, so we retry next run
            sys.exit(1)

    # mark everything matched this run as seen (so we don't re-alert)
    for j in matched:
        seen.add(j["id"])
    save_seen(seen)
    print("Done.")


if __name__ == "__main__":
    main()
