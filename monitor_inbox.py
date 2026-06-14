#!/usr/bin/env python3
"""
Recruiter inbox monitor.
Scans Gmail via IMAP every 6 hours, detects job-opportunity and interview-invite
emails using Claude, and sends a formatted alert with .ics calendar attachments.
"""

import email as email_lib
import email.header
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SEEN_FILE = Path(__file__).parent / "seen_emails.json"
IMAP_HOST = "imap.gmail.com"


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
    SEEN_FILE.write_text(json.dumps(list(seen)[-500:]))


# ---------------------------------------------------------------------------
# Gmail IMAP reader
# ---------------------------------------------------------------------------

def decode_header_value(value):
    parts = email.header.decode_header(value or "")
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def get_text_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
    return ""


def fetch_recent_emails(days=7, limit=120):
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(user, password)
        mail.select("INBOX")

        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE "{since}")')
        all_ids = data[0].split()
        email_ids = all_ids[-limit:]  # most recent first

        results = []
        for eid in email_ids:
            try:
                _, msg_data = mail.fetch(eid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                msg_id = msg.get("Message-ID", "").strip() or f"uid-{eid.decode()}"
                subject = decode_header_value(msg.get("Subject", ""))
                from_addr = decode_header_value(msg.get("From", ""))
                date_str = msg.get("Date", "")
                body = get_text_body(msg)

                results.append({
                    "id": msg_id,
                    "subject": subject,
                    "from": from_addr,
                    "date": date_str,
                    "body": body[:2000],
                })
            except Exception as e:
                print(f"  ! error reading email {eid}: {e}", file=sys.stderr)

        mail.logout()
        print(f"  fetched {len(results)} emails from last {days} days")
        return results

    except imaplib.IMAP4.error as e:
        print(f"  ! IMAP login error: {e}", file=sys.stderr)
        print("    Make sure IMAP is enabled in Gmail settings and you're using an App Password.")
        return []
    except Exception as e:
        print(f"  ! IMAP error: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Claude classifier
# ---------------------------------------------------------------------------

def _parse_claude_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()
    return json.loads(raw)


def classify_with_claude(em):
    """Returns a dict with job-relevance info, or None on failure."""
    raw = ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        prompt = f"""Analyze this email. Determine if it's related to a job opportunity or interview for a software developer.
Output ONLY a raw JSON object — no markdown, no code fences, no extra text.

From: {em['from']}
Subject: {em['subject']}
Date: {em['date']}
Body:
{em['body'][:1500]}

Output exactly:
{{"is_job_related": <true or false>, "is_interview_invite": <true or false>, "company": "<company or empty>", "role": "<job title or empty>", "recruiter_name": "<first name or empty>", "proposed_times": "<times mentioned or empty>", "summary": "<one sentence>"}}

is_job_related: true if email mentions a job/position/role/opportunity/interview
is_interview_invite: true ONLY if they explicitly ask to schedule a call or interview"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text if msg.content else ""
        return _parse_claude_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  ! JSON parse error: {e} | raw: {raw[:120]!r}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! classify error: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Calendar file (.ics)
# ---------------------------------------------------------------------------

def make_ics(company, role):
    """
    Generate a placeholder .ics file.
    Time is set to tomorrow 10am UTC — user adjusts to the agreed time.
    """
    now = datetime.now(timezone.utc)
    start = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    end = start.replace(hour=11)
    uid = f"jobbot-{now.strftime('%Y%m%d%H%M%S')}@jobbot"

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//job-bot//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"SUMMARY:Interview \u2014 {role} @ {company}\r\n"
        "DESCRIPTION:Detected by job-bot. Adjust date/time to match what you agreed with the recruiter.\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


# ---------------------------------------------------------------------------
# Alert email
# ---------------------------------------------------------------------------

def build_alert_html(alerts):
    cards = []
    for a in alerts:
        em = a["email"]
        info = a["info"]
        is_invite = info.get("is_interview_invite", False)
        badge_bg = "#16a34a" if is_invite else "#d97706"
        badge_text = "🗓 Interview Invite" if is_invite else "📩 Job Opportunity"
        times_row = (
            f'<div style="margin-top:6px;font-size:12px;color:#555;">⏰ <strong>Horarios propuestos:</strong> {info["proposed_times"]}</div>'
            if info.get("proposed_times") else ""
        )
        ics_note = (
            '<div style="margin-top:6px;font-size:11px;color:#888;">📎 Archivo .ics adjunto — ajusta la hora antes de confirmar</div>'
            if is_invite else ""
        )
        cards.append(f"""
        <div style="border:1px solid #e5e5e5;border-radius:8px;margin-bottom:16px;overflow:hidden;">
          <div style="background:{badge_bg};color:#fff;padding:8px 14px;font-size:12px;font-weight:700;">{badge_text}</div>
          <div style="padding:14px 16px;">
            <div style="font-size:16px;font-weight:700;color:#0B5260;margin-bottom:4px;">
              {info.get('company', '?')} — {info.get('role', '?')}
            </div>
            <div style="color:#555;font-size:13px;">De: {em['from']}</div>
            <div style="color:#888;font-size:12px;">Fecha: {em['date']}</div>
            <div style="margin-top:10px;padding:10px;background:#f9f9f9;border-radius:4px;font-size:13px;color:#333;">
              {info.get('summary', '')}
            </div>
            {times_row}
            {ics_note}
            <div style="margin-top:8px;font-size:11px;color:#aaa;">Asunto: {em['subject']}</div>
          </div>
        </div>""")

    total = len(alerts)
    invites = sum(1 for a in alerts if a["info"].get("is_interview_invite"))
    headline = f"{total} recruiter(s) te contactaron"
    if invites:
        headline += f" · {invites} entrevista(s) 🎉"

    return f"""\
<html><body style="font-family:Helvetica,Arial,sans-serif;background:#f5f5f5;padding:20px;">
  <div style="max-width:640px;margin:0 auto;">
    <div style="background:#0B5260;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
      <h2 style="margin:0;font-size:18px;">📬 {headline}</h2>
      <p style="margin:4px 0 0;font-size:12px;opacity:.85;">{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
    </div>
    <div style="background:#fff;padding:20px;border:1px solid #e5e5e5;border-top:none;border-radius:0 0 8px 8px;">
      {''.join(cards)}
      <p style="color:#bbb;font-size:11px;margin-top:8px;">
        Los .ics adjuntos son placeholders — abre el archivo, ajusta la fecha/hora y guarda en tu calendario.
      </p>
    </div>
  </div>
</body></html>"""


def send_alert(alerts):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("EMAIL_TO", user)

    invites = sum(1 for a in alerts if a["info"].get("is_interview_invite"))
    subject = f"📬 {len(alerts)} recruiter(s) te contactaron"
    if invites:
        subject += f" · {invites} entrevista(s)! 🎉"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    html_part = MIMEMultipart("alternative")
    html_part.attach(MIMEText(build_alert_html(alerts), "html"))
    msg.attach(html_part)

    # Attach .ics for every interview invite
    for a in alerts:
        if a["info"].get("is_interview_invite"):
            company = a["info"].get("company", "Company")
            role = a["info"].get("role", "Role")
            ics = make_ics(company, role)
            part = MIMEApplication(ics.encode("utf-8"), _subtype="ics")
            filename = f"interview_{company.replace(' ', '_')}.ics"
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    print(f"  ✓ alert sent to {to_addr}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Inbox monitor started {datetime.now(timezone.utc).isoformat()}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  ! ANTHROPIC_API_KEY not set — skipping inbox monitor")
        return

    emails = fetch_recent_emails(days=7, limit=120)
    if not emails:
        print("  no emails fetched — check IMAP access in Gmail settings")
        return

    seen = load_seen()
    new_emails = [e for e in emails if e["id"] not in seen]
    print(f"  {len(new_emails)} new emails to classify (of {len(emails)} fetched)")

    alerts = []
    for i, em in enumerate(new_emails):
        print(f"  [{i+1}/{len(new_emails)}] {em['subject'][:60]}")
        info = classify_with_claude(em)
        seen.add(em["id"])

        if info and info.get("is_job_related"):
            alerts.append({"email": em, "info": info})
            tag = "🗓 INVITE" if info.get("is_interview_invite") else "📩 opportunity"
            print(f"    → {tag} | {info.get('company','?')} / {info.get('role','?')}")
        else:
            print(f"    → not job related")

    print(f"\n{len(alerts)} job-related emails found")

    if alerts:
        try:
            send_alert(alerts)
        except Exception as e:
            print(f"  ! alert email failed: {e}", file=sys.stderr)
            save_seen(seen)
            sys.exit(1)

    save_seen(seen)
    print("Done.")


if __name__ == "__main__":
    main()
