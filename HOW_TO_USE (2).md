# 🛠 How To Use

---

## 1️⃣ Setup Environment

From your project root:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Make sure you have:

- Chrome/Chromium (or Firefox) installed
- The matching WebDriver (e.g., `chromedriver` or `geckodriver`) on your PATH

---

## 2️⃣ Add Gemini / LLM API Key

You can provide the key in **two ways**:

### Option A – Environment variable (recommended for first time)

```bash
export GEMINI_API_KEY="YOUR_REAL_GEMINI_KEY"
```

When you run the bot, it will:

- Use this key,
- Save it into `gemini_api_key.txt` in the project root,
- So future runs work even without the env var.

### Option B – `gemini_api_key.txt` directly

Create the file manually:

```bash
nano gemini_api_key.txt
```

Paste your key on **one line only**:

```text
AIzaxxxxxxxxxxxxxxxxxxxxxx
```

> The file `gemini_api_key.txt` is **gitignored** and should never be committed.

If neither env var nor txt file is present, the code will:

- Speak: “No Gemini key found. Please paste Gemini key in terminal.”
- Ask you once in the terminal,
- Save it to `gemini_api_key.txt` for future runs.

---

## 3️⃣ Prepare Inputs

- A base resume PDF, e.g. `ASU_Resume_Template_Shivam.pdf`
- An applicant profile JSON, e.g. `applicant_info.json`, containing your details used
  in form filling and text generation.

The LLM can then:

- Tailor your resume bullets & sections for each job,
- Draft or refine a cover letter that reflects the job description,
- While still staying true to your original experience,
- And stay within **friendly word ranges**:
  - Resume: ~400–800 words total
  - Cover letter: ~200–400 words

Example `applicant_info.json` structure (simplified):

```jsonc
{
  "name": "Your Name",
  "email": "you@example.com",
  "phone": "+1 555 123 4567",
  "location": "Worldwide",
  "skills": ["Python", "Machine Learning", "Robotics"],
  "experience": [ /* ... */ ]
}
```

Adapt this to whatever your current `auto_apply.py` expects.

---

## 4️⃣ Run the Bot

Basic run:

```bash
python auto_apply.py   --resume-pdf ASU_Resume_Template_Shivam.pdf   --applicant-json applicant_info.json   --max-jobs 2
```

Common flags (your script may include more):

- `--resume-pdf PATH` – base resume PDF to tailor per job
- `--applicant-json PATH` – applicant info JSON
- `--max-jobs N` – how many jobs to attempt in this run

On each job, the bot will:

1. Open the job on LinkedIn
2. Decide Easy Apply vs External Apply
3. Generate LLM‑backed tailored resume + cover letter for that job
4. Use LLM‑guided `click`/`type`/`wait` actions where needed
5. Check for submission confirmation text on LinkedIn or the external portal

---

## 5️⃣ Handling CAPTCHAs / Security Checks

If LinkedIn shows a **security check / CAPTCHA**:

- The bot will detect this,
- Tell you (via logs and optional speech),
- And pause so you can **solve it manually** in the browser.

Once you complete the challenge and reach your home / jobs page again, the bot continues.

---

## 6️⃣ Recording Manual Actions (Optional)

You can capture complex manual workflows using the Firefox recorder:

```bash
python record_web_actions_firefox.py --url "https://example.com" --out macro.json
```

This is useful when:

- Forms use very custom widgets,
- You want to replay a specific sequence of UI events yourself,
- Or want an example for improving Gemini prompts later.

---

## 7️⃣ Stopping Safely

Press:

```text
CTRL + C
```

in the terminal. The bot will try to close the browser and exit cleanly.

---

## 8️⃣ Recommended Workflow (to impress recruiters, not just bots)

1. Start with `--max-jobs 1` and **watch** what the browser does.
2. Review the generated resume + cover letter for the first few jobs:
   - Are the bullets clear and impact‑focused?
   - Are the right keywords from the job description included?
   - Are they staying within a compact word range (≈ 400–800 for resume, 200–400 for cover letter)?
3. Tweak your prompts / templates if needed.
4. Once you’re happy with how it “sounds like you”, increase `--max-jobs` gradually.
5. Periodically check LinkedIn and external portals to confirm applications are being submitted correctly.

---

Happy automating 🚀  
…and may your tailored resumes catch the right recruiter eyes.
