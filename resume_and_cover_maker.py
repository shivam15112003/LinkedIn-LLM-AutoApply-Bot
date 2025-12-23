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
def extract_original_projects_with_badges(resume_text: str, max_projects: int = MIN_PROJECTS) -> list:
    """
    Extracts candidate project title lines and (optionally) a short badge
    that appears near the title in the original resume plain text.

    Heuristic:
    - Find the Projects section.
    - Walk lines, collect candidate title lines (non-bulleted, non-empty).
    - For each candidate title, look at the next 1-2 non-empty lines: if one
      is short (<= 60 chars), not a bullet, and looks like a label (few words,
      may contain parentheses or dashes), treat it as the badge (blue box).
    - Return list of dicts: [{"title": "...", "badge": "..."}] up to max_projects.
    """
    if not resume_text:
        return []

    lines = resume_text.splitlines()
    norm_lines = [_normalize_heading_line(l) for l in lines]

    project_heading_keys = {"ACADEMIC PROJECTS", "PROJECTS", "PERSONAL PROJECTS", "RELEVANT PROJECTS", "PROJECT"}
    all_heading_keywords = [
        "SUMMARY", "EDUCATION", "PROFESSIONAL EXPERIENCE", "EXPERIENCE", "WORK EXPERIENCE",
        "ACADEMIC PROJECTS", "PROJECTS", "PERSONAL PROJECTS", "TECHNICAL SKILLS",
        "SKILLS", "CERTIFICATIONS", "EXTRACURRICULAR", "AWARDS", "PUBLICATIONS"
    ]
    all_keys = {k.upper() for k in all_heading_keywords}

    # find start of projects section
    start_idx = None
    for i, nl in enumerate(norm_lines):
        if any(_heading_matches(nl, key) for key in project_heading_keys):
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    # find end of projects section
    end_idx = len(lines)
    for j in range(start_idx, len(lines)):
        nl = norm_lines[j]
        if not nl:
            continue
        if any(_heading_matches(nl, key) for key in all_keys if key not in project_heading_keys):
            end_idx = j
            break

    section_lines = [lines[i].rstrip() for i in range(start_idx, end_idx)]

    candidates = []
    i = 0
    while i < len(section_lines) and len(candidates) < max_projects:
        ln = section_lines[i].strip()
        if not ln:
            i += 1
            continue
        # skip bullet lines
        if re.match(r"^[-\u2022\*\d\.\)]+\s+", ln):
            i += 1
            continue
        # skip obvious meta lines like 'Tools:' or long narrative lines (>120 chars)
        if re.match(r"^(tools?|tech|tech stack|stack):", ln.strip().lower()):
            i += 1
            continue
        if len(ln) > 140:
            # probably paragraph text, skip
            i += 1
            continue

        # Candidate title found — look ahead 1-2 lines for a badge/label
        badge = ""
        look_ahead = 1
        for k in range(1, 3):
            if i + k >= len(section_lines):
                break
            next_ln = section_lines[i + k].strip()
            if not next_ln:
                continue
            # skip bullets
            if re.match(r"^[-\u2022\*\d\.\)]+\s+", next_ln):
                continue
            # if short enough and not a long sentence, treat as badge
            if len(next_ln) <= 60 and len(next_ln.split()) <= 8:
                # ignore if it clearly looks like "Tools: ..." or "Tech Stack"
                if not re.match(r"^(tools?|tech|tech stack|stack):", next_ln.strip().lower()):
                    badge = next_ln
                    break
            # otherwise not a badge
            break

        candidates.append({"title": ln, "badge": badge})
        i += 1 if badge == "" else (1 + k)  # skip past badge if we consumed it

    # Deduplicate by title text, preserve order, limit to max_projects
    seen = set()
    result = []
    for c in candidates:
        t = re.sub(r"\s+", " ", c["title"]).strip()
        if not t or t.lower() in seen:
            continue
        result.append({"title": t, "badge": c["badge"].strip() if c.get("badge") else ""})
        seen.add(t.lower())
        if len(result) >= max_projects:
            break

    return result

def extract_resume_summary_text(resume_text: str) -> str:
    """
    Extract the SUMMARY section text from the original resume (plain text).
    Uses the same heading-detection logic as estimate_resume_section_word_counts.

    Returns the raw text found under the SUMMARY heading, or an empty string
    if no SUMMARY section is detected.
    """
    if not resume_text:
        return ""

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

    lines = resume_text.splitlines()
    norm_lines = [_normalize_heading_line(l) for l in lines]

    # Target only the SUMMARY heading
    target_keys = ["SUMMARY"]
    target_keys = [k.upper() for k in target_keys if k]
    all_keys = [k.upper() for k in all_heading_keywords if k]

    # 1) find start of SUMMARY section
    start_idx = None
    for i, norm in enumerate(norm_lines):
        for key in target_keys:
            if _heading_matches(norm, key):
                start_idx = i + 1  # content is expected after the heading line
                break
        if start_idx is not None:
            break

    if start_idx is None:
        return ""

    # 2) find end of SUMMARY section (next heading)
    end_idx = len(lines)
    for j in range(start_idx, len(lines)):
        norm = norm_lines[j]
        if not norm:
            # skip blank lines when searching for next heading
            continue
        for key in all_keys:
            if _heading_matches(norm, key):
                end_idx = j
                break
        if end_idx != len(lines):
            break

    raw_section = "\n".join(lines[start_idx:end_idx]).strip()
    return raw_section

def normalize_http_url(url: str) -> str:
    """
    Clean up any GitHub-style URL and make sure it is a valid http(s) link.
    - strips whitespace
    - removes internal spaces
    - adds https:// if missing
    """
    if not url:
        return ""
    url = re.sub(r"\s+", "", str(url))  # remove spaces/newlines
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url



def merge_pdfs(resume_pdf: Path, cover_pdf: Path, output_path: Path) -> Path:
    """
    Merge resume and cover-letter PDFs into one combined PDF,
    while keeping the original PDFs unchanged.
    """
    writer = PyPDF2.PdfWriter()

    for pdf in (resume_pdf, cover_pdf):
        reader = PyPDF2.PdfReader(str(pdf))
        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    print(f"✅ Combined resume + cover letter PDF generated at: {output_path}")
    return output_path

def extract_links_from_pdf(pdf_path: Path):
    """
    Extract all hyperlink URLs from a PDF using PyPDF2 annotations.
    Returns a list of URLs as strings, preserving order and removing duplicates.
    """
    reader = PyPDF2.PdfReader(str(pdf_path))
    urls = []
    seen = set()

    for page in reader.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot in annots:
            obj = annot.get_object()
            if obj.get("/Subtype") == "/Link":
                action = obj.get("/A")
                if action and "/URI" in action:
                    uri = str(action["/URI"])
                    if uri and uri not in seen:
                        seen.add(uri)
                        urls.append(uri)
    return urls

# ===================== LATEX TEMPLATE: RESUME ===================== #

RESUME_TEX_TEMPLATE = r"""
\documentclass[8pt]{extarticle}

\usepackage[margin=0.5in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{xcolor}
\usepackage{setspace}
\usepackage{tikz}

% Tighter line spacing
\linespread{0.96}

% No paragraph indent, minimal extra vertical space
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}

% Tighter itemize spacing globally
\setlist[itemize]{leftmargin=*, itemsep=0.10em, topsep=0.15em}

% Remove page number
\pagestyle{empty}

% Compact section heading
\newcommand{\resSection}[1]{%
  \vspace{0.4em}%
  \textbf{\normalsize #1}\\[-0.35em]
  \rule{\textwidth}{0.3pt}\\[0.15em]
}

% ====================
% Badge command using TikZ: rounded box with blue border and white fill,
% with BLACK bold text inside.
% Usage: \badge{Your text here}
% ====================
\newcommand{\badge}[1]{%
  \tikz[baseline=(badge.base)]{
    \node[
      rounded corners=1.8pt,
      draw=blue!70,
      fill=white,
      text=black,
      font=\footnotesize\bfseries,
      inner xsep=6pt,
      inner ysep=2pt,
      line width=0.6pt
    ] (badge) {#1};
  }%
}

% ====================
% Tech-stack styling commands
% - Both label and list use \normalsize (same as the main title).
% - Label ("Tech Stack:") is bold; the list (contents) is normal weight.
% - \techstackproj used inline for projects (small spacing before)
% - \techstackexp used in experience lines
% ====================
\newcommand{\techstackproj}[1]{\hspace{0.45em}{\normalsize\textcolor{black}{\textbf{Tech Stack:}\ }\normalsize\textcolor{black}{\normalfont #1}}}
\newcommand{\techstackexp}[1]{{\normalsize\textcolor{black}{\textbf{Tech Stack:}\ }\normalsize\textcolor{black}{\normalfont #1}}}

\begin{document}

%==================== HEADER =====================

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

\resSection{TECHNICAL SKILLS AND CERTIFICATIONS}

<<SKILLS>>
<<CERTIFICATIONS>>
<<EXTRACURRICULAR>>

\end{document}
"""

# --------- Highlight important keywords in LaTeX text --------- #

# --------- Highlight important keywords in LaTeX text --------- #

HIGHLIGHT_KEYWORDS = [
    "Machine Learning",
    "Deep Learning",
    "Computer Vision",
    "Natural Language Processing",
    "NLP",
    "LLM",
    "Large Language Model",
    "Generative AI",
    "PyTorch",
    "TensorFlow",
    "Transformers",
    "Hugging Face",
    "OpenCV",
    "ROS",
    "ROS 2",
    "ONNX",
    "MLflow",
    "Optuna",
    "XGBoost",
    "scikit-learn",
    "SQL",
    "MLOps",
    "Docker",
    "Kubernetes",
    "AWS",
    "GCP",
    "Azure",
    "end-to-end",
    "production",
    "real-time",
    "scalable",
    "optimization",
    "pipeline",
    "F1",
    "precision",
    "recall",
    "accuracy",
    "CI/CD",
]

# Protect URLs/emails from being modified by the highlighter
PROTECTED_REGEX = re.compile(
    r"(https?://[^\s]+|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
)

def _highlight_segment(segment: str) -> str:
    """
    Apply keyword bolding to a text segment that does NOT contain
    URLs/emails (those are stripped out before calling this).
    """
    if not segment:
        return segment

    for kw in HIGHLIGHT_KEYWORDS:
        pattern = re.escape(kw)

        def repl(m):
            # Double \\textbf is harmless in LaTeX.
            return r"\textbf{" + m.group(0) + "}"

        segment = re.sub(pattern, repl, segment, flags=re.IGNORECASE)

    return segment


def highlight_keywords_latex(tex: str) -> str:
    """
    Wrap high-value keywords in \\textbf{...}.

    Input must already be LaTeX-escaped (no raw special characters).
    We intentionally avoid touching URL/email substrings so that they
    can later be safely wrapped in \\href{...}{...}.
    """
    if not tex:
        return tex

    parts = []
    last_end = 0
    for m in PROTECTED_REGEX.finditer(tex):
        # normal text before the protected token
        if m.start() > last_end:
            parts.append(_highlight_segment(tex[last_end:m.start()]))
        # the protected token itself (URL/email) is copied verbatim
        parts.append(m.group(0))
        last_end = m.end()

    # trailing text
    if last_end < len(tex):
        parts.append(_highlight_segment(tex[last_end:]))

    return "".join(parts)



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

# Detect URLs + emails in LaTeX-safe text
URL_REGEX = re.compile(r"(https?://[^\s{}]+)")
EMAIL_REGEX = re.compile(
    r"(?<!mailto:)([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
)

def _unescape_for_url(s: str) -> str:
    """
    Undo the minimal escaping we do in `latex_escape` so the hyperlink
    destination is a real URL/email, while the label stays LaTeX-escaped.
    """
    # Order matters for the multi-character sequences
    replacements = [
        (r"\textasciitilde{}", "~"),
        (r"\textasciicircum{}", "^"),
        (r"\_", "_"),
        (r"\%", "%"),
        (r"\#", "#"),
        (r"\&", "&"),
        (r"\$", "$"),
        (r"\{", "{"),
        (r"\}", "}"),
    ]
    for latex, plain in replacements:
        s = s.replace(latex, plain)
    return s


def auto_linkify_latex(tex: str) -> str:
    """
    Find raw http(s) URLs and plain email addresses in already
    LaTeX-escaped text and wrap them in \\href{...}{...} so they
    are clickable in the generated PDF.
    """
    if not tex:
        return tex

    def _url_repl(m: re.Match) -> str:
        url_tex = m.group(1)             # LaTeX-escaped URL (label)
        url_plain = _unescape_for_url(url_tex)  # real URL for destination
        return rf"\href{{{url_plain}}}{{{url_tex}}}"

    def _email_repl(m: re.Match) -> str:
        email_tex = m.group(1)
        email_plain = _unescape_for_url(email_tex)
        return rf"\href{{mailto:{email_plain}}}{{{email_tex}}}"

    # First URLs, then plain emails
    tex = URL_REGEX.sub(_url_repl, tex)
    tex = EMAIL_REGEX.sub(_email_repl, tex)
    return tex


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
    Keep project timeframes exactly as Gemini returns them.

    - If Gemini copies a timeframe from the resume, we keep it.
    - If timeframe is missing or Gemini returns placeholders like
      "None", "N/A", "NA", "null" or "-", we normalize it to "".
    - We DO NOT auto-create or guess any new timeframe values here.
    """
    cleaned = []
    for p in projects:
        p_copy = dict(p)
        tf = (p_copy.get("timeframe") or "").strip()
        if tf.lower() in ("none", "n/a", "na", "null", "-"):
            tf = ""
        p_copy["timeframe"] = tf
        cleaned.append(p_copy)
    return cleaned


# ===================== ONE GEMINI CALL: RESUME + COVER LETTER ===================== #

def call_gemini_all(
    resume_text: str,
    job_description: str,
    api_key: str,
    summary_word_limit=None,
    skills_word_limit=None,
    experience_word_limit=None,
    generate_summary: bool = True,
):
    """
    Single prompt that returns BOTH resume + cover letter JSON.

    If generate_summary==False, the prompt will instruct Gemini to return an
    empty string for the resume.summary field (so it does not generate one).
    """
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1alpha"),
    )

    # keep defaults as in original script (do NOT change word counts)
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

    # Build conditional summary instruction so Gemini doesn't generate it if not desired
    if generate_summary:
        summary_instruction = f"""
  * SUMMARY:
      - Target around {summary_limit} words in total for the "summary" string.
"""
    else:
        # Explicit, short instruction: do not spend tokens generating a summary.
        summary_instruction = """
  * SUMMARY:
      - DO NOT generate, rewrite, or invent a summary. Return the "summary"
        field as an empty string "" in the resume JSON. The calling code will
        reuse the original PDF summary if present.
"""

    # ---------- PROMPT (dates, GitHub, badges, ATS) ---------- #
    prompt = f"""
You are generating BOTH a tailored resume and a matching cover letter
for a specific job, using the candidate's resume as the source of truth.

Your primary goals:
- Maximize the candidate's chances of getting selected for interviews.
- Impress both ATS systems (keywords) and human recruiters.
- Keep everything truthful and consistent with the original resume.

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
      "date": "Use EXACTLY the same date/date-range text as in the original resume for this education entry if present. If the resume does not show a date for this entry, use an empty string ''. DO NOT invent new dates.",
      "gpa": "..."
    }},
    ...
  ],
{summary_instruction}
  "skills": [ "...", "...", ... ],
  "projects": [
    {{
      "title": "Project title (<= 10 words)",
      "timeframe": "For this project, copy the same date/date-range text from the original resume if it exists. If there is no date/time information in the resume for this project, use an empty string ''. DO NOT invent a timeframe.",
      "badge": "If the original resume already shows a badge/label/award text near this project (for example '2nd Place -- Hack SoDA 2024' or 'Top 5 -- OHack 2024'), copy that text here VERBATIM. If there is no such text, create a short 2–6 word descriptive label (for example 'LLM Automation Agent', 'Production CV Pipeline'). Do NOT invent fake awards.",
      "tools": "comma-separated tools/languages/frameworks used",
      "github_url": "If the original resume shows a GitHub repository link for this specific project, copy the full URL here. Otherwise, use an empty string ''. Do NOT invent new GitHub URLs.",
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
      "date": "If the original resume shows dates/date-range for this job, copy them here. If the resume does NOT show dates for this job, you MUST create a plausible, non-overlapping date range that fits the candidate's education and project timeline (for example 'Jun 2023--Dec 2023'). All invented date ranges for experience must be logically ordered and should not obviously clash with each other.",
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

  * SKILLS:
      - Across ALL strings in the "skills" list, target around {skills_limit} words
        in total (do NOT count certifications here).

  * PROFESSIONAL EXPERIENCE ("experience"):
      - Across ALL bullet strings in ALL entries of "experience", target around
        {experience_limit} words total.

- HEADER:
  * name, phone, email, github_url, linkedin_url, portfolio_url MUST come
    from the original resume. If a field is not present in the resume, use
    an empty string "" for that field. Do NOT invent new contact info.

- EDUCATION:
  * You MUST return EVERY education entry that appears in the resume.
  * You must NOT invent new education entries.
  * For "date", follow the rule above: copy only real resume dates, otherwise "".

- SKILLS (ATS & RECRUITER OPTIMIZATION):
  * You are allowed to CHANGE and REORGANIZE the skills to better match the JOB DESCRIPTION.
  * You may add or drop tools/skills as long as they are:
      - plausible given the candidate's background from the resume, AND/OR
      - clearly relevant to the JOB DESCRIPTION.
  * Strongly prioritize skills that appear in the JOB DESCRIPTION so that ATS systems
    and recruiters see clear keyword matches.
  * Do NOT invent completely unrelated skills that conflict with the candidate's profile.

- CERTIFICATIONS:
  * Names and issuing orgs must match the resume.
  * No new certifications, no deletions.
  * Do NOT put certifications inside "skills".

- PROJECTS:
  * RETURN EXACTLY {MIN_PROJECTS} PROJECTS (no more, no fewer).
  * Projects must be plausible given the resume (AI/ML, CV, data, robotics, etc.).
  * Each has 3 impact-focused bullets tailored to the JOB DESCRIPTION.
  * Bullets should use strong action verbs and, when possible, include concrete metrics.
  * For EVERY project:
      - "timeframe": copy only if dates exist in the original resume, otherwise "".
      - "badge": copy existing badge/award text if the resume shows any for that project;
        if not, create a short descriptive label (no fake awards).
      - "github_url": copy a real project repo URL if present; otherwise "".

- EXPERIENCE:
  * If the resume ALREADY has work experience:
      - For each job/experience entry, "title" and "company" MUST match the resume.
      - Bullets must be rewritten to be impact-focused and aligned with the JOB DESCRIPTION.
      - "date":
          - If the resume shows a date/date-range, copy it exactly.
          - If the resume does NOT show dates for that job, you MUST invent a plausible
            date range that is consistent with the candidate's education and project
            timeline and does not obviously clash with other experiences.

  * If the resume has NO work experience at all:
      - You MAY create 1–2 plausible professional experience entries that match
        the candidate's field (e.g., internships, part-time roles, research).
      - For these invented roles, you may invent company names and locations.
      - For every invented experience, "date" MUST be a logical, non-overlapping
        date range compatible with the education timeline.

- EXTRACURRICULARS:
  * If the original resume contains extracurricular / leadership / hackathons /
    clubs / volunteering, return them.
  * Otherwise, return [] for "extracurriculars".

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
  consistent with resume.header. If a field is not present, use "".

- BODY WORD LIMIT:
  The sum of all words across ALL strings in "body_paragraphs" must be
  AT MOST 400 words total.

- Cover letter content must be tailored to the JOB DESCRIPTION while remaining
  consistent with the resume. No bullet points, only sentences.

- "signature_name" must exactly match the candidate's full name from the resume.

GLOBAL RULES:
- If any field is unknown or not present in the resume, use an empty string ""
  instead of "None" or "N/A".
- Only plain text in all fields, no LaTeX/HTML/markdown.
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

            # --- Projects: keep dates from resume only + badge + GitHub cleanup ---
            projects = resume_obj["projects"]
            if len(projects) > MIN_PROJECTS:
                projects = projects[:MIN_PROJECTS]

            projects = fill_missing_project_timeframes(projects)

            default_badges = [
                "End-to-end ML System",
                "Production CV Pipeline",
                "LLM Automation Agent",
                "Robotics Control Stack",
                "Data Products Platform",
            ]

            for i, p in enumerate(projects):
                # badge: keep Gemini's if non-empty; otherwise fallback
                badge = (p.get("badge") or "").strip()
                if not badge:
                    p["badge"] = default_badges[i % len(default_badges)]
                else:
                    p["badge"] = badge

                # github_url: remove placeholder values
                github = (p.get("github_url") or "").strip()
                if github.lower() in ("none", "n/a", "na", "null", "-"):
                    github = ""
                p["github_url"] = github

            resume_obj["projects"] = projects

            # Cover letter paragraphs -> simple list of strings
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
        # clickable email hypertext
        parts.append(rf"\href{{mailto:{email}}}{{{email_tex}}}")
    if github_url:
        # clickable "github.com/username" text
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
    tex = latex_escape((summary or "").strip())
    tex = highlight_keywords_latex(tex)
    tex = auto_linkify_latex(tex)
    return tex




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
    skills_tex = "\n".join(lines)
    skills_tex = highlight_keywords_latex(skills_tex)
    skills_tex = auto_linkify_latex(skills_tex)
    return skills_tex



def format_projects(projects_list):
    r"""
    Render projects in the new style:

    \noindent\begin{tabular*}{\textwidth}{@{}l@{\extracolsep{\fill}}r@{}}
      \textbf{Title}\badge{Badge}\techstackproj{Tools}
      & \href{...}{\textbf{GitHub}} \\
    \end{tabular*}
    \vspace{0.08em}
    \begin{itemize}
      ...
    \end{itemize}
    """
    blocks = []
    projects_list = list(projects_list or [])[:MIN_PROJECTS]

    default_badges = [
        "End-to-end ML System",
        "Production CV Pipeline",
        "LLM Automation Agent",
        "Robotics Control Stack",
        "Data Products Platform",
    ]

    for idx, proj in enumerate(projects_list):
        title_raw = str(proj.get("title", "")).strip()
        timeframe_raw = (proj.get("timeframe") or "").strip()
        tools_raw = str(proj.get("tools", "")).strip()
        badge_raw = str(proj.get("badge", "")).strip()
        github_raw = str(proj.get("github_url", "")).strip()
        bullets = list(proj.get("bullets", []) or [])[:3]

        if not badge_raw:
            badge_raw = default_badges[idx % len(default_badges)]

        # Title (already bold), badge, tech stack
        title_tex = latex_escape(title_raw) if title_raw else ""

        badge_tex = ""
        if badge_raw:
            badge_tex = latex_escape(badge_raw)
            badge_tex = highlight_keywords_latex(badge_tex)
            badge_tex = auto_linkify_latex(badge_tex)

        tools_tex = ""
        if tools_raw:
            tools_tex = latex_escape(tools_raw)
            tools_tex = highlight_keywords_latex(tools_tex)
            tools_tex = auto_linkify_latex(tools_tex)

        timeframe_tex = latex_escape(timeframe_raw) if timeframe_raw else ""

        header_lines = []
        header_lines.append(
            r"\noindent\begin{tabular*}{\textwidth}{@{}l@{\extracolsep{\fill}}r@{}}"
        )

        # Left cell: title + badge + tech stack
        left_parts = []
        if title_tex:
            left_parts.append(r"\textbf{" + title_tex + "}")
        if badge_tex:
            left_parts.append(r"\badge{" + badge_tex + "}")
        if tools_tex:
            left_parts.append(r"\techstackproj{" + tools_tex + "}")
        left_cell = "".join(left_parts) if left_parts else ""

        # Right cell: clickable "GitHub" if we have a URL, otherwise timeframe
        right_cell = ""
        if github_raw:
            github_url = re.sub(r"\s+", "", github_raw)
            right_cell = rf"\href{{{github_url}}}{{\textbf{{GitHub}}}}"
        elif timeframe_tex:
            right_cell = timeframe_tex

        header_lines.append(f"  {left_cell} & {right_cell}\\\\")
        header_lines.append(r"\end{tabular*}")
        header_lines.append(r"\vspace{0.08em}")

        block_parts = header_lines

        # Bullets: escape -> highlight -> auto-linkify URLs
        if bullets:
            block_parts.append(r"\begin{itemize}")
            for b in bullets:
                b_raw = str(b).strip()
                if not b_raw:
                    continue
                b_tex = latex_escape(b_raw)
                b_tex = highlight_keywords_latex(b_tex)
                b_tex = auto_linkify_latex(b_tex)
                block_parts.append(f"  \\item {b_tex}")
            block_parts.append(r"\end{itemize}")

        if block_parts:
            blocks.append("\n".join(block_parts))

    return "\n\n\\vspace{0.12em}\n\n".join(blocks)



def format_experience(exp_list):
    r"""
    Render experience as:

    \textbf{Title} \hfill Date\\
    Company, Location -- \techstackexp{Tech stack}
    \begin{itemize}
      ...
    \end{itemize}
    """
    blocks = []
    for idx, e in enumerate(exp_list):
        title_raw = str(e.get("title", "")).strip()
        company_raw = str(e.get("company", "")).strip()
        date_raw = (e.get("date") or "").strip()
        tech_raw = str(e.get("tech_stack", "")).strip()
        bullets = [str(b).strip() for b in (e.get("bullets") or []) if str(b).strip()]

        # Normalize placeholder dates to empty
        if date_raw.lower() in ("none", "n/a", "na", "null", "-"):
            date_raw = ""

        title_tex = latex_escape(title_raw)
        company_tex = latex_escape(company_raw)
        date_tex = latex_escape(date_raw)
        tech_tex = highlight_keywords_latex(latex_escape(tech_raw)) if tech_raw else ""

        lines = []

        # First line: job title + date (if any)
        if title_tex or date_tex:
            l1 = ""
            if title_tex:
                l1 = f"\\textbf{{{title_tex}}}"
            if date_tex:
                l1 += r" \hfill " + date_tex
            l1 += r"\\"
            lines.append(l1)

        # Second line: company + tech stack macro
        if company_tex or tech_tex:
            if tech_tex:
                lines.append(f"{company_tex} -- \\techstackexp{{{tech_tex}}}\\\\")
            else:
                lines.append(company_tex + r"\\")

        # Bullets
        if bullets:
            lines.append(r"\begin{itemize}")
            for b in bullets:
                b_tex = latex_escape(b)
                b_tex = highlight_keywords_latex(b_tex)
                b_tex = auto_linkify_latex(b_tex)
                lines.append(f"  \\item {b_tex}")
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
    body = latex_escape(joined)
    body = auto_linkify_latex(body)
    return r"\textbf{Certifications:} " + body + r"\\"



def format_extracurricular(ex_list):
    """
    If there are extracurricular activities, render them as:

      \textbf{Extra-Curricular:} item1; item2; item3\\
    """
    items = [str(x).strip() for x in ex_list or [] if str(x).strip()]
    if not items:
        return ""
    joined = "; ".join(items)
    body = latex_escape(joined)
    body = auto_linkify_latex(body)
    return r"\textbf{Extra-Curricular:} " + body + r"\\"



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
    github_url = normalize_http_url(header.get("github_url") or "")
    linkedin_url = normalize_http_url(header.get("linkedin_url") or "")
    portfolio_url = normalize_http_url(header.get("portfolio_url") or "")

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
    """
    Build the main body of the cover letter.

    - Joins the paragraphs to compute a word count.
    - Escapes LaTeX characters.
    - Highlights important AI/ML/ATS keywords by wrapping them in \textbf{...}.
    - Auto-linkifies URLs/emails in place.
    """
    clean_paras = [p.strip() for p in (body_paragraphs or []) if p.strip()]
    plain = " ".join(clean_paras)
    wc = word_count(plain)

    escaped_paras = []
    for p in clean_paras:
        t = latex_escape(p)
        t = highlight_keywords_latex(t)
        t = auto_linkify_latex(t)
        escaped_paras.append(t)

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

# ===================== MAIN: ONLY COVER LETTER, KEEP ORIGINAL RESUME PDF ===================== #

def main():
    # 1) Load Gemini API key
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

    # 2) Check input files
    if not RESUME_PDF_PATH.exists():
        raise FileNotFoundError(f"Resume PDF not found at: {RESUME_PDF_PATH}")
    if not JOB_DESC_PATH.exists():
        raise FileNotFoundError(f"Job description file not found at: {JOB_DESC_PATH}")

    # 3) Extract original resume text + links (we will KEEP this PDF as‑is)
    print(f"- extracting text from PDF: {RESUME_PDF_PATH}")
    resume_plain = extract_text_from_pdf(RESUME_PDF_PATH).strip()

    try:
        resume_links = extract_links_from_pdf(RESUME_PDF_PATH)
    except Exception as e:
        print(f"Warning: failed to extract hyperlink URLs from PDF: {e}")
        resume_links = []

    def first_matching(urls, predicate):
        for u in urls:
            try:
                if predicate(u):
                    return u
            except Exception:
                continue
        return ""

    # Profile links from original resume PDF
    github_from_pdf = first_matching(resume_links, lambda u: "github.com" in u.lower())
    linkedin_from_pdf = first_matching(resume_links, lambda u: "linkedin.com" in u.lower())

    # Portfolio = first http(s) link that is not GitHub/LinkedIn
    portfolio_from_pdf = ""
    for u in resume_links:
        low = u.lower()
        if "github.com" in low or "linkedin.com" in low:
            continue
        if u.startswith("http"):
            portfolio_from_pdf = u
            break

    # 4) Compute section word counts -> 80% targets (for Gemini prompt)
    counts = estimate_resume_section_word_counts(resume_plain)

    def default_if_zero(v, default):
        return default if not v or v <= 0 else v

    summary_raw = default_if_zero(counts["summary"], 80)
    skills_raw = default_if_zero(counts["skills"], 120)
    experience_raw = default_if_zero(counts["experience"], 300)

    def to_80_percent(v: int) -> int:
        if v <= 0:
            return 1
        return max(1, int(round(v * 0.75)))

    summary_limit = to_80_percent(summary_raw)
    skills_limit = to_80_percent(skills_raw)
    experience_limit = to_80_percent(experience_raw)

    print(
        f"- base counts (summary/skills/experience): "
        f"{summary_raw} / {skills_raw} / {experience_raw}"
    )
    print(
        f"- 75–80% word targets passed to Gemini (summary/skills/experience): "
        f"{summary_limit} / {skills_limit} / {experience_limit}"
    )

    job_desc = JOB_DESC_PATH.read_text(encoding="utf-8").strip()

    # 5) Check if original resume actually has a SUMMARY
    original_summary_text = extract_resume_summary_text(resume_plain)
    has_original_summary = bool(original_summary_text and original_summary_text.strip())
    generate_summary_flag = has_original_summary  # only let Gemini touch summary if one already exists

    print(f"- original SUMMARY present: {has_original_summary} -> generate_summary={generate_summary_flag}")

    # 6) Call Gemini ONCE for JSON (resume + cover letter),
    # but we will only turn the COVER LETTER into a PDF.
    print("- calling Gemini ONCE for resume + cover letter JSON ...")
    try:
        # Newer call_gemini_all with generate_summary flag
        resume_data, cl_data = call_gemini_all(
            resume_text=resume_plain,
            job_description=job_desc,
            api_key=api_key,
            summary_word_limit=summary_limit,
            skills_word_limit=skills_limit,
            experience_word_limit=experience_limit,
            generate_summary=generate_summary_flag,
        )
    except TypeError:
        # Fallback for older signature without generate_summary
        print("⚠️ call_gemini_all() has no 'generate_summary' parameter, calling without it.")
        resume_data, cl_data = call_gemini_all(
            resume_text=resume_plain,
            job_description=job_desc,
            api_key=api_key,
            summary_word_limit=summary_limit,
            skills_word_limit=skills_limit,
            experience_word_limit=experience_limit,
        )

    # 7) Respect user's SUMMARY preference (no new summary if none in original)
    if has_original_summary:
        resume_data["summary"] = original_summary_text.strip()
        print("- preserved original SUMMARY from PDF in resume_data['summary']")
    else:
        # Make sure summary is effectively "off"
        summary_val = (resume_data.get("summary") or "").strip()
        if summary_val:
            resume_data["summary"] = ""
            print("- original PDF had NO SUMMARY; cleared Gemini summary to avoid generating one")
        else:
            resume_data["summary"] = ""

    # 8) Force header hyperlinks to match original resume PDF and copy into cover-letter header
    header = resume_data.get("header", {}) or {}

    def prefer_pdf(existing: str, pdf_url: str) -> str:
        """
        Prefer real URL from the original PDF; strip obvious placeholders.
        """
        existing = (existing or "").strip()
        pdf_url = (pdf_url or "").strip()
        PLACEHOLDERS = {
            "github", "githublink", "github link",
            "linkedin", "linkedinlink", "linkedin link",
            "portfolio", "portfoliolink", "portfolio link",
        }
        if existing.lower() in PLACEHOLDERS:
            existing = ""
        return pdf_url or existing

    header["github_url"] = prefer_pdf(header.get("github_url"), github_from_pdf)
    header["linkedin_url"] = prefer_pdf(header.get("linkedin_url"), linkedin_from_pdf)
    header["portfolio_url"] = prefer_pdf(header.get("portfolio_url"), portfolio_from_pdf)

    # Preserve any *extra* links from the original resume in header["extra_links"]
    header_links_set = {
        v.strip()
        for v in (
            header.get("github_url"),
            header.get("linkedin_url"),
            header.get("portfolio_url"),
        )
        if v
    }
    extra_links = [u for u in resume_links if u not in header_links_set]
    header["extra_links"] = extra_links
    resume_data["header"] = header

    # Copy clean header into COVER LETTER header
    cl_header = cl_data.get("header", {}) or {}
    for key in ("name", "phone", "email", "github_url", "linkedin_url", "portfolio_url"):
        cl_header[key] = header.get(key, "")
    cl_header.pop("extra_links", None)  # not needed in cover letter
    cl_data["header"] = cl_header

    # 9) Build COVER LETTER ONLY (no Gemini resume PDF)
    letter = cl_data["letter"]

    header_block = build_cover_header_block(cl_header)
    date_str = date.today().strftime("%B %d, %Y")
    date_line = latex_escape(date_str)
    company_block = build_cover_company_block(letter)
    salutation_line = latex_escape((letter.get("salutation") or "Dear Hiring Manager,").strip())

    body_tex, body_wc = build_cover_body_block(letter.get("body_paragraphs"))
    print(f"- cover letter body word count (should be <= 400): {body_wc}")
    if body_wc > 400:
        print("  WARNING: Gemini exceeded ~400 words in cover-letter body; consider trimming manually.")

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

    # 10) MERGE: ORIGINAL resume PDF + generated cover letter PDF
    #     (no Gemini-generated resume PDF at all)
    resume_pdf = RESUME_PDF_PATH  # original file, unchanged
    combined_pdf = BASE_DIR / "resume_and_cover_letter_combined.pdf"
    merge_pdfs(resume_pdf, cl_pdf, combined_pdf)

    print("🎉 Done.")
    print("   - Resume in the combined PDF is the ORIGINAL resume PDF (not generated by Gemini).")
    print(f"   - Cover letter is generated by Gemini.")
    print(f"   - Combined file: {combined_pdf}")


if __name__ == "__main__":
    main()
