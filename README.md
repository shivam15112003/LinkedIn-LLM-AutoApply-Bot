# 🚀 llm-linkedin-autoapply-bot

![Status](https://img.shields.io/badge/status-Active-green)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Automation](https://img.shields.io/badge/automation-Selenium%20%2B%20LLM-purple)

> **An intelligent LinkedIn job application assistant**  
> LLM-tailored resume & cover letter + LLM-guided UI interaction using safe click/type actions only.

---
## 📘 Demo & Presentation

- 🎬 **Watch the demo video (LinkedIn)** — see the bot in action (login → search → generate tailored resume → apply flow):  
  https://www.linkedin.com/posts/ss1511_artificialintelligence-python-generativeai-activity-7408413956794486784-3F5z?utm_source=share&utm_medium=member_desktop&rcm=ACoAADOiEBcBfeZohokyDTXwG0fo2BslAbJFwUk

- 📁 **Presentation (PPTX)**  
  I’ve uploaded the project presentation to this repository as:  
  `linkedin_auto_apply_bot_presentation.pptx`  
  (Open it to see slide-by-slide architecture, demo placeholder, and usage instructions.)

---

## ✨ What This Bot Does

- 🔧 **Generates & tailors resume & cover letter with LLM support**  
  - Starts from your base resume  
  - Uses Gemini (or compatible LLM) as a *backup brain* to rewrite bullet points, highlight impact, and inject the right keywords from the job description  
  - Produces recruiter-friendly, ATS-aware PDFs for each job
  - **Targets compact, high-impact documents:**  
    - Resume: typically 1–2 pages (≈ 400–800 words)  
    - Cover letter: 1 page (≈ 200–400 words)

- 🎯 **Detects Easy Apply vs External Apply** flows on LinkedIn

- 🧠 **LLM-backed UI interaction**
  - Looks at the current page (HTML + visible text + screenshot)
  - Asks the LLM for a small action plan: which button to click, what to type, when to wait
  - Uses **pixel coordinates of the button’s centre** for clicks, so it works even for icon-only / text-less buttons

- 🖱 Executes only **safe UI actions**:
  - `click` (via viewport pixel coords)
  - `type` (into specific fields)
  - `wait`

- 🔁 Repeats per page until:
  - A clear submission confirmation is detected on LinkedIn or the external portal, or
  - The safety limit of steps is reached

- 🛟 **Backup mechanism through LLM**
  - When regular heuristics cannot decide what to fill or which button to press, the bot falls back to Gemini for a one-shot plan
  - If Gemini is unsure or detects a security check, it stops and gives control back to you instead of doing something risky

> ⚠️ **Note:** CAPTCHAs / LinkedIn security checks are **never** automated.  
> The bot detects them, tells you, and waits for you to solve them manually.

---

## 📂 Repository Structure

```text
.
├── auto_apply.py                 # main bot (login, job loop, Easy/External apply)
├── gemini_actions.py             # Gemini/LLM helper utilities (if used separately)
├── resume_and_cover_maker2.py    # resume + cover-letter generation (LLM-backed)
├── record_web_actions_firefox.py # optional Firefox-based macro recorder
├── linkedin_auto_apply_bot_presentation.pptx  # project presentation (slides + demo)
├── README.md
├── METHODOLOGY.md
├── HOW_TO_USE.md
├── requirements.txt
└── gemini_api_key.txt            # Gemini key (local only, gitignored)
```

---

## ⚡ Quick Start

```bash
pip install -r requirements.txt
python auto_apply.py --resume-pdf YOUR_RESUME.pdf --applicant-json applicant_info.json --max-jobs 5
```

See [HOW_TO_USE.md](HOW_TO_USE.md) for full CLI examples and configuration.

---

## 🎯 LLM-Powered Tailoring for Recruiters & ATS

To increase your chances of selection:

- The bot can use Gemini to rewrite sections of your resume and cover letter **per job**:
  - Emphasise achievements and numbers (impact-focused bullets)
  - Mirror important keywords/skills from the job description
  - Adjust tone and seniority level (intern / junior / senior) while keeping facts true
  - Respect soft word-count guidelines so your documents stay concise and readable

- You still keep a **base resume** in your own style as the source of truth.  
  The LLM only reshapes and highlights what you already provide, so the final PDFs stay honest but more attractive to recruiters and ATS.

You can tune prompts (in `resume_and_cover_maker2.py` and Gemini helpers) to fit your personal brand.

---



## 🔒 Security & Keys

- The Gemini/LLM API key is stored **locally only** in: `gemini_api_key.txt`  
- That file is listed in `.gitignore` and **must never be committed**  
- If you export `GEMINI_API_KEY=...`, the code will:
  - use that value,
  - overwrite `gemini_api_key.txt` with the new key,
  - and use it for all LLM calls
- If no key is found (no env + no txt), the bot will:
  - prompt: *"No Gemini key found. Please paste Gemini key in terminal."*
  - save the key into `gemini_api_key.txt` for future runs.

---

## 📖 Extra Docs

- 🧠 [Methodology](METHODOLOGY.md) – how the LLM + Selenium flow is designed  
- 🛠 [How To Use](HOW_TO_USE.md) – setup, running, and tips

---

## ⚠️ Disclaimer

This project is for **educational / experimental** use only.

- Respect LinkedIn and external sites’ **Terms of Service**  
- Do **not** use this to spam applications  
- Do **not** attempt to bypass anti-bot measures or CAPTCHAs

---

Built with ❤️ to make repetitive job applications less painful —  
and with guardrails so humans stay in charge.
