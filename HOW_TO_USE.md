# 🛠 How To Use

---

## 1️⃣ Setup Environment

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 2️⃣ Add Gemini API Key

Create file:

```bash
nano gemini_api_key.txt
```

Paste your key on **one line only**:

```text
AIzaxxxxxxxxxxxxxxxxxxxxxx
```

> This file is ignored by Git.

---

## 3️⃣ Run the Bot

```bash
python auto_apply.py
```

---

## 4️⃣ What Happens Next

- Browser opens
- Job cards are scanned
- Resume + cover letter generated
- Gemini guides UI interaction
- Application loops until submitted

---

## 5️⃣ Recording Manual Actions (Optional)

```bash
python record_web_actions_firefox.py --url "https://example.com" --out macro.json
```

Use this for:
- Complex dropdowns
- Multi-step forms
- Unusual layouts

---

## 6️⃣ Stop Anytime

Press `CTRL + C` safely.

---

## 🧪 Recommended Testing

- Start with `max_jobs = 1`
- Observe browser actions
- Increase once stable

---

Happy automating 🚀
