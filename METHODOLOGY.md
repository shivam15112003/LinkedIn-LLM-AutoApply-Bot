# 🧠 Methodology

This project uses a **hybrid automation + reasoning approach**:
Selenium controls the browser, and an LLM (Gemini) suggests safe click/type steps and helps tailor your documents when the page or job description is confusing.

---

## 1️⃣ LLM‑Backed Resume & Cover Letter Generation

- You provide a **base resume** and optional profile JSON.
- The bot can call Gemini as a **backup writer** to:
  - Rewrite bullets to be more impact‑focused and recruiter‑friendly
  - Pull in key skills and phrases from the job description
  - Draft or refine a cover letter that speaks directly to the role
- The final resume & cover letter PDFs are rendered locally, and you retain full control
  over the source data.

To keep things recruiter‑friendly and easy to skim, prompts and templates are designed around **soft word limits**:

- Resume: typically 1–2 pages (≈ **400–800 words**), with punchy bullet points
- Cover letter: a single page (≈ **200–400 words**), focused on why you fit this specific role

This combination gives you:
- A stable base CV (your original document), plus
- A **job‑specific tailored version** that can stand out more for recruiters and ATS.

---

## 2️⃣ Application Flow Detection

- If the job has an **“Easy Apply”** button → run the LinkedIn Easy Apply flow.
- Otherwise → open the **company / external apply** link in a new tab and work there.

In both cases, the bot tries to follow the natural flow of the site, page by page.

---

## 3️⃣ Single‑Prompt LLM Decision Model (per page)

For each tricky or ambiguous page, the bot captures:

- **Visible text** from the page
- **HTML snippet** of the layout
- **Screenshot** (where supported)

It then sends a tightly‑scoped prompt to Gemini asking for a small plan:

```jsonc
{
  "actions": [
    {
      "type": "click",
      "x": 540,
      "y": 420,
      "wait": 0.5
    },
    {
      "type": "type",
      "by": "css",
      "selector": "#phone-number",
      "text": "+1 555 123 4567",
      "clear": true,
      "wait": 0.3
    },
    {
      "type": "wait",
      "seconds": 1.0
    }
  ],
  "comment": "Click the Next button, then fill the phone number field."
}
```

This serves as a **backup mechanism** when simple rules are not enough.

---

## 4️⃣ Allowed Actions (Strict)

The LLM is allowed to produce **only three** action types:

- `click` – at viewport pixel coordinates `(x, y)` representing the **centroid** of a clickable element (buttons, icons, etc.)
- `type` – type text into a specific field by selector or into the focused element
- `wait` – pause for a time so the UI can update

❌ **Not allowed from LLM:**
- scrolling the page  
- keyboard presses like ENTER, TAB, ESC  
- arbitrary JavaScript or system actions

This keeps the automation simple, predictable, and much safer.

---

## 5️⃣ Coordinate‑Based Clicks (Centroid)

Instead of trying to guess CSS selectors for every possible button shape/text, the LLM is asked to:

- Visually locate the relevant button / icon / checkbox in the screenshot
- Return the **viewport pixel coordinates** of the element’s centre
- The bot then uses `document.elementFromPoint(x, y)` to:
  - find the element at that pixel,
  - scroll it into view,
  - and dispatch a real click event.

Selectors are used mainly for typing into fields, where IDs/XPaths are more stable.

---

## 6️⃣ Upload Strategy

For upload controls (resume / CV / cover letter), the LLM chooses:

| LLM Decision   | Bot Uploads                               |
|----------------|-------------------------------------------|
| `resume`       | merged resume + cover‑letter PDF         |
| `cover_letter` | cover‑letter‑only PDF                    |
| `none`         | no upload this step                      |

These decisions are made based on the current page’s text and HTML.

---

## 7️⃣ Loop Until Submission (with Safeguards)

Per job, the bot:

1. Navigates through each form/page, using local heuristics + LLM actions.
2. After each step, checks for **“Application submitted”** or similar phrases on:
   - LinkedIn confirmation UI, or
   - External company portals
3. Stops when:
   - Submission phrase is detected, or
   - A maximum step count is reached (safety guard).

The bot **never force‑closes** an application modal/tab on its own; it leaves it visible for inspection.

---

## 8️⃣ Fail‑Safe Recovery & CAPTCHAs

If the bot is stuck on a normal form page:

- It asks Gemini for a small sequence of `click`/`type`/`wait` actions
- Executes them in order with cautious time gaps

If Gemini reports that:

- the page looks like a **security check / CAPTCHA**, or
- it is genuinely **unsure** how to proceed safely,

then the bot:

- Notifies you (optionally via speech), and
- Stops trying to automate that step, so **you** can complete it manually.

No CAPTCHAs or LinkedIn security checks are ever automated.

---

## 🎯 Result

You get a **robust, explainable, and human‑in‑the‑loop** automation pipeline that:

- Uses LLMs to tailor your resume & cover letters for each role,
- Boosts recruiter appeal and keyword coverage within sensible word limits,
- And still keeps you in control for all critical decisions.
