import os
import json
import subprocess
import re
import time
from pathlib import Path

from google import genai
from google.genai import types
import PyPDF2

# ===================== CONFIG ===================== #

MIN_PROJECTS = 3  # we want exactly 3 projects

# Paths (update RESUME_PDF_PATH to the resume you want to base on)
BASE_DIR = Path(__file__).parent
RESUME_PDF_PATH = BASE_DIR / "Ayush_Katiya_Resume.pdf"   # change if needed
JOB_DESC_PATH = BASE_DIR / "job.txt"
OUTPUT_TEX_PATH = BASE_DIR / "resume_generated.tex"


# ===================== LATEX TEMPLATE (YOUR FORMAT) ===================== #

TEX_TEMPLATE = r"""
\documentclass[8pt]{extarticle}

\usepackage[margin=0.5in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{needspace} % to keep blocks on the same page

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

% Keep the entire skills + certifications block together;
% if not enough space on this page, move all of it to the next page.
\needspace{10\baselineskip}

\resSection{TECHNICAL SKILLS AND CERTIFICATIONS}

<<SKILLS>>
<<CERTIFICATIONS>>

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
    return "".join(replacements.get(ch, ch) for ch in text)


# ===================== GEMINI CALL: ASK FOR EVERYTHING ===================== #

def call_gemini_sections(resume_text: str, job_description: str, api_key: str):
    """
    Gemini returns EVERYTHING in JSON:

    - header: {
        name, phone, email, github_url, linkedin_url, portfolio_url
      }
    - education: [
        { degree, institution, location, date, gpa },
        ...
      ]
    - summary: str
    - skills: [str]
    - projects: [ {title, timeframe, tools, bullets[3]} ]  (EXACTLY 3)
    - experience: [ {title, company, date, tech_stack, bullets[3]} ]
    - certifications: [str]
    """

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1alpha"),
    )

    prompt = f"""
You are rewriting and structuring an existing one-page resume so that it is
strongly optimized for the target JOB DESCRIPTION and suitable for LaTeX.

You are given the ORIGINAL resume (plain text from PDF) and the target job:

RESUME (plain text):
<<<RESUME_START>>>
{resume_text}
<<<RESUME_END>>>

JOB DESCRIPTION:
<<<JOB_START>>>
{job_description}
<<<JOB_END>>>

You must produce JSON ONLY with these keys:
  - "header": {{
      "name": "...",
      "phone": "...",
      "email": "...",
      "github_url": "...",
      "linkedin_url": "...",
      "portfolio_url": "..."
    }}
  - "education": [
      {{
        "degree": "...",
        "institution": "...",
        "location": "...",
        "date": "...",
        "gpa": "..."
      }},
      ...
    ]
  - "summary": "..."
  - "skills": [ "...", "...", ... ]
  - "projects": [ {{...}}, ... ]     // EXACTLY 3 projects
  - "experience": [ {{...}}, ... ]
  - "certifications": [ "...", "...", ... ]

HARD CONSTRAINTS:

1) HEADER (name, phone, email, GitHub URL, LinkedIn URL, portfolio URL)
- Copy these from the original resume.
- Only trim whitespace.
- Do NOT invent values that are not in the resume text.

2) EDUCATION OBJECTS
- For each degree, fill:
    - degree     : degree name as in resume (no rephrasing),
    - institution: university/college name as in resume,
    - location   : city/state/country as in resume,
    - date       : date range as in resume (e.g., "Aug 2021--May 2025"),
    - gpa        : GPA string as in resume (e.g., "9.8/10 GPA", "3.9 GPA").
- Do NOT invent or remove degrees.

3) CERTIFICATIONS
- Certification names and issuing orgs must match the resume.
- No new certifications, no deletions.
- Only light whitespace normalization allowed.

4) SUMMARY
- Formal, ATS-optimized, strongly aligned to the JOB DESCRIPTION.
- No fixed word limit, but concise enough for a one-page-style resume.

5) SKILLS
- Multiple lines like "Category: item1, item2, ...".
- Only skills/tools consistent with the resume.
- NO certifications here.
- Focus on tools and technologies relevant to the JOB DESCRIPTION.

6) PROJECTS
- Return EXACTLY {MIN_PROJECTS} projects (no more, no fewer).
- Each project object:
  {{
    "title": "Project title (<= 10 words)",
    "timeframe": "e.g., Jan 2025--Apr 2025 (optional)",
    "tools": "comma-separated tools/languages/frameworks used",
    "bullets": [
      "Bullet 1",
      "Bullet 2",
      "Bullet 3"
    ]
  }}
- Projects must be plausible given the resume (e.g., AI/ML, robotics, CV, data).
- Emphasize impact, metrics, and tools aligned with the JOB DESCRIPTION.

7) EXPERIENCE
- For every job in the resume, create an object:
  {{
    "title": "Job title exactly as in resume",
    "company": "Company + location exactly as in resume",
    "date": "Date range exactly as in resume",
    "tech_stack": "Comma-separated tools/languages/frameworks",
    "bullets": [
      "Bullet 1",
      "Bullet 2",
      "Bullet 3"
    ]
  }}
- tech_stack: tools from resume, prioritized for JOB DESCRIPTION relevance.
- Bullets: strong action verbs, impact, metrics, tools, ownership; 3 bullets per role.

8) CERTIFICATIONS
- List of lines describing certifications; same certification names as resume.

RULES:
- No LaTeX markup in any values.
- Do NOT include certifications in "skills".
- Return ONLY valid JSON (no markdown fences).
    """.strip()

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            raw = (response.text or "").strip()
            if raw.startswith("```"):
                parts = raw.split("```")
                if len(parts) >= 2:
                    raw = parts[1].lstrip("json").strip()

            data = json.loads(raw)

            required_keys = (
                "header",
                "education",
                "summary",
                "skills",
                "projects",
                "experience",
                "certifications",
            )
            if any(k not in data for k in required_keys):
                raise ValueError(f"Gemini JSON missing one of: {required_keys}")

            header = dict(data["header"])
            education = list(data["education"])
            summary = str(data["summary"])
            skills = [str(s) for s in data["skills"]]
            projects = list(data["projects"])
            experience = list(data["experience"])
            certifications = [str(c) for c in data["certifications"]]

            # safety: keep skills lines that don't mention "certif"
            filtered_skills = [ln for ln in skills if "certif" not in ln.lower()]

            # enforce at most 3 projects
            if len(projects) > MIN_PROJECTS:
                projects = projects[:MIN_PROJECTS]

            return {
                "header": header,
                "education": education,
                "summary": summary,
                "skills": filtered_skills,
                "projects": projects,
                "experience": experience,
                "certifications": certifications,
            }
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(2 * attempt)


# ===================== FORMAT FROM GEMINI → YOUR LATEX ===================== #

def format_header_from_gemini(header: dict) -> str:
    """
    Build LaTeX header from Gemini header object.
    Also fix weird 'github. com' or 'lin kedin.com' spacing inside URLs.
    """
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

    def make_label(url: str) -> str:
        if not url:
            return ""
        label = url.replace("https://", "").replace("http://", "").rstrip("/")
        return latex_escape(label)

    parts = []
    if phone_tex:
        parts.append(phone_tex)
    if email:
        parts.append(rf"\href{{mailto:{email}}}{{{email_tex}}}")
    if github_url:
        parts.append(rf"\href{{{github_url}}}{{{make_label(github_url)}}}")
    if linkedin_url:
        parts.append(rf"\href{{{linkedin_url}}}{{{make_label(linkedin_url)}}}")
    if portfolio_url:
        parts.append(rf"\href{{{portfolio_url}}}{{{make_label(portfolio_url)}}}")

    lines = [
        r"\begin{center}",
        f"  {{\\normalsize \\textbf{{{name_tex}}}}}\\\\[2pt]",
    ]
    if parts:
        lines.append("  " + " \\textbar{} ".join(parts))
    lines.append(r"\end{center}")
    return "\n".join(lines)


def format_education_from_gemini(education_list):
    r"""
    Format education like your example:

    \textbf{Degree} \hfill Date\\
    Institution, Location \hfill \textbf{GPA}\\[0.1em]
    """
    lines = []
    for edu in education_list:
        degree = (edu.get("degree") or "").strip()
        institution = (edu.get("institution") or "").strip()
        location = (edu.get("location") or "").strip()
        date = (edu.get("date") or "").strip()
        gpa = (edu.get("gpa") or "").strip()

        if not (degree or institution or location or date or gpa):
            continue

        deg_tex = latex_escape(degree)
        date_tex = latex_escape(date.replace("–", "--"))
        inst_loc = ", ".join(x for x in [institution, location] if x)
        inst_loc_tex = latex_escape(inst_loc)
        gpa_tex = latex_escape(gpa)

        # First line: Degree + date
        if deg_tex or date_tex:
            line1 = ""
            if deg_tex:
                line1 = f"\\textbf{{{deg_tex}}}"
            if date_tex:
                line1 += r" \hfill " + date_tex
            line1 += r"\\"
            lines.append(line1)

        # Second line: Institution, Location + GPA
        if inst_loc_tex or gpa_tex:
            if gpa_tex:
                line2 = f"{inst_loc_tex} \\hfill \\textbf{{{gpa_tex}}}\\\\[0.1em]"
            else:
                line2 = inst_loc_tex + r"\\[0.1em]"
            lines.append(line2)

        lines.append("")  # blank line between degrees

    return "\n".join(lines).strip()


def format_summary_to_latex(summary: str) -> str:
    """
    No word limit; just escape and use as is.
    """
    return latex_escape(summary.strip())


def format_skills_to_latex(skills_list):
    """
    Skills from Gemini (no word limit).
    Each line is either:
      Category: item1, item2, ...
    or plain text.
    """
    out_lines = []
    for raw in skills_list:
        line = str(raw).strip()
        if not line:
            continue

        if ":" in line:
            category, rest = line.split(":", 1)
            cat_tex = latex_escape(category.strip() + ":")
            rest_tex = latex_escape(rest.strip())
            out_lines.append(f"\\textbf{{{cat_tex}}} {rest_tex}\\\\")
        else:
            out_lines.append(latex_escape(line) + r"\\")
    return "\n".join(out_lines)


def format_projects_to_latex(projects_list):
    """
    Projects: list of dicts {title, timeframe, tools, bullets[3]}.
    No word limit; we just render them nicely.
    We already trimmed to at most 3 projects in call_gemini_sections.
    """
    blocks = []

    projects_list = list(projects_list or [])[:MIN_PROJECTS]

    for proj in projects_list:
        title_raw = str(proj.get("title", "")).strip()
        timeframe_raw = str(proj.get("timeframe", "")).strip()
        tools_raw = str(proj.get("tools", "")).strip()
        bullets = list(proj.get("bullets", []) or [])[:3]

        block_parts = []

        # Header: title + timeframe
        title_tex = latex_escape(title_raw) if title_raw else ""
        timeframe_tex = latex_escape(timeframe_raw) if timeframe_raw else ""
        if title_tex:
            header_line = f"\\textbf{{{title_tex}}}"
            if timeframe_tex:
                header_line += r" \hfill " + timeframe_tex
            header_line += r"\\"
            block_parts.append(header_line)

        # Tools/Languages line
        if tools_raw:
            tools_tex = latex_escape(tools_raw)
            block_parts.append(f"\\textbf{{Tools/Languages:}} {tools_tex}\\\\")

        # Bullets (max 3)
        if bullets:
            block_parts.append(r"\begin{itemize}")
            for b in bullets[:3]:
                b_raw = str(b).strip()
                if not b_raw:
                    continue
                b_tex = latex_escape(b_raw)
                block_parts.append(f"  \\item {b_tex}")
            block_parts.append(r"\end{itemize}")

        if block_parts:
            blocks.append("\n".join(block_parts))

    return "\n\n\\vspace{0.12em}\n\n".join(blocks)


def format_experience_from_gemini(exp_list):
    r"""
    PROFESSIONAL EXPERIENCE:

      \textbf{Job Title} \hfill Date\\
      Company -- \textbf{Tech Stack:} ...\\
      \begin{itemize} ... \end{itemize}

    No word limit; we render all bullets Gemini gives (usually 3).
    """
    blocks = []

    for idx, e in enumerate(exp_list):
        title_raw = str(e.get("title", "")).strip()
        company_raw = str(e.get("company", "")).strip()
        date_raw = str(e.get("date", "")).strip()
        tech_raw = str(e.get("tech_stack", "")).strip()
        bullets = [str(b).strip() for b in (e.get("bullets") or []) if str(b).strip()]

        title_tex = latex_escape(title_raw)
        company_tex = latex_escape(company_raw)
        date_tex = latex_escape(date_raw.replace("–", "--").replace(" - ", "--"))
        tech_tex = latex_escape(tech_raw)

        lines = []

        # Line 1: Job title + date
        if title_tex or date_tex:
            line1 = ""
            if title_tex:
                line1 = f"\\textbf{{{title_tex}}}"
            if date_tex:
                line1 += r" \hfill " + date_tex
            line1 += r"\\"
            lines.append(line1)

        # Line 2: Company + Tech Stack (highlighted)
        if company_tex or tech_tex:
            if tech_tex:
                lines.append(f"{company_tex} -- \\textbf{{Tech Stack:}} {tech_tex}\\")
            else:
                lines.append(company_tex + r"\\")

        # Bullets
        if bullets:
            lines.append(r"\begin{itemize}")
            for b in bullets:
                b_tex = latex_escape(b)
                lines.append(f"  \\item {b_tex}")
            lines.append(r"\end{itemize}")

        block = "\n".join(lines)
        if idx != len(exp_list) - 1 and block:
            block += "\n\n\\vspace{0.15em}\n"
        if block:
            blocks.append(block)

    return "\n\n".join(blocks)


def format_certifications_to_latex(cert_list):
    """
    Render certifications as ONE line:

      \textbf{Certifications:} cert1, cert2, cert3\\
    """
    cleaned_parts = []
    for c in cert_list:
        s = str(c).strip()
        if not s:
            continue
        # strip any leading "certifications:" Gemini might add
        s = re.sub(r'^\s*certifications\s*:\s*', '', s, flags=re.IGNORECASE)
        s = s.rstrip(",;")
        if s:
            cleaned_parts.append(s)

    if not cleaned_parts:
        return ""

    combined = ", ".join(cleaned_parts)
    return r"\textbf{Certifications:} " + latex_escape(combined) + r"\\"


# ===================== LATEX / PDF / WORD BUILD ===================== #

def write_latex_file(path: Path,
                     header_tex: str,
                     summary_tex: str,
                     education_tex: str,
                     skills_tex: str,
                     certifications_tex: str,
                     projects_tex: str,
                     experience_tex: str):
    tex = (
        TEX_TEMPLATE
        .replace("<<HEADER>>", header_tex)
        .replace("<<SUMMARY>>", summary_tex)
        .replace("<<EDUCATION>>", education_tex)
        .replace("<<SKILLS>>", skills_tex)
        .replace("<<CERTIFICATIONS>>", certifications_tex)
        .replace("<<PROJECTS>>", projects_tex)
        .replace("<<EXPERIENCE>>", experience_tex)
    )
    path.write_text(tex, encoding="utf-8")
    print(f"- wrote LaTeX to {path}")


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


def convert_tex_to_word(tex_path: Path) -> Path:
    """
    Convert the LaTeX resume to a Word .docx using pandoc.
    Requires pandoc installed on the system.
    """
    word_path = tex_path.with_suffix(".docx")
    print(f"- converting {tex_path.name} to Word (.docx) with pandoc -")
    result = subprocess.run(
        ["pandoc", str(tex_path), "-o", str(word_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        print("pandoc failed; Word (.docx) file not created. Output:\n")
        print(result.stdout)
        raise RuntimeError("pandoc returned non-zero exit code while creating .docx")
    print(f"- Word document generated at: {word_path}")
    return word_path


def check_max_two_pages(pdf_path: Path):
    """
    Ensure PDF is at most 2 pages using pdfinfo.

    - 1 page: ideal resume length.
    - 2 pages: allowed; skills + certifications block is kept together
      (and may appear entirely on page 2).
    - >2 pages: treat as error (content is too long).
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
        raise RuntimeError(f"PDF has {pages} pages, expected at most 2. Reduce content.")
    if pages == 1:
        print("✅ PDF is 1 page.")
    else:
        print("ℹ️ PDF is 2 pages; skills + certifications block will be on page 2 if needed.")


# ===================== MAIN PIPELINE ===================== #

def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set.\n"
            'Run:  export GEMINI_API_KEY="YOUR_REAL_KEY_HERE"'
        )

    if not RESUME_PDF_PATH.exists():
        raise FileNotFoundError(f"Resume PDF not found at: {RESUME_PDF_PATH}")
    if not JOB_DESC_PATH.exists():
        raise FileNotFoundError(f"Job description file not found at: {JOB_DESC_PATH}")

    print(f"- extracting text from PDF: {RESUME_PDF_PATH}")
    resume_plain = extract_text_from_pdf(RESUME_PDF_PATH).strip()

    job_desc = JOB_DESC_PATH.read_text(encoding="utf-8").strip()

    print("- calling Gemini for header, education, summary, skills, projects, experience, certifications -")
    g = call_gemini_sections(
        resume_text=resume_plain,
        job_description=job_desc,
        api_key=api_key,
    )

    # Build LaTeX blocks
    header_tex = format_header_from_gemini(g["header"])
    education_tex = format_education_from_gemini(g["education"])
    summary_tex = format_summary_to_latex(g["summary"])
    skills_tex = format_skills_to_latex(g["skills"])
    projects_tex = format_projects_to_latex(g["projects"])
    experience_tex = format_experience_from_gemini(g["experience"])
    certifications_tex = format_certifications_to_latex(g["certifications"])

    # Write LaTeX file
    write_latex_file(
        OUTPUT_TEX_PATH,
        header_tex,
        summary_tex,
        education_tex,
        skills_tex,
        certifications_tex,
        projects_tex,
        experience_tex,
    )

    # Compile to PDF and allow up to 2 pages
    pdf_path = compile_tex(OUTPUT_TEX_PATH)
    check_max_two_pages(pdf_path)

    # ALSO produce a Word (.docx) version via pandoc
    convert_tex_to_word(OUTPUT_TEX_PATH)

    print("✅ Done. Tailored resume generated in both PDF and Word formats.")


if __name__ == "__main__":
    main()
