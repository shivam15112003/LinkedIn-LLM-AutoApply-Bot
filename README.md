# 🚀 linkedin-autoapply-bot

![Status](https://img.shields.io/badge/status-Active-green)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Automation](https://img.shields.io/badge/automation-Selenium%20%2B%20Gemini-purple)
![License](https://img.shields.io/badge/license-MIT-green)

> **An intelligent LinkedIn job application automation system**  
> Local resume & cover letter generation + Gemini-guided UI interaction.

---

## ✨ What This Bot Does

✔ Generates **tailored resume & cover letter locally**  
✔ Detects **Easy Apply vs External Apply**  
✔ Sends **live webpage snapshot to Gemini**  
✔ Gemini decides:
- What to upload (resume / cover letter / none)
- What to click, type, scroll — **in one prompt**
✔ Executes only safe UI actions:
- `click`
- `type`
- `scroll`
- `wait`
✔ Loops until application is **submitted or confirmed**

---

## 📂 Repository Structure

```text
.
├── auto_apply.py
├── gemini_actions.py
├── resume_and_cover_maker.py
├── record_web_actions.py
├── README.md
├── METHODOLOGY.md
├── HOW_TO_USE.md
├── requirements.txt
└── gemini_api_key.txt  (ignored)
```

---

## ⚡ Quick Start

```bash
pip install -r requirements.txt
python auto_apply.py
```

---

## 🔒 Security

- API key stored **only** in `gemini_api_key.txt`
- File is ignored by Git
- Environment variables are NOT used

---

## 📖 Documentation

- 👉 [Methodology](METHODOLOGY.md)  
- 👉 [How To Use](HOW_TO_USE.md)

---

## ⚠️ Disclaimer

Educational & experimental use only.  
Respect LinkedIn and external site Terms of Service.

---

Built with ❤️ for automation research.
