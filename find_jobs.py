#!/usr/bin/env python3
"""
Remote React Native / Frontend job finder with Claude AI scoring.
Pulls from several stable public job APIs, filters by keywords,
scores fit against your CV using Claude, and emails only the best matches.
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

# Jobs must match at least one keyword in title OR tags to pass initial filter
MUST_MATCH = [
    # Mobile
    "react native", "react-native", "mobile developer", "mobile engineer",
    "ios developer", "android developer", "expo",
    # Frontend
    "frontend developer", "frontend engineer", "front-end developer",
    "front-end engineer", "react developer", "next.js developer",
    "nextjs developer",
]

# Strong signals — match even if only in tags/description
STRONG_SIGNALS = ["react native", "react-native", "expo"]

# Exclude these titles (e.g. "principal", "staff", "intern")
EXCLUDE = []

# Minimum Claude score (0–100) to include in the email.
# Jobs below this are skipped (still marked seen so they don't reappear).
MIN_SCORE = 65

SEEN_FILE = Path(__file__).parent / "seen_jobs.json"
APPLIED_FILE = Path(__file__).parent / "applied_jobs.json"
USER_AGENT = "Mozilla/5.0 (job-finder; +https://github.com)"
REQUEST_TIMEOUT = 30
CANDIDATE_NAME = "Duvan Orozco"

# Email addresses to never auto-apply to
SKIP_EMAIL_PREFIXES = {"noreply", "no-reply", "donotreply", "support",
                       "info", "contact", "admin", "newsletter", "hello", "team"}
SKIP_EMAIL_DOMAINS = {"weworkremotely.com", "remoteok.com", "remotive.com",
                      "himalayas.app", "arbeitnow.com", "example.com", "github.com"}

# ---------------------------------------------------------------------------
# CANDIDATE PROFILES (used by Claude to score fit)
# ---------------------------------------------------------------------------

CV_MOBILE = """\
Candidate: Duvan Felipe Orozco Obregozo
Title: React Native Mobile Developer (Senior)
Experience: 5+ years
Location: Brisbane, Australia — open to fully remote roles worldwide

Key skills: React Native, Expo, TypeScript, JavaScript, Redux, iOS/Android native builds,
CocoaPods, Gradle, Xcode, Firebase, Node.js, Express, GraphQL, REST APIs, MongoDB,
Auth0, Azure, Git, Figma, Scrum/Agile.

Current role (Jun 2023 – Present): Full Stack Developer at Zezamii, Brisbane.
- Builds and maintains a cross-platform React Native (Expo) app published on Apple App Store and Google Play.
- Owns full mobile lifecycle: feature design → state management (Redux) → native iOS/Android builds → release → maintenance.
- Resolved complex native build issues (CocoaPods, gRPC, Firebase static frameworks, Gradle, Java 17).
- Integrates mobile clients with Node.js/Express backend and real hardware (Bluetooth smart locks).
- Agile/Scrum on Azure DevOps, Git submodules.

Previous:
- Full Stack Developer at SaanaSalud (Jun 2021 – Feb 2022): React Native for healthtech SaaS, Node.js/GraphQL/MongoDB backend, Firebase auth.
- Web Developer at Mediaty (Mar 2022 – Dec 2022): TypeScript, Next.js, Material UI, Auth0, Elasticsearch.
- Frontend Developer at Cuponatic LATAM (Apr 2022 – Jun 2022): Next.js, Sass, Nest.js.

Projects:
- Space Clean (spacecleans.com): Brisbane cleaning marketplace — React Native + Expo + Firebase, published on Apple App Store.
- Kantto Design (kanttodesign.com): Next.js e-commerce with payment gateway.

Languages: Spanish (native), English (B2+ professional).
"""

CV_FRONTEND = """\
Candidate: Duvan Felipe Orozco Obregozo
Title: Senior Frontend Engineer
Experience: 5+ years
Location: Brisbane, Australia — open to fully remote roles worldwide

Key skills: React, Redux, Next.js, TypeScript, JavaScript, Ant Design, Material UI,
HTML, CSS/Sass, Node.js, Express, Nest.js, GraphQL, REST APIs, MongoDB, MySQL,
Azure, Azure DevOps, Firebase, Vercel, Git, Figma, Scrum/Agile.

Current role (Jun 2023 – Present): Full Stack Developer at Zezamii, Brisbane.
- Architects React/Redux frontend of a multi-product SaaS access-control platform.
- Defines component hierarchy, state management (Redux sagas & slices) and error-handling patterns.
- Built and enforces a design system in TypeScript with Ant Design 5 (design tokens, typography scale, reusable components).
- Integrates with Node.js/Express services and third-party hardware APIs over HTTPS/XML.
- Engineered backend reliability with Azure event queues and retry handling.
- Ships via Git, Git submodules and Azure DevOps CI.

Previous:
- Full Stack Developer at SaanaSalud (Jun 2021 – Feb 2022): React/TypeScript frontend for healthtech SaaS, Node.js/GraphQL/MongoDB, Firebase.
- Frontend Developer at Mediaty (Mar 2022 – Dec 2022): TypeScript, Next.js, Material UI, Auth0, Elasticsearch.
- Frontend Developer at Cuponatic LATAM (Apr 2022 – Jun 2022): TypeScript, Next.js, Sass, Nest.js.

Projects:
- Space Clean (spacecleans.com): React + Next.js + Firebase cleaning marketplace (also published on App Store).
- Kantto Design (kanttodesign.com): Next.js furniture e-commerce with multi-step configurator and payment integration.

Languages: Spanish (native), English (B2+ professional).
"""


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
    jobs = []
    seen_ids = set()
    for category in ["software-dev", "frontend"]:
        data = fetch(f"https://remotive.com/api/remote-jobs?category={category}")
        if not data or "jobs" not in data:
            continue
        for item in data["jobs"]:
            jid = f"remotive-{item.get('id')}"
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            jobs.append({
                "id": jid,
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "url": item.get("url", ""),
                "location": item.get("candidate_required_location", "") or "Remote",
                "source": "Remotive",
                "tags": " ".join(item.get("tags", []) or []),
            })
        time.sleep(1)
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
            "id": f"himalayas-{item.get('guid', item.get('title', ''))}",
            "title": item.get("title", ""),
            "company": item.get("companyName", ""),
            "url": item.get("applicationLink", "") or item.get("guid", ""),
            "location": ", ".join(locs) if locs else "Remote",
            "source": "Himalayas",
            "tags": " ".join(item.get("categories", []) or []),
        })
    return jobs


def from_weworkremotely():
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
        region_el = item.find("region")

        raw_title = title_el.text if title_el is not None and title_el.text else ""
        if ":" in raw_title:
            company, _, title = raw_title.partition(":")
            company, title = company.strip(), title.strip()
        else:
            company, title = "", raw_title.strip()

        link = link_el.text if link_el is not None and link_el.text else ""
        guid = guid_el.text if guid_el is not None and guid_el.text else link
        desc = desc_el.text if desc_el is not None and desc_el.text else ""
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
# Filtering & job type detection
# ---------------------------------------------------------------------------

MOBILE_SIGNALS = {"react native", "react-native", "expo", "mobile developer",
                  "mobile engineer", "ios developer", "android developer"}
FRONTEND_SIGNALS = {"frontend developer", "frontend engineer", "front-end developer",
                    "front-end engineer", "react developer", "next.js developer",
                    "nextjs developer", "next.js", "nextjs"}


def detect_job_type(job):
    """Returns 'mobile', 'frontend', or 'both'."""
    haystack = f"{job['title']} {job['tags']}".lower()
    is_mobile = any(kw in haystack for kw in MOBILE_SIGNALS)
    is_frontend = any(kw in haystack for kw in FRONTEND_SIGNALS)
    if is_mobile and is_frontend:
        return "both"
    return "mobile" if is_mobile else "frontend"


def matches(job):
    haystack = f"{job['title']} {job['tags']}".lower()

    for bad in EXCLUDE:
        if bad.lower() in haystack:
            return False

    for sig in STRONG_SIGNALS:
        if sig in haystack:
            return True

    title = job["title"].lower()
    for kw in MUST_MATCH:
        if kw in title:
            return True
    return False


# ---------------------------------------------------------------------------
# Claude scoring
# ---------------------------------------------------------------------------

def _parse_claude_json(raw: str) -> dict:
    """Parse JSON from Claude response, stripping markdown fences if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()
    return json.loads(raw)


def score_with_claude(job, cv_text):
    """Score job fit 0–100 using Claude Haiku. Returns (score, reason)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return 70, "no ANTHROPIC_API_KEY — defaulting to pass"

    raw = ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"""You are a job fit evaluator. Score how well this candidate matches the job.
Output ONLY a raw JSON object — no markdown, no code fences, no extra text.

CANDIDATE PROFILE:
{cv_text}

JOB:
Title: {job['title']}
Company: {job['company']}
Location: {job['location']}
Tags/Description: {job['tags'][:800]}

Scoring weights: skill match 50% · seniority fit 30% · remote/location compatibility 20%

Output exactly: {{"score": <integer 0-100>, "reason": "<max 12 words>"}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text if msg.content else ""
        result = _parse_claude_json(raw)
        return int(result.get("score", 70)), str(result.get("reason", ""))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  ! Claude JSON parse error: {e} | raw: {raw[:120]!r}", file=sys.stderr)
        return 70, "parse error — defaulting to pass"
    except Exception as e:
        print(f"  ! Claude scoring error: {e}", file=sys.stderr)
        return 70, "scoring error — defaulting to pass"


# ---------------------------------------------------------------------------
# Auto-apply
# ---------------------------------------------------------------------------

_APPLY_EMAIL_RE = re.compile(r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b')


def find_apply_email(job):
    """Extract a direct application email from the job description, if present."""
    text = job.get("tags", "")
    smtp_user = os.environ.get("SMTP_USER", "").lower()
    for email in _APPLY_EMAIL_RE.findall(text):
        local, domain = email.lower().split("@", 1)
        if domain in SKIP_EMAIL_DOMAINS:
            continue
        if any(local.startswith(p) for p in SKIP_EMAIL_PREFIXES):
            continue
        if email.lower() == smtp_user:
            continue
        return email
    return None


def load_applied():
    if APPLIED_FILE.exists():
        try:
            return set(json.loads(APPLIED_FILE.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_applied(applied):
    APPLIED_FILE.write_text(json.dumps(list(applied)[-1000:]))


def generate_cover_letter(job, cv_text):
    """Use Claude Sonnet to write a personalized cover letter. Returns body text or None."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        prompt = f"""Write a short, genuine job application email for this candidate.

CANDIDATE:
{cv_text}

JOB:
Title: {job['title']}
Company: {job['company']}
Location: {job['location']}
Description: {job['tags'][:1000]}

Rules:
- 3 paragraphs max, each 2-3 sentences
- Paragraph 1: specific reason this role/company is interesting (reference something real from the description)
- Paragraph 2: the 2 most relevant experiences that match this job specifically
- Paragraph 3: short call to action — available for a call, include GitHub and LinkedIn
- Tone: direct and confident, not overly formal, not sycophantic
- Do NOT use "I am writing to express my interest" or any clichéd opener
- Do NOT address to "Dear Hiring Manager" — start directly with content
- Links to include: github.com/felipetrick12 · linkedin.com/in/duvantrick-07
- Sign off: Duvan Orozco

Output ONLY the email body. No subject line. No extra text."""

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip() if msg.content else None
    except Exception as e:
        print(f"  ! cover letter error: {e}", file=sys.stderr)
        return None


def send_application(apply_email, job, cover_letter):
    """Send the application email directly to the company."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]

    job_type = job.get("job_type", "frontend")
    if "mobile" in job_type:
        subject = f"React Native Developer – {job['title']} @ {job['company']}"
    else:
        subject = f"Senior Frontend Engineer – {job['title']} @ {job['company']}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{CANDIDATE_NAME} <{user}>"
    msg["To"] = apply_email
    msg["Reply-To"] = user
    msg.attach(MIMEText(cover_letter, "plain"))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(user, [apply_email], msg.as_string())


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
    SEEN_FILE.write_text(json.dumps(list(seen)[-2000:]))


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def score_color(score):
    if score >= 80:
        return "#16a34a"   # green
    if score >= 65:
        return "#d97706"   # amber
    return "#dc2626"       # red


def build_email_html(new_jobs):
    rows = []
    for j in new_jobs:
        score = j.get("score", 50)
        reason = j.get("reason", "")
        job_type = j.get("job_type", "")
        applied_to = j.get("applied_to", "")

        if job_type == "mobile":
            type_badge = "📱 Mobile"
        elif job_type == "frontend":
            type_badge = "💻 Frontend"
        else:
            type_badge = "📱💻 Mobile+Frontend"

        applied_badge = (
            f'&nbsp;<span style="background:#16a34a;color:#fff;font-size:11px;'
            f'font-weight:700;padding:2px 8px;border-radius:12px;">✉️ Auto-applied</span>'
            if applied_to else ""
        )

        rows.append(f"""
          <tr>
            <td style="padding:12px 16px;border-bottom:1px solid #eee;">
              <div style="margin-bottom:5px;">
                <span style="background:{score_color(score)};color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:12px;">{score}/100</span>
                &nbsp;<span style="color:#777;font-size:11px;">{type_badge}</span>
                {applied_badge}
              </div>
              <a href="{j['url']}" style="color:#0B5260;font-weight:600;text-decoration:none;font-size:15px;">{j['title']}</a><br>
              <span style="color:#555;font-size:13px;">{j['company']}</span>
              <span style="color:#999;font-size:12px;"> · {j['location']}</span>
              <span style="color:#0CA3C1;font-size:11px;"> · {j['source']}</span>
              {f'<br><span style="color:#888;font-size:11px;font-style:italic;">{reason}</span>' if reason else ''}
            </td>
          </tr>""")

    auto_applied_count = sum(1 for j in new_jobs if j.get("applied_to"))
    subtitle = f"solo matches con score ≥ {MIN_SCORE}"
    if auto_applied_count:
        subtitle += f" · ✉️ {auto_applied_count} auto-aplicados"

    return f"""\
<html><body style="font-family:Helvetica,Arial,sans-serif;background:#f5f5f5;padding:20px;">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;border:1px solid #e5e5e5;">
    <div style="background:#0B5260;color:#fff;padding:16px 20px;">
      <h2 style="margin:0;font-size:18px;">🚀 {len(new_jobs)} jobs nuevos — React Native &amp; Frontend</h2>
      <p style="margin:4px 0 0;font-size:12px;opacity:.85;">{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · {subtitle}</p>
    </div>
    <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
    <div style="padding:12px 20px;color:#999;font-size:11px;">
      Fuentes: RemoteOK · Remotive · Arbeitnow · Himalayas · We Work Remotely · scored &amp; applied by Claude AI
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
    msg["Subject"] = f"🚀 {len(new_jobs)} jobs — React Native/Frontend (score ≥{MIN_SCORE})"
    msg["From"] = user
    msg["To"] = to_addr

    text = "\n".join(
        f"[{j.get('score', '?')}/100] {j['title']} — {j['company']} ({j['location']}) [{j['source']}]\n{j['url']}\n"
        for j in new_jobs
    )
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
        time.sleep(1)

    # Deduplicate by id across all sources
    seen_ids: set = set()
    unique_jobs = []
    for j in all_jobs:
        if j["id"] not in seen_ids:
            seen_ids.add(j["id"])
            unique_jobs.append(j)
    all_jobs = unique_jobs

    matched = [j for j in all_jobs if matches(j)]
    print(f"\n{len(matched)} jobs matched keyword filters (out of {len(all_jobs)} total)")

    seen = load_seen()
    new_jobs = [j for j in matched if j["id"] not in seen]
    print(f"{len(new_jobs)} are NEW since last run")

    # Score new jobs with Claude
    use_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not use_claude:
        print("  ! ANTHROPIC_API_KEY not set — sending all keyword matches without scoring")

    scored_jobs = []
    for i, job in enumerate(new_jobs):
        job_type = detect_job_type(job)
        job["job_type"] = job_type

        if use_claude:
            cv = CV_MOBILE if job_type in ("mobile", "both") else CV_FRONTEND
            print(f"  scoring [{i+1}/{len(new_jobs)}] {job['title'][:55]}...")
            score, reason = score_with_claude(job, cv)
            job["score"] = score
            job["reason"] = reason
            if score >= MIN_SCORE:
                scored_jobs.append(job)
            else:
                print(f"    skipped (score {score}/100 — {reason})")
        else:
            job["score"] = 50
            job["reason"] = ""
            scored_jobs.append(job)

    # Sort best first
    scored_jobs.sort(key=lambda j: j.get("score", 0), reverse=True)
    print(f"{len(scored_jobs)} jobs passed score threshold (≥{MIN_SCORE})")

    # Auto-apply to jobs that have a direct application email in their description
    if use_claude and scored_jobs:
        applied = load_applied()
        applied_count = 0
        for job in scored_jobs:
            if job["id"] in applied:
                continue
            apply_email = find_apply_email(job)
            if not apply_email:
                continue
            cv = CV_MOBILE if job.get("job_type") in ("mobile", "both") else CV_FRONTEND
            print(f"  auto-applying: {job['title'][:50]} @ {job['company']} → {apply_email}")
            cover_letter = generate_cover_letter(job, cv)
            if not cover_letter:
                continue
            try:
                send_application(apply_email, job, cover_letter)
                job["applied_to"] = apply_email
                applied.add(job["id"])
                applied_count += 1
                print(f"    ✓ application sent")
                time.sleep(3)  # be polite, avoid spam filters
            except Exception as e:
                print(f"    ! failed to send application: {e}", file=sys.stderr)
        save_applied(applied)
        if applied_count:
            print(f"\n✉️  Auto-applied to {applied_count} job(s)")

    if scored_jobs:
        try:
            send_email(scored_jobs)
        except KeyError as e:
            print(f"  ! missing email env var {e} — skipping email, still recording seen")
        except Exception as e:
            print(f"  ! email failed: {e}")
            sys.exit(1)

    # Mark all keyword-matched jobs as seen (even low-score ones, so they don't repeat)
    for j in matched:
        seen.add(j["id"])
    save_seen(seen)
    print("Done.")


if __name__ == "__main__":
    main()
