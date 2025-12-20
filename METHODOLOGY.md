# 🧠 Methodology

This project uses a **hybrid automation + reasoning approach**.

---

## 1️⃣ Local Resume & Cover Letter Generation

- Resume and cover letter are generated **before** any LLM calls.
- This keeps private data local and reduces API usage.
- Cover letter is optional and merged with resume when needed.

---

## 2️⃣ Application Flow Detection

- If button text contains **“Easy Apply”** → Easy Apply flow.
- Otherwise → External company site flow.

---

## 3️⃣ Single-Prompt Gemini Decision Model

For every page state, the bot sends Gemini:

- Page HTML (trimmed)
- Visible text
- Screenshot (optional)

Gemini responds with **one JSON plan**:

```json
{
  "upload_choice": "resume | cover_letter | none",
  "actions": [
    { "type": "click", "selector": "..." },
    { "type": "type", "selector": "...", "text": "..." }
  ]
}
```

---

## 4️⃣ Allowed Actions (Strict)

Gemini is limited to:

- `click`
- `type`
- `scroll`
- `wait`

❌ No keyboard presses  
❌ No system actions  

---

## 5️⃣ Upload Strategy

| Gemini Decision | Bot Uploads                      |
|-----------------|----------------------------------|
| resume          | merged resume + cover letter    |
| cover_letter    | cover letter only               |
| none            | skip upload                     |

---

## 6️⃣ Loop Until Submission

- The bot stays on the job until:
  - LinkedIn shows confirmation, OR
  - Gemini confirms submission, OR
  - Safety limit is reached

The UI is **never auto-closed**.

---

## 7️⃣ Fail-Safe Recovery

If stuck:
- Gemini suggests mouse & typing recovery steps
- Bot executes them sequentially

---

## 🎯 Result

A robust, explainable, and safe automation pipeline.
