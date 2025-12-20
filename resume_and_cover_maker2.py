#!/usr/bin/env python3
"""
resume_and_cover_maker.py

Single Gemini prompt that generates BOTH:
- a tailored resume (LaTeX -> PDF)
- a matching cover letter (LaTeX -> PDF)

Extras:
- Counts words in SUMMARY, SKILLS, and PROFESSIONAL EXPERIENCE from the original resume.
- Uses ~80% of those counts as word targets for Gemini for each section.
"""

import os
import json
import subprocess
import re
import time
from pathlib import Path
from datetime import date

from google import genai
from google.genai import types
import PyPDF2

# ===================== CONFIG ===================== #

MIN_PROJECTS = 3  # number of projects to keep (and display)

BASE_DIR = Path(__file__).parent
GEMINI_KEY_FILE = BASE_DIR / "gemini_api_key.txt"

# These defaults are overridden by auto_apply.py at runtime:
RESUME_PDF_PATH = BASE_DIR / "ASU_Resume_Template_Shivam.pdf"
JOB_DESC_PATH = BASE_DIR / "job.txt"

RESUME_TEX_PATH = BASE_DIR / "resume_generated.tex"
COVER_LETTER_TEX_PATH = BASE_DIR / "cover_letter_generated.tex"


# ===================== LATEX TEMPLATE: RESUME ===================== #

RESUME_TEX_TEMPLATE = r"""
\documentclass[8pt]{extarticle}

\usepackage[margin=0.5in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{needspace}

\linespread{0.96}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\setlist[itemize]{leftmargin=*, itemsep=0.10em, topsep=0.15em}
\pagestyle{empty}

\newcommand{\resSection}[1]{%
  \vspace{0.4em}%
  \textbf{\normalsize #1}\\[-0.35em]
  \rule{\textwidth}{0.3pt}\\[0.15em]
}

\begin{document}

%==================== HEADER ====================

<<HEADER>>

%==================== SUMMARY ====================

\resSection{SUMMARY}

<<SUMMARY>>

%==================== EDUCATION ====================

\resSection{EDUCATION}

<<EDUCATION>>

%==================== PROFESSIONAL EXPERIENCE ====================

\resSection{PROFESSIONAL EXPERIENCE}

<<EXPERIENCE>>

%==================== ACADEMIC PROJECTS ====================

\resSection{ACADEMIC PROJECTS}

<<PROJECTS>>

%==================== TECHNICAL SKILLS AND CERTIFICATIONS ====================

\needspace{10\baselineskip}
\resSection{TECHNICAL SKILLS AND CERTIFICATIONS}

<<SKILLS>>
<<CERTIFICATIONS>>
<<EXTRACURRICULAR>>

\end{document}
"""


# ===================== LATEX TEMPLATE: COVER LETTER ===================== #

COVER_LETTER_TEX_TEMPLATE = r"""
\documentclass[10pt]{article}

\usepackage[margin=0.8in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[hidelinks]{hyperref}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.6em}

\begin{document}

<<HEADER_BLOCK>>\\[0.5em]

<<DATE_LINE>>\\[0.5em]

<<COMPANY_BLOCK>>\\[0.5em]

<<SALUTATION>>\\

<<BODY_BLOCK>>

<<CLOSING_BLOCK>>

\end{document}
"""


# ===================== BASIC HELPERS ===================== #

def extract_text_from_pdf(pdf_path: Path) -> str:
    reader = PyPDF2.PdfReader(str(pdf_path))
    texts = []
    for page in reader.pages:
        t = page.extract_text()
        if t:
            texts.append(t)
    return "\n".join(texts)


def latex_escape(text: str) -> str:
    if text is None:
        return ""
    replacements = {
        "\\": r"\\",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(c, c) for c in text)


def word_count(text: str) -> int:
    if not text:
        return 0
    tokens = re.findall(r"\b\w+\b", text)
    return len(tokens)


# --------- NEW: Section heading helpers + section word counting --------- #

def _normalize_heading_line(line: str) -> str:
    """
    Normalize a line so heading detection is robust:
    - keep only letters + spaces
    - uppercase
    - collapse whitespace
    """
    if line is None:
        return ""
    s = re.sub(r"[^A-Za-z]+", " ", str(line)).upper()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _heading_matches(norm_line: str, key: str) -> bool:
    """
    Check whether a normalized line looks like a heading "key".
    We require whole-word-ish matches to avoid matching bullets.
    """
    if not norm_line or not key:
        return False
    key = key.upper().strip()
    if not key:
        return False
    if norm_line == key:
        return True
    if norm_line.startswith(key + " "):
        return True
    if norm_line.endswith(" " + key):
        return True
    if f" {key} " in norm_line:
        return True
    return False


def estimate_section_word_count(
    resume_text: str,
    target_headings,
    all_heading_keywords,
    ignore_cert_lines: bool = False,
) -> int:
    """
    Roughly estimate word count for one section (e.g. SUMMARY / SKILLS / EXPERIENCE)
    in the original resume text, using headings to slice the text.
    """
    if not resume_text:
        return 0

    lines = resume_text.splitlines()
    norm_lines = [_normalize_heading_line(l) for l in lines]

    target_keys = [k.upper() for k in target_headings if k]
    all_keys = [k.upper() for k in all_heading_keywords if k]

    # 1) find start of section
    start_idx = None
    for i, norm in enumerate(norm_lines):
        for key in target_keys:
            if _heading_matches(norm, key):
                start_idx = i + 1  # content usually starts after heading line
                break
        if start_idx is not None:
            break

    if start_idx is None:
        return 0

    # 2) find end of section (next heading)
    end_idx = len(lines)
    for j in range(start_idx, len(lines)):
        norm = norm_lines[j]
        if not norm:
            continue
        for key in all_keys:
            if _heading_matches(norm, key):
                end_idx = j
                break
        if end_idx != len(lines):
            break

    raw_section = "\n".join(lines[start_idx:end_idx]).strip()
    if not raw_section:
        return 0

    if ignore_cert_lines:
        filtered = []
        for ln in raw_section.splitlines():
            nl = _normalize_heading_line(ln)
            # drop lines that look like a "Certifications" subheading
            if "CERTIFICATION" in nl or "CERTIFICATIONS" in nl:
                continue
            filtered.append(ln)
        raw_section = "\n".join(filtered).strip()

    return word_count(raw_section)


def estimate_resume_section_word_counts(resume_text: str):
    """
    Return raw word counts for SUMMARY, SKILLS, and EXPERIENCE sections.

    These are counts from the ORIGINAL resume; we later take 80% of each
    to use as Gemini's targets.
    """
    all_heading_keywords = [
        "SUMMARY",
        "EDUCATION",
        "PROFESSIONAL EXPERIENCE",
        "EXPERIENCE",
        "WORK EXPERIENCE",
        "ACADEMIC PROJECTS",
        "PROJECTS",
        "PERSONAL PROJECTS",
        "TECHNICAL SKILLS",
        "TECHNICAL SKILLS AND CERTIFICATIONS",
        "SKILLS",
        "CERTIFICATIONS",
        "EXTRACURRICULAR",
        "EXTRA CURRICULAR",
        "EXTRA-CURRICULAR",
        "AWARDS",
        "HONORS",
        "PUBLICATIONS",
        "COURSEWORK",
        "PERSONAL DETAILS",
        "CONTACT",
        "INTERESTS",
    ]

    summary_wc = estimate_section_word_count(
        resume_text,
        target_headings=["SUMMARY"],
        all_heading_keywords=all_heading_keywords,
        ignore_cert_lines=False,
    )

    skills_wc = estimate_section_word_count(
        resume_text,
        target_headings=[
            "TECHNICAL SKILLS AND CERTIFICATIONS",
            "TECHNICAL SKILLS",
            "SKILLS",
        ],
        all_heading_keywords=all_heading_keywords,
        ignore_cert_lines=True,  # ignore certifications lines in skills section
    )

    experience_wc = estimate_section_word_count(
        resume_text,
        target_headings=[
            "PROFESSIONAL EXPERIENCE",
            "WORK EXPERIENCE",
            "EXPERIENCE",
        ],
        all_heading_keywords=all_heading_keywords,
        ignore_cert_lines=False,
    )

    return {
        "summary": summary_wc,
        "skills": skills_wc,
        "experience": experience_wc,
    }


def fill_missing_project_timeframes(projects, start_year: int = 2021):
    """
    Ensure every project has a non-empty, unique timeframe string.

    - If Gemini already provided a timeframe, we keep it.
    - If timeframe is missing/empty, we assign a plausible range like
      "Jan 2023--Apr 2023".
    - We make sure we don't reuse the same range for two different projects.
    """
    used = set()
    for p in projects:
        tf = (p.get("timeframe") or "").strip()
        if tf:
            used.add(tf)

    year = start_year
    patterns = [
        ("Jan", "Apr"),
        ("Aug", "Nov"),
    ]
    pattern_idx = 0

    for p in projects:
        tf = (p.get("timeframe") or "").strip()
        if tf:
            continue

        while True:
            start_month, end_month = patterns[pattern_idx % len(patterns)]
            candidate = f"{start_month} {year}--{end_month} {year}"
            pattern_idx += 1
            if pattern_idx % len(patterns) == 0:
                year += 1
            if candidate not in used:
                used.add(candidate)
                p["timeframe"] = candidate
                break

    return projects


# ===================== ONE GEMINI CALL: RESUME + COVER LETTER ===================== #

def call_gemini_all(
    resume_text: str,
    job_description: str,
    api_key: str,
    summary_word_limit=None,
    skills_word_limit=None,
    experience_word_limit=None,
):
    """
    Single prompt that returns BOTH resume + cover letter JSON.

    Word limits:
    - summary_word_limit, skills_word_limit, experience_word_limit
      are ~80% of the original section lengths (computed in main()).
    """
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1alpha"),
    )

    DEFAULT_SUMMARY_LIMIT = 80
    DEFAULT_SKILLS_LIMIT = 120
    DEFAULT_EXPERIENCE_LIMIT = 300

    def choose_limit(passed, default):
        try:
            val = int(passed) if passed is not None else 0
        except (TypeError, ValueError):
            val = 0
        return val if val > 0 else default

    summary_limit = choose_limit(summary_word_limit, DEFAULT_SUMMARY_LIMIT)
    skills_limit = choose_limit(skills_word_limit, DEFAULT_SKILLS_LIMIT)
    experience_limit = choose_limit(experience_word_limit, DEFAULT_EXPERIENCE_LIMIT)

    print(
        f"- using resume section word limits (75% targets) summary/skills/experience: "
        f"{summary_limit} / {skills_limit} / {experience_limit}"
    )

    # ---------- PROMPT (EDUCATION + WORD-LIMIT BEHAVIOR) ---------- #
    prompt = f"""
You are generating BOTH a tailored resume and a matching cover letter
for a specific job, using the candidate's resume as the source of truth.

You are given the ORIGINAL resume (plain text from PDF) and the target job:

RESUME (plain text):
<<<RESUME_START>>>
{resume_text}
<<<RESUME_END>>>

JOB DESCRIPTION:
<<<JOB_START>>>
{job_description}
<<<JOB_END>>>

You MUST return a SINGLE JSON object with exactly these top-level keys:
  - "resume"
  - "cover_letter"

1) "resume" object:

"resume": {{
  "header": {{
    "name": "...",
    "phone": "...",
    "email": "...",
    "github_url": "...",
    "linkedin_url": "...",
    "portfolio_url": "..."
  }},
  "education": [
    {{
      "degree": "...",
      "institution": "...",
      "location": "...",
      "date": "...",
      "gpa": "..."
    }},
    ...
  ],
  "summary": "...",
  "skills": [ "...", "...", ... ],
  "projects": [
    {{
      "title": "Project title (<= 10 words)",
      "timeframe": "NON-EMPTY date or date range string, using the SAME FORMAT STYLE as in the resume, e.g. 'Aug 2023 – Dec 2023' or 'Aug 2023--Dec 2023'",
      "tools": "comma-separated tools/languages/frameworks used",
      "bullets": [
        "Bullet 1",
        "Bullet 2",
        "Bullet 3"
      ]
    }},
    ...
  ],
  "experience": [
    {{
      "title": "Job title exactly as in resume (if any jobs exist)",
      "company": "Company + location exactly as in resume (if any jobs exist)",
      "date": "NON-EMPTY date or date range string, using the SAME FORMAT STYLE as in the resume, e.g. 'Jun 2022 – Dec 2022'",
      "tech_stack": "Comma-separated tools/languages/frameworks",
      "bullets": [
        "Bullet 1",
        "Bullet 2",
        "Bullet 3"
      ]
    }},
    ...
  ],
  "certifications": [ "...", "...", ... ],
  "extracurriculars": [ "...", "...", ... ]
}}

HARD CONSTRAINTS FOR "resume":

- SECTION WORD LIMITS (VERY IMPORTANT):
  * For the following sections, the TOTAL words must be CLOSE to these values.
    These values are approximately 80% of the word counts in the ORIGINAL resume.
    Do NOT exceed them by more than ~10–15%, and avoid going much shorter
    than ~70% of them.

  * SUMMARY:
      - Target around {summary_limit} words in total for the "summary" string.

  * SKILLS:
      - Across ALL strings in the "skills" list, target around {skills_limit} words
        in total (do NOT count certifications here).

  * PROFESSIONAL EXPERIENCE ("experience"):
      - Across ALL bullet strings in ALL entries of "experience", target around
        {experience_limit} words total.

- HEADER:
  * name, phone, email, github_url, linkedin_url, portfolio_url MUST come
    from the original resume. Do NOT invent new contact info.

- EDUCATION (VERY IMPORTANT):
  * You MUST return EVERY education entry that appears in the resume, including:
      - School / High School / Secondary education,
      - ALL Bachelor-level degrees (e.g., B.Tech, BSc, BE, BS),
      - ALL Master-level degrees (e.g., MS, MSc, M.Tech),
      - ALL PhD / Doctoral degrees,
      - Diplomas, Post-Graduate Diplomas,
      - Universities, Colleges, Bootcamps or similar programs.
  * Do NOT drop older degrees or earlier schooling when building the JSON.
    The final EDUCATION array should reflect the full progression from
    the earliest to the latest education.
  * For each entry, fill:
      - degree (or qualification label, e.g., "High School Diploma", "B.Tech ..."),
      - institution,
      - location,
      - date (or date range, e.g. "2016–2020" or "Aug 2021–May 2023"),
      - gpa (if given in the resume, otherwise empty string).
  * Do NOT invent extra education that is not present in the resume.
  * Do NOT remove or merge real education entries that appear separately
    in the resume.

- SKILLS:
  * You are allowed to CHANGE and REORGANIZE the skills to better match the JOB DESCRIPTION.
  * You do NOT need to copy the skill lines exactly as written in the resume.
  * You may add or drop tools/skills as long as they are:
      - plausible given the candidate's background from the resume, AND/OR
      - clearly relevant to the JOB DESCRIPTION.
  * Do NOT invent completely unrelated skills that conflict with the candidate's profile.

- CERTIFICATIONS:
  * Names and issuing orgs must match the resume.
  * No new certifications, no deletions.
  * Do NOT put certifications inside "skills".

- PROJECTS:
  * RETURN EXACTLY {MIN_PROJECTS} PROJECTS (no more, no fewer).
  * Projects must be plausible given the resume (AI/ML, CV, data, robotics, etc.).
  * Each has 3 impact-focused bullets tailored to the JOB DESCRIPTION.
  * For EVERY project, "timeframe" MUST be a NON-EMPTY string.
    - If the original resume already gives dates, copy or lightly normalize them,
      but KEEP the same visual style.
    - If the resume does NOT specify dates for a project, you MUST choose a
      plausible date range (for example "Jan 2023–Apr 2023" or
      "Jan 2023--Apr 2023") that fits into the candidate's overall timeline.
    - IMPORTANT: Do NOT reuse the exact same timeframe for two different projects.
      Each project's "timeframe" must be distinct (no clashes).

- EXPERIENCE:
  * If the resume ALREADY has work experience:
      - For each job/experience entry, "title" and "company" MUST match the resume
        (you may include location in "company" as in the resume).
      - You ARE allowed to CHANGE and REWRITE the "bullets" and "tech_stack"
        to better match the JOB DESCRIPTION, as long as they stay plausible
        for that role.
      - For EVERY job/experience entry, "date" MUST be a NON-EMPTY string.
        - If the original resume already gives dates, copy or lightly normalize them,
          but KEEP the same visual style as in the resume.
        - If the resume does NOT specify any dates for that job, you MUST choose a
          plausible date range (for example "Jun 2022–Dec 2022") that is consistent
          with the candidate's education and project timeline.
        - Avoid reusing the exact same date range for two different jobs if possible.

  * If the resume has NO work experience at all:
      - You MUST create 1–2 plausible professional experience entries that match
        the candidate's field (e.g., internships, part-time roles, research).
      - For these invented roles, you may invent company names and locations.
      - Choose job titles, tech_stack, and bullets that are:
          - realistic for the candidate's skill level and education, and
          - strongly aligned with the JOB DESCRIPTION.
      - For EVERY invented experience, "date" MUST be a NON-EMPTY date range
        in the same style as the resume, and you MUST make sure those date ranges
        do NOT clash with each other or with project timeframes.

- EXTRACURRICULARS:
  * If the original resume contains extracurricular / co-curricular / leadership /
    clubs / hackathons / competitions / student societies / sports / volunteering,
    return them as short phrases in "extracurriculars".
  * If there are NO such activities, return an EMPTY LIST [] for "extracurriculars".

2) "cover_letter" object:

"cover_letter": {{
  "header": {{
    "name": "...",
    "location": "...",
    "phone": "...",
    "email": "...",
    "github_url": "...",
    "linkedin_url": "...",
    "portfolio_url": "..."
  }},
  "letter": {{
    "date": "Month DD, YYYY",
    "company_name": "...",
    "company_line_2": "...",
    "company_location": "City, State or Country",
    "position_title": "...",
    "salutation": "Dear Hiring Manager,",
    "body_paragraphs": [
      "Paragraph 1 ...",
      "Paragraph 2 ...",
      "Paragraph 3 ..."
    ],
    "closing": "Sincerely,",
    "signature_name": "..."
  }}
}}

HARD CONSTRAINTS FOR "cover_letter":

- All contact info in cover_letter.header MUST come from the original resume,
  consistent with resume.header. Do NOT invent any new contact info.

- BODY WORD LIMIT (VERY IMPORTANT):
  The sum of all words across ALL strings in "body_paragraphs" must be
  AT MOST 400 words total. "Words" are tokens separated by spaces.
  This body is everything AFTER the salutation and BEFORE the closing.

- Cover letter content must be tailored to the JOB DESCRIPTION while remaining
  consistent with the resume. No bullet points, only sentences.

- "signature_name" must exactly match the candidate's full name from the resume.

GLOBAL RULES:
- Only plain text in all fields, no LaTeX/HTML/markdown or bullet characters.
- Do NOT include any keys other than "resume" and "cover_letter".
- Return ONLY valid JSON (no markdown fences, no comments, no extra text).
""".strip()

    # ---------- CALL GEMINI WITH RETRIES ---------- #
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            raw = (resp.text or "").strip()

            # Strip markdown fences if Gemini wraps JSON in ```json ... ```
            if raw.startswith("```"):
                parts = raw.split("```")
                if len(parts) >= 2:
                    raw = parts[1].lstrip("json").strip()

            data = json.loads(raw)

            if "resume" not in data or "cover_letter" not in data:
                raise ValueError("JSON missing 'resume' or 'cover_letter' key")

            resume_obj = dict(data["resume"])
            cl_obj = dict(data["cover_letter"])

            # Ensure required keys exist in resume JSON
            for key in (
                "header",
                "education",
                "summary",
                "skills",
                "projects",
                "experience",
                "certifications",
                "extracurriculars",
            ):
                if key not in resume_obj:
                    raise ValueError(f"'resume' JSON missing key: {key}")

            if "header" not in cl_obj or "letter" not in cl_obj:
                raise ValueError("cover_letter JSON missing 'header' or 'letter'")

            # Normalize types
            resume_obj["skills"] = [str(s).strip() for s in resume_obj.get("skills", [])]
            resume_obj["certifications"] = [str(c).strip() for c in resume_obj.get("certifications", [])]
            resume_obj["projects"] = list(resume_obj.get("projects", []))
            resume_obj["experience"] = list(resume_obj.get("experience", []))
            resume_obj["education"] = list(resume_obj.get("education", []))
            resume_obj["extracurriculars"] = [
                str(x).strip() for x in resume_obj.get("extracurriculars", [])
            ]

            # enforce exactly MIN_PROJECTS & fill missing project timeframes
            projects = resume_obj["projects"]
            if len(projects) > MIN_PROJECTS:
                projects = projects[:MIN_PROJECTS]
            projects = fill_missing_project_timeframes(projects)
            resume_obj["projects"] = projects

            cl_obj["letter"]["body_paragraphs"] = [
                str(p) for p in cl_obj["letter"].get("body_paragraphs", [])
            ]

            return resume_obj, cl_obj

        except Exception as e:
            if attempt == max_retries:
                raise
            print(f"Gemini call failed (attempt {attempt}): {e}")
            time.sleep(2 * attempt)


# ===================== RESUME: FORMATTERS (PDF CONTENT) ===================== #

def format_resume_header(header: dict) -> str:
    def fix_url(u: str) -> str:
        if not u:
            return ""
        return re.sub(r"\s+", "", u)

    name = (header.get("name") or "").strip() or "YOUR NAME"
    phone = (header.get("phone") or "")
    phone = re.sub(r"\s+", " ", phone).strip()
    email = (header.get("email") or "").replace(" ", "").strip()
    github_url = fix_url(header.get("github_url") or "")
    linkedin_url = fix_url(header.get("linkedin_url") or "")
    portfolio_url = fix_url(header.get("portfolio_url") or "")

    name_tex = latex_escape(name)
    phone_tex = latex_escape(phone) if phone else ""
    email_tex = latex_escape(email) if email else ""

    def label(url: str) -> str:
        if not url:
            return ""
        lbl = url.replace("https://", "").replace("http://", "").rstrip("/")
        return latex_escape(lbl)

    parts = []
    if phone_tex:
        parts.append(phone_tex)
    if email:
        parts.append(rf"\href{{mailto:{email}}}{{{email_tex}}}")
    if github_url:
        parts.append(rf"\href{{{github_url}}}{{{label(github_url)}}}")
    if linkedin_url:
        parts.append(rf"\href{{{linkedin_url}}}{{{label(linkedin_url)}}}")
    if portfolio_url:
        parts.append(rf"\href{{{portfolio_url}}}{{{label(portfolio_url)}}}")

    lines = [
        r"\begin{center}",
        f"  {{\\normalsize \\textbf{{{name_tex}}}}}\\\\[2pt]",
    ]
    if parts:
        lines.append("  " + " \\textbar{} ".join(parts))
    lines.append(r"\end{center}")
    return "\n".join(lines)


def format_education(education_list):
    """
    Print EVERY education entry Gemini returns.
    """
    lines = []
    for edu in education_list:
        degree = (edu.get("degree") or "").strip()
        institution = (edu.get("institution") or "").strip()
        location = (edu.get("location") or "").strip()
        date_str = (edu.get("date") or "").strip()
        gpa = (edu.get("gpa") or "").strip()

        # Skip only if everything is truly empty
        if not (degree or institution or location or date_str or gpa):
            continue

        deg_tex = latex_escape(degree)
        date_tex = latex_escape(date_str)
        inst_loc = ", ".join(x for x in [institution, location] if x)
        inst_loc_tex = latex_escape(inst_loc)
        gpa_tex = latex_escape(gpa)

        # Line 1: degree + date
        if deg_tex or date_tex:
            l1 = ""
            if deg_tex:
                l1 = f"\\textbf{{{deg_tex}}}"
            if date_tex:
                l1 += r" \hfill " + date_tex
            l1 += r"\\"
            lines.append(l1)

        # Line 2: institution, location, GPA (if any)
        if inst_loc_tex or gpa_tex:
            if gpa_tex:
                l2 = f"{inst_loc_tex} \\hfill \\textbf{{{gpa_tex}}}\\\\[0.1em]"
            else:
                l2 = inst_loc_tex + r"\\[0.1em]"
            lines.append(l2)

        lines.append("")  # small extra gap between entries

    return "\n".join(lines).strip()


def format_summary(summary: str) -> str:
    return latex_escape(summary.strip())


def format_skills(skills_list):
    """
    Take a flat list of skills and regroup them into categories.
    """
    raw_tokens = []
    for line in skills_list or []:
        if not line:
            continue
        s = re.sub(
            r'^\s*(skills?|technical skills?)\s*:\s*',
            '',
            str(line),
            flags=re.IGNORECASE,
        )
        parts = re.split(r"[,/]", s)
        for part in parts:
            token = part.strip()
            if token:
                raw_tokens.append(token)

    categories = {
        "Programming Languages": set(),
        "Machine Learning & Deep Learning": set(),
        "Data & Analytics": set(),
        "MLOps & Optimization": set(),
        "Computer Vision & Robotics": set(),
        "Natural Language Processing": set(),
        "Visualization": set(),
        "Other": set(),
    }

    def add_skill(token: str):
        t_norm = token.strip()
        if not t_norm:
            return
        low = t_norm.lower()

        def has(*subs):
            return any(sub in low for sub in subs)

        if has("python") or has("sql") or has("c++", "c/c++") or has("javascript", "java script") or has("java"):
            categories["Programming Languages"].add(t_norm)
        elif has("pytorch", "torch") or has("tensorflow") or has("keras") \
             or has("scikit-learn", "sklearn") or has("xgboost") \
             or has("lightgbm") or has("catboost") or has("transformer"):
            categories["Machine Learning & Deep Learning"].add(t_norm)
        elif has("pandas") or has("numpy"):
            categories["Data & Analytics"].add(t_norm)
        elif has("mlflow") or has("optuna") or has("onnx"):
            categories["MLOps & Optimization"].add(t_norm)
        elif has("opencv") or has("mediapipe") or has("dlib") or has("ros2", "ros 2") or has("robot"):
            categories["Computer Vision & Robotics"].add(t_norm)
        elif has("spacy") or has("nltk") or has("sentence-bert", "sentence bert") or has("llm", "gpt"):
            categories["Natural Language Processing"].add(t_norm)
        elif has("matplotlib") or has("seaborn") or has("plotly"):
            categories["Visualization"].add(t_norm)
        else:
            categories["Other"].add(t_norm)

    for token in raw_tokens:
        add_skill(token)

    ordered_cats = [
        "Programming Languages",
        "Machine Learning & Deep Learning",
        "Data & Analytics",
        "MLOps & Optimization",
        "Computer Vision & Robotics",
        "Natural Language Processing",
        "Visualization",
        "Other",
    ]

    lines = []
    for cat in ordered_cats:
        skills = sorted(categories[cat])
        if not skills:
            continue
        cat_tex = latex_escape(cat + ":")
        vals_tex = latex_escape(", ".join(skills))
        lines.append(f"\\textbf{{{cat_tex}}} {vals_tex}\\\\")
    return "\n".join(lines)


def format_projects(projects_list):
    blocks = []
    projects_list = list(projects_list or [])[:MIN_PROJECTS]

    for proj in projects_list:
        title_raw = str(proj.get("title", "")).strip()
        timeframe_raw = str(proj.get("timeframe", "")).strip()
        tools_raw = str(proj.get("tools", "")).strip()
        bullets = list(proj.get("bullets", []) or [])[:3]

        block_parts = []

        title_tex = latex_escape(title_raw) if title_raw else ""
        timeframe_tex = latex_escape(timeframe_raw) if timeframe_raw else ""
        if title_tex:
            header_line = f"\\textbf{{{title_tex}}}"
            if timeframe_tex:
                header_line += r" \hfill " + timeframe_tex
            header_line += r"\\"
            block_parts.append(header_line)

        if tools_raw:
            tools_tex = latex_escape(tools_raw)
            block_parts.append(f"\\textbf{{Tools/Languages:}} {tools_tex}\\\\")

        if bullets:
            block_parts.append(r"\begin{itemize}")
            for b in bullets:
                b_raw = str(b).strip()
                if not b_raw:
                    continue
                block_parts.append(f"  \\item {latex_escape(b_raw)}")
            block_parts.append(r"\end{itemize}")

        if block_parts:
            blocks.append("\n".join(block_parts))

    return "\n\n\\vspace{0.12em}\n\n".join(blocks)


def format_experience(exp_list):
    blocks = []
    for idx, e in enumerate(exp_list):
        title_raw = str(e.get("title", "")).strip()
        company_raw = str(e.get("company", "")).strip()
        date_raw = str(e.get("date", "")).strip()
        tech_raw = str(e.get("tech_stack", "")).strip()
        bullets = [str(b).strip() for b in (e.get("bullets") or []) if str(b).strip()]

        title_tex = latex_escape(title_raw)
        company_tex = latex_escape(company_raw)
        date_tex = latex_escape(date_raw)
        tech_tex = latex_escape(tech_raw)

        lines = []

        if title_tex or date_tex:
            l1 = ""
            if title_tex:
                l1 = f"\\textbf{{{title_tex}}}"
            if date_tex:
                l1 += r" \hfill " + date_tex
            l1 += r"\\"
            lines.append(l1)

        if company_tex or tech_tex:
            if tech_tex:
                lines.append(f"{company_tex} -- \\textbf{{Tech Stack:}} {tech_tex}\\")
            else:
                lines.append(company_tex + r"\\")
        if bullets:
            lines.append(r"\begin{itemize}")
            for b in bullets:
                lines.append(f"  \\item {latex_escape(b)}")
            lines.append(r"\end{itemize}")

        block = "\n".join(lines)
        if idx != len(exp_list) - 1 and block:
            block += "\n\n\\vspace{0.15em}\n"
        if block:
            blocks.append(block)

    return "\n\n".join(blocks)


def format_certifications(cert_list):
    cleaned = []
    for c in cert_list:
        s = str(c).strip()
        if not s:
            continue
        s = re.sub(r'^\s*certifications?\s*:\s*', '', s, flags=re.IGNORECASE)
        s = s.rstrip(",;")
        if s:
            cleaned.append(s)
    if not cleaned:
        return ""
    joined = ", ".join(cleaned)
    return r"\textbf{Certifications:} " + latex_escape(joined) + r"\\"


def format_extracurricular(ex_list):
    """
    If there are extracurricular activities, render them as:

      \textbf{Extra-Curricular:} item1; item2; item3\\

    If none, return empty string (so no line is shown).
    """
    items = [str(x).strip() for x in ex_list or [] if str(x).strip()]
    if not items:
        return ""
    joined = "; ".join(items)
    return r"\textbf{Extra-Curricular:} " + latex_escape(joined) + r"\\"


# ===================== COVER LETTER: FORMATTERS ===================== #

def build_cover_header_block(header: dict) -> str:
    """
    Name
    Location · phone · email
    GitHub · LinkedIn
    Portfolio  (next line, no blank gap)
    """
    def fix_url(u: str) -> str:
        if not u:
            return ""
        return re.sub(r"\s+", "", u.strip())

    name = (header.get("name") or "").strip() or "Your Name"
    location = (header.get("location") or "").strip()
    phone = (header.get("phone") or "").strip()
    email = (header.get("email") or "").replace(" ", "").strip()
    github_url = fix_url(header.get("github_url") or "")
    linkedin_url = fix_url(header.get("linkedin_url") or "")
    portfolio_url = fix_url(header.get("portfolio_url") or "")

    name_tex = latex_escape(name)
    lines = [rf"\textbf{{{name_tex}}}\\"]

    contact_parts = []
    if location:
        contact_parts.append(latex_escape(location))
    if phone:
        contact_parts.append(latex_escape(phone))
    if email:
        email_tex = latex_escape(email)
        contact_parts.append(rf"\href{{mailto:{email}}}{{{email_tex}}}")
    if contact_parts:
        lines.append(" $\\cdot$ ".join(contact_parts) + r"\\")

    link_parts = []
    if github_url:
        label = github_url.replace("https://", "").replace("http://", "").rstrip("/")
        link_parts.append(
            rf"GitHub: \href{{{github_url}}}{{{latex_escape(label)}}}"
        )
    if linkedin_url:
        label = linkedin_url.replace("https://", "").replace("http://", "").rstrip("/")
        link_parts.append(
            rf"LinkedIn: \href{{{linkedin_url}}}{{{latex_escape(label)}}}"
        )
    if link_parts:
        lines.append(" $\\cdot$ ".join(link_parts) + r"\\")

    if portfolio_url:
        label = portfolio_url.replace("https://", "").replace("http://", "").rstrip("/")
        lines.append(
            rf"Portfolio: \href{{{portfolio_url}}}{{{latex_escape(label)}}}"
        )

    return "\n".join(lines).strip()


def build_cover_company_block(letter: dict) -> str:
    company_name = (letter.get("company_name") or "").strip()
    company_line_2 = (letter.get("company_line_2") or "").strip()
    company_location = (letter.get("company_location") or "").strip()

    lines = ["Hiring Manager\\"]
    if company_name:
        lines.append(latex_escape(company_name) + r"\\")
    if company_line_2:
        lines.append(latex_escape(company_line_2) + r"\\")
    if company_location:
        lines.append(latex_escape(company_location))
    return "\n".join(lines).strip()


def build_cover_body_block(body_paragraphs):
    clean_paras = [p.strip() for p in (body_paragraphs or []) if p.strip()]
    plain = " ".join(clean_paras)
    wc = word_count(plain)
    escaped_paras = [latex_escape(p) for p in clean_paras]
    body_tex = "\n\n".join(escaped_paras)
    return body_tex, wc


def build_cover_closing_block(letter: dict, header: dict) -> str:
    closing = (letter.get("closing") or "Sincerely,").strip()
    sig_name = (letter.get("signature_name") or header.get("name") or "").strip() or "Your Name"
    return f"{latex_escape(closing)}\\\\\n{latex_escape(sig_name)}"


# ===================== LATEX / PDF HELPERS ===================== #

def write_resume_tex(path: Path,
                     header_tex: str,
                     summary_tex: str,
                     education_tex: str,
                     skills_tex: str,
                     certifications_tex: str,
                     extracurricular_tex: str,
                     projects_tex: str,
                     experience_tex: str):
    tex = (
        RESUME_TEX_TEMPLATE
        .replace("<<HEADER>>", header_tex)
        .replace("<<SUMMARY>>", summary_tex)
        .replace("<<EDUCATION>>", education_tex)
        .replace("<<SKILLS>>", skills_tex)
        .replace("<<CERTIFICATIONS>>", certifications_tex)
        .replace("<<EXTRACURRICULAR>>", extracurricular_tex)
        .replace("<<PROJECTS>>", projects_tex)
        .replace("<<EXPERIENCE>>", experience_tex)
    )
    path.write_text(tex, encoding="utf-8")
    print(f"- wrote resume LaTeX to {path}")


def compile_tex(tex_path: Path) -> Path:
    tex_file = tex_path.resolve()
    workdir = tex_file.parent
    tex_name = tex_file.name

    print(f"- compiling {tex_name} with pdflatex -")
    for i in range(2):
        print(f"  pdflatex pass {i+1}...")
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", tex_name],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0:
            print(result.stdout)
            raise RuntimeError("pdflatex returned non-zero exit code")

    pdf_path = workdir / (tex_file.stem + ".pdf")
    if not pdf_path.exists():
        raise RuntimeError("pdflatex finished but PDF not found.")
    print(f"- PDF generated at: {pdf_path}")
    return pdf_path


def check_max_two_pages(pdf_path: Path):
    """
    Optional page-count check – keeps resumes to 1–2 pages.
    """
    print("- checking page count with pdfinfo -")
    result = subprocess.run(
        ["pdfinfo", str(pdf_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout)
        raise RuntimeError("pdfinfo returned non-zero exit code")

    m = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
    if not m:
        raise RuntimeError("Could not find 'Pages' line in pdfinfo output.")

    pages = int(m.group(1))
    print(f"  PDF has {pages} page(s).")
    if pages > 2:
        raise RuntimeError(f"PDF has {pages} pages, expected at most 2.")
    if pages == 1:
        print("✅ Resume PDF is 1 page.")
    else:
        print("ℹ️ Resume PDF is 2 pages (allowed).")


# ===================== MAIN: ONE PROMPT → RESUME + COVER LETTER ===================== #

def main():
    # Always read the Gemini API key from gemini_api_key.txt in this folder.
    api_key = ""
    try:
        if GEMINI_KEY_FILE.exists():
            api_key = GEMINI_KEY_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"[resume_and_cover_maker] Failed to read {GEMINI_KEY_FILE}: {e}", flush=True)

    if not api_key:
        raise RuntimeError(
            f"Gemini API key not found in {GEMINI_KEY_FILE}.\n"
            "Create this file and put your Gemini API key on the first line."
        )


    if not RESUME_PDF_PATH.exists():
        raise FileNotFoundError(f"Resume PDF not found at: {RESUME_PDF_PATH}")
    if not JOB_DESC_PATH.exists():
        raise FileNotFoundError(f"Job description file not found at: {JOB_DESC_PATH}")

    print(f"- extracting text from PDF: {RESUME_PDF_PATH}")
    resume_plain = extract_text_from_pdf(RESUME_PDF_PATH).strip()

    # --- NEW: compute base section counts + 80% targets ---
    counts = estimate_resume_section_word_counts(resume_plain)

    def default_if_zero(v, default):
        return default if not v or v <= 0 else v

    # raw counts from original; if we fail to detect, use defaults
    summary_raw = default_if_zero(counts["summary"], 80)
    skills_raw = default_if_zero(counts["skills"], 120)
    experience_raw = default_if_zero(counts["experience"], 300)

    def to_80_percent(v: int) -> int:
        if v <= 0:
            return 1
        return max(1, int(round(v * 0.5)))

    summary_limit = to_80_percent(summary_raw)
    skills_limit = to_80_percent(skills_raw)
    experience_limit = to_80_percent(experience_raw)

    print(
        f"- base counts (summary/skills/experience): "
        f"{summary_raw} / {skills_raw} / {experience_raw}"
    )
    print(
        f"- 80% word targets passed to Gemini (summary/skills/experience): "
        f"{summary_limit} / {skills_limit} / {experience_limit}"
    )

    job_desc = JOB_DESC_PATH.read_text(encoding="utf-8").strip()

    print("- calling Gemini ONCE for resume + cover letter JSON ...")
    resume_data, cl_data = call_gemini_all(
        resume_text=resume_plain,
        job_description=job_desc,
        api_key=api_key,
        summary_word_limit=summary_limit,
        skills_word_limit=skills_limit,
        experience_word_limit=experience_limit,
    )

    # ----- Build resume content for PDF -----
    header_tex = format_resume_header(resume_data["header"])
    education_tex = format_education(resume_data["education"])  # ALL education printed here
    summary_tex = format_summary(resume_data["summary"])
    skills_tex = format_skills(resume_data["skills"])
    projects_tex = format_projects(resume_data["projects"])
    experience_tex = format_experience(resume_data["experience"])
    certifications_tex = format_certifications(resume_data["certifications"])
    extracurricular_tex = format_extracurricular(resume_data["extracurriculars"])

    write_resume_tex(
        RESUME_TEX_PATH,
        header_tex,
        summary_tex,
        education_tex,
        skills_tex,
        certifications_tex,
        extracurricular_tex,
        projects_tex,
        experience_tex,
    )

    resume_pdf = compile_tex(RESUME_TEX_PATH)
    try:
        check_max_two_pages(resume_pdf)
    except Exception as e:
        print(f"Page count check warning: {e}")
    print("✅ Tailored resume PDF generated.")

    # ----- Build cover letter -----
    cl_header = cl_data["header"]
    letter = cl_data["letter"]

    header_block = build_cover_header_block(cl_header)
    date_str = date.today().strftime("%B %d, %Y")  # always today's date
    date_line = latex_escape(date_str)
    company_block = build_cover_company_block(letter)
    salutation_line = latex_escape((letter.get("salutation") or "Dear Hiring Manager,").strip())

    body_tex, body_wc = build_cover_body_block(letter.get("body_paragraphs"))
    print(f"- cover letter body word count (should be <= 400): {body_wc}")
    if body_wc > 400:
        print("  WARNING: Gemini exceeded 400 words in cover letter body; consider trimming.")

    closing_block = build_cover_closing_block(letter, cl_header)

    cover_tex = (
        COVER_LETTER_TEX_TEMPLATE
        .replace("<<HEADER_BLOCK>>", header_block)
        .replace("<<DATE_LINE>>", date_line)
        .replace("<<COMPANY_BLOCK>>", company_block)
        .replace("<<SALUTATION>>", salutation_line)
        .replace("<<BODY_BLOCK>>", body_tex)
        .replace("<<CLOSING_BLOCK>>", closing_block)
    )

    COVER_LETTER_TEX_PATH.write_text(cover_tex, encoding="utf-8")
    print(f"- wrote cover letter LaTeX to {COVER_LETTER_TEX_PATH}")

    cl_pdf = compile_tex(COVER_LETTER_TEX_PATH)
    print(f"✅ Cover letter PDF generated at: {cl_pdf}")

    print("🎉 Done. One Gemini prompt, 80% word targets applied, resume + cover letter ready.")


if __name__ == "__main__":
    main()
