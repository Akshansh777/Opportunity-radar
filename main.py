"""
YouTube Opportunity Digest — Gemini Edition
--------------------------------------------
100% free stack:
  - Channel new-video detection: YouTube's public RSS feed (no API key, no quota)
  - Video understanding: Gemini API, given the YouTube URL directly (no transcript
    scraping — Gemini watches/reads the video itself)
  - Delivery: email (SMTP) + a small static site written into docs/ (GitHub Pages)

Required GitHub secrets:
  GEMINI_API_KEY   (free, from https://aistudio.google.com/apikey)
  SMTP_USER        (sending email address)
  SMTP_PASS        (app password / SMTP password)
  EMAIL_TO         (recipient email address)
  SMTP_HOST        (optional, default smtp.gmail.com)
  SMTP_PORT        (optional, default 587)
"""

import os
import re
import json
import smtplib
import datetime
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import markdown as md
from google import genai
from google.genai import types

# ---------- CONFIG ----------
LOOKBACK_HOURS = 48
MAX_VIDEOS_PER_CHANNEL = 15
MODEL = "gemini-3-flash"    # free tier, supports direct YouTube URL analysis

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
gemini = genai.Client(api_key=GEMINI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
}

RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


# ---------- STEP 1: RESOLVE CHANNEL INPUT -> CHANNEL ID (no API key) ----------

CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
CHANNEL_ID_PATTERNS = [
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'itemprop="channelId" content="(UC[\w-]{22})"'),
]


def resolve_channel_id(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None

    if CHANNEL_ID_RE.match(raw):
        return raw

    if raw.startswith("http"):
        url = raw
    elif raw.startswith("@"):
        url = f"https://www.youtube.com/{raw}"
    else:
        url = f"https://www.youtube.com/@{raw}"

    # CONSENT cookie skips YouTube's EU cookie-consent interstitial page,
    # which otherwise gets served instead of the real channel page and
    # contains none of the patterns below.
    cookies = {"CONSENT": "YES+1", "SOCS": "CAI"}

    try:
        resp = requests.get(url, headers=HEADERS, cookies=cookies, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        for pattern in CHANNEL_ID_PATTERNS:
            match = pattern.search(resp.text)
            if match:
                return match.group(1)
    except requests.RequestException as e:
        print(f"  [!] Failed to resolve '{raw}': {e}")

    print(f"  [!] Could not resolve channel ID for: {raw}")
    return None


def load_channels() -> list[dict]:
    channels = []
    with open("channels.txt") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            channel_id = resolve_channel_id(line)
            if channel_id:
                channels.append({"input": line, "channel_id": channel_id})
    return channels


# ---------- STEP 2: FIND RECENT VIDEOS VIA RSS (no API key, no quota) ----------

def get_recent_videos(channel_id: str) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [!] RSS fetch failed for {channel_id}: {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        print(f"  [!] Could not parse RSS for {channel_id}")
        return []

    channel_name_el = root.find("atom:author/atom:name", RSS_NS)
    channel_name = channel_name_el.text if channel_name_el is not None else channel_id

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=LOOKBACK_HOURS)
    videos = []

    for entry in root.findall("atom:entry", RSS_NS)[:MAX_VIDEOS_PER_CHANNEL]:
        video_id = entry.find("yt:videoId", RSS_NS).text
        title = entry.find("atom:title", RSS_NS).text
        published_text = entry.find("atom:published", RSS_NS).text
        published_at = datetime.datetime.fromisoformat(published_text)

        if published_at < cutoff:
            continue

        group = entry.find("media:group", RSS_NS)
        description = ""
        if group is not None:
            desc_el = group.find("media:description", RSS_NS)
            description = desc_el.text or "" if desc_el is not None else ""

        videos.append({
            "video_id": video_id,
            "title": title,
            "description": description,
            "published_at": published_text,
            "channel_name": channel_name,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })

    return videos


# ---------- STEP 3: GEMINI - EXTRACT OPPORTUNITIES DIRECTLY FROM VIDEO ----------

STAGE1_SYSTEM_PROMPT = """You are a tech-opportunity scout. You will be given a YouTube video (watch it \
directly) plus its title and description. Your job is to extract EVERY genuine, actionable tech \
opportunity mentioned: Internships, Hackathons, Hiring Challenges, Off-Campus Drives, Open Source \
Programs, Scholarships, Contests/Competitions, Certifications, and Courses (free or paid, official or \
informal — anything a viewer could actually go sign up for or apply to).

BE INCLUSIVE. Only skip a video entirely if it has ZERO actionable opportunities in it — e.g. pure \
channel promotion ("subscribe for more"), a personal vlog/life update, a reaction video, unrelated tech \
news/reviews with nothing to apply to, or a "my setup" tour. Do NOT skip something just because it's \
presented as a list (a "top 5 internships" video with 5 real internships should yield 5 extracted \
opportunities, not zero). Do NOT skip something just because some details are missing — extract what IS \
known and mark the rest "Not specified". When in doubt, extract it rather than skip it.

For each opportunity found, extract:
- company: organization/company/platform name
- title: role, program, or course title
- category: "Opportunity" (internships, hackathons, hiring challenges, off-campus drives, open source \
programs, scholarships, contests) OR "Course" (certifications, courses, learning programs, skill-building \
tracks)
- type: one of "Internship", "Hackathon", "Hiring Challenge", "Off-Campus Drive", "Open Source Program", \
"Scholarship", "Contest", "Certification", "Course"
- eligibility: batch year, branch, skill requirements, etc. (best guess from content, "Not specified" if unclear)
- deadline_text: the deadline or urgency info exactly as stated/shown in the video (e.g. "apply within 48 hours", "by 15th July", "rolling basis", "Not specified" if none given)
- context: ONE sentence summarizing what the video said about it
- apply_link: the actual application/company portal URL if mentioned in the description or shown on-screen \
in the video. If only a generic Linktree, Telegram, Instagram, or "link in description" reference is given \
with no real destination URL visible, set this to "Check Channel Links".

Respond with ONLY valid JSON (no markdown fences, no preamble, no commentary), in this exact shape:
{"found": true or false, "opportunities": [ { ... } ]}

Only respond with {"found": false, "opportunities": []} if the video truly has nothing actionable at all — \
double check before concluding this.
"""


def extract_opportunities(video: dict) -> list[dict]:
    prompt = f"""Video Title: {video['title']}
Channel: {video['channel_name']}
Published: {video['published_at']}

Description:
{video['description']}
"""
    try:
        response = gemini.models.generate_content(
            model=MODEL,
            contents=types.Content(parts=[
                types.Part(file_data=types.FileData(file_uri=video["url"])),
                types.Part(text=prompt),
            ]),
            config=types.GenerateContentConfig(
                system_instruction=STAGE1_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        raw = response.text.strip()
    except Exception as e:
        print(f"    [!] Gemini video analysis failed, falling back to description-only: {e}")
        try:
            response = gemini.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=STAGE1_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            raw = response.text.strip()
        except Exception as e2:
            print(f"    [!] Description-only fallback also failed: {e2}")
            return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"    [!] Could not parse Gemini JSON for '{video['title']}'")
        print(f"    [debug] raw response was: {raw[:500]}")
        return []

    found = data.get("found", False)
    opps = data.get("opportunities", [])
    if not found or not opps:
        print(f"    -> Gemini found nothing actionable in this video")
    else:
        print(f"    -> raw extraction: {len(opps)} opportunity(ies)")

    for o in opps:
        o["source_video"] = video["title"]
        o["source_channel"] = video["channel_name"]
        o["source_url"] = video["url"]
    return opps


# ---------- STEP 4: DEDUPE, CLASSIFY, FORMAT ----------

STAGE2_SYSTEM_PROMPT = """You are formatting a daily tech-opportunity digest for a student/job-seeker. \
You will receive a JSON list of raw extracted items (possibly with duplicates from multiple videos \
covering the same thing — merge duplicates, keeping the best link and most complete details).

Each item has a "category" field: "Opportunity" (internships, hackathons, hiring challenges, off-campus \
drives, open source programs, scholarships, contests) or "Course" (certifications, courses, learning \
programs). Group the digest into two top-level sections by this category — see structure below.

Within EACH section, classify every item into exactly one urgency bucket using this matrix:
- HIGH PRIORITY: deadline within 72 hours from today, flash hiring, or explicitly heavily limited referral/slots.
- MEDIUM PRIORITY: deadline between 3-7 days from today, or standard off-campus drives with no extreme urgency.
- LOW PRIORITY / FLEXIBLE: open-ended windows, programs weeks away, ongoing/rolling enrollment, or no firm deadline stated.

If deadline_text is ambiguous or relative (e.g. "this week"), use today's date (given below) to reason about \
which bucket it falls into. If truly no timing information exists, default to LOW PRIORITY / FLEXIBLE.

Output ONLY a clean Markdown digest in EXACTLY this structure. Omit an entire urgency subsection if it has \
zero entries. Omit an entire top-level section (## 🎯 Opportunities or ## 📚 Courses & Certifications) only \
if it has zero entries across all three buckets:

### 📅 Daily Opportunity Digest: {today}

## 🎯 Opportunities

#### 🚨 High Priority (Act Fast!)
* **[Company Name] – [Title]**
  - **Type:** [Internship / Hackathon / Hiring Challenge / Off-Campus Drive / Open Source Program / Scholarship / Contest]
  - **Eligibility:** ...
  - **Deadline/Urgency:** ...
  - **Source/Context:** ...
  - **Apply Here Link:** ...

#### ⏳ Medium Priority (Apply This Weekend)
* **[Company Name] – [Title]**
  - **Type:** ...
  - **Eligibility:** ...
  - **Deadline:** ...
  - **Apply Here Link:** ...

#### 🟢 Low Priority / Flexible
* **[Company Name] – [Title]**
  - **Type:** ...
  - **Eligibility:** ...
  - **Deadline:** ...
  - **Apply Here Link:** ...

## 📚 Courses & Certifications

#### 🚨 High Priority (Act Fast!)
* **[Provider] – [Course/Certification Title]**
  - **Eligibility:** ...
  - **Deadline/Urgency:** ...
  - **Source/Context:** ...
  - **Apply Here Link:** ...

#### ⏳ Medium Priority
* **[Provider] – [Course/Certification Title]**
  - **Eligibility:** ...
  - **Deadline:** ...
  - **Apply Here Link:** ...

#### 🟢 Low Priority / Flexible
* **[Provider] – [Course/Certification Title]**
  - **Eligibility:** ...
  - **Deadline:** ...
  - **Apply Here Link:** ...

If there are zero items across everything, output exactly:
### 📅 Daily Opportunity Digest: {today}

No opportunities or courses found in the last 48 hours. Check back tomorrow.
"""


def build_digest(all_opportunities: list[dict]) -> str:
    today_str = datetime.date.today().strftime("%B %d, %Y")

    if not all_opportunities:
        return (
            f"### 📅 Daily Opportunity Digest: {today_str}\n\n"
            "No opportunities or courses found in the last 48 hours. Check back tomorrow."
        )

    system_prompt = STAGE2_SYSTEM_PROMPT.replace("{today}", today_str)
    response = gemini.models.generate_content(
        model=MODEL,
        contents=json.dumps(all_opportunities, indent=2),
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.2,
        ),
    )
    return response.text.strip()


# ---------- STEP 5: EMAIL ----------

def send_email(markdown_body: str):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    email_to = os.environ["EMAIL_TO"]
    # EMAIL_FROM lets you use a provider (like Brevo) where the SMTP login
    # isn't the same as the verified "From" address. Falls back to SMTP_USER
    # if not set (fine for Gmail, where they're the same).
    email_from = os.environ.get("EMAIL_FROM", smtp_user)

    today_str = datetime.date.today().strftime("%B %d, %Y")
    subject = f"📅 Daily Opportunity Digest — {today_str}"

    html_body = md.markdown(markdown_body)
    html_wrapped = f"""
    <html><body style="font-family: -apple-system, Arial, sans-serif; line-height:1.6; color:#1a1a1a;">
    {html_body}
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(markdown_body, "plain"))
    msg.attach(MIMEText(html_wrapped, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_from, email_to, msg.as_string())

    print("Email sent.")


# ---------- STEP 6: WRITE STATIC SITE FILES (docs/ -> GitHub Pages) ----------

def write_site_files(markdown_body: str):
    os.makedirs("docs/archive", exist_ok=True)
    today_iso = datetime.date.today().isoformat()

    with open("docs/digest-latest.md", "w") as f:
        f.write(markdown_body)

    archive_path = f"docs/archive/digest-{today_iso}.md"
    with open(archive_path, "w") as f:
        f.write(markdown_body)

    manifest_path = "docs/archive/manifest.json"
    manifest = []
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
    entry = f"digest-{today_iso}.md"
    if entry not in manifest:
        manifest.insert(0, entry)
    with open(manifest_path, "w") as f:
        json.dump(manifest[:60], f, indent=2)  # keep last 60 days

    print("Site files written to docs/")


# ---------- MAIN ----------

def main():
    print("Resolving channels...")
    channels = load_channels()
    print(f"Resolved {len(channels)} channel(s)")

    all_opportunities = []

    for ch in channels:
        print(f"Checking channel: {ch['input']} -> {ch['channel_id']}")
        videos = get_recent_videos(ch["channel_id"])
        print(f"  {len(videos)} recent video(s) found")
        for v in videos:
            print(f"  Analyzing: {v['title']}")
            opps = extract_opportunities(v)
            if opps:
                print(f"    -> {len(opps)} opportunity(ies) extracted")
            all_opportunities.extend(opps)

    print(f"\nTotal raw opportunities before dedupe/classify: {len(all_opportunities)}")
    digest = build_digest(all_opportunities)
    print("\n----- DIGEST -----\n")
    print(digest)

    write_site_files(digest)
    send_email(digest)


if __name__ == "__main__":
    main()
