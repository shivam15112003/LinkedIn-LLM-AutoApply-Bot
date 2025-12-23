#!/usr/bin/env python3
"""
auto_apply.py

Semi-automatic LinkedIn job applying script that:

1. Logs into your LinkedIn account.
2. Searches for AI / Robotics / ML jobs.
3. For each job:
   - Collects the job description and writes it to a local text file.
   - Calls your existing `resume_and_cover_maker.py` script to generate
     a tailored resume + cover letter for that job.
   - Tries to apply either via **Easy Apply** on LinkedIn or via
     **External Apply** on the company site.

NEW (Gemini-assisted forms):
- Extracts the job application form fields (questions + options)
  from the current step.
- Sends a JSON payload to Gemini that includes:
    * your resume text (from the base PDF),
    * your applicant_info.json (profile),
    * the job description text,
    * a JSON description of the current form (labels, options, etc.).
- Gemini returns a JSON with which options to select or what text to write.
- The script uses those answers to fill the form, then clicks Next/Submit.
- For every step, it saves a `*.txt` file with all Gemini answers for
  that job application step under `form_answers/`.

IMPORTANT:
- This code is for personal/educational use only.
- You MUST review LinkedIn’s and external job sites’ Terms of Service,
  and only use this in ways they permit.
- You are responsible for the correctness and honesty of all answers,
  especially legal/immigration questions (citizenship, visa, etc.).
- For those legal/identity questions, Gemini is instructed to use ONLY
  the values you provide in applicant_info.json and never guess.

Dependencies (install via pip):

  pip install selenium google-genai PyPDF2

…and you still need LaTeX + pandoc for resume_and_cover_maker.py.

Environment variables required:

  export LINKEDIN_EMAIL="you@example.com"
  export LINKEDIN_PASSWORD="yourLinkedInPassword"
  export GEMINI_API_KEY="your-gemini-api-key"

Usage example:

  python auto_apply.py \
      --resume-pdf ./Shivam_Sharma_Resume.pdf \
      --applicant-json ./applicant_info.json \
      --max-jobs 2 \
      --headless
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
)

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.common.action_chains import ActionChains
from google import genai
from google.genai import types
from selenium.webdriver.common.action_chains import ActionChains
from typing import Tuple

# Make sure this file lives in the same folder as resume_and_cover_maker.py
import resume_and_cover_maker as rcm
from gemini_actions import call_gemini_for_actions, resolve_gemini_api_key_from_env_or_disk
import random  # used for fast random filling in two-pass Easy Apply

# ======================= Gemini browser recovery helpers =======================
# --- Gemeni-based "ask and perform actions" helper -----------------------
# Requires: `from gemini_actions import call_gemini_for_actions, resolve_gemini_api_key_from_env_or_disk`
# Add this function into auto_apply.py near other Playwright helpers.

# ---------- Gemini key helpers (local to this script) ---------- #
from selenium.webdriver.common.by import By

FINAL_ACTION_LABELS = [
    "submit application",
    "submit",
    "apply",
    "apply now",
    "send application",
    "preview",
    "review",
    "review application",
    "done",
    "finish",
    "complete",
]
def build_form_schema(container) -> Dict[str, Any]:
    """
    Build a lightweight JSON description of all form controls inside `container`.

    This is what we send to Gemini so it knows:
      - which text fields / textareas exist
      - which dropdowns (selects) exist
      - which radio groups (yes/no, MCQ) exist
      - which checkboxes exist

    IMPORTANT CHANGE:
      - For radio / checkbox inputs we NO LONGER require them to be visible.
        LinkedIn and many portals hide the real <input> and make only the
        label / fake circle visible. We still need to model those so Gemini
        can answer yes/no and MCQ questions.
    """
    schema: Dict[str, Any] = {
        "text_fields": [],
        "textareas": [],
        "select_fields": [],
        "radio_groups": [],
        "checkboxes": [],
    }

    # --------- INPUT ELEMENTS (text, radio, checkbox, etc.) ---------
    try:
        inputs = container.find_elements(By.CSS_SELECTOR, "input")
    except Exception:
        inputs = []

    radio_by_name: Dict[str, List[Any]] = {}

    for idx, inp in enumerate(inputs):
        # Try to get the input type first; we treat radios/checkboxes specially
        try:
            input_type = (inp.get_attribute("type") or "text").lower()
        except Exception:
            input_type = "text"

        # Skip clearly irrelevant types
        if input_type in {"hidden", "password", "submit", "button", "image"}:
            continue

        # For everything EXCEPT radio/checkbox, require visible + enabled
        try:
            is_displayed = inp.is_displayed()
            is_enabled = inp.is_enabled()
        except Exception:
            # If Selenium can't tell, treat as invisible/disabled for non‑radios
            is_displayed = False
            is_enabled = False

        if input_type not in {"radio", "checkbox"}:
            if not (is_displayed and is_enabled):
                continue

        elem_id = (inp.get_attribute("id") or "").strip()
        name_attr = (inp.get_attribute("name") or "").strip()
        placeholder = (inp.get_attribute("placeholder") or "").strip()
        label = get_label_for_element(container, inp)

        # 1) Radio buttons → grouped into radio_groups
        if input_type == "radio":
            group_name = name_attr or elem_id or f"radio_group_{len(radio_by_name) + 1}"
            radio_by_name.setdefault(group_name, []).append(inp)

        # 2) Checkboxes
        elif input_type == "checkbox":
            box_key = elem_id or name_attr or f"checkbox_{len(schema['checkboxes']) + 1}"
            schema["checkboxes"].append(
                {
                    "box_key": box_key,
                    "id": elem_id,
                    "name": name_attr,
                    "label": label or placeholder or box_key,
                }
            )

        # 3) All other text-ish inputs
        else:
            field_key = elem_id or name_attr or f"input_{len(schema['text_fields']) + 1}"
            schema["text_fields"].append(
                {
                    "field_key": field_key,
                    "id": elem_id,
                    "name": name_attr,
                    "placeholder": placeholder,
                    "label": label or placeholder or field_key,
                    "type": input_type,
                }
            )

    # --------- TEXTAREAS ---------
    try:
        textareas = container.find_elements(By.CSS_SELECTOR, "textarea")
    except Exception:
        textareas = []

    for ta in textareas:
        try:
            if not ta.is_displayed() or not ta.is_enabled():
                continue
        except Exception:
            continue

        elem_id = (ta.get_attribute("id") or "").strip()
        name_attr = (ta.get_attribute("name") or "").strip()
        placeholder = (ta.get_attribute("placeholder") or "").strip()
        label = get_label_for_element(container, ta)
        field_key = elem_id or name_attr or f"textarea_{len(schema['textareas']) + 1}"

        schema["textareas"].append(
            {
                "field_key": field_key,
                "id": elem_id,
                "name": name_attr,
                "placeholder": placeholder,
                "label": label or placeholder or field_key,
            }
        )

    # --------- SELECT (DROPDOWNS) ---------
    try:
        selects = container.find_elements(By.CSS_SELECTOR, "select")
    except Exception:
        selects = []

    for sel in selects:
        try:
            if not sel.is_displayed() or not sel.is_enabled():
                continue
        except Exception:
            continue

        elem_id = (sel.get_attribute("id") or "").strip()
        name_attr = (sel.get_attribute("name") or "").strip()
        label = get_label_for_element(container, sel)
        field_key = elem_id or name_attr or f"select_{len(schema['select_fields']) + 1}"

        # Capture available options for Gemini
        options: List[str] = []
        try:
            for opt in sel.find_elements(By.TAG_NAME, "option"):
                txt = (opt.text or "").strip()
                if not txt:
                    txt = (opt.get_attribute("value") or "").strip()
                if txt:
                    options.append(txt)
        except Exception:
            pass

        schema["select_fields"].append(
            {
                "field_key": field_key,
                "id": elem_id,
                "name": name_attr,
                "label": label or field_key,
                "options": options,
            }
        )

    # --------- RADIO GROUPS (YES/NO, MCQ, etc.) ---------
    for name_attr, radios in radio_by_name.items():
        options: List[str] = []
        inputs_meta: List[Dict[str, Any]] = []

        for r in radios:
            rid = (r.get_attribute("id") or "").strip()
            rname = (r.get_attribute("name") or "").strip()

            opt_label = get_label_for_element(container, r)
            if not opt_label:
                opt_label = (r.get_attribute("value") or "").strip()
            if not opt_label:
                opt_label = f"Option {len(options) + 1}"

            options.append(opt_label)
            inputs_meta.append({"id": rid, "name": rname})

        group_key = name_attr or f"radio_group_{len(schema['radio_groups']) + 1}"
        schema["radio_groups"].append(
            {
                "group_key": group_key,
                "name": name_attr,
                "options": options,
                "inputs": inputs_meta,
            }
        )

    return schema
def is_any_field_empty(container, form_schema: Optional[Dict[str, Any]] = None) -> bool:
    """
    Heuristic: return True if the form inside `container` has at least one
    obviously-empty field that we know how to fill.

    This is used as a lightweight check to decide whether to call Gemini at
    all for a given step.

    IMPORTANT CHANGE:
      - Radio groups (yes/no, single‑choice questions) are now considered
        EMPTY whenever we can find underlying <input type="radio"> elements
        and NONE of them is selected — even if those inputs are hidden.
    """
    try:
        schema = form_schema or build_form_schema(container)
    except Exception as e:
        debug(f"is_any_field_empty: build_form_schema failed: {e!r}; assuming fields may be empty.")
        return True

    def _find_elem(tag: str, item: Dict[str, Any]):
        elem = None
        elem_id = (item.get("id") or "").strip()
        name_attr = (item.get("name") or "").strip()

        # Prefer ID
        if elem_id:
            try:
                elem = container.find_element(By.ID, elem_id)
            except Exception:
                elem = None

        # Fallback to name
        if elem is None and name_attr:
            try:
                if tag == "input":
                    elem = container.find_element(By.CSS_SELECTOR, f"input[name='{name_attr}']")
                elif tag == "textarea":
                    elem = container.find_element(By.CSS_SELECTOR, f"textarea[name='{name_attr}']")
                elif tag == "select":
                    elem = container.find_element(By.CSS_SELECTOR, f"select[name='{name_attr}']")
            except Exception:
                elem = None

        return elem

    # ---------- TEXT INPUTS ----------
    for item in schema.get("text_fields", []):
        elem = _find_elem("input", item)
        if not elem:
            continue
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue
            val = (elem.get_attribute("value") or "").strip()
            if not val:
                debug(f"is_any_field_empty: empty text field {item.get('field_key')}")
                return True
        except Exception:
            return True

    # ---------- TEXTAREAS ----------
    for item in schema.get("textareas", []):
        elem = _find_elem("textarea", item)
        if not elem:
            continue
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue
            val = (elem.get_attribute("value") or "").strip()
            if not val:
                debug(f"is_any_field_empty: empty textarea {item.get('field_key')}")
                return True
        except Exception:
            return True

    # ---------- SELECT / DROPDOWN ----------
    for item in schema.get("select_fields", []):
        elem = _find_elem("select", item)
        if not elem:
            continue
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue

            sel = Select(elem)
            selected = sel.all_selected_options
            if not selected:
                debug(f"is_any_field_empty: empty select {item.get('field_key')}")
                return True

            opt = selected[0]
            text = (opt.text or "").strip().lower()
            value = (opt.get_attribute("value") or "").strip().lower()
            placeholders = {
                "select",
                "select one",
                "select an option",
                "please select",
                "choose",
                "choose one",
            }
            if (not text and not value) or any(p in text for p in placeholders):
                debug(
                    f"is_any_field_empty: placeholder option selected in "
                    f"select {item.get('field_key')}: {text!r}"
                )
                return True
        except Exception:
            return True

    # ---------- RADIO GROUPS (YES/NO, MCQ) ----------
    for group in schema.get("radio_groups", []):
        inputs_meta = group.get("inputs") or []
        radios: List[Any] = []

        for meta in inputs_meta:
            rid = (meta.get("id") or "").strip()
            rname = (meta.get("name") or "").strip()

            if rid:
                try:
                    radios.append(container.find_element(By.ID, rid))
                except Exception:
                    pass
            if rname:
                try:
                    radios.extend(
                        container.find_elements(
                            By.CSS_SELECTOR,
                            f"input[type='radio'][name='{rname}']",
                        )
                    )
                except Exception:
                    pass

        if not radios:
            name_attr = (group.get("name") or "").strip()
            if name_attr:
                try:
                    radios = container.find_elements(
                        By.CSS_SELECTOR,
                        f"input[type='radio'][name='{name_attr}']",
                    )
                except Exception:
                    radios = []

        if not radios:
            # No underlying radios found → nothing we can check
            continue

        any_selected = False
        for r in radios:
            try:
                if r.is_selected():
                    any_selected = True
                    break
            except Exception:
                continue

        if not any_selected:
            debug(f"is_any_field_empty: empty radio group {group.get('group_key')}")
            return True

    # ---------- CHECKBOXES ----------
    for item in schema.get("checkboxes", []):
        elem = _find_elem("input", item)
        if not elem:
            continue
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue

            req = (elem.get_attribute("required") or "").lower()
            aria_req = (elem.get_attribute("aria-required") or "").lower()
            required = bool(req) or "required" in req or aria_req in {"true", "1"}

            if required and not elem.is_selected():
                debug(f"is_any_field_empty: required checkbox {item.get('box_key')} not selected")
                return True
        except Exception:
            return True

    # No obviously empty field found
    return False

def card_looks_already_applied(card) -> bool:
    """
    Heuristic check on the LEFT LinkedIn job card to see if it already
    shows an 'Applied' badge.

    We ignore colours and only look at the visible text.
    """
    try:
        raw = card.text or ""
    except Exception:
        return False

    if not raw:
        return False

    # Work line-by-line to avoid matching things like "Applied Physics"
    # in a job title. The 'Applied' badge is usually a short, separate line.
    lines = [ln.strip().lower() for ln in raw.splitlines() if ln.strip()]
    exact_labels = {
        "applied",
        "already applied",
        "you applied",
    }

    for line in lines:
        if line in exact_labels:
            return True
        if line.startswith("applied on "):
            return True
        if line.startswith("applied ") and " ago" in line:
            # e.g. "applied 2 days ago"
            return True
        if line.startswith("you applied on "):
            return True
        if "see application" in line:
            return True

    # Fallback: look in the whole text for slightly longer phrases that are
    # unlikely to appear in the job description itself.
    text = " ".join(raw.split()).lower()
    fallback_phrases = [
        "you applied",
        "applied on ",
        "applied 1 day ago",
        "applied 2 days ago",
        "applied 3 days ago",
        "already applied",
        "see application",
    ]
    for p in fallback_phrases:
        if p in text:
            return True

    return False


def job_detail_looks_already_applied(driver: webdriver.Chrome) -> bool:
    """
    Heuristic check on the RIGHT job details pane/page to see if LinkedIn
    says that you already applied to this job.

    This matches phrases such as:
      - 'Applied 2 days ago · See application'
      - 'You applied on <date>'
      - 'You already applied for this job'
    """
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        raw = body.text or ""
    except Exception:
        return False

    if not raw:
        return False

    text = " ".join(raw.split()).lower()

    phrases = [
        "you applied",
        "you have applied",
        "you previously applied",
        "already applied",
        "applied on ",
        "applied 1 day ago",
        "applied 2 days ago",
        "applied 3 days ago",
        "applied yesterday",
        "applied today",
        "see application",
        "application submitted",
        "your application was sent",
        "your application has been submitted",
        "thanks for applying",
    ]

    for p in phrases:
        if p in text:
            return True

    return False


def click_external_portal_apply_buttons(
    driver: webdriver.Chrome,
    max_clicks: int = 2,
) -> bool:
    """
    On the external portal, try to click an 'Apply' / 'Apply now' /
    'Apply for this job' / 'Start application' button or link to open
    the actual application form.

    We may need to do this twice for portals that show an intermediate
    landing page.

    Returns True if at least one such button/link was clicked.
    """
    clicked_any = False

    for _ in range(max_clicks):
        try:
            body = driver.find_element(By.TAG_NAME, "body")
        except Exception:
            break

        candidates = []
        texts = [
            "apply on company site",
            "apply on company website",
            "apply now",
            "apply for this job",
            "start your application",
            "start application",
            "begin application",
            "apply",
        ]

        for t in texts:
            # <a> with 'apply' text
            try:
                elems = body.find_elements(
                    By.XPATH,
                    ".//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                    f"'{t.lower()}')]"
                )
                candidates.extend(elems)
            except Exception:
                pass
            # <button> with 'apply' text
            try:
                elems = body.find_elements(
                    By.XPATH,
                    ".//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                    f"'{t.lower()}')]"
                )
                candidates.extend(elems)
            except Exception:
                pass

        # Filter to visible, enabled, non‑weird candidates
        filtered = []
        for el in candidates:
            try:
                label = (el.text or "").strip().lower()
                # avoid obvious non‑apply things
                if "easy apply" in label:
                    continue
                if "apply filter" in label:
                    continue
                if not (el.is_displayed() and el.is_enabled()):
                    continue
                filtered.append(el)
            except Exception:
                continue

        if not filtered:
            break

        btn = filtered[0]
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn
            )
        except Exception:
            pass

        try:
            debug(f"External portal: clicking in-page apply button/link with text '{btn.text.strip()[:60]}'")
            btn.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception as e:
                debug(f"External portal: failed to click in-page apply button: {e!r}")
                break

        clicked_any = True
        time.sleep(4.0)

    return clicked_any

# Keep track of fields that were already filled by LinkedIn/portal
# *before* we touched the form in pass 1.
REMEMBERED_PREFILLED_DOM_KEYS: Dict[Tuple[str, int], set] = {}
def container_has_validation_error(container) -> bool:
    """
    Look for obvious validation / error messages in a form container.
    This catches LinkedIn's red 'Please make a selection' and similar messages.
    """
    try:
        error_elems = container.find_elements(
            By.CSS_SELECTOR,
            (
                ".artdeco-inline-feedback__message, "
                ".artdeco-inline-feedback--error, "
                ".artdeco-inline-feedback, "
                ".error, .errors, "
                "[role='alert'], "
                "[data-test*='error'], "
                "[data-test*='alert']"
            ),
        )
    except Exception:
        error_elems = []

    keywords = [
        "please make a selection",
        "this field is required",
        "required",
        "must be completed",
        "must be answered",
        "enter a value",
        "fix the errors",
    ]
    for el in error_elems:
        try:
            txt = (el.text or "").strip().lower()
        except Exception:
            continue
        if not txt:
            continue
        for kw in keywords:
            if kw in txt:
                return True
    return False


def _dom_key_for_form_element(elem) -> str:
    """
    Build a stable-ish key for a form element based on tag/id/name/type/value.
    We use this to recognise the same field across passes.
    """
    try:
        tag = (elem.tag_name or "").lower()
    except Exception:
        tag = "elem"

    try:
        input_type = (elem.get_attribute("type") or "").lower()
    except Exception:
        input_type = ""

    try:
        field_id = (elem.get_attribute("id") or "").strip()
    except Exception:
        field_id = ""

    try:
        name_attr = (elem.get_attribute("name") or "").strip()
    except Exception:
        name_attr = ""

    try:
        value_attr = (elem.get_attribute("value") or "").strip()
    except Exception:
        value_attr = ""

    parts = [tag]
    if input_type:
        parts.append(f"type={input_type}")
    if field_id:
        parts.append(f"id={field_id}")
    if name_attr:
        parts.append(f"name={name_attr}")
    # For radios/checkboxes we include the value as well
    if input_type in ("radio", "checkbox") and value_attr:
        parts.append(f"value={value_attr}")

    return "|".join(parts)


def remember_prefilled_dom_fields(container, job_index: int, mode: str = "easy") -> None:
    """
    Scan the current step and remember ONLY fields that were already filled
    by LinkedIn / the portal *before* we touch anything.

    This includes:
      - text inputs
      - textareas
      - dropdowns (select)
      - radios (MCQs) that are already selected
      - checkboxes that are already ticked
    """
    key = ((mode or "easy").lower(), int(job_index))
    remembered = REMEMBERED_PREFILLED_DOM_KEYS.setdefault(key, set())

    # Collect elements
    try:
        inputs = container.find_elements(By.CSS_SELECTOR, "input")
    except Exception:
        inputs = []
    try:
        textareas = container.find_elements(By.CSS_SELECTOR, "textarea")
    except Exception:
        textareas = []
    try:
        selects = container.find_elements(By.TAG_NAME, "select")
    except Exception:
        selects = []

    # Text-like inputs (we treat any non‑empty value as prefilled)
    for inp in inputs:
        try:
            if not inp.is_displayed() or not inp.is_enabled():
                continue
            itype = (inp.get_attribute("type") or "").lower()

            # Skip hidden & password
            if itype in ("hidden", "password"):
                continue

            if itype in ("text", "email", "tel", "number", "url", "search", ""):
                val = (inp.get_attribute("value") or "").strip()
                if val:
                    remembered.add(_dom_key_for_form_element(inp))
            elif itype in ("radio", "checkbox"):
                # For radios/checkboxes we remember ones that are already selected
                if inp.is_selected():
                    remembered.add(_dom_key_for_form_element(inp))
        except Exception:
            continue

    # Textareas
    for ta in textareas:
        try:
            if not ta.is_displayed() or not ta.is_enabled():
                continue
            val = (ta.get_attribute("value") or ta.text or "").strip()
            if val:
                remembered.add(_dom_key_for_form_element(ta))
        except Exception:
            continue

    # Dropdowns (selects)
    placeholder_tokens = {
        "select",
        "select one",
        "please select",
        "choose",
        "choose one",
        "select an option",
        "none",
        "n/a",
    }

    for sel_elem in selects:
        try:
            if not sel_elem.is_displayed() or not sel_elem.is_enabled():
                continue

            sel = Select(sel_elem)
            selected = sel.all_selected_options
            if not selected:
                continue

            # We treat it as prefilled only if the selected option is not a placeholder
            opt = selected[0]
            txt = (opt.text or "").strip().lower()
            if not txt:
                continue
            if any(tok in txt for tok in placeholder_tokens):
                continue

            remembered.add(_dom_key_for_form_element(sel_elem))
        except Exception:
            continue


def clear_nonremembered_fields_in_container(
    driver: webdriver.Chrome,
    container,
    job_index: int,
    mode: str = "easy",
) -> None:
    """
    Clear ONLY fields that were *not* prefilled by LinkedIn/portal in pass 1.

    - Prefilled (remembered) fields are left untouched.
    - Everything else (our random filler, our own profile fills, etc.)
      is cleared so Gemini can overwrite it cleanly.
    """
    key = ((mode or "easy").lower(), int(job_index))
    remembered = REMEMBERED_PREFILLED_DOM_KEYS.get(key, set()) or set()

    try:
        try:
            inputs = container.find_elements(By.CSS_SELECTOR, "input")
        except Exception:
            inputs = []
        try:
            textareas = container.find_elements(By.CSS_SELECTOR, "textarea")
        except Exception:
            textareas = []
        try:
            selects = container.find_elements(By.TAG_NAME, "select")
        except Exception:
            selects = []

        # Text-like inputs
        for inp in inputs:
            try:
                if not inp.is_displayed() or not inp.is_enabled():
                    continue
                itype = (inp.get_attribute("type") or "").lower()
                if itype in ("hidden",):
                    continue

                dom_key = _dom_key_for_form_element(inp)

                if itype in ("text", "email", "tel", "number", "url", "search", ""):
                    # Skip clearing if this field was prefilled by LinkedIn/portal
                    if dom_key in remembered:
                        continue
                    try:
                        inp.clear()
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].value='';", inp)
                        except Exception:
                            pass

                elif itype in ("radio", "checkbox"):
                    # For radios/checkboxes: unselect only if they were *not* remembered
                    if not inp.is_selected():
                        continue
                    if dom_key in remembered:
                        continue
                    try:
                        inp.click()
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].checked = false;", inp)
                        except Exception:
                            pass

            except Exception:
                continue

        # Textareas
        for ta in textareas:
            try:
                if not ta.is_displayed() or not ta.is_enabled():
                    continue
                dom_key = _dom_key_for_form_element(ta)
                if dom_key in remembered:
                    continue
                try:
                    ta.clear()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].value='';", ta)
                    except Exception:
                        pass
            except Exception:
                continue

        # Dropdowns
        for sel_elem in selects:
            try:
                if not sel_elem.is_displayed() or not sel_elem.is_enabled():
                    continue
                dom_key = _dom_key_for_form_element(sel_elem)
                if dom_key in remembered:
                    # This dropdown had a real prefilled choice; leave it alone
                    continue
                try:
                    # Reset to first option if possible
                    driver.execute_script("arguments[0].selectedIndex = 0;", sel_elem)
                except Exception:
                    try:
                        Select(sel_elem).deselect_all()
                    except Exception:
                        # some single-selects don't support deselect_all
                        pass
            except Exception:
                continue

    except Exception as e:
        debug(f"clear_nonremembered_fields_in_container: failed with {e!r}")

def card_looks_already_applied(card) -> bool:
    """
    Check the LEFT job card for an 'Applied' status.

    We ignore colour and styling completely and just look at the *visible text*.
    In the current LinkedIn UI this appears exactly like the screenshot:
        - 'Applied' as a standalone word under the card
        - sometimes followed by phrases like 'See application'
    """
    try:
        raw = card.text or ""
    except Exception:
        return False

    text = raw.strip().lower()
    if not text:
        return False

    # Very direct patterns based on the screenshot:
    #   - 'Applied' on its own line
    #   - 'Applied' somewhere in the card footer
    #   - 'See application' shown under jobs you already applied to
    patterns = [
        "Applied",          # e.g. line with just 'Applied'
        "see application",  # right‑side header & sometimes in cards
    ]

    for p in patterns:
        if p in text:
            debug(f"card_looks_already_applied: matched '{p}' in card text.")
            return True

    return False


def is_job_already_applied_on_linkedin(driver: webdriver.Chrome) -> bool:
    """
    Check the RIGHT job detail pane/page for 'Applied' status.

    In the current LinkedIn UI this shows up in the header as:
        'Applied 2 days ago  See application'
    above the 'About the job' section.

    We ignore styling and just look for those words in the visible body text.
    """
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        raw = body.text or ""
    except Exception:
        return False

    text = raw.strip().lower()
    if not text:
        return False

    patterns = [
        "applied ",         # 'applied 2 days ago'
        " applied",         # ' is applied' (defensive)
        " Applied"
        "applied 2 days ago",
        "applied 1 day ago",
        "applied yesterday",
        "already applied",
        "you applied",
        "see application",  # very strong signal from the screenshot
        "application submitted",
        "your application was sent",
        "your application has been submitted",
        "thanks for applying",
        "Applied"
    ]

    for p in patterns:
        if p in text:
            debug(
                f"is_job_already_applied_on_linkedin: matched '{p}' in job detail "
                "text; treating as already applied."
            )
            return True

    return False


def container_has_final_submit_button(container) -> bool:
    """
    Heuristic: return True if the container has a visible, enabled button
    that looks like a final action: Submit / Preview / Review / Done / Finish.
    """
    for label in FINAL_ACTION_LABELS:
        label_l = label.lower()

        # <button> with matching text
        try:
            buttons = container.find_elements(
                By.XPATH,
                ".//button[contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                f"'{label_l}')]",
            )
        except Exception:
            buttons = []

        for btn in buttons:
            try:
                if btn.is_displayed() and btn.is_enabled():
                    return True
            except Exception:
                continue

        # <input type='submit'|'button'> with matching value
        try:
            inputs = container.find_elements(
                By.XPATH,
                ".//input[(translate(@value, "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                f"'{label_l}') and (@type='submit' or @type='button')]",
            )
        except Exception:
            inputs = []

        for inp in inputs:
            try:
                if inp.is_displayed() and inp.is_enabled():
                    return True
            except Exception:
                continue

    return False

TEMP_RANDOM_PREFIX = "[AUTO-RANDOM]"  # used only in first pass placeholder text


def clear_all_editable_fields_in_container(driver: webdriver.Chrome, container) -> None:
    """
    Clear text inputs, textareas, dropdowns, radios and checkboxes inside this container.

    This is used at the start of the *second* pass so that Gemini / memory logic
    sees every field as empty and can refill everything with correct answers.
    """
    try:
        # Text-like inputs
        try:
            inputs = container.find_elements(By.CSS_SELECTOR, "input")
        except Exception:
            inputs = []
        try:
            textareas = container.find_elements(By.CSS_SELECTOR, "textarea")
        except Exception:
            textareas = []

        for inp in inputs:
            try:
                if not inp.is_displayed() or not inp.is_enabled():
                    continue
                input_type = (inp.get_attribute("type") or "").lower()
                if input_type in ("hidden",):
                    continue

                # For text-like fields, clear the value
                if input_type in ("text", "email", "tel", "number", "url", "search", ""):
                    try:
                        inp.clear()
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].value='';", inp)
                        except Exception:
                            pass
                # checkbox / radio handled later
            except Exception:
                continue

        for ta in textareas:
            try:
                if not ta.is_displayed() or not ta.is_enabled():
                    continue
                try:
                    ta.clear()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].value='';", ta)
                    except Exception:
                        pass
            except Exception:
                continue

        # Dropdowns
        try:
            selects = container.find_elements(By.TAG_NAME, "select")
        except Exception:
            selects = []
        for sel_elem in selects:
            try:
                if not sel_elem.is_displayed() or not sel_elem.is_enabled():
                    continue
                # Try to reset to first option (often a placeholder)
                try:
                    driver.execute_script("arguments[0].selectedIndex = 0;", sel_elem)
                except Exception:
                    try:
                        Select(sel_elem).deselect_all()
                    except Exception:
                        # some single-selects don't support deselect_all
                        pass
            except Exception:
                continue

        # Checkboxes and radios
        for inp in inputs:
            try:
                if not inp.is_displayed() or not inp.is_enabled():
                    continue
                itype = (inp.get_attribute("type") or "").lower()
                if itype not in ("checkbox", "radio"):
                    continue
                if inp.is_selected():
                    try:
                        inp.click()
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].checked = false;", inp)
                        except Exception:
                            pass
            except Exception:
                continue
    except Exception as e:
        debug(f"clear_all_editable_fields_in_container: failed with {e!r}")


def click_back_or_previous_in_container(container, mode: str = "easy") -> bool:
    """
    Try to click a 'Back' / 'Previous' style button inside the container.

    Returns True if a click was performed, False otherwise.
    """
    labels = [
        "Back",
        "Previous",
        "Go back",
        "Back to previous",
        "Back to application",
        "Back to review",
        "Back to step",
    ]

    for label in labels:
        # Buttons
        try:
            btns = container.find_elements(
                By.XPATH,
                ".//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                f"'{label.lower()}')]"
            )
        except Exception:
            btns = []
        for btn in btns:
            try:
                if btn.is_displayed() and btn.is_enabled():
                    debug(f"Clicking {mode} form BACK button: '{label}'")
                    btn.click()
                    time.sleep(1.5)
                    return True
            except Exception:
                continue

        # Inputs
        try:
            inputs = container.find_elements(
                By.XPATH,
                ".//input[(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                f"'{label.lower()}') and (@type='button' or @type='submit')]"
            )
        except Exception:
            inputs = []
        for inp in inputs:
            try:
                if inp.is_displayed() and inp.is_enabled():
                    debug(f"Clicking {mode} form BACK input: '{label}'")
                    inp.click()
                    time.sleep(1.5)
                    return True
            except Exception:
                continue

    return False


def click_progress_button_in_container(container, mode: str = "easy") -> bool:
    """
    Click a **non-final** progress button such as Next/Continue/Save and continue,
    explicitly avoiding final actions like Submit/Apply/Done/Preview/Review.

    Returns True if something was clicked, False otherwise.
    """
    labels = [
        "Next",
        "Continue",
        "Save and continue",
        "Save & continue",
        "Proceed",
    ]

    for label in labels:
        try:
            btns = container.find_elements(
                By.XPATH,
                ".//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                f"'{label.lower()}')]"
            )
        except Exception:
            btns = []
        for btn in btns:
            try:
                if btn.is_displayed() and btn.is_enabled():
                    debug(f"Clicking {mode} progress button: '{label}'")
                    btn.click()
                    time.sleep(1.5)
                    return True
            except Exception:
                continue

        try:
            inputs = container.find_elements(
                By.XPATH,
                ".//input[(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                f"'{label.lower()}') and (@type='submit' or @type='button')]"
            )
        except Exception:
            inputs = []
        for inp in inputs:
            try:
                if inp.is_displayed() and inp.is_enabled():
                    debug(f"Clicking {mode} progress input: '{label}'")
                    inp.click()
                    time.sleep(1.5)
                    return True
            except Exception:
                continue

    return False


def fast_random_fill_required_fields(container, mode: str = "easy") -> None:
    """
    Very simple heuristic random filler for the current step.

    It only touches fields that are:
      - visible and enabled
      - currently EMPTY

    For text/textareas: inserts a short '[AUTO-RANDOM]' placeholder.
    For dropdowns: selects a random non-placeholder option.
    For radio groups: selects one random option if none is selected.
    For checkboxes: only ticks ones that look obviously required.
    """
    # Text inputs
    try:
        inputs = container.find_elements(By.CSS_SELECTOR, "input")
    except Exception:
        inputs = []
    try:
        textareas = container.find_elements(By.CSS_SELECTOR, "textarea")
    except Exception:
        textareas = []
    try:
        selects = container.find_elements(By.TAG_NAME, "select")
    except Exception:
        selects = []

    # Text-like
    for inp in inputs:
        try:
            if not inp.is_displayed() or not inp.is_enabled():
                continue
            itype = (inp.get_attribute("type") or "").lower()
            if itype in ("hidden", "password", "file"):
                continue
            val = (inp.get_attribute("value") or "").strip()
            if val:
                continue  # do not overwrite
            placeholder = (inp.get_attribute("placeholder") or "").lower()
            aria = (inp.get_attribute("aria-label") or "").lower()
            if "search" in placeholder or "search" in aria:
                continue
            inp.click()
            inp.send_keys(TEMP_RANDOM_PREFIX)
            time.sleep(0.05)
        except Exception:
            continue

    for ta in textareas:
        try:
            if not ta.is_displayed() or not ta.is_enabled():
                continue
            val = (ta.get_attribute("value") or "").strip()
            if val:
                continue
            ta.click()
            ta.send_keys(TEMP_RANDOM_PREFIX)
            time.sleep(0.05)
        except Exception:
            continue

    # Dropdowns
    placeholder_tokens = {"select", "select one", "please select", "choose", "choose one", "select an option"}
    for sel_elem in selects:
        try:
            if not sel_elem.is_displayed() or not sel_elem.is_enabled():
                continue
            sel = Select(sel_elem)
            options = sel.options
            if not options or len(options) == 1:
                continue
            selected = sel.all_selected_options
            if selected:
                text = (selected[0].text or "").strip().lower()
                if text and not any(tok in text for tok in placeholder_tokens):
                    continue
            # Pick a random non-placeholder option
            candidates = []
            for opt in options:
                txt = (opt.text or "").strip().lower()
                if not txt or any(tok in txt for tok in placeholder_tokens):
                    continue
                candidates.append(opt)
            if not candidates:
                continue
            choice = random.choice(candidates)
            sel.select_by_visible_text(choice.text)
            time.sleep(0.05)
        except Exception:
            continue

    # Radio groups
    radios_by_name = {}
    for inp in inputs:
        try:
            if not inp.is_displayed() or not inp.is_enabled():
                continue
            itype = (inp.get_attribute("type") or "").lower()
            if itype == "radio":
                name = (inp.get_attribute("name") or "").strip() or "__anon_radio__"
                radios_by_name.setdefault(name, []).append(inp)
        except Exception:
            continue

    for name, group in radios_by_name.items():
        try:
            if any(r.is_selected() for r in group):
                continue
            candidate_radios = [r for r in group if r.is_displayed() and r.is_enabled()]
            if not candidate_radios:
                continue
            choice = random.choice(candidate_radios)
            choice.click()
            time.sleep(0.05)
        except Exception:
            continue

    # Checkboxes: only tick ones that look obviously required
    for inp in inputs:
        try:
            if not inp.is_displayed() or not inp.is_enabled():
                continue
            itype = (inp.get_attribute("type") or "").lower()
            if itype != "checkbox":
                continue
            req = (inp.get_attribute("required") or "").lower()
            if ("required" in req or req == "true") and not inp.is_selected():
                inp.click()
                time.sleep(0.05)
        except Exception:
            continue


def container_has_final_submit_button(container) -> bool:
    """
    Heuristic: detect whether this step has a *final* action button such as
    Submit / Preview / Done / Finish / Review.

    Used during the first pass so that we stop BEFORE accidentally submitting.
    """
    final_tokens = ("submit", "preview", "done", "finish", "review")
    tokens_lower = [t.lower() for t in final_tokens]

    # Check visible buttons
    try:
        buttons = container.find_elements(By.TAG_NAME, "button")
    except Exception:
        buttons = []
    for btn in buttons:
        try:
            if not btn.is_displayed() or not btn.is_enabled():
                continue
            label = (btn.text or "").strip().lower()
            if any(tok in label for tok in tokens_lower):
                return True
        except Exception:
            continue

    # Check input[type=submit/button]
    try:
        inputs = container.find_elements(By.XPATH, ".//input[@type='submit' or @type='button']")
    except Exception:
        inputs = []
    for inp in inputs:
        try:
            if not inp.is_displayed() or not inp.is_enabled():
                continue
            val = (inp.get_attribute("value") or "").strip().lower()
            if any(tok in val for tok in tokens_lower):
                return True
        except Exception:
            continue

    return False

def is_linkedin_security_check_page(driver: webdriver.Chrome) -> bool:
    """
    Heuristically detect if the current page is a LinkedIn security check /
    checkpoint / CAPTCHA page.

    Returns True if it looks like a security / verification wall.
    """
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        url = ""
    if "checkpoint" in url or "captcha" in url:
        return True

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        body_text = ""

    phrases = [
        "security check",
        "verify your identity",
        "confirm your identity",
        "we've detected unusual activity",
        "unusual activity",
        "we just need to make sure it's really you",
        "complete the security check",
        "please complete the verification",
        "additional verification",
    ]
    return any(p in body_text for p in phrases)


def handle_linkedin_security_check(driver: webdriver.Chrome, max_wait_minutes: int = 5) -> None:
    """
    Ask the human to solve LinkedIn's security check / CAPTCHA manually,
    then press ENTER in the terminal to continue.

    This does NOT attempt to bypass or automate the CAPTCHA.
    """
    msg = (
        "LinkedIn is showing a security check or CAPTCHA. "
        "Please solve it manually in the browser, complete login, "
        "and then press Enter here to continue."
    )
    try:
        speak(msg)
    except Exception:
        debug(msg)

    print("\n[LinkedIn] Security check or CAPTCHA detected.", flush=True)
    print("Please solve it manually in the open browser window.", flush=True)
    print("Once you reach your LinkedIn home page (or jobs tab),", flush=True)
    print("return to this terminal and press ENTER to continue.\n", flush=True)

    # Block until user confirms
    try:
        input("Press ENTER here once login appears complete: ")
    except KeyboardInterrupt:
        # allow user to abort
        raise

    # We don't force a strict check here; login_to_linkedin will verify again.
    debug("User signaled that security check / CAPTCHA was handled manually.")

def _click_at_viewport_coordinate(driver: webdriver.Chrome, x: int, y: int) -> bool:
    """
    Click at the given viewport coordinates (clientX, clientY).

    Uses document.elementFromPoint() to find the element at that pixel,
    scrolls it into view, and dispatches a synthetic click.

    Returns True if a click was dispatched, False otherwise.
    """
    try:
        x = int(x)
        y = int(y)
    except Exception:
        debug(f"_click_at_viewport_coordinate: invalid coords x={x!r}, y={y!r}")
        return False

    if x < 0:
        x = 0
    if y < 0:
        y = 0

    debug(f"_click_at_viewport_coordinate: clicking at viewport coords ({x}, {y})")
    try:
        js = """
        (function(cx, cy) {
            var el = document.elementFromPoint(cx, cy);
            if (!el) return false;
            el.scrollIntoView({block:'center', inline:'center'});
            var rect = el.getBoundingClientRect();

            var clickX = cx;
            var clickY = cy;
            if (cx < rect.left || cx > rect.right || cy < rect.top || cy > rect.bottom) {
                clickX = rect.left + rect.width / 2;
                clickY = rect.top + rect.height / 2;
            }

            var ev = new MouseEvent('click', {
                view: window,
                bubbles: true,
                cancelable: true,
                clientX: clickX,
                clientY: clickY
            });
            el.dispatchEvent(ev);
            return true;
        })(arguments[0], arguments[1]);
        """
        result = driver.execute_script(js, x, y)
        return bool(result)
    except Exception as e:
        debug(f"_click_at_viewport_coordinate: JS click failed: {e!r}")
        return False

def try_gemini_page_recovery(
    driver: webdriver.Chrome,
    container,
    gemini_api_key: str,
    problem_description: str,
    phase: str,
    max_steps: int = 3,
) -> bool:
    """
    Capture page context, ask Gemini for a short sequence of actions (click/type/wait),
    and execute them in order.

    If Gemini reports that it is unsure or that the page is a security check / CAPTCHA,
    we hand control back to the human instead of looping forever.
    """
    ctx = capture_page_context(driver, container, redact_passwords=True)
    screenshot_path = ctx.get("screenshot_path")
    html = ctx.get("html", "")
    visible_text = ctx.get("visible_text", "")

    # ensure we have a key
    api_key = (gemini_api_key or (os.environ.get("GEMINI_API_KEY") or "")).strip()
    if not api_key:
        speak("Gemini key missing. Please provide a valid Gemini API key to attempt automated recovery.")
        api_key = prompt_for_new_gemini_key()
        if not api_key:
            debug("try_gemini_page_recovery: user did not provide a Gemini key; aborting recovery.")
            return False

    for attempt in range(max_steps):
        debug(f"try_gemini_page_recovery: recovery attempt {attempt+1}/{max_steps} (phase={phase})")
        resp = call_gemini_for_page_recovery(
            api_key=api_key,
            page_html=html,
            visible_text=visible_text,
            screenshot_path=screenshot_path,
            problem_description=problem_description,
            phase=f"{phase}.{attempt+1}",
            max_actions=6,
        )

        if not resp:
            debug("try_gemini_page_recovery: Gemini returned no response; stopping recovery.")
            return False

        actions = resp.get("actions") or []
        comment = (resp.get("comment") or "").lower()

        # If Gemini explicitly says it's a security check / CAPTCHA or that it's unsure,
        # do NOT keep looping — ask the human to handle it.
        if not actions:
            if any(tok in comment for tok in ["captcha", "security check", "manual intervention needed"]):
                msg = (
                    "Gemini reports that this page is a security check or CAPTCHA, "
                    "or that it cannot safely decide what to do. "
                    "Please handle it manually in the browser."
                )
                try:
                    speak(msg)
                except Exception:
                    debug(msg)

                print("\n[Gemini] Recovery is not safe to automate (security check / CAPTCHA / unsure).", flush=True)
                print("Please complete the required action manually in the browser, then continue.", flush=True)
                return False

            debug("try_gemini_page_recovery: no actions returned by Gemini; continuing.")
            time.sleep(1.0)
            continue

        debug(
            f"try_gemini_page_recovery: executing {len(actions)} Gemini actions "
            f"(comment={resp.get('comment')!r})"
        )
        execute_gemini_actions(driver, actions)
        time.sleep(1.2)
        return True

    debug("try_gemini_page_recovery: exhausted recovery attempts without actionable steps.")
    return False

def execute_gemini_recovery_plan(
    driver: webdriver.Chrome,
    steps: List[Dict[str, Any]],
) -> bool:
    """
    Execute a Gemini recovery plan (from call_gemini_for_recovery_actions).

    Each step may contain:
      - "mouse": coords click
      - "keyboard": type into a field

    Returns True if any action was executed.
    """
    if not steps:
        debug("execute_gemini_recovery_plan: no steps to execute.")
        return False

    executed = False

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue

        mouse_cfg = step.get("mouse") or None
        kb_cfg = step.get("keyboard") or None

        if mouse_cfg:
            try:
                mouse_action(
                    driver,
                    action=mouse_cfg.get("action", "click"),
                    locator_type=mouse_cfg.get("locator_type"),
                    locator=mouse_cfg.get("locator"),
                    offset_x=mouse_cfg.get("offset_x"),
                    offset_y=mouse_cfg.get("offset_y"),
                )
                executed = True
            except Exception as e:
                debug(f"execute_gemini_recovery_plan: mouse step #{idx} failed: {e!r}")

        if kb_cfg:
            try:
                keyboard_action(
                    driver,
                    action=kb_cfg.get("action", "type"),
                    locator_type=kb_cfg.get("locator_type"),
                    locator=kb_cfg.get("locator"),
                    text=kb_cfg.get("text"),
                    key=kb_cfg.get("key"),
                )
                executed = True
            except Exception as e:
                debug(f"execute_gemini_recovery_plan: keyboard step #{idx} failed: {e!r}")

        # small delay between steps
        time.sleep(0.5)

    return executed

def _click_at_viewport_coordinate(driver: webdriver.Chrome, x: int, y: int) -> bool:
    """
    Click at the given viewport coordinates (clientX, clientY).

    Uses document.elementFromPoint() to find the element at that pixel,
    scrolls it into view, and dispatches a synthetic click.

    Returns True if a click was dispatched, False otherwise.
    """
    try:
        x = int(x)
        y = int(y)
    except Exception:
        debug(f"_click_at_viewport_coordinate: invalid coords x={x!r}, y={y!r}")
        return False

    if x < 0:
        x = 0
    if y < 0:
        y = 0

    debug(f"_click_at_viewport_coordinate: clicking at viewport coords ({x}, {y})")
    try:
        js = """
        (function(cx, cy) {
            var el = document.elementFromPoint(cx, cy);
            if (!el) return false;
            el.scrollIntoView({block:'center', inline:'center'});
            var rect = el.getBoundingClientRect();

            var clickX = cx;
            var clickY = cy;
            if (cx < rect.left || cx > rect.right || cy < rect.top || cy > rect.bottom) {
                clickX = rect.left + rect.width / 2;
                clickY = rect.top + rect.height / 2;
            }

            var ev = new MouseEvent('click', {
                view: window,
                bubbles: true,
                cancelable: true,
                clientX: clickX,
                clientY: clickY
            });
            el.dispatchEvent(ev);
            return true;
        })(arguments[0], arguments[1]);
        """
        result = driver.execute_script(js, x, y)
        return bool(result)
    except Exception as e:
        debug(f"_click_at_viewport_coordinate: JS click failed: {e!r}")
        return False


def detect_generic_application_confirmation(
    driver: webdriver.Chrome,
    timeout: float = 4.0,
) -> bool:
    """
    Heuristically detect whether a generic job portal / external site
    shows an 'Application submitted / received' confirmation.

    Returns True if we see a typical confirmation phrase, False otherwise.
    """
    phrases = [
        "thank you for applying",
        "thank you for your application",
        "thank you for submitting your application",
        "your application has been received",
        "we have received your application",
        "application received",
        "application submitted",
        "you have successfully applied",
        "your application is complete",
        "we'll review your application",
        "we will review your application",
    ]

    end_time = time.time() + max(timeout, 0.0)
    while time.time() < end_time:
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            text = (body.text or "").lower()
            for p in phrases:
                if p in text:
                    debug(
                        f"Detected generic submission confirmation phrase: '{p}'. "
                        "Treating this job as successfully submitted."
                    )
                    return True
        except Exception:
            # Ignore transient issues and retry until timeout
            pass
        time.sleep(0.5)

    return False

def page_looks_like_captcha(driver: webdriver.Chrome) -> bool:
    """
    Heuristic check for common CAPTCHA / 'I'm not a robot' / human verification pages.
    This does NOT solve the CAPTCHA; it only detects that one is likely present.
    """
    # Look for reCAPTCHA / hCaptcha iframes
    try:
        frames = driver.find_elements(
            By.CSS_SELECTOR,
            "iframe[src*='recaptcha'], iframe[title*='recaptcha'], iframe[src*='hcaptcha']",
        )
        for f in frames:
            try:
                if f.is_displayed():
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Look for typical text on the page
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        txt = (body.text or "").lower()
        phrases = [
            "i'm not a robot",
            "i am not a robot",
            "are you a robot",
            "verify that you are human",
            "verify you are human",
            "select all images with",
            "select all squares with",
            "click each image that",
            "complete the security check",
        ]
        if any(p in txt for p in phrases):
            return True
    except Exception:
        pass

    return False


def wait_for_captcha_to_be_solved(
    driver: webdriver.Chrome,
    max_wait_seconds: int = 300,
    poll_interval: float = 3.0,
) -> bool:
    """
    If a CAPTCHA / 'I'm not a robot' check is present, speak to the user and wait
    for them to solve it. Returns True when the CAPTCHA is gone, False on timeout.

    Behaviour:
      - If no CAPTCHA is detected: returns True immediately.
      - If CAPTCHA appears: speaks once, then keeps checking until it disappears
        or max_wait_seconds is reached.
    """
    start = time.time()
    announced = False

    while time.time() - start < max_wait_seconds:
        if page_looks_like_captcha(driver):
            if not announced:
                debug("Possible CAPTCHA / 'I'm not a robot' check detected; pausing for human to solve it.")
                try:
                    speak(
                        "The website is asking to confirm you are not a robot. "
                        "Please solve the captcha in the browser window. "
                        "The script will continue automatically after you finish."
                    )
                except Exception:
                    # If TTS fails, we already logged above via debug()
                    pass
                announced = True

            time.sleep(poll_interval)
            continue

        # No CAPTCHA detected right now
        if announced:
            debug("CAPTCHA no longer detected; resuming automation.")
        return True

    # Timed out
    if announced:
        debug(
            f"Timed out waiting {max_wait_seconds} seconds for CAPTCHA to be solved; "
            "stopping this apply flow."
        )
        return False

    # Never saw a CAPTCHA at all
    return True


def wait_for_captcha_to_be_solved(
    driver: webdriver.Chrome,
    max_wait_seconds: int = 300,
    poll_interval: float = 3.0,
) -> bool:
    """
    If a CAPTCHA / 'I'm not a robot' check is present, speak to the user and wait
    for them to solve it. Returns True when the CAPTCHA is gone, False on timeout.

    Behaviour:
      - If no CAPTCHA is detected: returns True immediately.
      - If CAPTCHA appears: speaks once, then keeps checking until it disappears
        or max_wait_seconds is reached.
    """
    start = time.time()
    announced = False

    while time.time() - start < max_wait_seconds:
        if page_looks_like_captcha(driver):
            if not announced:
                debug("Possible CAPTCHA / 'I'm not a robot' check detected; pausing for human to solve it.")
                try:
                    speak(
                        "The website is asking to confirm you are not a robot. "
                        "Please solve the captcha in the browser window. "
                        "The script will continue automatically after you finish."
                    )
                except Exception:
                    # If TTS fails, we already logged above via debug()
                    pass
                announced = True

            time.sleep(poll_interval)
            continue

        # No CAPTCHA detected right now
        if announced:
            debug("CAPTCHA no longer detected; resuming automation.")
        return True

    # Timed out
    if announced:
        debug(
            f"Timed out waiting {max_wait_seconds} seconds for CAPTCHA to be solved; "
            "stopping this apply flow."
        )
        return False

    # Never saw a CAPTCHA at all
    return True

def page_looks_like_captcha(driver: webdriver.Chrome) -> bool:
    """
    Best-effort check for common CAPTCHA patterns like
    the 'I'm not a robot' checkbox or generic human verification pages.

    This does NOT solve the CAPTCHA; it only detects that one is present.
    """
    # Try to detect Google reCAPTCHA or similar iframes
    try:
        frames = driver.find_elements(
            By.CSS_SELECTOR,
            "iframe[src*='recaptcha'], iframe[title*='recaptcha']",
        )
        for f in frames:
            try:
                if f.is_displayed():
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Try to detect typical "I'm not a robot" / verification text
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        txt = (body.text or "").lower()
        phrases = [
            "i'm not a robot",
            "i am not a robot",
            "verify that you are human",
            "verify you are human",
            "select all images with",
            "select all squares with",
        ]
        if any(p in txt for p in phrases):
            return True
    except Exception:
        pass

    return False


def detect_and_wait_for_captcha(
    driver: webdriver.Chrome,
    max_wait_seconds: int = 300,
    check_interval: float = 3.0,
) -> bool:
    """
    If a CAPTCHA / 'I'm not a robot' page is detected, notify the user and wait
    for manual solving. Returns True if either no CAPTCHA is seen, or it
    disappears before timeout. Returns False if it was seen but never solved.
    """
    start = time.time()
    seen = False

    while time.time() - start < max_wait_seconds:
        if page_looks_like_captcha(driver):
            if not seen:
                debug(
                    "Possible CAPTCHA detected "
                    "('I'm not a robot' or human verification page). "
                    "Waiting for manual solve..."
                )
                try:
                    speak(
                        "Captcha detected. "
                        "Please switch to the browser window and solve it manually. "
                        "The script will resume afterwards."
                    )
                except Exception:
                    pass
                seen = True

            time.sleep(check_interval)
            continue

        # No CAPTCHA currently detected
        if seen:
            debug("CAPTCHA no longer detected; resuming automation.")
        return True

    if seen:
        debug(
            f"Timed out waiting {max_wait_seconds} seconds for CAPTCHA to be solved. "
            "Stopping this apply flow."
        )
        return False

    # Never saw a CAPTCHA at all
    return True

def speak(text):
    """
    Best-effort text-to-speech.
    Uses pyttsx3 if installed; otherwise just prints.
    """
    try:
        import pyttsx3  # type: ignore
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except Exception:
        # Fallback: console only
        print(f"[speak] {text}", flush=True)


def prompt_for_new_gemini_key(previous_error=None):
    """
    Ask the user for a NEW Gemini API key when the current one is exhausted
    or invalid.

    - Speaks a message once.
    - Prints a clear prompt.
    - Waits for user input.
    - Saves the new key to:
        - environment variable GEMINI_API_KEY
        - BASE_DIR / 'gemini_api_key.txt'
    Returns the new key string, or None if user presses ENTER with no input.
    """
    print("\n[Gemini] Your Gemini API key appears exhausted or invalid.", flush=True)
    if previous_error is not None:
        print(f"[Gemini] Last error: {previous_error}", flush=True)

    print(
        "[Gemini] Please open Google AI Studio, create a NEW API key "
        "(for example in a new project), then paste it below.",
        flush=True,
    )
    print(
        "[Gemini] Press ENTER without typing anything to skip and continue "
        "WITHOUT Gemini for this run.",
        flush=True,
    )

    # Speak once
    if not getattr(prompt_for_new_gemini_key, "_spoken_once", False):
        try:
            speak("Gemini key exhausted. Please add a new valid key to continue.")
        except Exception:
            pass
        setattr(prompt_for_new_gemini_key, "_spoken_once", True)

    new_key = input("New GEMINI_API_KEY: ").strip()
    if not new_key:
        print("[Gemini] No key entered; will not use Gemini for this run.", flush=True)
        return None

    # Save to env
    os.environ["GEMINI_API_KEY"] = new_key

    # Save to file gemini_api_key.txt in BASE_DIR
    try:
        key_file = BASE_DIR / "gemini_api_key.txt"
        key_file.write_text(new_key, encoding="utf-8")
        print(f"[Gemini] Saved new key to {key_file}", flush=True)
    except Exception as e:
        print(f"[Gemini] Warning: could not save key to disk: {e}", flush=True)

    return new_key


def detect_linkedin_application_confirmation(
    driver: webdriver.Chrome,
    timeout: float = 4.0,
) -> bool:
    """
    Heuristically detect whether LinkedIn shows an 'Application submitted'
    style confirmation on the current page.

    Returns True if we see a typical confirmation phrase, False otherwise.
    """
    # Phrases LinkedIn commonly uses in banners/toasts after applying
    phrases = [
        "application submitted",
        "your application was sent",
        "your application has been submitted",
        "you successfully applied",
        "you applied",
        "already applied",
        "thanks for applying",
    ]

    end_time = time.time() + max(timeout, 0.0)
    while time.time() < end_time:
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            text = (body.text or "").lower()
            for p in phrases:
                if p in text:
                    debug(
                        f"Detected LinkedIn submission confirmation phrase: '{p}'. "
                        "Treating this job as successfully submitted."
                    )
                    return True
        except Exception:
            # If anything fails, just retry until timeout
            pass
        time.sleep(0.5)

    return False

def _fallback_execute_actions_on_page(page, actions):
    """
    Minimal fallback executor if your code doesn't already include execute_actions_on_page().
    Actions expected in the shape:
      {"type":"mouse","params":{"x":int,"y":int,"button":"left"}}
      {"type":"keyboard","params":{"text":"..."}}
      {"type":"scroll","params":{"dx":int,"dy":int}}
    Uses Playwright's page.mouse, page.keyboard and window.scrollBy() via evaluate().
    """
    try:
        for i, a in enumerate(actions or []):
            typ = a.get("type")
            params = a.get("params", {}) or {}
            if typ == "mouse":
                x = int(params.get("x", 0))
                y = int(params.get("y", 0))
                button = params.get("button", "left")
                # safe bounds clamp (avoid negative)
                if x < 0:
                    x = 0
                if y < 0:
                    y = 0
                print(f"[actions.exec] mouse click #{i} at ({x},{y}) btn={button}")
                page.mouse.click(x, y, button=button)
                time.sleep(0.3)

            elif typ == "keyboard":
                text = str(params.get("text", ""))
                # do not type secret placeholder automatically
                if "<TYPE_SECRET_HERE>" in text:
                    print("[actions.exec] keyboard action requests secret typing; skipping actual typing for safety.")
                    continue
                print(f"[actions.exec] keyboard typing #{i}: {text[:80]}")
                page.keyboard.type(text)
                time.sleep(0.15)

            elif typ == "scroll":
                dx = int(params.get("dx", 0))
                dy = int(params.get("dy", 0))
                print(f"[actions.exec] scroll #{i} by (dx={dx}, dy={dy})")
                try:
                    page.evaluate("window.scrollBy(arguments[0], arguments[1]);", dx, dy)
                except Exception as e:
                    print(f"[actions.exec] scroll failed: {e!r}")
                time.sleep(0.2)

            else:
                print(f"[actions.exec] unknown action type '{typ}' - skipping")
    except Exception as e:
        print(f"[actions.exec] failed to execute actions: {e!r}")



def request_and_execute_gemini_actions(
    page,
    model: str = "gemini-2.5-flash",
    max_retries: int = 2,
    ask_question: str = "The bot is stuck on this application page. Provide a short sequence of mouse/keyboard actions to proceed and submit the application."
) -> bool:
    """
    Capture the page (screenshot + html), ask Gemini for actions (via call_gemini_for_actions),
    then execute them on the page.

    Returns True if at least one action was executed, False otherwise.

    Usage: replace old Ollama block with:
        executed = request_and_execute_gemini_actions(page)
    """
    # Ensure imports exist in this file:
    # from gemini_actions import call_gemini_for_actions, resolve_gemini_api_key_from_env_or_disk
    # `time` must be imported at top-level; if not, import here
    import base64
    try:
        import time as _time_mod  # already used elsewhere; safe import
    except Exception:
        _time_mod = None

    # 1) capture page snapshot
    try:
        screenshot = page.screenshot(full_page=True)
    except Exception as e:
        print(f"[gemini_actions_helper] Failed to capture screenshot: {e!r}. Attempting viewport screenshot.")
        try:
            screenshot = page.screenshot()
        except Exception as e2:
            print(f"[gemini_actions_helper] Screenshot failed entirely: {e2!r}")
            return False

    try:
        html = page.content()
    except Exception as e:
        print(f"[gemini_actions_helper] Failed to get page HTML: {e!r}")
        html = ""

    # 2) Ask Gemini for actions
    try:
        # include ask_question into page_html to give better context if desired
        gemini_resp = call_gemini_for_actions(
            screenshot_bytes=screenshot,
            page_html=html + "\n\nQUESTION: " + ask_question,
            model=model,
            max_retries=max_retries,
        )
    except Exception as e:
        print(f"[gemini_actions_helper] call_gemini_for_actions raised: {e!r}")
        return False

    if not gemini_resp or not isinstance(gemini_resp, dict):
        print("[gemini_actions_helper] No valid response from Gemini.")
        return False

    actions = gemini_resp.get("actions", [])
    explain = gemini_resp.get("explain", "") or gemini_resp.get("explanation", "")

    if not actions:
        print(f"[gemini_actions_helper] Gemini suggested no actions. Explanation: {explain}")
        return False

    print(f"[gemini_actions_helper] Gemini suggested {len(actions)} actions. Explain: {explain!r}")

    # 3) execute actions via existing executor or fallback
    try:
        # prefer existing execute_actions_on_page if defined
        executor = globals().get("execute_actions_on_page")
        if callable(executor):
            executor(page, actions)
        else:
            _fallback_execute_actions_on_page(page, actions)
    except Exception as e:
        print(f"[gemini_actions_helper] Exception while executing actions: {e!r}")
        return False

    # small pause to let page update after actions
    try:
        time.sleep(1.2)
    except Exception:
        pass

    return True
# -------------------------------------------------------------------------

def capture_page_context_for_gemini(driver: webdriver.Chrome, container=None, redact_passwords: bool = True) -> Dict[str, Any]:
    """
    Backwards-compatible wrapper expected by other code.
    Captures a screenshot (saved under BASE_DIR/page_screenshots), sanitized HTML,
    and visible text. Redacts password inputs before screenshot if redact_passwords=True.

    Returns: {"screenshot_path": Path|None, "html": str, "visible_text": str}
    """
    # Reuse capture_page_context if already present
    try:
        # If capture_page_context exists (from previous helper), call it.
        if "capture_page_context" in globals():
            return capture_page_context(driver, container=container, redact_passwords=redact_passwords)
    except Exception:
        # fall through to local implementation below
        pass

    # Local implementation (same behavior if capture_page_context not present)
    try:
        ts = int(time.time())
        screenshots_dir = BASE_DIR / "page_screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshots_dir / f"page_snapshot_{ts}.png"

        # target element (container) or whole page body
        try:
            target = container if container is not None else driver.find_element(By.TAG_NAME, "body")
        except Exception:
            target = None

        # redact password fields before screenshot
        if redact_passwords:
            try:
                pw_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
                for inp in pw_inputs:
                    try:
                        driver.execute_script("arguments[0].value = '';", inp)
                        driver.execute_script("arguments[0].setAttribute('placeholder','[REDACTED]');", inp)
                    except Exception:
                        pass
            except Exception:
                pass

        # capture screenshot (try full-page then fallback to element)
        try:
            driver.save_screenshot(str(screenshot_path))
        except Exception:
            try:
                if target is not None:
                    target.screenshot(str(screenshot_path))
                else:
                    screenshot_path = None
            except Exception:
                screenshot_path = None

        # capture HTML
        html = ""
        try:
            if container is not None:
                html = container.get_attribute("outerHTML") or ""
            else:
                html = driver.execute_script("return document.documentElement.outerHTML;") or driver.page_source or ""
        except Exception:
            try:
                html = driver.page_source or ""
            except Exception:
                html = ""

        # sanitize HTML: strip input values, redact password values
        try:
            # remove value attributes (best-effort)
            html = re.sub(r'(<input\b[^>]*?)\svalue=(["\'])(.*?)\2', r'\1 value="\2[REDACTED]\2"', html, flags=re.IGNORECASE)
            # specifically redact password inputs
            html = re.sub(r'(<input[^>]+type=(["\'])password\2[^>]*?)\svalue=(["\'])(.*?)\3', r'\1 value="\3[REDACTED]\3"', html, flags=re.IGNORECASE)
        except Exception:
            pass

        # visible text
        visible_text = ""
        try:
            visible_text = driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            visible_text = ""

        return {"screenshot_path": screenshot_path if screenshot_path and screenshot_path.exists() else None, "html": html, "visible_text": visible_text}
    except Exception as e:
        debug(f"capture_page_context_for_gemini: failed to capture page context: {e!r}")
        return {"screenshot_path": None, "html": "", "visible_text": ""}

def call_gemini_for_page_recovery(
    api_key: str,
    page_html: str,
    visible_text: str,
    screenshot_path: Optional[Path],
    problem_description: str,
    phase: str,
    max_actions: int = 8,
) -> Optional[Dict[str, Any]]:
    """
    Ask Gemini to provide a small ordered list of actions to recover from a stuck page.

    Gemini must return ONLY three kinds of actions:

      1) Click at viewport pixel centroid:
         {
           "type": "click",
           "x": <int>,
           "y": <int>,
           "wait": <float optional>
         }

      2) Type into a field:
         {
           "type": "type",
           "by": "css" | "xpath" | "id" | "name" | "text" | null,
           "selector": "<selector or text or null>",
           "text": "<text to type>",
           "clear": true | false,
           "wait": <float optional>
         }

      3) Wait:
         {
           "type": "wait",
           "seconds": <float>
         }

    NO scroll, NO key‑press actions.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        debug("call_gemini_for_page_recovery: no API key supplied.")
        return None

    # Build context snippets (keep them reasonably small)
    visible_text_snip = (visible_text or "")[:4000]
    html_snip = (page_html or "")[:8000]

    screenshot_info = ""
    if screenshot_path is not None:
        try:
            if screenshot_path.exists():
                import base64 as _b64
                raw_bytes = screenshot_path.read_bytes()
                b64 = _b64.b64encode(raw_bytes).decode("ascii")
                screenshot_info = f"SCREENSHOT_BASE64 (truncated): {b64[:3000]}"
        except Exception as e:
            debug(f"call_gemini_for_page_recovery: failed to read screenshot: {e!r}")

    prompt = f"""
You are helping a Selenium-based bot recover when it gets stuck on a web page.

You MUST output EXACTLY ONE JSON object and NOTHING ELSE, with this shape:

{{
  "actions": [
    {{
      "type": "click",
      "x": <int>,              // viewport pixel X of the visual centre of the clickable element
      "y": <int>,              // viewport pixel Y of the visual centre of the clickable element
      "wait": <float optional> // seconds to wait after the click
    }},
    {{
      "type": "type",
      "by": "css" | "xpath" | "id" | "name" | "text" | null,
      "selector": "<selector or text or null>",
      "text": "<text to type>",
      "clear": true | false,
      "wait": <float optional>
    }},
    {{
      "type": "wait",
      "seconds": <float>
    }}
  ],
  "comment": "short explanation"
}}

Rules:
- Only use "click", "type", or "wait" actions.
- Do NOT use scroll or key-press actions (ENTER, TAB, etc.).
- For click:
    - Buttons may have no text or be icon-only.
    - Always click by viewport pixel coordinates ("x","y") of the element's centre.
- For type:
    - If you know which field, use 'by' + 'selector' ("css","xpath","id","name","text").
    - If selector is unclear, you may set selector=null to type into the currently focused field.
- Use at most {max_actions} actions.
- If you are unsure, return {{"actions": [], "comment": "I am unsure; manual intervention needed."}}.

Phase: {phase}
Problem description:
{problem_description}

VISIBLE TEXT (truncated):
{visible_text_snip}

HTML (truncated):
{html_snip}

{screenshot_info}
""".strip()

    # ----- Call Gemini -----
    try:
        client = get_gemini_client(api_key)
    except Exception as e:
        debug(f"call_gemini_for_page_recovery: get_gemini_client failed: {e!r}")
        return None

    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.15,
                max_output_tokens=768,
                # text/plain is more robust; we will extract the JSON manually.
                response_mime_type="text/plain",
            ),
        )
    except Exception as e:
        debug(f"call_gemini_for_page_recovery: generate_content failed: {e!r}")
        return None

    # ----- Extract raw text from the Gemini response -----
    raw = ""
    try:
        if getattr(resp, "text", None):
            raw = str(resp.text)
        else:
            # Fallback: concatenate text parts if resp.text is not populated
            pieces = []
            for cand in getattr(resp, "candidates", []) or []:
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", []) or []:
                    txt = getattr(part, "text", None)
                    if txt:
                        pieces.append(str(txt))
            raw = "\n".join(pieces)
    except Exception as e:
        debug(f"call_gemini_for_page_recovery: failed to extract text from response: {e!r}")
        raw = ""

    raw = (raw or "").strip()
    debug(f"call_gemini_for_page_recovery: raw Gemini response (truncated): {raw[:400]!r}")

    if not raw:
        debug("call_gemini_for_page_recovery: empty response from Gemini")
        return None

    # Strip common Markdown code fences if present
    if raw.startswith("```"):
        # remove leading ```json / ``` etc
        raw = re.sub(r"^```[a-zA-Z0-9_-]*", "", raw).strip()
        # remove trailing ```
        raw = re.sub(r"```\\s*$", "", raw).strip()

    # ----- Parse JSON, with very forgiving salvage -----
    try:
        blob: Any = json.loads(raw)
    except Exception:
        # Try to locate the first {...} block in the text
        s = raw.find("{")
        e = raw.rfind("}")
        if s == -1 or e == -1 or e <= s:
            debug("call_gemini_for_page_recovery: could not find JSON object in Gemini response")
            return None
        json_str = raw[s : e + 1]
        try:
            blob = json.loads(json_str)
        except Exception as e2:
            debug(f"call_gemini_for_page_recovery: JSON parse failed even after trimming: {e2!r}")
            return None

    if not isinstance(blob, dict):
        debug("call_gemini_for_page_recovery: top-level JSON is not an object")
        return None

    actions = blob.get("actions")
    if not isinstance(actions, list):
        debug("call_gemini_for_page_recovery: 'actions' is not a list")
        actions = []

    sanitized: List[Dict[str, Any]] = []
    allowed_types = {"click", "type", "wait"}

    for act in actions[:max_actions]:
        if not isinstance(act, dict):
            continue
        t = (act.get("type") or "").strip().lower()
        if t not in allowed_types:
            continue

        safe: Dict[str, Any] = {"type": t}

        if t == "click":
            x = act.get("x")
            y = act.get("y")
            centroid = act.get("centroid")
            if (x is None or y is None) and isinstance(centroid, dict):
                x = centroid.get("x")
                y = centroid.get("y")
            try:
                x = int(x)
                y = int(y)
            except Exception:
                debug("call_gemini_for_page_recovery: click missing valid x/y; skipping.")
                continue
            safe["x"] = x
            safe["y"] = y
            if "wait" in act:
                try:
                    w = float(act["wait"])
                    safe["wait"] = max(0.0, min(w, 10.0))
                except Exception:
                    pass

        elif t == "type":
            by = (act.get("by") or "css").strip().lower()
            selector = act.get("selector")
            text_val = act.get("text", "")
            clear = bool(act.get("clear", False))

            if by not in {"css", "xpath", "id", "name", "text"}:
                by = "css"

            safe.update(
                {
                    "by": by,
                    "selector": selector if selector is not None else None,
                    "text": str(text_val),
                    "clear": clear,
                }
            )
            if "wait" in act:
                try:
                    w = float(act["wait"])
                    safe["wait"] = max(0.0, min(w, 10.0))
                except Exception:
                    pass

        elif t == "wait":
            sec = act.get("seconds", act.get("wait", 0.5))
            try:
                sec = float(sec)
            except Exception:
                sec = 0.5
            safe["seconds"] = max(0.0, min(sec, 15.0))

        sanitized.append(safe)

    comment = str(blob.get("comment") or "").strip()
    return {"actions": sanitized, "comment": comment}

def execute_gemini_actions(
    driver: webdriver.Chrome,
    actions: List[Dict[str, Any]],
    max_retries: int = 1,  # kept for signature compatibility
) -> bool:
    """
    Execute a sanitized list of actions on the driver.

    Supported actions (from call_gemini_for_page_recovery):

      - CLICK at viewport pixel centroid:
          { "type": "click", "x": int, "y": int, "wait": float? }

      - TYPE into a field:
          {
            "type": "type",
            "by": "css" | "xpath" | "id" | "name" | "text",
            "selector": "<selector or text or null>",
            "text": "<string>",
            "clear": bool,
            "wait": float?
          }

      - WAIT:
          { "type": "wait", "seconds": float }

    NO scroll, NO key-press actions.
    """
    if not actions:
        debug("execute_gemini_actions: no actions to execute.")
        return False

    executed_any = False

    for idx, act in enumerate(actions):
        try:
            t = (act.get("type") or "").strip().lower()
            if t not in {"click", "type", "wait"}:
                debug(f"execute_gemini_actions: skipping unsupported action type {t!r}")
                continue

            default_wait = float(act.get("wait", 0.35))

            if t == "wait":
                sec = act.get("seconds", default_wait)
                try:
                    sec = float(sec)
                except Exception:
                    sec = default_wait
                if sec > 0:
                    time.sleep(sec)
                executed_any = True
                continue

            if t == "click":
                x = act.get("x")
                y = act.get("y")
                centroid = act.get("centroid")
                if (x is None or y is None) and isinstance(centroid, dict):
                    x = centroid.get("x")
                    y = centroid.get("y")

                elem = None
                ok = False

                if x is not None and y is not None:
                    try:
                        ok = _click_at_viewport_coordinate(driver, int(x), int(y))
                    except Exception as e:
                        debug(f"execute_gemini_actions: centroid click invalid coords: {e!r}")
                        ok = False
                if ok:
                    executed_any = True
                    if default_wait > 0:
                        time.sleep(default_wait)
                    continue

                # Fallback: if Gemini also supplied a selector, try that
                by = (act.get("by") or "").strip().lower()
                selector = act.get("selector")
                if selector:
                    try:
                        if by in {"", "css", "css_selector"}:
                            elem = driver.find_element(By.CSS_SELECTOR, selector)
                        elif by in {"xpath", "xp"}:
                            elem = driver.find_element(By.XPATH, selector)
                        elif by == "id":
                            elem = driver.find_element(By.ID, selector)
                        elif by == "name":
                            elem = driver.find_element(By.NAME, selector)
                        elif by == "text":
                            xpath = f"//*[contains(normalize-space(.), {json.dumps(selector)})]"
                            elem = driver.find_element(By.XPATH, xpath)
                        else:
                            elem = driver.find_element(By.CSS_SELECTOR, selector)
                    except Exception as e:
                        debug(
                            f"execute_gemini_actions: click selector not found {selector!r} (by={by}): {e!r}"
                        )
                        elem = None

                if elem is None:
                    debug("execute_gemini_actions: click action had no usable coords or element; skipping.")
                    continue

                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
                except Exception:
                    pass
                try:
                    elem.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", elem)
                    except Exception as e:
                        debug(f"execute_gemini_actions: selector-based click failed: {e!r}")
                        continue

                executed_any = True
                if default_wait > 0:
                    time.sleep(default_wait)
                continue

            if t == "type":
                by = (act.get("by") or "css").strip().lower()
                selector = act.get("selector")
                text_value = act.get("text", "")
                if not isinstance(text_value, str):
                    text_value = str(text_value)
                clear_flag = bool(act.get("clear", True))

                elem = None
                if selector:
                    try:
                        if by in {"", "css", "css_selector"}:
                            elem = driver.find_element(By.CSS_SELECTOR, selector)
                        elif by in {"xpath", "xp"}:
                            elem = driver.find_element(By.XPATH, selector)
                        elif by == "id":
                            elem = driver.find_element(By.ID, selector)
                        elif by == "name":
                            elem = driver.find_element(By.NAME, selector)
                        elif by == "text":
                            xpath = f"//*[contains(normalize-space(.), {json.dumps(selector)})]"
                            elem = driver.find_element(By.XPATH, xpath)
                        else:
                            elem = driver.find_element(By.CSS_SELECTOR, selector)
                    except Exception as e:
                        debug(
                            f"execute_gemini_actions: type selector not found {selector!r} (by={by}): {e!r}"
                        )
                        elem = None

                if elem is not None:
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
                    except Exception:
                        pass
                    try:
                        if clear_flag:
                            try:
                                elem.clear()
                            except Exception:
                                driver.execute_script("arguments[0].value='';", elem)
                        elem.click()
                        ActionChains(driver).send_keys(text_value).perform()
                        if default_wait > 0:
                            time.sleep(default_wait)
                        executed_any = True
                    except Exception as e:
                        debug(f"execute_gemini_actions: type action failed: {e!r}")
                    continue

                # If no element but we still have text, type into the currently focused field
                try:
                    ActionChains(driver).send_keys(text_value).perform()
                    if default_wait > 0:
                        time.sleep(default_wait)
                    executed_any = True
                except Exception as e:
                    debug(f"execute_gemini_actions: type-without-selector failed: {e!r}")

        except Exception as e:
            debug(f"execute_gemini_actions: exception on action #{idx}: {e!r}")
            continue

    return executed_any



def ensure_form_answers_applied_and_recover(
    driver: webdriver.Chrome,
    container,
    resume_plain: str,
    applicant_profile: Dict[str, Any],
    job_description: str,
    gemini_api_key: Optional[str],
    job_index: int,
    step_index: int,
    job_title: str,
    mode: str,
    max_gemini_steps: int = 1,
    max_recovery_steps: int = 3,
    pass1_gemini_answers: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Robust orchestrator for a single Easy Apply step.

    It inspects the visible form fields inside `container`, figures out which ones
    are still empty (including dropdowns and yes/no / MCQ questions), and then
    tries to fill them using:

      1) Cached answers saved on disk for this job & step
         (from previous runs in `form_answers/`).
      2) Optional `pass1_gemini_answers` – answers that were computed earlier in a
         "pass 1" phase where you sent screenshots / HTML for all steps to Gemini
         in a single prompt.
      3) A fresh Gemini call for only the fields that are *still* missing.
      4) If we are still stuck after Gemini or we cannot call Gemini, a final
         `try_gemini_page_recovery` call that lets Gemini drive the mouse/keyboard
         to get the page unstuck.

    The function never overwrites non‑empty DOM fields. It only touches fields
    that are detected as empty.

    It returns True if it believes the step is in a reasonable state to continue
    (no obvious empty fields after all attempts), and False only for hard errors
    where even recovery failed.
    """
    # Small helper so we always have the same answer structure
    def _empty_answer_dict() -> Dict[str, Any]:
        return {
            "text_fields": {},
            "textareas": {},
            "select_fields": {},
            "radio_groups": {},
            "checkboxes": {},
        }

    # Prefer key from env/disk if available (non‑interactive here)
    resolved_key = resolve_gemini_api_key_from_env_or_disk(interactive=False)
    if resolved_key:
        gemini_api_key = resolved_key

    # Quick guard
    if container is None:
        debug("ensure_form_answers_applied_and_recover: container is None; nothing to do.")
        return False

    # Build schema for this container
    try:
        form_schema = build_form_schema(container)
    except Exception as e:
        debug(f"ensure_form_answers_applied_and_recover: build_form_schema failed: {e!r}")
        # If we cannot even understand the form, try a generic page recovery
        try:
            try_gemini_page_recovery(
                driver=driver,
                container=container,
                gemini_api_key=gemini_api_key or "",
                problem_description="Could not build form schema for this step.",
                phase=f"{mode}-schema-failed",
                max_steps=max_recovery_steps,
            )
            return True
        except Exception as e2:
            debug(f"ensure_form_answers_applied_and_recover: recovery after schema failure also failed: {e2!r}")
            return False

    # If there are no recognizable fields, nothing to do
    if not (
        form_schema.get("text_fields")
        or form_schema.get("textareas")
        or form_schema.get("select_fields")
        or form_schema.get("radio_groups")
        or form_schema.get("checkboxes")
    ):
        debug(
            f"ensure_form_answers_applied_and_recover: no recognizable fields "
            f"for {mode} step {step_index+1}; skipping."
        )
        return True

    # Helper: locate the DOM element for a schema item
    def _find_elem(tag: str, field: Dict[str, Any]):
        elem = None
        elem_id = (field.get("id") or "").strip()
        name_attr = (field.get("name") or "").strip()
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"{tag}[name='{name_attr}']")
            except Exception:
                elem = None
        return elem

    # Helper: is a field "filled" in the DOM?
    # Important: radio_groups here cover yes/no and other single‑choice MCQ/SCQ sets.
    def _is_filled(field_type: str, item: Dict[str, Any]) -> bool:
        try:
            if field_type == "text":
                el = _find_elem("input", item)
                if not el:
                    return False
                if not el.is_displayed() or not el.is_enabled():
                    return True
                val = (el.get_attribute("value") or "").strip()
                return bool(val)

            if field_type == "textarea":
                el = _find_elem("textarea", item)
                if not el:
                    return False
                if not el.is_displayed() or not el.is_enabled():
                    return True
                val = (el.get_attribute("value") or "").strip()
                return bool(val)

            if field_type == "select":
                el = _find_elem("select", item)
                if not el:
                    return False
                if not el.is_displayed() or not el.is_enabled():
                    return True
                sel = Select(el)
                selected = sel.all_selected_options
                if not selected:
                    return False
                txt = (selected[0].text or "").strip()
                if not txt:
                    return False
                lowered = txt.lower()
                placeholders = {
                    "select",
                    "select an option",
                    "choose",
                    "choose an option",
                    "please select",
                }
                if lowered in placeholders:
                    return False
                return True

            if field_type == "radio":
                """
                For yes/no or MCQ radio groups, treat the group as FILLED
                **only if at least one underlying <input type="radio"> is selected**.
                We do NOT require the inputs to be visible, because LinkedIn often
                hides the real inputs and uses a styled wrapper.
                """
                inputs_meta = item.get("inputs") or []
                radios = []

                # Collect radios by id / name from the schema metadata
                for meta in inputs_meta:
                    rid = (meta.get("id") or "").strip()
                    rname = (meta.get("name") or "").strip()
                    if rid:
                        try:
                            radios.append(container.find_element(By.ID, rid))
                        except Exception:
                            pass
                    if rname:
                        try:
                            radios.extend(
                                container.find_elements(
                                    By.CSS_SELECTOR,
                                    f"input[type='radio'][name='{rname}']",
                                )
                            )
                        except Exception:
                            pass

                # Fallback: use the group's own name attribute if no inputs_meta worked
                if not radios:
                    name_attr = (item.get("name") or "").strip()
                    if name_attr:
                        try:
                            radios = container.find_elements(
                                By.CSS_SELECTOR,
                                f"input[type='radio'][name='{name_attr}']",
                            )
                        except Exception:
                            radios = []

                if not radios:
                    # No radios found at all → treat as NOT filled so Gemini/page recovery
                    # can still try to do something with this question.
                    return False

                # Group is filled only if ANY radio input is actually selected
                for r in radios:
                    try:
                        if r.is_selected():
                            return True
                    except Exception:
                        continue
                return False


            if field_type == "checkbox":
                el = _find_elem("input", item)
                if not el:
                    return False
                if not el.is_displayed() or not el.is_enabled():
                    return True
                # If required, we insist it be ticked; otherwise treat unticked as "missing"
                req = (el.get_attribute("required") or "").lower()
                required = bool(req) or ("required" in req)
                if required and not el.is_selected():
                    return False
                return el.is_selected()

        except Exception:
            return False

    # Helper: walk the schema and return a map of missing keys by section
    def _compute_missing() -> Dict[str, List[str]]:
        missing: Dict[str, List[str]] = {
            "text_fields": [],
            "textareas": [],
            "select_fields": [],
            "radio_groups": [],
            "checkboxes": [],
        }
        try:
            for it in form_schema.get("text_fields", []):
                k = it.get("field_key")
                if k and not _is_filled("text", it):
                    missing["text_fields"].append(k)
            for it in form_schema.get("textareas", []):
                k = it.get("field_key")
                if k and not _is_filled("textarea", it):
                    missing["textareas"].append(k)
            for it in form_schema.get("select_fields", []):
                k = it.get("field_key")
                if k and not _is_filled("select", it):
                    missing["select_fields"].append(k)
            for it in form_schema.get("radio_groups", []):
                k = it.get("group_key")
                if k and not _is_filled("radio", it):
                    missing["radio_groups"].append(k)
            for it in form_schema.get("checkboxes", []):
                k = it.get("box_key")
                if k and not _is_filled("checkbox", it):
                    missing["checkboxes"].append(k)
        except Exception as e:
            debug(f"ensure_form_answers_applied_and_recover: computing missing fields failed: {e!r}")
        return missing

    def _missing_count(m: Dict[str, List[str]]) -> int:
        return sum(len(v) for v in m.values())

    missing = _compute_missing()
    total_missing = _missing_count(missing)
    if total_missing == 0:
        debug("ensure_form_answers_applied_and_recover: no empty fields detected; nothing to do.")
        return True

    debug(
        "ensure_form_answers_applied_and_recover: initial missing keys for "
        f"{mode} step {step_index+1}: "
        + ", ".join(f"{k}={len(v)}" for k, v in missing.items())
    )

    # ---- 1) Load cached answers from disk (memory-first) ----
    cached: Optional[Dict[str, Any]] = None
    try:
        try:
            cached = load_form_answers_from_file(
                job_index=job_index,
                step_index=step_index,
                job_title=job_title,
                mode=mode,
            )
        except TypeError:
            # Backwards compatible with older 3‑arg version
            cached = load_form_answers_from_file(job_index, step_index, mode)  # type: ignore[arg-type]
    except Exception as e:
        debug(f"ensure_form_answers_applied_and_recover: error loading cached answers: {e!r}")
        cached = None

    # Combine caches: start with pass1 answers, then layer on disk cache so cached answers win when they overlap
    combined_memory = _empty_answer_dict()
    if pass1_gemini_answers:
        try:
            combined_memory = merge_gemini_answer_dicts(combined_memory, pass1_gemini_answers)
        except Exception as e:
            debug(f"ensure_form_answers_applied_and_recover: merging pass1_gemini_answers failed: {e!r}")
    if cached:
        try:
            combined_memory = merge_gemini_answer_dicts(combined_memory, cached)
        except Exception as e:
            debug(f"ensure_form_answers_applied_and_recover: merging cached answers failed: {e!r}")

    # Apply whatever we already know for the currently-missing keys
    def _apply_answers(source: Dict[str, Any], missing_map: Dict[str, List[str]], label: str) -> None:
        if not source:
            return
        to_apply = _empty_answer_dict()
        for sect in to_apply.keys():
            src_sec = source.get(sect) or {}
            for k in missing_map.get(sect, []):
                v = src_sec.get(k)
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                to_apply[sect][k] = v
        if any(len(v) for v in to_apply.values()):
            debug(f"Applying {label} answers for missing fields.")
            try:
                apply_gemini_answers_to_form(driver, container, form_schema, to_apply)
            except Exception as e:
                debug(f"ensure_form_answers_applied_and_recover: error applying {label} answers: {e!r}")

    _apply_answers(combined_memory, missing, label="cached/pass1")

    # Recompute missing after applying known answers
    missing = _compute_missing()
    total_missing = _missing_count(missing)
    if total_missing == 0:
        debug("ensure_form_answers_applied_and_recover: cached/pass1 answers covered all missing fields.")
        # Persist combined memory so future runs reuse it
        try:
            save_form_answers_to_file(
                job_index=job_index,
                step_index=step_index,
                job_title=job_title,
                mode=mode,
                answers=combined_memory,
            )
        except Exception as e:
            debug(f"ensure_form_answers_applied_and_recover: failed to save combined memory answers: {e!r}")
        return True

    # ---- 2) Gemini calls for remaining fields ----
    if not gemini_api_key:
        debug(
            "ensure_form_answers_applied_and_recover: Gemini API key is missing; "
            "skipping AI form filling and attempting page recovery instead."
        )
        try:
            try_gemini_page_recovery(
                driver=driver,
                container=container,
                gemini_api_key="",
                problem_description="Missing Gemini API key while some form fields are still empty.",
                phase=f"{mode}-no-key",
                max_steps=max_recovery_steps,
            )
        except Exception as e:
            debug(f"ensure_form_answers_applied_and_recover: page recovery after missing key failed: {e!r}")
        return True

    # Use combined_memory as the base answers we already know
    existing_answers = combined_memory

    for attempt in range(max_gemini_steps):
        missing_now = _compute_missing()
        if _missing_count(missing_now) == 0:
            break

        debug(
            f"Calling Gemini to fill {_missing_count(missing_now)} remaining fields "
            f"(attempt {attempt+1}/{max_gemini_steps})."
        )

        gemini_out: Optional[Dict[str, Any]] = None
        try:
            try:
                gemini_out = call_gemini_for_form_answers(
                    resume_text=resume_plain,
                    applicant_profile=applicant_profile,
                    job_description=job_description,
                    form_schema=form_schema,
                    api_key=gemini_api_key,
                    existing_answers=existing_answers,
                    missing_keys=missing_now,
                )
            except TypeError:
                # Backwards compatibility with older signature that did not take missing_keys
                gemini_out = call_gemini_for_form_answers(
                    resume_text=resume_plain,
                    applicant_profile=applicant_profile,
                    job_description=job_description,
                    form_schema=form_schema,
                    api_key=gemini_api_key,
                    existing_answers=existing_answers,
                )
        except Exception as e:
            debug(f"ensure_form_answers_applied_and_recover: Gemini call failed on attempt {attempt+1}: {e!r}")
            break

        if not gemini_out:
            debug("ensure_form_answers_applied_and_recover: Gemini returned no data; stopping Gemini attempts.")
            break

        # Merge new answers into our memory (new values override older ones when non‑empty)
        try:
            existing_answers = merge_gemini_answer_dicts(existing_answers, gemini_out)
        except Exception as e:
            debug(
                "ensure_form_answers_applied_and_recover: merge_gemini_answer_dicts "
                f"failed: {e!r}; using raw Gemini output."
            )
            existing_answers = gemini_out

        # Save merged answers so future runs can reuse them
        try:
            save_form_answers_to_file(
                job_index=job_index,
                step_index=step_index,
                job_title=job_title,
                mode=mode,
                answers=existing_answers,
            )
        except Exception as e:
            debug(f"ensure_form_answers_applied_and_recover: failed to save Gemini answers: {e!r}")

        # Apply only the answers for fields that are still missing
        _apply_answers(existing_answers, missing_now, label=f"Gemini attempt {attempt+1}")

        # Recheck after applying
        missing_after = _compute_missing()
        if _missing_count(missing_after) == 0:
            debug("ensure_form_answers_applied_and_recover: Gemini filled all remaining fields.")
            return True

    # ---- 3) If things are still missing, try page recovery as a last resort ----
    remaining = _compute_missing()
    if _missing_count(remaining) > 0:
        debug(
            "ensure_form_answers_applied_and_recover: fields are still missing after "
            "Gemini attempts; invoking Gemini-driven page recovery."
        )
        try:
            try_gemini_page_recovery(
                driver=driver,
                container=container,
                gemini_api_key=gemini_api_key or "",
                problem_description="Form fields still missing or page appears stuck after Gemini filling.",
                phase=f"{mode}-post-fill",
                max_steps=max_recovery_steps,
            )
        except Exception as e:
            debug(f"ensure_form_answers_applied_and_recover: page recovery after Gemini also failed: {e!r}")
            return False

    return True



def is_any_field_empty(container) -> bool:
    """
    Inspect 'container' for visible/enabled form controls and return True
    if any one of them appears empty / unselected / placeholder-like.
    """
    try:
        schema = build_form_schema(container)
    except Exception:
        debug("is_any_field_empty: build_form_schema failed; assuming fields may be empty.")
        return True

    def _is_text_field_empty(item: Dict[str, Any]) -> bool:
        elem = None
        elem_id = (item.get("id") or "").strip()
        name_attr = (item.get("name") or "").strip()
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"input[name='{name_attr}']")
            except Exception:
                elem = None
        if not elem:
            return False
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                return False
            val = (elem.get_attribute("value") or "").strip()
            return val == ""
        except Exception:
            return True

    def _is_textarea_empty(item: Dict[str, Any]) -> bool:
        elem = None
        elem_id = (item.get("id") or "").strip()
        name_attr = (item.get("name") or "").strip()
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"textarea[name='{name_attr}']")
            except Exception:
                elem = None
        if not elem:
            return False
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                return False
            val = (elem.get_attribute("value") or "").strip()
            return val == ""
        except Exception:
            return True

    def _is_select_empty(item: Dict[str, Any]) -> bool:
        elem = None
        elem_id = (item.get("id") or "").strip()
        name_attr = (item.get("name") or "").strip()
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"select[name='{name_attr}']")
            except Exception:
                elem = None
        if not elem:
            return False
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                return False
            sel = Select(elem)
            selected = sel.all_selected_options
            if not selected:
                return True
            text = (selected[0].text or "").strip().lower()
            value = (selected[0].get_attribute("value") or "").strip().lower()
            placeholders = {"select", "select one", "please select", "choose", "choose one", "select an option"}
            if (not text and not value) or any(p in text for p in placeholders):
                return True
            return False
        except Exception:
            return True

    def _is_radio_group_empty(item: Dict[str, Any]) -> bool:
        inputs_meta = item.get("inputs") or []
        radios = []
        for meta in inputs_meta:
            rid = (meta.get("id") or "").strip()
            rname = (meta.get("name") or "").strip()
            if rid:
                try:
                    radios.append(container.find_element(By.ID, rid))
                except Exception:
                    pass
            if rname:
                try:
                    radios.extend(container.find_elements(By.CSS_SELECTOR, f"input[type='radio'][name='{rname}']"))
                except Exception:
                    pass

        if not radios:
            name_attr = (item.get("name") or "").strip()
            if name_attr:
                try:
                    radios = container.find_elements(By.CSS_SELECTOR, f"input[type='radio'][name='{name_attr}']")
                except Exception:
                    radios = []

        visible_radios = []
        for r in radios:
            try:
                if r.is_displayed() and r.is_enabled():
                    visible_radios.append(r)
            except Exception:
                continue

        if not visible_radios:
            return False

        for r in visible_radios:
            try:
                if r.is_selected():
                    return False
            except Exception:
                continue
        return True

    def _is_checkbox_empty(item: Dict[str, Any]) -> bool:
        elem = None
        elem_id = (item.get("id") or "").strip()
        name_attr = (item.get("name") or "").strip()
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"input[type='checkbox'][name='{name_attr}']")
            except Exception:
                elem = None
        if not elem:
            return False
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                return False
            req = (elem.get_attribute("required") or "").lower()
            required = bool(req) or "required" in req
            if required and not elem.is_selected():
                return True
            return False
        except Exception:
            return True

    try:
        for tf in schema.get("text_fields", []):
            if _is_text_field_empty(tf):
                debug(f"is_any_field_empty: detected empty text field {tf.get('field_key')}")
                return True
        for ta in schema.get("textareas", []):
            if _is_textarea_empty(ta):
                debug(f"is_any_field_empty: detected empty textarea {ta.get('field_key')}")
                return True
        for sel in schema.get("select_fields", []):
            if _is_select_empty(sel):
                debug(f"is_any_field_empty: detected empty select {sel.get('field_key')}")
                return True
        for rg in schema.get("radio_groups", []):
            if _is_radio_group_empty(rg):
                debug(f"is_any_field_empty: detected empty radio group {rg.get('group_key')}")
                return True
        for cb in schema.get("checkboxes", []):
            if _is_checkbox_empty(cb):
                debug(f"is_any_field_empty: detected empty checkbox {cb.get('box_key')}")
                return True
    except Exception as e:
        debug(f"is_any_field_empty: exception while checking fields: {e!r}")
        return True

    return False



def ensure_form_answers_applied(
    driver: webdriver.Chrome,
    container,
    resume_plain: str,
    applicant_profile: Dict[str, Any],
    job_description: str,
    gemini_api_key: str,
    job_index: int,
    step_index: int,
    job_title: str,
    mode: str,
) -> None:
    """
    Before invoking the heavy memory/Gemini logic, check *every* visible field.
    If any empty -> call answer_form_with_gemini_for_container(...).
    Otherwise skip.
    """
    if container is None:
        debug("ensure_form_answers_applied: container is None; skipping.")
        return

    # Prefer key from gemini_api_key.txt if available
    disk_key = load_gemini_api_key_from_disk()
    if disk_key:
        gemini_api_key = disk_key


    try:
        if is_any_field_empty(container):
            debug(
                f"ensure_form_answers_applied: detected empty fields for {mode} "
                f"step {step_index+1}; invoking Gemini/memory logic."
            )
            answer_form_with_gemini_for_container(
                driver=driver,
                container=container,
                resume_plain=resume_plain,
                applicant_profile=applicant_profile,
                job_description=job_description,
                gemini_api_key=gemini_api_key,
                job_index=job_index,
                step_index=step_index,
                job_title=job_title,
                mode=mode,
            )
        else:
            debug(
                f"ensure_form_answers_applied: all visible fields appear filled for "
                f"{mode} step {step_index+1}; skipping Gemini."
            )
    except Exception as e:
        debug(f"ensure_form_answers_applied: error while checking/applying answers: {e!r}")



def capture_page_context(driver: webdriver.Chrome, container=None, redact_passwords=True) -> Dict[str, Any]:
    """
    Capture screenshot + sanitized HTML + visible text of the page or a specific container.
    Returns dict: {"screenshot_path": Path, "html": str, "visible_text": str}
    - If redact_passwords is True: password inputs are cleared before screenshot.
    """
    ts = int(time.time())
    screenshots_dir = BASE_DIR / "page_screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshots_dir / f"page_snapshot_{ts}.png"

    try:
        # If container provided, operate on it; else use whole page
        target = container if container is not None else driver.find_element(By.TAG_NAME, "body")

        # Sanitize password fields by clearing them (so screenshots don't include passwords)
        if redact_passwords:
            try:
                pw_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
                for i in pw_inputs:
                    try:
                        # clear value and set placeholder so screenshot won't show actual password
                        driver.execute_script("arguments[0].value = '';", i)
                        driver.execute_script("arguments[0].setAttribute('placeholder','[REDACTED]');", i)
                    except Exception:
                        pass
            except Exception:
                pass

        # Full-page screenshot (Selenium) — prefer driver.save_screenshot for whole page
        try:
            # try full-page via webdriver's save_screenshot
            driver.save_screenshot(str(screenshot_path))
        except Exception:
            # fallback: element screenshot if supported
            try:
                target.screenshot(str(screenshot_path))
            except Exception:
                debug("capture_page_context: failed to capture screenshot")
                screenshot_path = None

        # Grab HTML of the container or entire document
        html = ""
        try:
            if container is not None:
                html = container.get_attribute("outerHTML") or ""
            else:
                html = driver.execute_script("return document.documentElement.outerHTML;")
        except Exception:
            try:
                html = driver.page_source or ""
            except Exception:
                html = ""

        # Optionally sanitize HTML: remove values of inputs (except maybe non-sensitive)
        try:
            # remove actual 'value' attributes to avoid sending user data
            sanitized_html = re.sub(r'(<input\b[^>]*?)\svalue=(["\'])(.*?)(\2)', r'\1 value="\2REDACTED\2"', html, flags=re.IGNORECASE)
            # specifically strip password fields' value attributes
            sanitized_html = re.sub(r'(<input[^>]+type=(["\'])password\2[^>]*?)\svalue=(["\'])(.*?)(\3)', r'\1 value="\3[REDACTED]\3"', sanitized_html, flags=re.IGNORECASE)
            html = sanitized_html
        except Exception:
            pass

        # Visible text (best-effort)
        visible_text = ""
        try:
            visible_text = driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            visible_text = ""

        return {"screenshot_path": screenshot_path, "html": html, "visible_text": visible_text}
    except Exception as e:
        debug(f"capture_page_context: error capturing page context: {e!r}")
        return {"screenshot_path": None, "html": "", "visible_text": ""}



def mouse_action(
    driver: webdriver.Chrome,
    action: str,
    locator_type: Optional[str] = None,
    locator: Optional[str] = None,
    offset_x: Optional[int] = None,
    offset_y: Optional[int] = None,
) -> None:
    """
    Generic mouse helper.

    For *Gemini*-driven recovery, you MUST use:
        action      = "click"
        locator_type = "coords"
        locator     = None
        offset_x    = <viewport pixel X of button centre>
        offset_y    = <viewport pixel Y of button centre>

    For non-Gemini code, legacy usage still works:
        - "click" with locator_type in {"css","xpath","id","text"}
        - "scroll" with offset_x/offset_y (scrollBy) if you use it yourself

    NOTE: Gemini is told NOT to use scrolling anymore.
    """
    action_norm = (action or "").strip().lower()
    loc_type = (locator_type or "").strip().lower() or None
    loc = (locator or "").strip() or None

    # ---- GEMINI COORDINATE CLICK PATH ----
    # When Gemini suggests:
    #   {"action": "click", "locator_type": "coords", "offset_x": X, "offset_y": Y}
    # we click at viewport pixel (X, Y).
    if action_norm == "click" and loc_type == "coords":
        if offset_x is None or offset_y is None:
            debug("mouse_action: 'coords' click but offset_x/offset_y missing.")
            return
        try:
            x = int(offset_x)
            y = int(offset_y)
        except Exception as e:
            debug(f"mouse_action: invalid coords offset_x={offset_x!r}, offset_y={offset_y!r}: {e!r}")
            return

        ok = _click_at_viewport_coordinate(driver, x, y)
        if not ok:
            debug(f"mouse_action: coordinate click at ({x},{y}) reported failure.")
        time.sleep(0.5)
        return

    # ---- LEGACY ELEMENT-BASED BEHAVIOUR (for non-Gemini callers) ----
    elem = None

    # Resolve element if locator is provided
    if loc:
        try:
            if loc_type == "css":
                elem = driver.find_element(By.CSS_SELECTOR, loc)
            elif loc_type == "xpath":
                elem = driver.find_element(By.XPATH, loc)
            elif loc_type == "id":
                elem = driver.find_element(By.ID, loc)
            elif loc_type == "text":
                # best-effort text-based search
                xpath = f"//*[contains(normalize-space(.), {repr(loc)})]"
                elem = driver.find_element(By.XPATH, xpath)
            else:
                # default to CSS
                elem = driver.find_element(By.CSS_SELECTOR, loc)
        except Exception as e:
            debug(
                f"mouse_action: could not find element for locator_type={locator_type} "
                f"locator={locator}: {e}"
            )
            elem = None

    # Now perform action
    if action_norm == "click":
        if elem is None:
            debug("mouse_action: 'click' requested but no element and no coords; doing nothing.")
            return
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        except Exception:
            pass
        try:
            elem.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", elem)
            except Exception as e:
                debug(f"mouse_action: click failed: {e}")
                return
        time.sleep(0.5)
        return

    if action_norm == "scroll":
        # We no longer let Gemini use this, but other code may still call it.
        dx = int(offset_x or 0)
        dy = int(offset_y or 0)
        try:
            debug(f"mouse_action: scrolling by ({dx}, {dy})")
            driver.execute_script("window.scrollBy(arguments[0], arguments[1]);", dx, dy)
            time.sleep(0.4)
        except Exception as e:
            debug(f"mouse_action: scroll failed: {e}")
        return

    debug(f"mouse_action: unknown action '{action_norm}', doing nothing.")



def keyboard_action(
    driver: webdriver.Chrome,
    action: str,
    locator_type: Optional[str] = None,
    locator: Optional[str] = None,
    text: Optional[str] = None,
    key: Optional[str] = None,
) -> None:
    """
    Perform a keyboard action.

    Args:
      driver: Selenium WebDriver.
      action: one of:
          - "type"  (type text into a field)
          - "press" (press a special key like ENTER, TAB, ESC)
      locator_type: how to find the element when typing ("css", "xpath", "id", "text"), or None.
      locator: locator string for the element (when typing).
      text: text to type (when action == "type").
      key: name of special key to press (when action == "press"), e.g.:
           "ENTER", "TAB", "ESC", "SPACE", "BACKSPACE", "UP", "DOWN", "LEFT", "RIGHT".
    """
    action_norm = (action or "").strip().lower()
    if not action_norm:
        debug("keyboard_action: empty action, doing nothing.")
        return

    def find_element_by_locator(loc_type: Optional[str], loc_value: Optional[str]):
        loc_type_norm = (loc_type or "").strip().lower()
        loc_val = (loc_value or "").strip()
        if not loc_type_norm or not loc_val:
            return None
        try:
            if loc_type_norm == "css":
                return driver.find_element(By.CSS_SELECTOR, loc_val)
            if loc_type_norm == "xpath":
                return driver.find_element(By.XPATH, loc_val)
            if loc_type_norm == "id":
                return driver.find_element(By.ID, loc_val)
            if loc_type_norm == "text":
                xpath = f"//*[contains(normalize-space(.), '{loc_val}')]"
                return driver.find_element(By.XPATH, xpath)
        except Exception as e:
            debug(
                f"keyboard_action: could not find element for "
                f"locator_type={loc_type_norm} locator={loc_val}: {e}"
            )
            return None
        return None

    if action_norm == "type":
        if not text:
            debug("keyboard_action: 'type' action but no text provided.")
            return
        elem = find_element_by_locator(locator_type, locator)
        if elem is None:
            debug(
                f"keyboard_action: 'type' action but no element found for "
                f"locator_type={locator_type} locator={locator}"
            )
            return
        try:
            debug(
                f"keyboard_action: typing into element ({locator_type}={locator}) -> {text!r}"
            )
            elem.click()
            # Do not clear by default; we can be additive
            elem.send_keys(text)
            time.sleep(0.5)
        except Exception as e:
            debug(f"keyboard_action: typing failed: {e}")
        return

    if action_norm == "press":
        key_name = (key or "").strip().upper()
        if not key_name:
            debug("keyboard_action: 'press' action but no key provided.")
            return

        special_keys = {
            "ENTER": Keys.ENTER,
            "RETURN": Keys.RETURN,
            "TAB": Keys.TAB,
            "ESC": Keys.ESCAPE,
            "ESCAPE": Keys.ESCAPE,
            "SPACE": Keys.SPACE,
            "BACKSPACE": Keys.BACKSPACE,
            "DELETE": Keys.DELETE,
            "UP": Keys.ARROW_UP,
            "DOWN": Keys.ARROW_DOWN,
            "LEFT": Keys.ARROW_LEFT,
            "RIGHT": Keys.ARROW_RIGHT,
        }
        key_obj = special_keys.get(key_name)
        if not key_obj:
            debug(f"keyboard_action: unknown key '{key_name}', doing nothing.")
            return

        try:
            debug(f"keyboard_action: pressing key {key_name}")
            active = driver.switch_to.active_element
            active.send_keys(key_obj)
            time.sleep(0.5)
        except Exception:
            # Fallback: send to body
            try:
                body = driver.find_element(By.TAG_NAME, "body")
                body.send_keys(key_obj)
                time.sleep(0.5)
            except Exception as e:
                debug(f"keyboard_action: 'press' failed: {e}")
        return

    debug(f"keyboard_action: unknown action '{action_norm}', doing nothing.")

def call_gemini_for_recovery_actions(
    driver: webdriver.Chrome,
    container,
    api_key: str,
    problem_description: str,
    phase: str,
) -> Dict[str, Any]:
    """
    Ask Gemini for a small ordered list of recovery steps.

    Each step is:
      {
        "mouse": {
            "action": "click",
            "locator_type": "coords",
            "offset_x": <int>,   # viewport pixel X (button centre)
            "offset_y": <int>    # viewport pixel Y (button centre)
        } or null,
        "keyboard": {
            "action": "type",
            "locator_type": "css"|"xpath"|"id"|"text"|null,
            "locator": "<selector or text>",
            "text": "<what to type>"
        } or null
      }

    - Gemini is NOT allowed to:
        * use mouse actions other than "click"
        * use keyboard actions other than "type"
        * request scrolling
    """
    api_key = (api_key or "").strip()
    if not api_key:
        # try to resolve from disk/env
        api_key = resolve_gemini_api_key_from_env_or_disk(interactive=False) or ""
    if not api_key:
        debug("call_gemini_for_recovery_actions: no API key; skipping recovery.")
        return {"steps": []}

    # ---- capture context for prompt ----
    try:
        page_ctx = capture_page_context_for_gemini(driver, container=container)
    except TypeError:
        try:
            page_ctx = capture_page_context_for_gemini(driver)
        except Exception as e:
            debug(f"call_gemini_for_recovery_actions: capture_page_context_for_gemini failed: {e!r}")
            page_ctx = ""

    if isinstance(page_ctx, dict):
        visible_text = (page_ctx.get("visible_text") or "").strip()
        html = (page_ctx.get("html") or "").strip()
        url = driver.current_url
        title = driver.title
        ctx_text = (
            f"URL: {url}\n"
            f"TITLE: {title}\n\n"
            f"VISIBLE TEXT:\n{visible_text}\n\n"
            f"HTML SNIPPET:\n{html[:2000]}"
        )
    else:
        ctx_text = str(page_ctx)

    ctx_text = ctx_text[:4000]

    mouse_fn_desc = """
mouse_action(driver, action, locator_type=None, locator=None, offset_x=None, offset_y=None)

- For Gemini you are allowed ONLY action="click".
- For clicks you MUST use locator_type="coords".
- offset_x and offset_y MUST be integers giving the viewport pixel coordinates
  of the visual centre (centroid) of the clickable button or element.
- Do NOT use 'locator' or locator_type='text'/'css'/'xpath' for your clicks.
- Do NOT request scrolling or any other mouse actions.
""".strip()

    keyboard_fn_desc = """
keyboard_action(driver, action, locator_type=None, locator=None, text=None, key=None)

- For Gemini you are allowed ONLY action="type".
- locator_type: "css", "xpath", "id", "text", or null (for typing into focused field).
- locator: selector or short text to find the field (if locator_type is text).
- text: the text to type.
- You MUST NOT use keypress-style 'press' actions (ENTER/TAB/etc.) in Gemini.
""".strip()

    prompt = f"""
You are a recovery controller for a Selenium-based job application bot.

The bot is STUCK during phase: {phase}
Problem description:
{problem_description}

You cannot execute code. Instead, the bot can call exactly two local helpers:

{mouse_fn_desc}

{keyboard_fn_desc}

Here is a snapshot of the current page (URL, title, visible text and HTML snippet):

<<<PAGE_CONTEXT>>>
{ctx_text}
<<<END_PAGE_CONTEXT>>>

Your job:
- Return a SMALL ordered list of steps (at most 6).
- Each step may include:
    * at most one mouse_action()
    * at most one keyboard_action()
- Mouse:
    * ONLY clicks, with locator_type="coords" and offset_x/offset_y = button centre in viewport pixels.
- Keyboard:
    * ONLY type actions into fields.

Return ONLY a JSON object, no extra text, with shape:

{{
  "steps": [
    {{
      "mouse": {{
        "action": "click",
        "locator_type": "coords",
        "offset_x": <int>,
        "offset_y": <int>
      }} | null,
      "keyboard": {{
        "action": "type",
        "locator_type": "css"|"xpath"|"id"|"text"|null,
        "locator": "<selector or text or null>",
        "text": "<text to type>"
      }} | null
    }},
    ...
  ]
}}
""".strip()

    client = get_gemini_client(api_key)
    last_error: Optional[Exception] = None

    for attempt in range(2):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.15,
                    max_output_tokens=512,
                    response_mime_type="text/plain",
                ),
            )
            raw = (resp.text or "").strip()
            if not raw:
                raise ValueError("Empty response from Gemini.")

            # Try parse JSON directly, then salvage if needed
            try:
                data = json.loads(raw)
            except Exception:
                m = re.search(r"\{[\s\S]*\}", raw)
                if not m:
                    raise ValueError("No JSON object found in Gemini response.")
                data = json.loads(m.group(0))

            if not isinstance(data, dict):
                raise ValueError("Gemini response is not a JSON object.")

            steps = data.get("steps")
            if not isinstance(steps, list):
                raise ValueError("Gemini response has no 'steps' list.")

            normalized: List[Dict[str, Any]] = []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                mouse_cfg = step.get("mouse")
                kb_cfg = step.get("keyboard")

                if mouse_cfg is not None and not isinstance(mouse_cfg, dict):
                    mouse_cfg = None
                if kb_cfg is not None and not isinstance(kb_cfg, dict):
                    kb_cfg = None

                # Enforce: mouse -> click + coords
                if mouse_cfg:
                    act = (mouse_cfg.get("action") or "").strip().lower()
                    if act != "click":
                        mouse_cfg = None
                    else:
                        lt = (mouse_cfg.get("locator_type") or "").strip().lower()
                        if lt != "coords":
                            # force coords semantics; drop any locators
                            mouse_cfg = None
                        else:
                            try:
                                ox = int(mouse_cfg.get("offset_x"))
                                oy = int(mouse_cfg.get("offset_y"))
                            except Exception:
                                mouse_cfg = None
                            else:
                                mouse_cfg = {
                                    "action": "click",
                                    "locator_type": "coords",
                                    "offset_x": ox,
                                    "offset_y": oy,
                                }

                # Enforce: keyboard -> type only
                if kb_cfg:
                    act = (kb_cfg.get("action") or "").strip().lower()
                    if act != "type":
                        kb_cfg = None
                    else:
                        text = kb_cfg.get("text")
                        if text is None:
                            kb_cfg = None
                        else:
                            kb_cfg = {
                                "action": "type",
                                "locator_type": kb_cfg.get("locator_type"),
                                "locator": kb_cfg.get("locator"),
                                "text": str(text),
                            }

                if not mouse_cfg and not kb_cfg:
                    continue
                normalized.append({"mouse": mouse_cfg, "keyboard": kb_cfg})

            return {"steps": normalized}

        except Exception as e:
            last_error = e
            msg = (str(e) or "").lower()
            debug(f"call_gemini_for_recovery_actions: attempt {attempt+1} failed: {e!r}")

            key_tokens = [
                "api key", "apikey", "invalid api key", "permission_denied",
                "unauthorized", "quota", "billing", "401", "403"
            ]
            if any(tok in msg for tok in key_tokens) and attempt == 0:
                new_key = prompt_for_new_gemini_key()
                if new_key:
                    save_gemini_api_key_to_disk(new_key.strip())
                    api_key = new_key.strip()
                    client = get_gemini_client(api_key)
                    continue
                else:
                    break

            time.sleep(1.0)

    debug(f"call_gemini_for_recovery_actions: ultimately failed: {last_error!r}")
    return {"steps": []}



def execute_recovery_plan(
    driver: webdriver.Chrome,
    plan: Dict[str, Any],
    max_steps: int = 5,
) -> None:
    """
    Execute a Gemini-proposed recovery plan consisting of mouse and keyboard steps.
    """
    steps = plan.get("steps") or []
    if not isinstance(steps, list):
        debug("execute_recovery_plan: no valid steps to execute.")
        return

    for idx, step in enumerate(steps[:max_steps]):
        if not isinstance(step, dict):
            continue

        mouse_cmd = step.get("mouse")
        keyboard_cmd = step.get("keyboard")

        if mouse_cmd:
            try:
                mouse_action(
                    driver=driver,
                    action=mouse_cmd.get("action"),
                    locator_type=mouse_cmd.get("locator_type"),
                    locator=mouse_cmd.get("locator"),
                    offset_x=mouse_cmd.get("offset_x"),
                    offset_y=mouse_cmd.get("offset_y"),
                )
            except Exception as e:
                debug(f"execute_recovery_plan: mouse step {idx+1} failed: {e!r}")

        if keyboard_cmd:
            try:
                keyboard_action(
                    driver=driver,
                    action=keyboard_cmd.get("action"),
                    locator_type=keyboard_cmd.get("locator_type"),
                    locator=keyboard_cmd.get("locator"),
                    text=keyboard_cmd.get("text"),
                    key=keyboard_cmd.get("key"),
                )
            except Exception as e:
                debug(f"execute_recovery_plan: keyboard step {idx+1} failed: {e!r}")

        # Small pause between steps
        time.sleep(1.0)


def ask_gemini_for_browser_action(
    driver: webdriver.Chrome,
    reason: str,
    api_key: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    Ask Gemini which single high-level browser action to take
    (click, type, scroll, press_key, or none) when the automation
    is stuck on a page.

    Returns a dict of the form:
        {
          "function": "click_element" | "type_text" | "scroll_page" | "press_key" | "none",
          "params": { ... }
        }
    or None if Gemini cannot provide a usable answer.
    """
    # Resolve API key if not provided or if env/disk changed
    if not api_key:
        api_key = resolve_gemini_api_key_from_env_or_disk()
    api_key = (api_key or "").strip()
    if not api_key:
        debug("No Gemini API key available for browser recovery; skipping.")
        return None

    try:
        url = driver.current_url
    except Exception:
        url = ""

    try:
        html = driver.page_source or ""
    except Exception:
        html = ""

    # Truncate HTML to keep prompt size reasonable
    max_len = 12000
    if len(html) > max_len:
        html = html[:max_len] + "\n<!-- HTML truncated for prompt -->"

    prompt = f"""
You are helping a Selenium-based Python script that is stuck while applying to a job.

CURRENT URL:
{url}

REASON WE ARE STUCK:
{reason}

You will see a partial HTML snapshot of the current page. Based on this, choose AT MOST ONE
high-level action for the script to perform, using ONLY the functions described below:

Available actions (you choose exactly one function):

1) click_element
   - Use this to click a button, link, or other element.
   - Parameters:
       function: "click_element"
       params: {{
         "by": "css" or "xpath",
         "selector": "<CSS selector or XPath>",
         "description": "<short human description of what you are clicking>"
       }}

2) type_text
   - Use this to type text into a text box or textarea.
   - Parameters:
       function: "type_text"
       params: {{
         "by": "css" or "xpath",
         "selector": "<CSS selector or XPath for the input>",
         "text": "<what to type>",
         "clear": true or false (whether to clear the field first),
         "press_enter": true or false (whether to press Enter after typing),
         "description": "<short description of the field>"
       }}

3) scroll_page
   - Use this to scroll the page up or down.
   - Parameters:
       function: "scroll_page"
       params: {{
         "direction": "up" or "down",
         "amount": 300 (pixels to scroll; use a reasonable integer),
         "description": "<what you're trying to reveal by scrolling>"
       }}

4) press_key
   - Use this to simulate a single key press (TAB, ENTER, ESCAPE, etc.).
   - Parameters:
       function: "press_key"
       params: {{
         "key": "ENTER" | "TAB" | "ESCAPE" | "SPACE" | "ARROW_DOWN" | "ARROW_UP",
         "description": "<why you are pressing this key>"
       }}

5) none
   - Use this if you are not confident any action is safe or helpful.
   - Parameters:
       function: "none"
       params: {{}}

RESPONSE FORMAT (VERY IMPORTANT):

Return ONLY a JSON object, no markdown, no extra commentary, exactly like:

{{
  "function": "click_element" | "type_text" | "scroll_page" | "press_key" | "none",
  "params": {{
    ... as described above ...
  }}
}}

If you are uncertain or the HTML is not enough to choose safely, return:

{{ "function": "none", "params": {{}} }}

HTML SNAPSHOT:
<<<HTML>>>
{html}
<<<END_HTML>>>
""".strip()

    # Only allow Gemini to suggest clicking, typing, or doing nothing.
    allowed_functions = {"click_element", "type_text", "none"}


    def _empty_action() -> Dict[str, Any]:
        return {"function": "none", "params": {}}

    max_retries = 3
    last_err: Optional[Exception] = None
    key = api_key

    for attempt in range(1, max_retries + 1):
        try:
            client = get_gemini_client(key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=512,
                    response_mime_type="application/json",
                ),
            )
            raw = (resp.text or "").strip()
            if not raw:
                debug("Gemini browser-action call returned empty response.")
                return _empty_action()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
                if not m:
                    debug("Gemini browser-action response not valid JSON; treating as 'none'.")
                    return _empty_action()
                data = json.loads(m.group(0))

            if not isinstance(data, dict):
                debug("Gemini browser-action JSON is not an object; treating as 'none'.")
                return _empty_action()

            func_name = (data.get("function") or "").strip().lower()
            params = data.get("params") or {}
            if func_name not in allowed_functions:
                debug(f"Gemini suggested unknown function '{func_name}'; treating as 'none'.")
                return _empty_action()

            action = {
                "function": func_name,
                "params": params if isinstance(params, dict) else {},
            }
            debug(f"Gemini browser-action suggestion: {action}")
            return action

        except Exception as e:
            last_err = e
            msg = (str(e) or "").lower()
            debug(f"Gemini browser-action call attempt {attempt} failed: {e!r}")

            # tokens that indicate the error is about API key / quota / auth
            key_issue_tokens = [
                "api key",
                "apikey",
                "invalid api key",
                "invalid api_key",
                "unauthorized",
                "permission_denied",
                "permission denied",
                "quota",
                "exhausted",
                "insufficient",
                "billing",
                "403",
                "401",
            ]
            is_key_issue = any(tok in msg for tok in key_issue_tokens)

            if is_key_issue:
                debug("Detected Gemini API key / quota problem during browser-action call.")
                new_key = prompt_for_new_gemini_key()
                if new_key:
                    # prompt_for_new_gemini_key already writes to gemini_api_key.txt;
                    # here we just retry with the new key in this function.
                    key = new_key.strip()
                    continue
                else:
                    # user did not provide a new key: stop trying
                    break
            else:
                # not a key/quota problem: brief delay then retry
                time.sleep(1.0)


    if last_err is not None:
        debug(f"Gemini browser-action call ultimately failed: {last_err!r}")
    return None


def execute_gemini_browser_action(
    driver: webdriver.Chrome,
    action: Dict[str, Any],
) -> bool:
    """
    Execute a single high-level browser action suggested by Gemini.

    Returns True if we believe the action had some effect, False otherwise.
    """
    func = (action.get("function") or "").lower()
    params = action.get("params") or {}

    if func == "none" or not func:
        debug("execute_gemini_browser_action: function='none'; no action taken.")
        return False

    # ---------------- click_element ----------------
    if func == "click_element":
        by = (params.get("by") or "css").lower()
        selector = (params.get("selector") or "").strip()
        desc = (params.get("description") or "").strip()
        if not selector:
            debug("Gemini click_element missing selector; skipping.")
            return False

        debug(f"Executing Gemini click_element on selector '{selector}' (by={by}, desc={desc})")

        try:
            if by == "xpath":
                elems = driver.find_elements(By.XPATH, selector)
            else:
                elems = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception as e:
            debug(f"Error locating elements for click_element: {e!r}")
            return False

        for el in elems:
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", el
                )
                time.sleep(0.2)
                try:
                    el.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(0.5)
                return True
            except Exception:
                continue

        debug("No clickable element found for Gemini click_element action.")
        return False

    # ---------------- type_text ----------------
    if func == "type_text":
        by = (params.get("by") or "css").lower()
        selector = (params.get("selector") or "").strip()
        text = (params.get("text") or "")
        clear_first = bool(params.get("clear", True))
        press_enter = bool(params.get("press_enter", False))
        desc = (params.get("description") or "").strip()

        if not selector:
            debug("Gemini type_text missing selector; skipping.")
            return False

        debug(
            f"Executing Gemini type_text on selector '{selector}' (by={by}, "
            f"clear={clear_first}, press_enter={press_enter}, desc={desc})"
        )

        try:
            if by == "xpath":
                elems = driver.find_elements(By.XPATH, selector)
            else:
                elems = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception as e:
            debug(f"Error locating elements for type_text: {e!r}")
            return False

        for el in elems:
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", el
                )
                time.sleep(0.2)
                el.click()
                if clear_first:
                    el.clear()
                if text:
                    el.send_keys(text)
                if press_enter:
                    el.send_keys(Keys.ENTER)
                time.sleep(0.5)
                return True
            except Exception:
                continue

        debug("No suitable element found for Gemini type_text action.")
        return False

    # ---------------- scroll_page ----------------
    if func == "scroll_page":
        direction = (params.get("direction") or "down").lower()
        amount = params.get("amount", 300)
        desc = (params.get("description") or "").strip()

        try:
            amount_val = int(amount)
        except Exception:
            amount_val = 300

        if direction == "up":
            amount_val = -abs(amount_val)
        else:
            amount_val = abs(amount_val)

        debug(f"Executing Gemini scroll_page direction={direction}, amount={amount_val}, desc={desc}")
        try:
            driver.execute_script("window.scrollBy(0, arguments[0]);", amount_val)
            time.sleep(0.5)
            return True
        except Exception as e:
            debug(f"Error executing scroll_page: {e!r}")
            return False

    # ---------------- press_key ----------------
    if func == "press_key":
        key_name = (params.get("key") or "").upper()
        desc = (params.get("description") or "").strip()

        key_map = {
            "ENTER": Keys.ENTER,
            "RETURN": Keys.RETURN,
            "TAB": Keys.TAB,
            "ESC": Keys.ESCAPE,
            "ESCAPE": Keys.ESCAPE,
            "SPACE": Keys.SPACE,
            "ARROW_DOWN": Keys.ARROW_DOWN,
            "ARROW_UP": Keys.ARROW_UP,
        }

        key_obj = key_map.get(key_name)
        if not key_obj:
            debug(f"Unknown key '{key_name}' for Gemini press_key; skipping.")
            return False

        debug(f"Executing Gemini press_key key={key_name}, desc={desc}")
        try:
            actions = ActionChains(driver)
            actions.send_keys(key_obj).perform()
            time.sleep(0.3)
            return True
        except Exception as e:
            debug(f"Error executing press_key: {e!r}")
            return False

    debug(f"execute_gemini_browser_action: unsupported function '{func}'; no action taken.")
    return False


def maybe_recover_with_gemini_action(
    driver: webdriver.Chrome,
    reason: str,
    gemini_api_key: Optional[str] = None,
) -> bool:
    """
    Convenience helper: when the automation is stuck, call Gemini once
    to request a browser action, and execute it if it's not 'none'.

    Returns True if an action was executed, False otherwise.
    """
    key = gemini_api_key or resolve_gemini_api_key_from_env_or_disk()
    if not key:
        debug("maybe_recover_with_gemini_action: no Gemini key available; skipping.")
        return False

    action = ask_gemini_for_browser_action(driver, reason=reason, api_key=key)
    if not action:
        return False

    func_name = (action.get("function") or "").lower()
    if func_name == "none" or not func_name:
        debug("Gemini suggested no browser action (function='none'); nothing to do.")
        return False

    return execute_gemini_browser_action(driver, action)

def merge_gemini_answer_dicts(
    base: Dict[str, Any],
    new: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Merge two Gemini answer dicts.

    - For each section (text_fields, textareas, etc.), keys from `new`
      override `base` only when the new value is non-empty.
    - This lets us keep good previous answers while filling in newly
      added questions.
    """
    sections = ["text_fields", "textareas", "select_fields", "radio_groups", "checkboxes"]
    result: Dict[str, Any] = {s: {} for s in sections}

    for section in sections:
        base_sec = base.get(section) or {}
        new_sec = new.get(section) or {}
        merged_sec: Dict[str, Any] = {}

        all_keys = set(base_sec.keys()) | set(new_sec.keys())
        for key in all_keys:
            new_val = new_sec.get(key, None)
            # Treat empty strings / None as "no new info"
            if isinstance(new_val, str) and not new_val.strip():
                new_val = None
            if new_val is None:
                merged_sec[key] = base_sec.get(key)
            else:
                merged_sec[key] = new_val

        result[section] = merged_sec

    return result

def form_needs_gemini(container, form_schema: Dict[str, Any]) -> bool:
    """
    Return True if there appears to be at least one unfilled question in the form.

    We check:
      - text inputs
      - textareas
      - select dropdowns
      - radio groups
      - required checkboxes
    If everything looks filled, we skip Gemini for this step.
    """
    # ---------- text inputs ----------
    for field in form_schema.get("text_fields", []):
        elem_id = field.get("id") or ""
        name_attr = field.get("name") or ""
        elem = None
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"input[name='{name_attr}']")
            except Exception:
                elem = None
        if elem is None:
            continue
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue
            val = (elem.get_attribute("value") or "").strip()
            if not val:
                return True
        except Exception:
            continue

    # ---------- textareas ----------
    for field in form_schema.get("textareas", []):
        elem_id = field.get("id") or ""
        name_attr = field.get("name") or ""
        elem = None
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"textarea[name='{name_attr}']")
            except Exception:
                elem = None
        if elem is None:
            continue
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue
            val = (elem.get_attribute("value") or "").strip()
            if not val:
                return True
        except Exception:
            continue

    # ---------- select dropdowns ----------
    for field in form_schema.get("select_fields", []):
        elem_id = field.get("id") or ""
        name_attr = field.get("name") or ""
        elem = None
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.NAME, name_attr)
            except Exception:
                elem = None
        if elem is None:
            continue
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue
            sel = Select(elem)
            selected = sel.all_selected_options
            if not selected:
                return True
            text = (selected[0].text or "").strip().lower()
            value = (selected[0].get_attribute("value") or "").strip().lower()
            # Heuristic: treat obvious placeholders as "unfilled"
            if (not value) and (not text or "select" in text or "choose" in text):
                return True
        except Exception:
            continue

    # ---------- radio groups ----------
    for group in form_schema.get("radio_groups", []):
        name_attr = group.get("name") or ""
        if not name_attr:
            continue
        try:
            radios = container.find_elements(
                By.CSS_SELECTOR, f"input[type='radio'][name='{name_attr}']"
            )
        except Exception:
            radios = []
        has_checked = False
        for r in radios:
            try:
                if r.is_displayed() and r.is_enabled() and r.is_selected():
                    has_checked = True
                    break
            except Exception:
                continue
        if not has_checked and radios:
            return True

    # ---------- required checkboxes ----------
    for field in form_schema.get("checkboxes", []):
        elem_id = field.get("id") or ""
        name_attr = field.get("name") or ""
        elem = None
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(
                    By.CSS_SELECTOR, f"input[type='checkbox'][name='{name_attr}']"
                )
            except Exception:
                elem = None
        if elem is None:
            continue
        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue
            required_attr = (elem.get_attribute("required") or "").lower()
            is_required = bool(required_attr) or "required" in required_attr
            if is_required and not elem.is_selected():
                return True
        except Exception:
            continue

    # If we didn't find any obviously empty/required field, Gemini is not needed.
    return False

def speak(text: str) -> None:
    """
    Best-effort text-to-speech helper.
    Tries pyttsx3 if available; otherwise just prints to console.
    """
    try:
        import pyttsx3  # type: ignore
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except Exception:
        print(f"[speak] {text}", flush=True)

def prompt_for_new_gemini_key() -> Optional[str]:
    """
    Ask the user for a new Gemini API key when the old one is exhausted/invalid.

    - Speaks an audible prompt the first time it is called.
    - Saves the key to:
        * environment variable GEMINI_API_KEY
        * gemini_api_key.txt in the same folder as this script
      so future runs can reuse it automatically.
    """
    # Speak only once per process
    if not getattr(prompt_for_new_gemini_key, "_spoken_once", False):
        speak(
            "Your Gemini key is exhausted or invalid. "
            "Please create a new Gemini key and paste it into the terminal."
        )
        setattr(prompt_for_new_gemini_key, "_spoken_once", True)

    print("\n[auto-apply] ⚠️ Gemini API key appears exhausted or invalid.")
    print("[auto-apply] Please create a new Gemini key in Google AI Studio,")
    print("[auto-apply] then paste it here. Leave blank and press Enter to skip Gemini.\n")

    new_key = input("New GEMINI_API_KEY: ").strip()
    if not new_key:
        debug("No new Gemini key provided; Gemini assistance will be skipped.")
        return None

    # Use in this run
    os.environ["GEMINI_API_KEY"] = new_key

    # Persist to disk for the next run
    try:
        key_file = BASE_DIR / "gemini_api_key.txt"
        key_file.write_text(new_key, encoding="utf-8")
        debug(f"Saved new Gemini key to {key_file}")
    except Exception as e:
        debug(f"Failed to save Gemini key to disk: {e!r}")

    return new_key


def save_form_answers_to_file(
    job_index: int,
    step_index: int,
    job_title: Optional[str],
    mode: str,
    answers: Dict[str, Any],
) -> Path:
    """
    Save Gemini's answers for this job & step as a JSON file.

    File naming:
      form_answers/job_{job_index+1:02d}_{mode}_step_{step_index+1}_gemini_answers.txt

    Returns Path to the saved file.
    """
    try:
        # Ensure constants exist
        global FORM_ANSWERS_DIR
        try:
            FORM_ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            # If FORM_ANSWERS_DIR missing, fallback to ./form_answers
            FORM_ANSWERS_DIR = Path("./form_answers")
            FORM_ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        debug(f"save_form_answers_to_file: failed to ensure directory: {e!r}")

    safe_title = ""
    try:
        safe_title = re.sub(r"[^\w\-]+", "_", (job_title or f"job_{(job_index or 0)+1}")).strip("_")
    except Exception:
        safe_title = f"job_{(job_index or 0)+1}"

    filename = f"job_{(job_index or 0)+1:02d}_{(mode or 'easy')}_step_{(step_index or 0)+1}_gemini_answers.txt"
    path = FORM_ANSWERS_DIR / filename

    blob = {
        "job_index": (job_index or 0) + 1,
        "step_index": (step_index or 0) + 1,
        "job_title": job_title,
        "mode": mode,
        "answers": answers,
    }

    try:
        path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        debug(f"Saved Gemini answers to {path}")
    except Exception as e:
        debug(f"save_form_answers_to_file: failed to write file {path}: {e!r}")
        # try a safe fallback filename
        try:
            fallback = FORM_ANSWERS_DIR / f"job_{(job_index or 0)+1:02d}_{int(time.time())}_gemini_answers.txt"
            fallback.write_text(json.dumps(blob, indent=2), encoding="utf-8")
            debug(f"Saved Gemini answers to fallback {fallback}")
            return fallback
        except Exception as e2:
            debug(f"save_form_answers_to_file: fallback write failed: {e2!r}")
            raise

    return path


def load_form_answers_from_file(*args, **kwargs) -> Optional[Dict[str, Any]]:
    """
    Robust loader for cached Gemini answers.

    Accepts calling styles:
      load_form_answers_from_file(job_index, step_index, mode)
      load_form_answers_from_file(job_index, step_index, mode, job_title)
      load_form_answers_from_file(job_index=..., step_index=..., mode=..., job_title=...)

    Returns the inner 'answers' dict (mapping with sections) or None.
    """
    # Normalize inputs (positional or kwargs)
    job_index = None
    step_index = None
    mode = None
    job_title = None

    # positional
    if len(args) >= 1:
        job_index = args[0]
    if len(args) >= 2:
        step_index = args[1]
    if len(args) >= 3:
        third = args[2]
        # If third looks like a short mode token, treat as mode; else treat as job_title
        if isinstance(third, str) and third.lower() in {"easy", "external", "apply", "step"}:
            mode = third
        elif isinstance(third, str) and len(third) <= 20 and third.isalpha():
            mode = third
        else:
            job_title = third
    if len(args) >= 4:
        job_title = args[3]

    # kwargs override
    if "job_index" in kwargs:
        job_index = kwargs.get("job_index")
    if "step_index" in kwargs:
        step_index = kwargs.get("step_index")
    if "mode" in kwargs:
        mode = kwargs.get("mode")
    if "job_title" in kwargs:
        job_title = kwargs.get("job_title")

    # Minimal validation
    if job_index is None or step_index is None:
        debug("load_form_answers_from_file: missing job_index or step_index in call.")
        return None

    if not mode:
        mode = "easy"

    # Construct candidate filenames (support older naming patterns)
    candidates = []
    try:
        idx = int(job_index) + 0  # allow int-like
        step = int(step_index) + 0
    except Exception:
        idx = job_index
        step = step_index

    # primary pattern
    candidates.append(f"job_{idx+1:02d}_{mode}_step_{step+1}_gemini_answers.txt")

    # variant with different naming (older code)
    candidates.append(f"job_{idx+1:02d}_{mode}_step_{step+1}_answers.txt")
    candidates.append(f"{safe_filename(job_title or f'job_{idx+1}')}_{mode}_step_{step+1}_gemini_answers.txt")
    candidates.append(f"job_{idx+1:02d}_step_{step+1}_gemini_answers.txt")

    # helper to sanitize job_title
    def _candidates_with_dir():
        for name in candidates:
            yield FORM_ANSWERS_DIR / name
        # also search entire directory for files matching job index / step patterns
        pattern = f"job_{idx+1:02d}_*step_{step+1}_*gemini_answers.txt"
        try:
            for p in FORM_ANSWERS_DIR.glob(pattern):
                yield p
        except Exception:
            pass

    # Attempt reading candidates
    for path in _candidates_with_dir():
        try:
            if not path.exists():
                continue
            raw = path.read_text(encoding="utf-8")
            blob = json.loads(raw)
            # old saved blob may directly be the answers dict; handle both shapes
            if isinstance(blob, dict):
                if "answers" in blob and isinstance(blob["answers"], dict):
                    debug(f"Loaded cached answers from {path}")
                    return blob["answers"]
                # If the file itself is the answers dict, return it
                keys_expected = {"text_fields", "textareas", "select_fields", "radio_groups", "checkboxes"}
                if keys_expected & set(blob.keys()):
                    debug(f"Loaded answers (direct) from {path}")
                    return blob
        except Exception as e:
            debug(f"load_form_answers_from_file: failed to read/parse {path}: {e!r}")
            continue

    return None


# small helper used above to build safe filenames (kept local to avoid dependency)
def safe_filename(s: Optional[str]) -> str:
    s = (s or "").strip()
    if not s:
        return "untitled"
    return re.sub(r"[^\w\-]+", "_", s)[:200]


def close_easy_apply_modal_if_open(driver: webdriver.Chrome) -> None:
    """
    If a LinkedIn Easy Apply modal/dialog is still open, try to close it.

    Used as a cleanup step when the Easy Apply flow ends without
    a clear submission, so the script can continue gracefully.
    """
    selectors = [
        "div[role='dialog']",
        "div[aria-modal='true']",
        "div.jobs-easy-apply-modal",
        "div.artdeco-modal",
    ]

    dialog = None

    # Find any visible dialog/modal that looks like Easy Apply
    for sel in selectors:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:
            elems = []
        for e in elems:
            try:
                if e.is_displayed():
                    dialog = e
                    break
            except Exception:
                continue
        if dialog is not None:
            break

    if dialog is None:
        # No modal found, nothing to close
        return

    # Look for a close/dismiss/cancel button inside the dialog
    close_btn = None
    try:
        close_btn = dialog.find_element(By.XPATH, ".//button[contains(@aria-label, 'Dismiss')]")
    except NoSuchElementException:
        try:
            close_btn = dialog.find_element(By.XPATH, ".//button[contains(., 'Cancel')]")
        except NoSuchElementException:
            close_btn = None

    if not close_btn:
        return

    try:
        debug("Closing Easy Apply modal via close/cancel button.")
        close_btn.click()
        time.sleep(1.0)
    except Exception:
        # If we can't click it, just move on
        pass



BASE_DIR = Path(__file__).parent.resolve()
FORM_ANSWERS_DIR = BASE_DIR / "form_answers"


# resume_and_cover_maker will compile to these names by default
TAILORED_RESUME_PDF_NAME = "resume_generated.pdf"

def speak(text: str) -> None:
    """
    Best-effort text-to-speech helper.

    Uses pyttsx3 if available; otherwise just logs via debug().
    """
    try:
        import pyttsx3  # type: ignore
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except Exception:
        # Fall back to console only
        debug(f"[speak] {text}")

def prompt_for_new_gemini_key() -> Optional[str]:
    """
    Prompt the user once (with voice + console) for a new Gemini API key.

    Returns the new key (str) or None if user declined.
    """
    msg = (
        "\n[Gemini] Your Gemini API key appears exhausted or invalid.\n"
        "Please create a new key in the Google AI Studio console, then paste it below.\n"
        "Press ENTER without typing anything to skip and continue without Gemini for this step.\n"
    )
    print(msg, flush=True)

    # Speak the message only once
    if not getattr(prompt_for_new_gemini_key, "_spoken_once", False):
        try:
            speak("Gemini key exhausted. Please add a new valid key to continue.")
        except Exception:
            pass
        setattr(prompt_for_new_gemini_key, "_spoken_once", True)

    new_key = input("New GEMINI_API_KEY: ").strip()
    if not new_key:
        debug("User did not enter a new Gemini key; disabling Gemini assistance for this step.")
        return None

    # Persist for the rest of the process
    os.environ["GEMINI_API_KEY"] = new_key
    debug("Updated GEMINI_API_KEY from user input.")
    return new_key

def debug(msg: str) -> None:
    """Lightweight logger with a tiny suppression filter for noisy messages."""
    text = str(msg)

    # Suppress some very noisy internal messages that confused you even when
    # things actually worked.
    suppressed_prefixes = (
        "mouse_action: clicking element",  # e.g. (text=Done)
        "Still no Next/Submit/Apply button after Gemini recovery; stopping Easy Apply flow.",
        "Easy Apply flow ended without a clear submission; trying to close any open modal if present.",
        "No external 'Apply' button found on LinkedIn job page.",
    )
    suppressed_contains = (
        "⚠️ Could not auto-apply to job",   # per‑job failure message
    )

    for p in suppressed_prefixes:
        if text.startswith(p):
            return
    for p in suppressed_contains:
        if p in text:
            return

    print(f"[auto-apply] {text}", flush=True)



def create_driver(headless: bool = False) -> webdriver.Chrome:
    """
    Create and return a clean Chrome WebDriver.

    We let Selenium's built-in Selenium Manager find the right ChromeDriver,
    and we do NOT use any custom Chrome profile. LinkedIn login will be done
    by email/password via login_to_linkedin().
    """
    chrome_options = Options()

    # Minimal, stable flags for Linux
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--start-maximized")

    if headless:
        chrome_options.add_argument("--headless=new")

    # ❗ No Service / ChromeDriverManager here – Selenium manager handles it
    driver = webdriver.Chrome(options=chrome_options)
    driver.implicitly_wait(3)
    return driver




def login_to_linkedin(
    driver: webdriver.Chrome,
    email: str,
    password: str,
    gemini_api_key: Optional[str] = None,
) -> None:
    debug("Logging in to LinkedIn...")
    driver.get("https://www.linkedin.com/login")

    # 1) Find login fields
    try:
        email_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "username"))
        )
        password_input = driver.find_element(By.ID, "password")
    except TimeoutException:
        raise RuntimeError("Could not find LinkedIn login fields; UI may have changed.")

    # 2) Fill credentials and submit
    email_input.clear()
    email_input.send_keys(email)
    password_input.clear()
    password_input.send_keys(password)
    password_input.send_keys(Keys.RETURN)

    def is_logged_in(d: webdriver.Chrome) -> bool:
        try:
            nav = d.find_element(By.ID, "global-nav-search")
            return nav.is_displayed()
        except Exception:
            return False

    # 3) Wait a bit, watching for either login success or security check
    deadline = time.time() + 25.0
    while time.time() < deadline:
        if is_logged_in(driver):
            debug("Login seems successful (global nav found).")
            return
        if is_linkedin_security_check_page(driver):
            debug("LinkedIn security check / CAPTCHA detected after login submit.")
            break
        time.sleep(1.0)

    # 4) If we’re now on a security check page, ask the user to solve it manually
    if is_linkedin_security_check_page(driver):
        handle_linkedin_security_check(driver)
        # After user says done, verify login again
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.ID, "global-nav-search"))
            )
            debug("Login successful after manual security check.")
            return
        except TimeoutException:
            raise RuntimeError(
                "Login did not appear to succeed after manual security check. "
                "Check your credentials, CAPTCHA, or 2FA."
            )

    # 5) No nav, no obvious security check → optional Gemini recovery, then final check
    if gemini_api_key:
        try:
            debug("Login did not complete; attempting Gemini page recovery for login.")
            try:
                body_container = driver.find_element(By.TAG_NAME, "body")
            except Exception:
                body_container = None
            if body_container is not None:
                try_gemini_page_recovery(
                    driver=driver,
                    container=body_container,
                    gemini_api_key=gemini_api_key,
                    problem_description=(
                        "Login form submitted, but no navigation bar or "
                        "LinkedIn home UI appeared. Try clicking the right button "
                        "or typing into any remaining fields."
                    ),
                    phase="login",
                    max_steps=2,
                )
        except Exception as e:
            debug(f"login_to_linkedin: Gemini recovery raised exception: {e!r}")

    # 6) Final check
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "global-nav-search"))
        )
        debug("Login successful after Gemini recovery.")
        return
    except TimeoutException:
        raise RuntimeError(
            "Login did not appear to succeed. "
            "Check your credentials, CAPTCHA, or 2FA."
        )



def build_jobs_search_url(keywords: List[str], location: str) -> str:
    """
    Build a LinkedIn jobs search URL with given keywords and location.

    NOTE:
    - We do NOT filter to Easy Apply only anymore (&f_AL=true),
      because that sometimes returns 0 results or uses a different layout.
    - The script will still *prefer* Easy Apply when available, and
      fall back to external apply otherwise.
    """
    from urllib.parse import quote_plus

    query = " OR ".join(k.strip() for k in keywords if k.strip())
    base = "https://www.linkedin.com/jobs/search/"
    params = (
        f"?keywords={quote_plus(query)}"
        f"&location={quote_plus(location or 'Worldwide')}"
    )
    return base + params



def open_jobs_search(driver: webdriver.Chrome, keywords: List[str], location: str) -> None:
    url = build_jobs_search_url(keywords, location)
    debug(f"Opening jobs search URL: {url}")
    driver.get(url)

    # Wait for some kind of results container to appear
    try:
        WebDriverWait(driver, 25).until(
            EC.any_of(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "li[data-occludable-job-id]")
                ),
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "li.jobs-search-results__list-item")
                ),
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "ul.jobs-search__results-list")
                ),
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div.jobs-search-results-list")
                ),
            )
        )
        # Slightly shorter extra buffer
        time.sleep(1.0)
    except TimeoutException:
        debug("Could not see job results list; layout may have changed or there are no results.")


def load_applicant_profile(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Applicant profile JSON not found at {path}. "
            "Create it as described in the script header."
        )
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Applicant profile JSON must contain a top-level object.")
    return data


def safe_text(elem) -> str:
    try:
        return elem.text or ""
    except Exception:
        return ""


def find_job_cards(driver: webdriver.Chrome, log: bool = True) -> List[Any]:
    """
    Find job cards in the search results list.
    LinkedIn often changes selectors, so we try several common patterns.
    """
    # Slightly shorter delay to speed up job iteration
    time.sleep(1.5)

    selectors = [
        "li[data-occludable-job-id]",          # common LinkedIn job card list item
        "li.jobs-search-results__list-item",   # alt job card list item
        "div.job-card-container--clickable",   # card container
        "div.job-card-container",              # generic job card container
        "ul.jobs-search__results-list li",     # legacy selector
    ]

    for sel in selectors:
        cards = driver.find_elements(By.CSS_SELECTOR, sel)
        visible = [c for c in cards if c.is_displayed()]
        if visible:
            if log:
                debug(f"Found {len(visible)} job cards using selector '{sel}'")
            return visible

    if log:
        debug("No visible job cards found with known selectors.")
    return []


def extract_job_title(card) -> str:
    for selector in [
        "a.job-card-list__title",
        "a[data-control-name='job_card_title']",
    ]:
        try:
            title_elem = card.find_element(By.CSS_SELECTOR, selector)
            t = safe_text(title_elem).strip()
            if t:
                return t
        except NoSuchElementException:
            continue
    return safe_text(card).strip()[:80]


def extract_company_name(card) -> str:
    for selector in [
        "a.job-card-container__company-name",
        "span.job-card-container__primary-description",
        "a[data-control-name='job_card_company_link']",
    ]:
        try:
            elem = card.find_element(By.CSS_SELECTOR, selector)
            txt = safe_text(elem).strip()
            if txt:
                return txt
        except NoSuchElementException:
            continue
    return ""


def open_job_and_get_description(
    driver: webdriver.Chrome,
    card,
    index: int,
    expected_title: str = "",
    expected_company: str = "",
) -> Optional[str]:
    """
    Click a job card, wait for the *correct* job details pane to load,
    and return its description text.

    Fixes:
      - Uses robust click (ActionChains + JS fallback).
      - Waits for the page/URL to change after clicking.
      - Tries to verify that the right-hand job header (title/company)
        matches the left-hand card, to avoid mixing up jobs.
    """
    url_before = driver.current_url

    # Scroll the card into view and click robustly
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", card
        )
        time.sleep(0.2)
    except Exception:
        pass

    # Wait until the card is visible
    try:
        WebDriverWait(driver, 5).until(lambda d: card.is_displayed())
    except TimeoutException:
        debug(f"Job card #{index+1} is not visible/clickable; skipping.")
        return None

    clicked = False

    # Try a normal ActionChains click first
    try:
        actions = ActionChains(driver)
        actions.move_to_element(card).pause(0.1).click().perform()
        clicked = True
    except ElementClickInterceptedException as e:
        debug(f"Click on job card #{index+1} intercepted ({e}); trying JS click fallback.")
    except Exception as e:
        debug(f"Standard click on job card #{index+1} failed ({e}); trying JS click fallback.")

    # Fallback: JS click
    if not clicked:
        try:
            driver.execute_script("arguments[0].click();", card)
            clicked = True
        except Exception as e:
            debug(f"Could not click job card #{index+1} even with JS click: {e}; skipping.")
            return None

    # Helper to check whether the right-hand header looks like the expected job
    def header_matches_expected(d) -> bool:
        if not expected_title and not expected_company:
            return True

        title_query = (expected_title or "").strip().lower()
        company_query = (expected_company or "").strip().lower()

        title_text = ""
        company_text = ""

        title_selectors = [
            "h1.jobs-unified-top-card__job-title",
            "h1.topcard__title",
            "h2.jobs-unified-top-card__job-title",
            "h2.jobs-details-top-card__job-title",
        ]
        for sel in title_selectors:
            try:
                els = d.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                els = []
            for el in els:
                try:
                    if el.is_displayed():
                        txt = (el.text or "").strip()
                        if txt:
                            title_text = txt
                            break
                except Exception:
                    continue
            if title_text:
                break

        company_selectors = [
            "a.jobs-unified-top-card__company-name",
            "span.jobs-unified-top-card__company-name",
            "a.topcard__org-name-link",
            "span.topcard__flavor",
            "span.jobs-details-top-card__company-name",
        ]
        for sel in company_selectors:
            try:
                els = d.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                els = []
            for el in els:
                try:
                    if el.is_displayed():
                        txt = (el.text or "").strip()
                        if txt:
                            company_text = txt
                            break
                except Exception:
                    continue
            if company_text:
                break

        # If we can't find any header text, don't block – assume OK.
        if not title_text and not company_text:
            return True

        def matches(query: str, text: str) -> bool:
            q = (query or "").strip().lower()
            t = (text or "").strip().lower()
            if not q or not t:
                return True
            return q in t or t in q

        ok_title = matches(title_query, title_text) if title_query else True
        ok_company = matches(company_query, company_text) if company_query else True

        return ok_title and ok_company

    # Wait for the URL to change or header to look like the new job
    try:
        WebDriverWait(driver, 10).until(
            lambda d: d.current_url != url_before or header_matches_expected(d)
        )
    except TimeoutException:
        debug(
            f"Job card #{index+1}: URL/header did not clearly update after click; "
            "continuing and trying to read description anyway."
        )

    # Now wait for the right-hand job description pane
    try:
        desc_elem = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "div.jobs-description-content__text, div.jobs-box__html-content",
                )
            )
        )
    except TimeoutException:
        debug(f"Timed out waiting for description for job #{index+1}.")
        return None

    description = (desc_elem.text or "").strip()
    if not description:
        debug(f"Empty description for job #{index+1}.")
        return None

    # Final safety check: if header clearly mismatches expected job, skip
    try:
        if expected_title or expected_company:
            if not header_matches_expected(driver):
                debug(
                    f"Job card #{index+1}: right-hand job header does not match "
                    f"'{expected_title}' at '{expected_company}'; skipping to avoid wrong job."
                )
                return None
    except Exception:
        # If header check explodes, just accept the description
        pass

    return description




def write_job_description_file(job_title: str, company: str, description: str, job_index: int) -> Path:
    """
    Write job description to a text file and return its path.
    Also includes some metadata (title, company) at the top.
    """
    safe_title = (job_title or f"job_{job_index+1}").replace("/", "_").replace("\\", "_")
    filename = f"job_{job_index+1:02d}_{safe_title[:40].replace(' ', '_')}.txt"
    path = BASE_DIR / filename

    header_lines = []
    if job_title:
        header_lines.append(f"JOB TITLE: {job_title}")
    if company:
        header_lines.append(f"COMPANY: {company}")
    header_lines.append("")
    text = "\n".join(header_lines) + description

    path.write_text(text, encoding="utf-8")
    debug(f"Wrote job description to {path}")
    return path

def _find_cover_letter_pdf() -> Optional[Path]:
    """
    Best-effort helper to locate the tailored cover letter PDF produced
    by resume_and_cover_maker.py.

    It tries, in order:
      1) Known attributes on the rcm module (if they exist).
      2) Recent PDFs in BASE_DIR whose name includes 'cover' and 'letter'.
    """
    candidate_paths: List[Path] = []

    # Try explicit attributes on resume_and_cover_maker, if present
    for attr in ("COVER_LETTER_PDF_PATH", "COVER_LETTER_TEX_PATH", "COVER_LETTER_PDF_NAME"):
        try:
            val = getattr(rcm, attr)
        except AttributeError:
            continue

        if isinstance(val, Path):
            p = val if val.suffix.lower() == ".pdf" else val.with_suffix(".pdf")
            candidate_paths.append(p)
        elif isinstance(val, str):
            p = BASE_DIR / val
            p = p if p.suffix.lower() == ".pdf" else p.with_suffix(".pdf")
            candidate_paths.append(p)

    for p in candidate_paths:
        if p.exists():
            return p

    # Fallback: any recent PDF with "cover" + "letter" in the filename
    try:
        pdfs = sorted(
            BASE_DIR.glob("*.pdf"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        pdfs = list(BASE_DIR.glob("*.pdf"))

    for p in pdfs:
        name = p.name.lower()
        if "cover" in name and "letter" in name:
            return p

    return None

def _build_merged_resume_pdf(
    tailored_resume: Path,
    cover_letter_pdf: Optional[Path],
    job_index: int,
) -> Path:
    """
    Merge resume + cover letter into a single PDF for upload when a
    'resume / CV' field is present.

    If no cover_letter_pdf is provided or it does not exist, the original
    tailored_resume is returned unchanged.
    """
    if not cover_letter_pdf or not cover_letter_pdf.exists():
        return tailored_resume

    merged_name = f"job_{job_index+1:02d}_resume_plus_cover.pdf"
    merged_path = BASE_DIR / merged_name

    try:
        from PyPDF2 import PdfReader, PdfWriter  # type: ignore

        writer = PdfWriter()
        for pdf in (tailored_resume, cover_letter_pdf):
            reader = PdfReader(str(pdf))
            for page in reader.pages:
                writer.add_page(page)

        with merged_path.open("wb") as f:
            writer.write(f)

        debug(f"Merged resume + cover letter into {merged_path}")
        return merged_path
    except Exception as e:
        debug(f"Failed to merge resume + cover letter PDFs: {e}")
        # Fall back to just the resume if merge fails
        return tailored_resume


def generate_tailored_docs(base_resume_pdf: Path, job_desc_path: Path) -> Path:
    """
    Use resume_and_cover_maker.py to generate tailored resume + cover letter
    for the given job description.

    Returns the path to the tailored resume PDF.

    Side effect:
      - Stores the most recently detected tailored cover letter PDF on
        generate_tailored_docs._last_cover_letter_pdf so that other
        functions (Easy Apply / external apply) can upload it.
    """
    if not base_resume_pdf.exists():
        raise FileNotFoundError(f"Base resume PDF not found at {base_resume_pdf}")

    # Point the helper script at the right paths
    rcm.RESUME_PDF_PATH = base_resume_pdf
    rcm.JOB_DESC_PATH = job_desc_path

    debug("Generating tailored resume + cover letter with resume_and_cover_maker.py...")
    rcm.main()

    tailored_resume = BASE_DIR / TAILORED_RESUME_PDF_NAME
    if not tailored_resume.exists():
        # Fall back to whatever RESUME_TEX_PATH compiled to
        fallback = rcm.RESUME_TEX_PATH.with_suffix(".pdf")
        if fallback.exists():
            tailored_resume = fallback
        else:
            raise FileNotFoundError(
                f"Could not find tailored resume PDF at {tailored_resume} "
                f"or {fallback}. Check resume_and_cover_maker.py output."
            )

    # Try to discover the cover letter PDF produced by resume_and_cover_maker.py
    cover_letter_pdf = _find_cover_letter_pdf()
    setattr(generate_tailored_docs, "_last_cover_letter_pdf", cover_letter_pdf)

    return tailored_resume



def find_easy_apply_button(d) -> Optional[Any]:
        """
        Locate the blue **Easy Apply** button on the job detail pane.

        We *only* return buttons whose visible text or aria-label contains
        'Easy Apply', so that plain 'Apply' (external) is not treated as
        Easy Apply by mistake.
        """
        selectors = [
            "button.jobs-apply-button",
            "button.jobs-apply-button--top-card",
            "button[aria-label*='Easy Apply']",
            "button[aria-label*='easy apply']",
        ]

        for sel in selectors:
            try:
                btns = d.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                btns = []
            for b in btns:
                try:
                    if not (b.is_displayed() and b.is_enabled()):
                        continue
                    label = (b.text or "") + " " + (b.get_attribute("aria-label") or "")
                    label_lower = label.lower()
                    # must explicitly say "easy apply"
                    if "easy apply" in label_lower:
                        return b
                except Exception:
                    continue
        return None



def find_external_apply_button(driver: webdriver.Chrome):
    """
    Find the blue **Apply / Apply on company site** button for
    external applications.

    This explicitly *excludes* 'Easy Apply' so classification is clean:
      - 'Easy Apply'  → Easy Apply flow
      - 'Apply' / 'Apply on company site' → external flow
    """
    selectors = [
        "button.jobs-apply-button",
        "button.jobs-apply-button--top-card",
        "button[aria-label*='Apply']",
        "button[aria-label*='apply']",
    ]

    for sel in selectors:
        try:
            candidates = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:
            candidates = []

        for btn in candidates:
            try:
                if not (btn.is_displayed() and btn.is_enabled()):
                    continue

                label = (btn.text or "") + " " + (btn.get_attribute("aria-label") or "")
                label_lower = label.lower()

                # Skip true Easy Apply buttons – those are handled elsewhere
                if "easy apply" in label_lower:
                    continue

                # We want anything that looks like an Apply button:
                # "Apply", "Apply now", "Apply on company site", etc.
                if "apply" in label_lower:
                    return btn
            except Exception:
                continue

    return None



def upload_resume_in_container(
    container,
    resume_pdf: Path,
    cover_letter_pdf: Optional[Path] = None,
    merged_resume_pdf: Optional[Path] = None,
) -> None:
    """
    Find file inputs under the given container (dialog or full page)
    and upload the appropriate document.

    Behaviour:
      - If the input label/name/placeholder/id/class clearly mentions
        "cover letter" (or variants), upload cover_letter_pdf (when provided).
      - Otherwise, treat it as a resume / CV upload and upload
        merged_resume_pdf if given, falling back to resume_pdf.
      - If no visible file input is found, we also try hidden but enabled ones,
        because many sites hide <input type="file"> behind a styled button.
    """
    debug(
        f"Uploading documents if file inputs found: "
        f"resume={resume_pdf}, cover_letter={cover_letter_pdf}, merged_resume={merged_resume_pdf}"
    )
    try:
        all_inputs = container.find_elements(By.CSS_SELECTOR, "input[type='file']")
    except Exception:
        all_inputs = []

    if not all_inputs:
        debug("No <input type='file'> elements found in this container.")
        return

    def is_cover_field(inp) -> bool:
        """Heuristic: is this input a 'cover letter' field?"""
        try:
            label_text = (get_label_for_element(container, inp) or "").lower()
        except Exception:
            label_text = ""

        aria = (inp.get_attribute("aria-label") or "").lower()
        placeholder = (inp.get_attribute("placeholder") or "").lower()
        name_attr = (inp.get_attribute("name") or "").lower()
        id_attr = (inp.get_attribute("id") or "").lower()
        class_attr = (inp.get_attribute("class") or "").lower()

        combined = " ".join(
            [label_text, aria, placeholder, name_attr, id_attr, class_attr]
        )

        # Normalize some common variants
        combined = combined.replace("coverletter", "cover letter")
        combined = combined.replace("cover-letter", "cover letter")
        combined = combined.replace("motivation letter", "cover letter")
        combined = combined.replace("motivational letter", "cover letter")

        if "cover letter" in combined:
            return True
        if "cover" in combined and "letter" in combined:
            return True

        return False

    def try_upload(inputs: List[Any], allow_hidden: bool) -> bool:
        uploaded_any = False
        for inp in inputs:
            try:
                # Skip disabled inputs
                if not inp.is_enabled():
                    continue

                # If we don't allow hidden ones, require displayed
                if not allow_hidden:
                    try:
                        if not inp.is_displayed():
                            continue
                    except Exception:
                        continue

                cover_field = is_cover_field(inp)

                if cover_field:
                    if cover_letter_pdf and cover_letter_pdf.exists():
                        target = cover_letter_pdf
                        debug(
                            f"Uploading cover letter PDF {target} "
                            f"to a detected 'cover letter' field."
                        )
                    else:
                        debug(
                            "Detected a 'cover letter' upload field but no cover letter PDF was found; "
                            "skipping this input."
                        )
                        continue
                else:
                    # Any non-cover file field is treated as resume/CV.
                    target = merged_resume_pdf or resume_pdf
                    debug(
                        f"Uploading merged resume PDF {target} "
                        f"to a detected 'resume/CV' or generic file field."
                    )

                inp.send_keys(str(target.resolve()))
                uploaded_any = True
                time.sleep(1.5)
            except Exception as e:
                debug(f"Error while uploading into file input: {e}")
                continue
        return uploaded_any

def upload_resume_in_container(
    container,
    resume_pdf: Path,
    cover_letter_pdf: Optional[Path] = None,
    merged_resume_pdf: Optional[Path] = None,
) -> None:
    """
    Find any <input type="file"> under the given container (dialog or full page)
    and upload the correct document:

    - If the field looks like a **cover letter** field -> use cover_letter_pdf (if provided),
      otherwise fall back to resume_pdf.
    - If the field looks like a **resume / CV** field -> use merged_resume_pdf
      (resume + cover letter) if provided, otherwise resume_pdf.

    Works with both visible and hidden-but-enabled file inputs.
    """
    debug(
        f"Uploading documents if file inputs found: resume={resume_pdf}, "
        f"cover_letter={cover_letter_pdf}, merged={merged_resume_pdf}"
    )

    # ---------------- helper: read label/placeholder/name for an input ---------------- #
    def get_input_context(inp) -> str:
        pieces = []

        try:
            elem_id = (inp.get_attribute("id") or "").strip()
        except Exception:
            elem_id = ""

        # <label for="id">
        if elem_id:
            try:
                label_el = container.find_element(
                    By.XPATH, f".//label[@for='{elem_id}']"
                )
                pieces.append(safe_text(label_el))
            except Exception:
                pass

        # ancestor <label>
        try:
            label_el = inp.find_element(By.XPATH, "./ancestor::label[1]")
            pieces.append(safe_text(label_el))
        except Exception:
            pass

        # aria-label / placeholder / name / id
        try:
            aria = (inp.get_attribute("aria-label") or "").strip()
        except Exception:
            aria = ""
        try:
            placeholder = (inp.get_attribute("placeholder") or "").strip()
        except Exception:
            placeholder = ""
        try:
            name_attr = (inp.get_attribute("name") or "").strip()
        except Exception:
            name_attr = ""

        pieces.extend([aria, placeholder, name_attr, elem_id])

        ctx = " ".join(pieces).lower()
        # avoid crazy long strings
        if len(ctx) > 400:
            ctx = ctx[:397] + "..."
        return ctx

    # ---------------- helper: detect if this is a cover-letter input ---------------- #
    def is_cover_input(inp) -> bool:
        ctx = get_input_context(inp)
        # Most common patterns
        if "cover letter" in ctx:
            return True
        if "motivation letter" in ctx:
            return True
        # generic "cover" + "letter" separately
        if "cover" in ctx and "letter" in ctx:
            return True
        return False

    # ---------------- helper: choose which file to send for a given input ------------ #
    def choose_file_for_input(inp) -> Optional[Path]:
        # Prefer merged resume for resume/CV fields
        ctx = get_input_context(inp)

        is_cover = is_cover_input(inp)

        # If it looks like a cover letter field
        if is_cover:
            if cover_letter_pdf and cover_letter_pdf.exists():
                return cover_letter_pdf
            # fallback: at least upload the resume
            if resume_pdf and resume_pdf.exists():
                return resume_pdf
            return None

        # Otherwise treat as resume / CV / generic document upload
        if merged_resume_pdf and merged_resume_pdf.exists():
            return merged_resume_pdf
        if resume_pdf and resume_pdf.exists():
            return resume_pdf
        return None

    # ---------------- collect all file inputs ---------------------------------------- #
    try:
        all_inputs = container.find_elements(By.CSS_SELECTOR, "input[type='file']")
    except Exception:
        all_inputs = []

    if not all_inputs:
        debug("No <input type='file'> elements found in this container.")
        return

    visible_inputs = []
    hidden_inputs = []
    for inp in all_inputs:
        try:
            if inp.is_displayed():
                visible_inputs.append(inp)
            else:
                hidden_inputs.append(inp)
        except Exception:
            hidden_inputs.append(inp)

    # ---------------- actual upload logic -------------------------------------------- #
    def try_upload(inputs, allow_hidden: bool = False) -> bool:
        uploaded_any = False
        for inp in inputs:
            try:
                # Optionally skip hidden fields
                if not allow_hidden:
                    try:
                        if not inp.is_displayed():
                            continue
                    except Exception:
                        continue

                if not inp.is_enabled():
                    continue

                file_to_send = choose_file_for_input(inp)
                if not file_to_send:
                    continue

                debug(
                    f"Sending file '{file_to_send.name}' to file input "
                    f"(cover_field={is_cover_input(inp)})"
                )
                inp.send_keys(str(file_to_send.resolve()))
                uploaded_any = True
                time.sleep(1.0)
            except Exception:
                continue
        return uploaded_any

    uploaded_any = False

    # First try visible file inputs
    if visible_inputs:
        uploaded_any = try_upload(visible_inputs, allow_hidden=False)

    # If nothing was uploaded, try hidden but enabled file inputs
    if not uploaded_any and hidden_inputs:
        debug("No visible file inputs accepted upload; trying hidden file inputs.")
        uploaded_any = try_upload(hidden_inputs, allow_hidden=True)

    if uploaded_any:
        debug("One or more document fields were populated with resume / cover letter.")
    else:
        debug("Tried file inputs but could not upload resume/cover letter to any of them.")



def fill_basic_fields_in_container(container, profile: Dict[str, Any]) -> None:
    """
    Fill simple text/email/phone/location fields using applicant profile.
    Used for both Easy Apply modal and external forms.
    """
    try:
        inputs = container.find_elements(By.CSS_SELECTOR, "input")
    except Exception:
        inputs = []

    mapping = {
        "first name": profile.get("first_name"),
        "given name": profile.get("first_name"),
        "last name": profile.get("last_name"),
        "surname": profile.get("last_name"),
        "family name": profile.get("last_name"),
        "full name": f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
        "name": f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
        "email": profile.get("email"),
        "e-mail": profile.get("email"),
        "phone": profile.get("phone"),
        "mobile": profile.get("phone"),
        "telephone": profile.get("phone"),
        "city": profile.get("city"),
        "town": profile.get("city"),
        "postal": profile.get("postal_code"),
        "zip": profile.get("postal_code"),
        "postcode": profile.get("postal_code"),
        "country": profile.get("country"),
        "location": profile.get("city"),
        "linkedin": profile.get("linkedin_url"),
        "github": profile.get("github_url"),
        "portfolio": profile.get("portfolio_url"),
        "website": profile.get("portfolio_url") or profile.get("github_url"),
    }

    for inp in inputs:
        try:
            if not inp.is_displayed() or not inp.is_enabled():
                continue

            input_type = (inp.get_attribute("type") or "").lower()
            if input_type in ("hidden", "password"):
                continue

            current_value = inp.get_attribute("value") or ""
            if current_value.strip():
                # do not overwrite existing values
                continue

            aria_label = (inp.get_attribute("aria-label") or "").lower()
            placeholder = (inp.get_attribute("placeholder") or "").lower()
            name_attr = (inp.get_attribute("name") or "").lower()
            combined = " ".join([aria_label, placeholder, name_attr])

            value_to_use = None
            for key, val in mapping.items():
                if val and key in combined:
                    value_to_use = val
                    break

            if value_to_use:
                inp.click()
                inp.clear()
                inp.send_keys(str(value_to_use))
                time.sleep(0.15)
        except Exception:
            continue


def click_next_or_submit_in_container(
    container,
    mode: str = "easy",
    return_label: bool = False,
):
    """
    Click a 'Next', 'Continue', 'Review', 'Preview', 'Apply', or 'Submit' button
    inside the given container (dialog or full page).

    If return_label is False (default):
        - returns True if something was clicked, False otherwise.

    If return_label is True:
        - returns the label string that matched (e.g. "Submit application",
          "Next", "Done"), or None if nothing was clicked.
    """
    labels = [
        "Submit application",
        "Submit",
        "Apply",
        "Apply now",
        "Review",
        "Preview",          # <--- NEW for your "preview" request
        "Next",
        "Continue",
        "Save and continue",
        "Save & continue",
        "Proceed",
        "Confirm",
        "Done",
        "Finish",
    ]

    for label in labels:
        # Buttons
        try:
            btns = container.find_elements(
                By.XPATH,
                f".//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label.lower()}')]"
            )
        except Exception:
            btns = []
        for btn in btns:
            try:
                if btn.is_displayed() and btn.is_enabled():
                    debug(f"Clicking {mode} form button: '{label}'")
                    btn.click()
                    time.sleep(1.3)
                    if return_label:
                        return label
                    return True
            except Exception:
                continue

        # <input type="submit"> and similar
        try:
            inputs = container.find_elements(
                By.XPATH,
                f".//input[(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
                f"='{label.lower()}') and (@type='submit' or @type='button')]"
            )
        except Exception:
            inputs = []
        for inp in inputs:
            try:
                if inp.is_displayed() and inp.is_enabled():
                    debug(f"Clicking {mode} submit input: '{label}'")
                    inp.click()
                    time.sleep(1.3)
                    if return_label:
                        return label
                    return True
            except Exception:
                continue

    if return_label:
        return None
    return False





# ============================ GEMINI FORM HELPERS ============================ #

# Where we persist a working Gemini key between runs
GEMINI_KEY_FILE = BASE_DIR / "gemini_api_key.txt"

_GEMINI_CLIENT: Optional[genai.Client] = None
_GEMINI_CLIENT_KEY: Optional[str] = None


def speak(text: str) -> None:
    """
    Best-effort text-to-speech helper.

    Uses pyttsx3 if installed; otherwise just logs.
    You can remove this if you don't care about voice prompts.
    """
    try:
        import pyttsx3  # pip install pyttsx3
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except Exception:
        debug(f"[speak] {text}")


def save_gemini_api_key_to_disk(api_key: str) -> None:
    """
    Save the provided Gemini key into gemini_api_key.txt so that
    future runs can reuse it automatically.

    Also updates the in‑process cache so we don't have to read the file again.
    """
    global _CACHED_GEMINI_KEY
    api_key = (api_key or "").strip()
    if not api_key:
        return

    try:
        GEMINI_KEY_FILE.write_text(api_key + "\n", encoding="utf-8")
        _CACHED_GEMINI_KEY = api_key
        debug(f"Saved Gemini API key to {GEMINI_KEY_FILE}")
    except Exception as e:
        debug(f"Failed to save Gemini API key to disk: {e!r}")


def load_gemini_api_key_from_disk() -> Optional[str]:
    """
    Load a Gemini API key from gemini_api_key.txt if it exists.

    Uses an in‑memory cache so the actual file is read at most once
    per process lifetime.
    """
    global _CACHED_GEMINI_KEY

    # Already loaded in this process
    if _CACHED_GEMINI_KEY:
        return _CACHED_GEMINI_KEY

    try:
        if GEMINI_KEY_FILE.exists():
            key = GEMINI_KEY_FILE.read_text(encoding="utf-8").strip()
            if key:
                _CACHED_GEMINI_KEY = key
                debug(f"Loaded Gemini API key from {GEMINI_KEY_FILE}")
                return key
    except Exception as e:
        debug(f"Failed to read Gemini API key from disk: {e!r}")

    return None


def resolve_gemini_api_key_from_env_or_disk(interactive: bool = True) -> Optional[str]:
    """
    Resolve the Gemini API key using this priority:

      1) If GEMINI_API_KEY environment variable is set and non-empty:
           - use it,
           - overwrite gemini_api_key.txt with this value (so it persists).
      2) Else, if gemini_api_key.txt exists and has a key:
           - use that.
      3) Else, if interactive=True:
           - speak + print: "No Gemini key found, please paste Gemini key in terminal",
           - ask user to paste key in the terminal,
           - save it to gemini_api_key.txt and also set os.environ["GEMINI_API_KEY"],
           - return that key.
      4) Else:
           - return None (no key).

    This matches the behaviour:
      export GEMINI_API_KEY="your-gemini-api-key"
    → will refresh gemini_api_key.txt and use that key.
    """
    # 1) Try environment variable first
    env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    disk_key = load_gemini_api_key_from_disk()

    if env_key:
        # If env key differs from disk key, overwrite disk so future runs reuse it
        if env_key != (disk_key or ""):
            try:
                save_gemini_api_key_to_disk(env_key)
                debug("resolve_gemini_api_key_from_env_or_disk: wrote env GEMINI_API_KEY to gemini_api_key.txt.")
            except Exception as e:
                debug(f"resolve_gemini_api_key_from_env_or_disk: failed to write env key to disk: {e!r}")
        return env_key

    # 2) If no env key, but we have a key on disk, use that
    if disk_key:
        return disk_key

    # 3) No env, no disk key → optionally prompt user
    if not interactive:
        debug(
            "resolve_gemini_api_key_from_env_or_disk: no Gemini key found in env "
            "or gemini_api_key.txt and interactive prompting is disabled."
        )
        return None

    # Speak + console prompt
    try:
        speak("No Gemini key found. Please paste Gemini key in terminal.")
    except Exception:
        # fall back to logging only
        debug("No Gemini key found. Please paste Gemini key in terminal.")

    print("\n[Gemini] No Gemini key found.", flush=True)
    print("[Gemini] Please paste your Gemini API key below and press ENTER.", flush=True)
    print("         (This will be saved into gemini_api_key.txt for future runs.)", flush=True)

    new_key = input("GEMINI_API_KEY: ").strip()
    if not new_key:
        debug("User did not enter a Gemini key; Gemini-assisted features will remain disabled.")
        return None

    # Persist to disk and env
    try:
        save_gemini_api_key_to_disk(new_key)
    except Exception as e:
        debug(f"resolve_gemini_api_key_from_env_or_disk: failed to save new key to disk: {e!r}")

    os.environ["GEMINI_API_KEY"] = new_key
    debug("resolve_gemini_api_key_from_env_or_disk: Gemini key loaded from user input and saved.")
    return new_key



def prompt_for_new_gemini_key() -> Optional[str]:
    """
    Called when the current Gemini key is exhausted/invalid.

    Behaviour:
      - Speaks a short message ("Gemini key exhausted").
      - Prints instructions in the terminal.
      - Waits for user to paste a NEW key.
      - Saves it to gemini_api_key.txt.
      - Updates the in-process cache and GEMINI_API_KEY env var.
    """
    global _CACHED_GEMINI_KEY

    # Speak only once per run to avoid being too noisy
    if not getattr(prompt_for_new_gemini_key, "_spoken_once", False):
        try:
            speak("Gemini key exhausted. Please paste a new Gemini API key in the terminal.")
        except Exception:
            debug("Gemini key exhausted. Please paste a new Gemini API key in the terminal.")
        setattr(prompt_for_new_gemini_key, "_spoken_once", True)

    print(
        "\n[Gemini] Your GEMINI_API_KEY appears exhausted or invalid.\n"
        "1) Open Google AI Studio in your browser.\n"
        "2) Create a NEW API key.\n"
        "3) Paste the new key here.\n"
        "Press ENTER without typing anything to skip (Gemini will be disabled for now).\n",
        flush=True,
    )

    new_key = input("New GEMINI_API_KEY: ").strip()
    if not new_key:
        debug("User did not enter a new Gemini key; Gemini will be skipped for this step.")
        return None

    # Persist, cache, and expose via env var
    save_gemini_api_key_to_disk(new_key)
    os.environ["GEMINI_API_KEY"] = new_key
    _CACHED_GEMINI_KEY = new_key
    debug("Gemini API key updated and saved to gemini_api_key.txt.")
    return new_key




# ============================ GEMINI FORM HELPERS ============================ #

_GEMINI_CLIENT: Optional[genai.Client] = None
_GEMINI_CLIENT_KEY: Optional[str] = None


def get_gemini_client(api_key: str) -> genai.Client:
    """
    Return a cached Gemini client for the given API key.

    If the key changes (because the user provided a new one after exhaustion),
    we recreate the client.
    """
    global _GEMINI_CLIENT, _GEMINI_CLIENT_KEY
    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("Empty Gemini API key passed to get_gemini_client().")

    if _GEMINI_CLIENT is None or _GEMINI_CLIENT_KEY != api_key:
        _GEMINI_CLIENT = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(api_version="v1alpha"),
        )
        _GEMINI_CLIENT_KEY = api_key
    return _GEMINI_CLIENT


# This text is sent to Gemini so it knows what helper functions exist
GEMINI_FUNCTION_REFERENCE = """
You are helping a Python automation script fill job application forms.

For context ONLY (you do NOT call these), the main helper functions are:

- build_form_schema(container) -> schema:
    Inspect the current form container in the browser and describe all fields:
    - text_fields, textareas, select_fields (dropdowns),
      radio_groups (MCQ / single-choice), checkboxes.

- form_needs_gemini(container, schema) -> bool:
    Returns True if there is at least one visible & enabled field that is empty.
    If everything is filled, the script skips calling you.

- save_form_answers_to_file(job_index, step_index, job_title, mode, answers):
    Persists your JSON answers for this specific job and step on disk
    under form_answers/.

- load_form_answers_from_file(job_index, step_index, mode) -> answers or None:
    Loads previously saved answers from disk ("memory") so the script can reuse
    your old answers instead of calling you again for the same step.

- merge_gemini_answer_dicts(base, update) -> merged_answers:
    Keeps existing non-empty answers from base, and only uses new ones from update
    for missing fields.

- apply_gemini_answers_to_form(driver, container, schema, answers):
    Takes your JSON answers and actually types/selects them into the HTML inputs,
    dropdowns, and MCQs.

- answer_form_with_gemini_for_container(...):
    Orchestrator for one step: builds schema, checks if anything is empty,
    tries to reuse memory, and only calls you if needed.
"""




def get_label_for_element(container, elem) -> str:
    """
    Try to derive a human-readable label for an input/select/textarea.
    """
    label_text = ""
    elem_id = (elem.get_attribute("id") or "").strip()
    if elem_id:
        try:
            label_elem = container.find_element(By.XPATH, f".//label[@for='{elem_id}']")
            label_text = safe_text(label_elem).strip()
        except NoSuchElementException:
            pass

    if not label_text:
        try:
            label_elem = elem.find_element(By.XPATH, "./ancestor::label[1]")
            label_text = safe_text(label_elem).strip()
        except NoSuchElementException:
            pass

    if not label_text:
        aria = (elem.get_attribute("aria-label") or "").strip()
        placeholder = (elem.get_attribute("placeholder") or "").strip()
        title_attr = (elem.get_attribute("title") or "").strip()
        pieces = [aria, placeholder, title_attr]
        label_text = " / ".join([p for p in pieces if p])

    # Avoid insanely long labels
    label_text = label_text.strip()
    if len(label_text) > 300:
        label_text = label_text[:297] + "..."
    return label_text


def build_form_schema(container) -> Dict[str, Any]:
    """
    Inspect the container (dialog or page section) and produce a schema describing
    recognizable fields (text inputs, textareas, selects, radio groups, checkboxes).
    """
    schema: Dict[str, Any] = {
        "text_fields": [],
        "textareas": [],
        "select_fields": [],
        "radio_groups": [],
        "checkboxes": [],
    }

    # ------------- INPUTS (text, email, tel, number, radio, checkbox, etc.) -------------
    try:
        inputs = container.find_elements(By.CSS_SELECTOR, "input")
    except Exception:
        inputs = []

    radio_by_name: Dict[str, List[Any]] = {}

    for idx, inp in enumerate(inputs):
        try:
            if not inp.is_displayed() or not inp.is_enabled():
                continue
        except Exception:
            continue

        input_type = (inp.get_attribute("type") or "text").lower()
        if input_type in ("hidden", "password", "submit", "button", "image"):
            continue

        elem_id = (inp.get_attribute("id") or "").strip()
        name_attr = (inp.get_attribute("name") or "").strip()
        placeholder = (inp.get_attribute("placeholder") or "").strip()
        label = get_label_for_element(container, inp)

        if input_type == "radio":
            # Group radios by a stable key. If there is no real name, we still group them.
            group_name = name_attr or elem_id or f"radio_group_{len(radio_by_name)+1}"
            radio_by_name.setdefault(group_name, []).append(inp)

        elif input_type == "checkbox":
            box_key = elem_id or name_attr or f"checkbox_{len(schema['checkboxes'])+1}"
            schema["checkboxes"].append(
                {
                    "box_key": box_key,
                    "id": elem_id,
                    "name": name_attr,
                    "label": label,
                }
            )
        else:
            field_key = elem_id or name_attr or f"text_{len(schema['text_fields'])+1}"
            schema["text_fields"].append(
                {
                    "field_key": field_key,
                    "id": elem_id,
                    "name": name_attr,
                    "placeholder": placeholder,
                    "label": label,
                    "type": input_type,
                }
            )

    # ------------- TEXTAREAS -------------
    try:
        textareas = container.find_elements(By.CSS_SELECTOR, "textarea")
    except Exception:
        textareas = []

    for idx, ta in enumerate(textareas):
        try:
            if not ta.is_displayed() or not ta.is_enabled():
                continue
        except Exception:
            continue

        elem_id = (ta.get_attribute("id") or "").strip()
        name_attr = (ta.get_attribute("name") or "").strip()
        placeholder = (ta.get_attribute("placeholder") or "").strip()
        label = get_label_for_element(container, ta)
        field_key = elem_id or name_attr or f"textarea_{len(schema['textareas'])+1}"

        schema["textareas"].append(
            {
                "field_key": field_key,
                "id": elem_id,
                "name": name_attr,
                "placeholder": placeholder,
                "label": label,
            }
        )

    # ------------- SELECTS (native dropdowns / top-down scrolls) -------------
    try:
        selects = container.find_elements(By.CSS_SELECTOR, "select")
    except Exception:
        selects = []

    for idx, sel in enumerate(selects):
        try:
            if not sel.is_displayed() or not sel.is_enabled():
                continue
        except Exception:
            continue

        elem_id = (sel.get_attribute("id") or "").strip()
        name_attr = (sel.get_attribute("name") or "").strip()
        label = get_label_for_element(container, sel)
        field_key = elem_id or name_attr or f"select_{len(schema['select_fields'])+1}"

        options = []
        try:
            for opt in sel.find_elements(By.TAG_NAME, "option"):
                txt = (opt.text or "").strip()
                if txt:
                    options.append(txt)
        except Exception:
            pass

        schema["select_fields"].append(
            {
                "field_key": field_key,
                "id": elem_id,
                "name": name_attr,
                "label": label,
                "options": options,
            }
        )

    # ------------- RADIO GROUPS (MCQ / one-choice) -------------
    for group_name, radios in radio_by_name.items():
        options: List[str] = []
        inputs_meta: List[Dict[str, Any]] = []
        group_label = ""

        for r in radios:
            option_label = get_label_for_element(container, r)

            if not group_label:
                try:
                    wrapper = r.find_element(By.XPATH, "./ancestor::div[1]")
                    group_label = safe_text(wrapper).strip()
                except Exception:
                    group_label = option_label

            options.append(option_label)

            rid = (r.get_attribute("id") or "").strip()
            rname = (r.get_attribute("name") or "").strip()
            rval = (r.get_attribute("value") or "").strip()

            inputs_meta.append(
                {
                    "id": rid,
                    "name": rname,
                    "value": rval,
                    "label": option_label,
                }
            )

        if group_label and len(group_label) > 300:
            group_label = group_label[:300] + "..."

        schema["radio_groups"].append(
            {
                "group_key": group_name,
                "name": group_name,
                "label": group_label,
                "options": options,
                "inputs": inputs_meta,
            }
        )

    return schema


def infer_experience_for_skill(skill: str, job_description: str, profile: Dict[str, Any]) -> str:
    """
    Smart experience inference engine (Option Z + Confidence Level 2)

    - Core skills → always 3 years unless job requires more
    - Secondary skills → 2 years unless job requires more
    - Rare skills → 1–2 years depending on relevance
    - If job lists a required number (3+, 5+), match that
    """

    skill_lower = skill.lower()

    # Detect job requirement years: "3+ years", "5 years of experience"
    import re
    requirement_match = re.search(r"(\d+)\+?\s+years?", job_description.lower())
    required_years = int(requirement_match.group(1)) if requirement_match else None

    # If job explicitly requires X years → follow it
    if required_years:
        return f"{required_years} years"

    # Core skills
    core_skills = [
        "python", "machine learning", "deep learning", "computer vision",
        "nlp", "natural language processing", "ai", "artificial intelligence"
    ]

    # Secondary skills
    secondary_skills = [
        "sql", "docker", "kubernetes", "aws", "git", "linux",
        "fastapi", "react", "javascript", "apis", "tensorflow", "pytorch"
    ]

    # If the skill is core → always 3 years
    if any(c in skill_lower for c in core_skills):
        return "3 years"

    # If the skill is secondary → always 2 years
    if any(s in skill_lower for s in secondary_skills):
        return "2 years"

    # Skills in extra_skills → assume 1–2 years (safe)
    extra = profile.get("extra_skills", [])
    if skill in extra:
        return "2 years"

    # Unknown skills → safe fallback
    return "1 year"


def call_gemini_for_form_answers(
    resume_text: str,
    applicant_profile: Dict[str, Any],
    job_description: str,
    form_schema: Dict[str, Any],
    api_key: str,
    existing_answers: Optional[Dict[str, Any]] = None,
    missing_keys: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """
    Call Gemini with a rich prompt and return the parsed answers dictionary.

    Supports both legacy and new calling styles:

        call_gemini_for_form_answers(..., api_key)
        call_gemini_for_form_answers(..., api_key,
                                     existing_answers=...,
                                     missing_keys=...)

    - Prefers GEMINI_API_KEY from the environment if set.
    - Falls back to gemini_api_key.txt, then to the api_key argument.
    - Detects key/quota errors and asks for a new key via prompt_for_new_gemini_key().
    - Always returns a well-formed (possibly empty) answers structure with sections:
        text_fields, textareas, select_fields, radio_groups, checkboxes.
    """
    # ---------- helpers ----------

    def empty_answer_dict() -> Dict[str, Any]:
        return {
            "text_fields": {},
            "textareas": {},
            "select_fields": {},
            "radio_groups": {},
            "checkboxes": {},
        }

    # Normalise existing_answers
    if existing_answers is None:
        existing_answers = empty_answer_dict()
    else:
        # Make a shallow copy and ensure all sections exist as dicts
        cleaned: Dict[str, Any] = empty_answer_dict()
        for sect in cleaned.keys():
            val = existing_answers.get(sect, {})
            cleaned[sect] = val if isinstance(val, dict) else {}
        existing_answers = cleaned

    # Normalise missing_keys
    if missing_keys is None:
        missing_keys = {
            "text_fields": [],
            "textareas": [],
            "select_fields": [],
            "radio_groups": [],
            "checkboxes": [],
        }
    else:
        norm_missing: Dict[str, List[str]] = {}
        for sect in ("text_fields", "textareas", "select_fields", "radio_groups", "checkboxes"):
            v = missing_keys.get(sect, [])
            if not isinstance(v, list):
                v = []
            norm_missing[sect] = v
        missing_keys = norm_missing

    # ---------- resolve API key ----------

    # Always prefer the key from gemini_api_key.txt
    api_key = (api_key or "").strip()
    try:
        key_file = BASE_DIR / "gemini_api_key.txt"
        if key_file.exists():
            file_key = key_file.read_text(encoding="utf-8").strip()
            if file_key:
                api_key = file_key
                debug(f"Loaded Gemini key from {key_file}")
    except Exception as e:
        debug(f"Could not read gemini_api_key.txt: {e!r}")


    if not api_key:
        debug("No Gemini API key found; skipping AI-assisted form filling for this step.")
        return empty_answer_dict()

    # ---------- build prompt ----------

    try:
        client = get_gemini_client(api_key)
    except Exception as e:
        debug(f"call_gemini_for_form_answers: get_gemini_client failed: {e!r}")
        return empty_answer_dict()

    min_years = applicant_profile.get("min_years_experience")
    extra_skills = applicant_profile.get("extra_skills") or []  # may be unused, but documented in prompt

    prompt = f"""
You are an extremely helpful job application assistant.
Your job is to fill online application questions so that I have the best chance
of being selected, while still staying consistent with the true information I provide.

You will receive:
1) My full resume text (resume_text).
2) A structured applicant profile JSON (applicant_profile) which may contain:
   - personal info (name, email, etc.),
   - min_years_experience: a trusted minimum total experience value if present (currently: {min_years!r}),
   - extra_skills: a list of additional skills I have, even if they are not explicitly written in my resume.
3) The job description text (job_description).
4) A form_schema JSON describing every visible question/field in the current step.
5) existing_answers JSON: your previously suggested answers that are already saved
   in the cache for this job+step (may be empty).
6) missing_keys JSON: for each section, a list of field keys that are still EMPTY
   in the web page right now (MCQs / single-choice, dropdowns / top-down scrolls,
   text inputs, textareas, and checkboxes).

VERY IMPORTANT GUIDELINES:

- General honesty:
    * Use resume_text and applicant_profile as the source of truth.
    * extra_skills in applicant_profile are real skills I have, they just might not be written in the resume.
      You may safely use them when answering skill questions.

- Years of experience:
    * If the question asks for TOTAL years of professional experience, always answer with AT LEAST
      applicant_profile.min_years_experience when it is provided (currently {min_years!r}),
      even if resume_text seems to list fewer years.
    * If the question is "Years of experience with <technology>" and that technology appears in either
      resume_text OR in applicant_profile.extra_skills, you may answer with a reasonable number of years
      (for example 2+) consistent with my overall timeline; do NOT leave it blank just because the exact
      number is not in the resume.
    * If a skill is clearly not present in either resume_text or extra_skills, prefer 0, "No experience", or
      a safe response like "I have academic / project exposure" rather than fabricating a strong claim.

- Skills questions:
    * If a skill appears in extra_skills (even if not in the resume text), you may confidently answer YES
      or describe reasonable proficiency.
    * When multiple options are available, choose the one that maximizes my chances while remaining plausible.

- Additional YES/NO and dropdown availability questions:
    * These usually appear under headings like "Additional Questions" as radio buttons or dropdowns, with labels such as:
      "Are you comfortable commuting to this job's location?",
      "Would you be willing to relocate to this job's location or region?",
      "Would you be comfortable working in a customer-facing role?",
      "Would you be comfortable traveling to perform field work?", etc.
    * For any such field whose key appears in missing_keys.radio_groups or missing_keys.select_fields,
      choose the option that most helps me get the job while staying realistic and consistent with
      resume_text and applicant_profile.
      Prefer positive / "Yes" style answers for willingness and availability questions unless that would
      clearly conflict with legal or factual constraints (for example, visa sponsorship or work-authorization
      questions where the truthful answer must be "No").

- Use of existing_answers and missing_keys:
    * existing_answers shows what you have already answered for this job+step (from cache).
    * missing_keys tells you which specific field keys are still empty in the browser right now.
    * Focus your effort on keys listed in missing_keys for each section.
    * You may ALSO provide answers for other keys if helpful, but do NOT invent keys that do not appear in form_schema.

FORM ANSWERS OUTPUT FORMAT (JSON ONLY):

Return STRICTLY a JSON object of the form:

{
  "text_fields": { "<field_key>": "answer or null", ... },
  "textareas": { "<field_key>": "answer or null", ... },
  "select_fields": { "<field_key>": "one of the option texts or null", ... },
  "radio_groups": { "<group_key>": "one of the option texts or null", ... },
  "checkboxes": { "<box_key>": true/false/null, ... }
}

Where:
- Each <field_key> / <group_key> / <box_key> is exactly one of the keys from form_schema.
- If you genuinely cannot answer a question, set its value to null (or false for clearly unselected checkboxes).
- DO NOT include any keys that are not present in form_schema.
- DO NOT wrap the JSON in markdown fences; return raw JSON that can be parsed directly.

Here is the data:

resume_text:
{resume_text}

applicant_profile (JSON):
{json.dumps(applicant_profile, indent=2)}

job_description:
{job_description}

form_schema (JSON):
{json.dumps(form_schema, indent=2)}

existing_answers (JSON):
{json.dumps(existing_answers, indent=2)}

missing_keys (JSON):
{json.dumps(missing_keys, indent=2)}
"""


    max_retries = 3
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                ),
            )
            raw_text = (response.text or "").strip()
            if not raw_text:
                debug("Gemini returned empty response; using empty answers.")
                return empty_answer_dict()

            # Try direct JSON parse first
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                # Sometimes it might wrap JSON in ```json ... ``` or include text around it; try to salvage.
                stripped = raw_text.strip()
                if stripped.startswith("```"):
                    parts = stripped.split("```")
                    if len(parts) >= 2:
                        candidate = parts[1].lstrip("json").strip()
                    else:
                        candidate = stripped
                else:
                    # best-effort: search for first {...} block
                    m = re.search(r"(\{.*\})", stripped, flags=re.DOTALL)
                    candidate = m.group(1) if m else stripped

                try:
                    data = json.loads(candidate)
                except Exception as e2:
                    debug(f"Gemini response was not valid JSON even after salvage: {e2!r}")
                    return empty_answer_dict()

            if not isinstance(data, dict):
                debug("Gemini response is not a JSON object; using empty answers.")
                return empty_answer_dict()

            # Normalise result sections
            result = empty_answer_dict()
            for sect in result.keys():
                sec_val = data.get(sect)
                if isinstance(sec_val, dict):
                    result[sect] = sec_val

            return result

        except Exception as e:
            last_error = e
            msg = (str(e) or "").lower()
            debug(f"call_gemini_for_form_answers: attempt {attempt} failed: {e!r}")

            # Detect key/quota related messages and prompt once more for a key if appropriate
            key_issue_tokens = [
                "apikey",
                "api key",
                "invalid api key",
                "invalid api_key",
                "permission_denied",
                "permission denied",
                "unauthorized",
                "quota",
                "exhaust",
                "exceeded",
                "billing",
                "403",
                "401",
            ]
            if any(tok in msg for tok in key_issue_tokens):
                debug("Detected possible Gemini API key / quota issue.")
                new_key = prompt_for_new_gemini_key()
                if new_key:
                    api_key = new_key.strip()
                    client = get_gemini_client(api_key)
                    continue  # retry with new key
                else:
                    # User chose not to provide a new key; stop retrying.
                    break


            # Non-key-related error: brief backoff then maybe retry
            time.sleep(1.5)

    if last_error is not None:
        debug(f"Gemini call ultimately failed after {max_retries} attempts: {last_error!r}")
    return empty_answer_dict()




def best_match_option(options: List[str], target: str) -> Optional[str]:
    """
    If Gemini returns an option not present in the real dropdown/list,
    pick the closest matching valid option.
    """
    target = target.lower().strip()
    for opt in options:
        if target == opt.lower().strip():
            return opt
    
    # loose contains match
    for opt in options:
        if target in opt.lower():
            return opt

    # fallback: return first option
    return options[0] if options else None


def apply_gemini_answers_to_form(
    driver: webdriver.Chrome,
    container,
    form_schema: Dict[str, Any],
    answers: Dict[str, Any],
) -> None:
    """
    Fill the form using Gemini’s answers.

    - Never overwrites existing fields.
    - Handles text inputs, textareas, dropdowns, radio MCQ/Yes‑No, checkboxes.
    """

    # ---------------- TEXT INPUT FIELDS ---------------- #
    tf_answers: Dict[str, Any] = answers.get("text_fields") or {}

    for field in form_schema.get("text_fields", []):
        key = field.get("field_key")
        if not key:
            continue

        value = tf_answers.get(key)
        if value is None or str(value).strip() == "":
            continue

        elem = None
        elem_id = (field.get("id") or "").strip()
        name_attr = (field.get("name") or "").strip()

        if elem_id:
            try:
                elem = container.find_element(By.ID, elem_id)
            except Exception:
                elem = None

        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"input[name='{name_attr}']")
            except Exception:
                elem = None

        if elem is None:
            continue

        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue

            current = elem.get_attribute("value") or ""
            if current.strip():
                # respect any existing non‑empty value (prefilled or user‑entered)
                continue

            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", elem
            )
            time.sleep(0.1)

            elem.click()
            elem.clear()
            elem.send_keys(str(value))
            time.sleep(0.15)
        except Exception:
            continue

    # ---------------- TEXTAREAS ---------------- #
    ta_answers: Dict[str, Any] = answers.get("textareas") or {}

    for field in form_schema.get("textareas", []):
        key = field.get("field_key")
        if not key:
            continue

        value = ta_answers.get(key)
        if value is None or str(value).strip() == "":
            continue

        elem = None
        elem_id = (field.get("id") or "").strip()
        name_attr = (field.get("name") or "").strip()

        if elem_id:
            try:
                elem = container.find_element(By.ID, elem_id)
            except Exception:
                elem = None

        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"textarea[name='{name_attr}']")
            except Exception:
                elem = None

        if elem is None:
            continue

        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue

            current = (elem.get_attribute("value") or elem.text or "").strip()
            if current:
                continue

            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", elem
            )
            time.sleep(0.1)

            elem.click()
            elem.clear()
            elem.send_keys(str(value))
            time.sleep(0.15)
        except Exception:
            continue

    # ---------------- SELECT FIELDS (DROPDOWNS) ---------------- #
    sel_answers: Dict[str, Any] = answers.get("select_fields") or {}

    for field in form_schema.get("select_fields", []):
        key = field.get("field_key")
        if not key:
            continue

        desired = sel_answers.get(key)
        if desired is None or str(desired).strip() == "":
            continue

        desired_str = str(desired).strip().lower()

        elem = None
        elem_id = (field.get("id") or "").strip()
        name_attr = (field.get("name") or "").strip()

        if elem_id:
            try:
                elem = container.find_element(By.ID, elem_id)
            except Exception:
                elem = None

        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"select[name='{name_attr}']")
            except Exception:
                elem = None

        if elem is None:
            continue

        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue

            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", elem
            )
            time.sleep(0.1)

            sel = Select(elem)
            options = [o.text.strip() for o in sel.options if o.text.strip()]

            # Use best_match_option helper to map Gemini text onto a real option
            chosen = best_match_option(options, str(desired))

            if chosen:
                sel.select_by_visible_text(chosen)
                time.sleep(0.15)
            else:
                debug(f"[select] No valid match for '{desired}' under key {key}")
        except Exception:
            continue

    # ---------------- RADIO GROUPS (MCQ / YES‑NO / SINGLE CHOICE) ---------------- #
    rg_answers: Dict[str, Any] = answers.get("radio_groups") or {}

    for group in form_schema.get("radio_groups", []):
        group_key = group.get("group_key")
        if not group_key:
            continue

        desired = rg_answers.get(group_key)
        if desired is None:
            continue

        raw_desired = str(desired).strip()
        if not raw_desired:
            continue

        desired_str = raw_desired.lower()

        # Normalise typical yes/no style answers using the schema options
        options = group.get("options") or []
        options_lower = [o.lower().strip() for o in options]

        yes_aliases = {"yes", "y", "true", "1", "yeah", "yep"}
        no_aliases = {"no", "n", "false", "0", "nope"}

        if desired_str in yes_aliases and options:
            mapped = best_match_option(options, "yes")
            if mapped:
                desired_str = mapped.lower().strip()
        elif desired_str in no_aliases and options:
            mapped = best_match_option(options, "no")
            if mapped:
                desired_str = mapped.lower().strip()
        elif options:
            # General mapping onto one of the real options
            mapped = best_match_option(options, raw_desired)
            if mapped:
                desired_str = mapped.lower().strip()

        # Collect underlying <input type="radio"> elements for this group
        radios: List[Any] = []
        inputs_meta = group.get("inputs") or []

        for meta in inputs_meta:
            rid = (meta.get("id") or "").strip()
            rname = (meta.get("name") or "").strip()

            if rid:
                try:
                    radios.append(container.find_element(By.ID, rid))
                except Exception:
                    pass

            if rname:
                try:
                    radios.extend(
                        container.find_elements(
                            By.CSS_SELECTOR,
                            f"input[type='radio'][name='{rname}']",
                        )
                    )
                except Exception:
                    pass

        # Fallback: group-level name
        if not radios:
            group_name = (group.get("name") or "").strip()
            if group_name:
                try:
                    radios = container.find_elements(
                        By.CSS_SELECTOR,
                        f"input[type='radio'][name='{group_name}']",
                    )
                except Exception:
                    radios = []

        if not radios:
            continue

        # De‑duplicate radios
        seen_ids: set = set()
        deduped: List[Any] = []
        for r in radios:
            ident = id(r)
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
            deduped.append(r)

        # Try to click a matching radio using its label or wrapper
        for r in deduped:
            try:
                label_text = (get_label_for_element(container, r) or "").strip().lower()
                val_text = (r.get_attribute("value") or "").strip().lower()

                # Does this radio correspond to the desired answer?
                if not (
                    desired_str == label_text
                    or desired_str == val_text
                    or desired_str in label_text
                    or desired_str in val_text
                    or (label_text and label_text in desired_str)
                    or (val_text and val_text in desired_str)
                ):
                    continue

                target = None

                # 1) Try <label for="id">
                rid = (r.get_attribute("id") or "").strip()
                if rid:
                    try:
                        lab = container.find_element(By.XPATH, f".//label[@for='{rid}']")
                        if lab.is_displayed() and lab.is_enabled():
                            target = lab
                    except Exception:
                        pass

                # 2) Try ancestor label or radio wrapper
                if target is None:
                    for xp in (
                        "./ancestor::label[1]",
                        "./ancestor::*[@role='radio'][1]",
                        "./ancestor::*[contains(@class,'radio')][1]",
                        "./ancestor::*[contains(@class,'artdeco-radio')][1]",
                    ):
                        try:
                            anc = r.find_element(By.XPATH, xp)
                            if anc.is_displayed() and anc.is_enabled():
                                target = anc
                                break
                        except Exception:
                            continue

                # 3) Fallback to the input itself (even if styled/hidden we'll try JS click)
                if target is None:
                    target = r

                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", target
                    )
                except Exception:
                    pass

                time.sleep(0.1)
                try:
                    target.click()
                except Exception:
                    # If normal click fails (hidden/overlay), force JS click
                    try:
                        driver.execute_script("arguments[0].click();", target)
                    except Exception:
                        continue

                time.sleep(0.1)
                # Once we successfully click a radio for this group, move to next group
                break

            except Exception:
                continue

    # ---------------- CHECKBOXES ---------------- #
    cb_answers: Dict[str, Any] = answers.get("checkboxes") or {}

    for field in form_schema.get("checkboxes", []):
        key = field.get("box_key")
        if not key:
            continue

        val = cb_answers.get(key)
        if val is None:
            continue

        target_state = bool(val)

        elem = None
        elem_id = (field.get("id") or "").strip()
        name_attr = (field.get("name") or "").strip()

        if elem_id:
            try:
                elem = container.find_element(By.ID, elem_id)
            except Exception:
                elem = None

        if elem is None and name_attr:
            try:
                elem = container.find_element(
                    By.CSS_SELECTOR, f"input[type='checkbox'][name='{name_attr}']"
                )
            except Exception:
                elem = None

        if elem is None:
            continue

        try:
            if not elem.is_displayed() or not elem.is_enabled():
                continue

            current_state = elem.is_selected()
            if current_state != target_state:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", elem
                )
                time.sleep(0.1)
                try:
                    elem.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", elem)
                time.sleep(0.1)
        except Exception:
            continue



def answer_form_with_gemini_for_container(
    driver: webdriver.Chrome,
    container,
    resume_plain: str,
    applicant_profile: Dict[str, Any],
    job_description: str,
    gemini_api_key: str,
    job_index: int,
    step_index: int,
    job_title: str,
    mode: str,
) -> None:
    """
    Smarter orchestrator that:
      - inspects the full form schema (text, textarea, selects, radio groups, checkboxes),
      - for every field that is currently EMPTY in the DOM:
          1) try to apply a cached answer for that specific field (memory-first),
          2) re-check which fields remain empty,
          3) if any remain empty, call Gemini ONCE (passing existing answers) to fill the rest,
             merge results into memory, save and apply them.

    Important:
      - This function never overwrites non-empty DOM fields.
      - It only calls Gemini when necessary and only once per container invocation.
    """
    # Resolve key once for this call (env > disk > prompt)
    effective_key = resolve_gemini_api_key_from_env_or_disk(interactive=False)
    if effective_key:
        gemini_api_key = effective_key

    gemini_api_key = (gemini_api_key or "").strip()
    if not gemini_api_key:
        debug("answer_form_with_gemini_for_container: no Gemini key available; skipping Gemini assistance.")
        return



    # Build the schema for the current container (all visible questions)
    form_schema = build_form_schema(container)

    # Quick exit if no recognizable fields
    if not (
        form_schema.get("text_fields")
        or form_schema.get("textareas")
        or form_schema.get("select_fields")
        or form_schema.get("radio_groups")
        or form_schema.get("checkboxes")
    ):
        debug(f"No recognizable form fields found for {mode} step {step_index+1}; skipping.")
        return

    # Helper to find element by id or name
    def _find_elem(tag: str, field: Dict[str, Any]):
        elem = None
        elem_id = (field.get("id") or "").strip()
        name_attr = (field.get("name") or "").strip()
        try:
            if elem_id:
                elem = container.find_element(By.ID, elem_id)
        except Exception:
            elem = None
        if elem is None and name_attr:
            try:
                elem = container.find_element(By.CSS_SELECTOR, f"{tag}[name='{name_attr}']")
            except Exception:
                elem = None
        return elem

    # Helper to test whether a schema item is filled in the DOM
    def _is_filled(field_type: str, item: Dict[str, Any]) -> bool:
        try:
            if field_type == "text":
                el = _find_elem("input", item)
                if not el:
                    return False
                if not el.is_displayed() or not el.is_enabled():
                    return True  # treat hidden/disabled as "not needing fill"
                val = (el.get_attribute("value") or "").strip()
                return bool(val)
            if field_type == "textarea":
                el = _find_elem("textarea", item)
                if not el:
                    return False
                if not el.is_displayed() or not el.is_enabled():
                    return True
                val = (el.get_attribute("value") or "").strip()
                return bool(val)
            if field_type == "select":
                el = _find_elem("select", item)
                if not el:
                    return False
                if not el.is_displayed() or not el.is_enabled():
                    return True
                sel = Select(el)
                selected = sel.all_selected_options
                if not selected:
                    return False
                txt = (selected[0].text or "").strip().lower()
                val = (selected[0].get_attribute("value") or "").strip().lower()
                placeholders = {"select", "select one", "please select", "choose", "choose one"}
                if (not txt and not val) or any(p in txt for p in placeholders):
                    return False
                return True
            if field_type == "radio":
                inputs_meta = item.get("inputs") or []
                radios = []
                for meta in inputs_meta:
                    rid = (meta.get("id") or "").strip()
                    rname = (meta.get("name") or "").strip()
                    if rid:
                        try:
                            radios.append(container.find_element(By.ID, rid))
                        except Exception:
                            pass
                    if rname:
                        try:
                            radios.extend(container.find_elements(By.CSS_SELECTOR, f"input[type='radio'][name='{rname}']"))
                        except Exception:
                            pass
                # if no radios discovered, fallback by group name
                if not radios:
                    name_attr = (item.get("name") or "").strip()
                    if name_attr:
                        try:
                            radios = container.find_elements(By.CSS_SELECTOR, f"input[type='radio'][name='{name_attr}']")
                        except Exception:
                            radios = []
                visible_radios = [r for r in radios if getattr(r, "is_displayed", lambda: False)() and getattr(r, "is_enabled", lambda: False)()]
                if not visible_radios:
                    return True  # no visible radios to answer
                for r in visible_radios:
                    try:
                        if r.is_selected():
                            return True
                    except Exception:
                        continue
                return False
            if field_type == "checkbox":
                el = None
                elem_id = (item.get("id") or "").strip()
                name_attr = (item.get("name") or "").strip()
                try:
                    if elem_id:
                        el = container.find_element(By.ID, elem_id)
                except Exception:
                    el = None
                if el is None and name_attr:
                    try:
                        el = container.find_element(By.CSS_SELECTOR, f"input[type='checkbox'][name='{name_attr}']")
                    except Exception:
                        el = None
                if not el:
                    return True  # no checkbox found -> treat as not actionable
                try:
                    if not el.is_displayed() or not el.is_enabled():
                        return True
                    return el.is_selected()
                except Exception:
                    return False
        except Exception:
            return False

    # Build list of currently-empty keys
    empty_keys = {
        "text_fields": [],
        "textareas": [],
        "select_fields": [],
        "radio_groups": [],
        "checkboxes": [],
    }

    # Map schema items by their keys for easy reference
    schema_index = {
        "text_fields": {it.get("field_key"): it for it in form_schema.get("text_fields", []) if it.get("field_key")},
        "textareas": {it.get("field_key"): it for it in form_schema.get("textareas", []) if it.get("field_key")},
        "select_fields": {it.get("field_key"): it for it in form_schema.get("select_fields", []) if it.get("field_key")},
        "radio_groups": {it.get("group_key"): it for it in form_schema.get("radio_groups", []) if it.get("group_key")},
        "checkboxes": {it.get("box_key"): it for it in form_schema.get("checkboxes", []) if it.get("box_key")},
    }

    # Detect empties
    for fk, item in schema_index["text_fields"].items():
        if not _is_filled("text", item):
            empty_keys["text_fields"].append(fk)
    for fk, item in schema_index["textareas"].items():
        if not _is_filled("textarea", item):
            empty_keys["textareas"].append(fk)
    for fk, item in schema_index["select_fields"].items():
        if not _is_filled("select", item):
            empty_keys["select_fields"].append(fk)
    for gk, item in schema_index["radio_groups"].items():
        if not _is_filled("radio", item):
            empty_keys["radio_groups"].append(gk)
    for bk, item in schema_index["checkboxes"].items():
        if not _is_filled("checkbox", item):
            empty_keys["checkboxes"].append(bk)

    # If no empties at all, nothing to do
    any_empty = any(len(v) for v in empty_keys.values())
    if not any_empty:
        debug(f"All visible fields already filled for {mode} step {step_index+1}; skipping Gemini.")
        return

    # Try to load cached answers (memory)
    cached_answers = load_form_answers_from_file(job_index, step_index, mode, job_title)

    # If cached answers exist, try applying them but only for the currently empty keys
    if cached_answers:
        # Build a minimal apply object with only keys relevant to current empties
        to_apply = {
            "text_fields": {},
            "textareas": {},
            "select_fields": {},
            "radio_groups": {},
            "checkboxes": {},
        }
        for sect in to_apply.keys():
            cached_section = cached_answers.get(sect) or {}
            for k in empty_keys.get(sect, []):
                val = cached_section.get(k)
                if val is None:
                    continue
                # treat empty strings as not useful
                if isinstance(val, str) and not val.strip():
                    continue
                to_apply[sect][k] = val

        # Apply any found cached answers for only the empty fields
        any_cached_applied = any(len(v) for v in to_apply.values())
        if any_cached_applied:
            debug(f"Applying cached answers for some empty fields for {mode} step {step_index+1}.")
            apply_gemini_answers_to_form(driver, container, form_schema, to_apply)

            # Recompute empties after applying cached answers
            # Clear and re-detect empties
            empty_keys = {k: [] for k in empty_keys.keys()}
            for fk, item in schema_index["text_fields"].items():
                if not _is_filled("text", item):
                    empty_keys["text_fields"].append(fk)
            for fk, item in schema_index["textareas"].items():
                if not _is_filled("textarea", item):
                    empty_keys["textareas"].append(fk)
            for fk, item in schema_index["select_fields"].items():
                if not _is_filled("select", item):
                    empty_keys["select_fields"].append(fk)
            for gk, item in schema_index["radio_groups"].items():
                if not _is_filled("radio", item):
                    empty_keys["radio_groups"].append(gk)
            for bk, item in schema_index["checkboxes"].items():
                if not _is_filled("checkbox", item):
                    empty_keys["checkboxes"].append(bk)

            any_empty = any(len(v) for v in empty_keys.values())
            if not any_empty:
                debug(f"Cached answers covered all empty fields for {mode} step {step_index+1}; saved memory reused.")
                return
        else:
            debug(f"Cached memory found but no applicable entries for current empty fields for {mode} step {step_index+1}.")

    # If we still have empty fields after applying cached answers, call Gemini once to fill remaining gaps
    any_empty = any(len(v) for v in empty_keys.values())
    if not any_empty:
        # Nothing left to do
        return

    if not gemini_api_key:
        debug("No Gemini API key available; cannot fill remaining empty fields for this step.")
        return

    debug(f"Requesting Gemini to fill remaining {sum(len(v) for v in empty_keys.values())} fields for {mode} step {step_index+1}.")

    # Call Gemini with full schema and any cached answers as existing_answers, it should fill missing ones
    existing_for_gemini = cached_answers or {
        "text_fields": {},
        "textareas": {},
        "select_fields": {},
        "radio_groups": {},
        "checkboxes": {},
    }

    try:
        gemini_out = call_gemini_for_form_answers(
        resume_text=resume_plain,
        applicant_profile=applicant_profile,
        job_description=job_description,
        form_schema=form_schema,
        api_key=gemini_api_key,
        existing_answers=existing_for_gemini,
        missing_keys=empty_keys,
    )

    except Exception as e:
        debug(f"Gemini call failed for {mode} step {step_index+1}: {e}")
        return

    # Merge cached + gemini outputs (if cached existed) or use gemini_out directly
    merged = merge_gemini_answer_dicts(existing_for_gemini, gemini_out) if cached_answers else gemini_out

    # Save merged answers to disk
    try:
        save_form_answers_to_file(
            job_index=job_index,
            step_index=step_index,
            job_title=job_title,
            mode=mode,
            answers=merged,
        )
    except Exception as e:
        debug(f"Failed to save merged Gemini answers to disk: {e}")

    # Apply only those answers that correspond to fields that are still empty (defensive)
    apply_gemini_answers_to_form(driver, container, form_schema, merged)





# ============================ APPLY FLOWS ============================ #


def easy_apply_for_job(
    driver: webdriver.Chrome,
    base_resume_pdf: Path,
    job_desc_path: Path,
    profile: Dict[str, Any],
    resume_plain: str,
    job_description: str,
    gemini_api_key: str,
    job_index: int,
    job_title: str,
    max_steps: int = 5,
) -> bool:
    """
    Single-pass Gemini-assisted Easy Apply flow (no pass1/pass2).

    For each step:
      - upload resume / cover letter / merged PDF when file inputs exist,
      - fill simple profile fields,
      - let Gemini answer text/dropdowns/yes-no/MCQ questions,
      - click Next / Submit / Apply.

    If no obvious Next/Submit button is found, or if we get stuck with
    red validation errors / missing fields, we call Gemini page recovery.
    """

    # --------- helpers to locate the Easy Apply container ----------

    def find_easy_apply_container() -> Optional[Any]:
        """Locate the Easy Apply modal or full-page apply form."""
        # Modal version
        try:
            modals = driver.find_elements(By.CLASS_NAME, "jobs-easy-apply-modal")
        except Exception:
            modals = []
        for m in modals:
            try:
                if m.is_displayed():
                    return m
            except Exception:
                continue

        # Full-page /jobs/apply
        try:
            body = driver.find_element(By.TAG_NAME, "body")
        except Exception:
            return None

        try:
            url = (driver.current_url or "").lower()
        except Exception:
            url = ""

        if "/jobs/apply" in url or "easyapply" in url:
            try:
                if body.is_displayed():
                    return body
            except Exception:
                pass

        return None

    def get_any_container():
        return find_easy_apply_container()

    def wait_for_easy_apply_container(timeout: int = 12):
        """Wait for Easy Apply UI to appear."""
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: find_easy_apply_container() is not None
            )
        except TimeoutException:
            return None
        return find_easy_apply_container()

    def robust_click_easy_apply(btn) -> bool:
        """Click Easy Apply like a user (scroll, hover, click)."""
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", btn
            )
        except Exception:
            pass

        try:
            WebDriverWait(driver, 10).until(
                lambda d: btn.is_displayed() and btn.is_enabled()
            )
        except TimeoutException:
            debug("Easy Apply button did not become clickable in time.")
            return False

        try:
            ActionChains(driver).move_to_element(btn).click().perform()
            return True
        except Exception:
            debug("ActionChains click on Easy Apply failed; trying JS click.")
            try:
                driver.execute_script("arguments[0].click();", btn)
                return True
            except Exception as e:
                debug(f"JS click on Easy Apply button also failed: {e!r}")
                return False

    # --------- locate and click Easy Apply button ----------

    easy_btn = find_easy_apply_button(driver)
    if not easy_btn:
        debug("No visible 'Easy Apply' button found for this job; skipping Easy Apply flow.")
        return False

    debug("Clicking 'Easy Apply' button...")
    if not robust_click_easy_apply(easy_btn):
        debug("Failed to click Easy Apply button; aborting Easy Apply for this job.")
        return False

    container = wait_for_easy_apply_container(timeout=15)
    if container is None:
        debug("Easy Apply UI never appeared after clicking Easy Apply; aborting Easy Apply.")
        return False

    # --------- generate tailored docs ---------

    try:
        tailored_resume = generate_tailored_docs(base_resume_pdf, job_desc_path)
    except Exception as e:
        debug(f"Failed to generate tailored resume for Easy Apply job #{job_index+1}: {e!r}")
        return False

    cover_letter_pdf = getattr(generate_tailored_docs, "_last_cover_letter_pdf", None)
    merged_resume = _build_merged_resume_pdf(tailored_resume, cover_letter_pdf, job_index)

    submitted = False
    hit_step_cap = False

    # --------- iterate through Easy Apply steps (single pass) ---------

    for step in range(max_steps):
        debug(f"Easy Apply: processing step {step+1}/{max_steps}")

        # Pause for CAPTCHA if present
        if not wait_for_captcha_to_be_solved(driver):
            debug("Stopping Easy Apply flow because CAPTCHA was not solved in time.")
            break

        container = get_any_container()
        if container is None:
            debug(
                "Easy Apply container/page disappeared; "
                "assuming the flow has finished or navigated away."
            )
            submitted = True
            break

        # Upload resume / cover letter / merged PDF if there is a file input
        try:
            upload_resume_in_container(
                container,
                tailored_resume,
                cover_letter_pdf=cover_letter_pdf,
                merged_resume_pdf=merged_resume,
            )
        except Exception as e:
            debug(f"Error uploading resume in Easy Apply: {e!r}")

        # Fill simple fields from stored profile
        try:
            fill_basic_fields_in_container(container, profile)
        except Exception as e:
            debug(f"Error filling basic fields in Easy Apply: {e!r}")

        # Let Gemini + cached answers fill remaining questions (incl. yes/no & MCQ)
        try:
            ensure_form_answers_applied_and_recover(
                driver=driver,
                container=container,
                resume_plain=resume_plain,
                applicant_profile=profile,
                job_description=job_description,
                gemini_api_key=gemini_api_key,
                job_index=job_index,
                step_index=step,
                job_title=job_title,
                mode="easy",
            )
        except Exception as e:
            debug(f"Gemini assistance (with recovery) failed on Easy Apply step {step+1}: {e!r}")

        # Click Next / Submit / Apply in the Easy Apply UI
        clicked = False
        try:
            clicked = click_next_or_submit_in_container(container, mode="easy")
        except Exception as e:
            debug(f"Error clicking Next/Submit in Easy Apply: {e!r}")

        if not clicked:
            debug(
                "No Next/Submit/Apply button found; attempting Gemini-based page "
                "recovery for Easy Apply."
            )
            try:
                try_gemini_page_recovery(
                    driver=driver,
                    container=container,
                    gemini_api_key=gemini_api_key,
                    problem_description="Easy Apply step has no obvious Next/Submit/Apply button.",
                    phase="easy-apply",
                    max_steps=3,
                )
            except Exception as e:
                debug(f"Gemini-based page recovery failed on Easy Apply: {e!r}")

            # After recovery attempt, try once more to click a progress button
            try:
                container = get_any_container() or container
                clicked = click_next_or_submit_in_container(container, mode="easy")
            except Exception as e:
                debug(f"Error re-trying Next/Submit after Gemini recovery: {e!r}")
                clicked = False

            if not clicked:
                debug("Still no Next/Submit/Apply button after Gemini recovery; stopping Easy Apply flow.")
                break

        # Wait a bit for the next step / confirmation to load
        time.sleep(3.0)

        # If the page stayed open and shows validation errors or empty fields, call Gemini backup
        current_container = get_any_container()
        if current_container is not None and gemini_api_key:
            try:
                if container_has_validation_error(current_container) or is_any_field_empty(current_container):
                    debug(
                        "Easy Apply: validation error or empty fields detected after clicking "
                        "Next/Review; invoking Gemini page recovery."
                    )
                    try_gemini_page_recovery(
                        driver=driver,
                        container=current_container,
                        gemini_api_key=gemini_api_key,
                        problem_description=(
                            "Red validation error (e.g. 'Please make a selection') or "
                            "missing required answer detected after clicking Next/Review "
                            "in Easy Apply. Please fix the missing answers and continue."
                        ),
                        phase="easy-validate-error",
                        max_steps=3,
                    )
            except Exception as e:
                debug(f"Error while checking for validation errors in Easy Apply: {e!r}")

        # If no Easy Apply container is left, we assume submission/redirect
        if get_any_container() is None:
            submitted = True
            debug(
                "Easy Apply: UI disappeared (modal closed or page changed); "
                "assuming submission or final redirect."
            )
            break

    else:
        # Loop finished normally (no break) → hit the step cap
        hit_step_cap = True
        debug(
            f"Easy Apply reached max_steps={max_steps} without a clear submission; "
            "leaving the Easy Apply UI open so you can finish manually."
        )

    if not submitted:
        if hit_step_cap:
            # Do NOT close the Easy Apply modal if we only hit the step cap.
            return False

        debug(
            "Easy Apply flow ended without a clear submission; "
            "trying to close any open modal if present."
        )
        close_easy_apply_modal_if_open(driver)

    return submitted






def external_apply_for_job(
    driver: webdriver.Chrome,
    base_resume_pdf: Path,
    job_desc_path: Path,
    profile: Dict[str, Any],
    resume_plain: str,
    job_description: str,
    gemini_api_key: str,
    job_index: int,
    job_title: str,
    max_steps: int = 3,
) -> bool:
    """
    Single-pass external application flow (no pass1/pass2).

    Steps:
      1) Click LinkedIn's external 'Apply' button.
      2) Switch to the new tab/window.
      3) On the external portal, click its own 'Apply / Start application'
         if present (your second apply).
      4) Generate tailored resume + cover letter + merged PDF.
      5) For each step on the external site:
           - upload resume / cover letter / merged PDF when file inputs exist,
           - fill simple profile fields,
           - let Gemini answer text/dropdowns/yes-no/MCQ questions,
           - click Next / Submit / Apply and record the button label.
         If there is no obvious button, fall back to Gemini page recovery once.
      6) Treat as success only if we clearly clicked a final submit/preview/done/
         finish/review button. In that case we close the external tab and
         switch back to LinkedIn.
    """
    # --------- 1) Remember LinkedIn window ----------

    try:
        original_window = driver.current_window_handle
    except Exception:
        debug("Could not read current_window_handle before external apply.")
        return False

    # --------- 2) Find and click the external apply button on LinkedIn ----------

    apply_btn = find_external_apply_button(driver)
    if not apply_btn:
        debug("No external 'Apply' button found on LinkedIn job page.")
        return False

    try:
        prior_handles = set(driver.window_handles)
        debug("Clicking external 'Apply' button...")
        try:
            apply_btn.click()
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", apply_btn)
    except Exception as e:
        debug(f"Failed to click external Apply button: {e!r}")
        return False

    # --------- 3) Wait for the external window/tab ----------

    new_window = None
    try:
        WebDriverWait(driver, 15).until(
            lambda d: len(set(d.window_handles) - prior_handles) >= 1
        )
        new_handles = set(driver.window_handles) - prior_handles
        if new_handles:
            new_window = new_handles.pop()
    except TimeoutException:
        debug("No new window/tab detected for external apply.")
        return False

    if not new_window:
        debug("Could not identify new window for external apply.")
        return False

    try:
        driver.switch_to.window(new_window)
    except Exception as e:
        debug(f"Could not switch to external apply window: {e!r}")
        return False

    time.sleep(5.0)

    # --------- 4) On the external portal, click its own in-page 'Apply' if any ----------

    try:
        try:
            portal_prior_handles = set(driver.window_handles)
        except Exception:
            portal_prior_handles = None

        clicked_portal_apply = click_external_portal_apply_buttons(
            driver,
            max_clicks=2,
        )
        if clicked_portal_apply:
            debug(
                "External apply: clicked 'Apply' on the external portal to open "
                "the application form."
            )
            # If that click opened yet another window/tab (e.g., Workday / Greenhouse),
            # switch into it.
            if portal_prior_handles is not None:
                try:
                    WebDriverWait(driver, 10).until(
                        lambda d: len(set(d.window_handles) - portal_prior_handles) >= 1
                    )
                    extra = set(driver.window_handles) - portal_prior_handles
                    if extra:
                        newest = extra.pop()
                        try:
                            driver.switch_to.window(newest)
                            new_window = newest  # track the active external window
                            debug(
                                "External apply: external portal opened a secondary "
                                "application window/tab; switched to it."
                            )
                        except Exception as e:
                            debug(
                                f"External apply: failed to switch to secondary "
                                f"application window: {e!r}"
                            )
                except TimeoutException:
                    # No additional window; form likely opened in-place
                    pass

            # Give the real application form a moment to render
            time.sleep(4.0)
        else:
            debug(
                "External apply: no in-page 'Apply' button found on the external "
                "portal; continuing with existing field-check logic."
            )
    except Exception as e:
        debug(f"External apply: error while clicking in-page external 'Apply' button: {e!r}")

    # --------- 5) Generate tailored docs for this job ----------

    try:
        tailored_resume = generate_tailored_docs(base_resume_pdf, job_desc_path)
    except Exception as e:
        debug(f"Failed to generate tailored resume for external job #{job_index+1}: {e!r}")
        # Best-effort cleanup: close external window and return
        try:
            if new_window in driver.window_handles:
                driver.close()
        except Exception:
            pass
        try:
            if original_window in driver.window_handles:
                driver.switch_to.window(original_window)
        except Exception:
            pass
        return False

    cover_letter_pdf = getattr(generate_tailored_docs, "_last_cover_letter_pdf", None)
    merged_resume = _build_merged_resume_pdf(
        tailored_resume, cover_letter_pdf, job_index
    )

    final_clicked = False
    clicked_any_submit = False
    hit_step_cap = False

    # --------- 6) Iterate external steps (single pass) ----------

    for step in range(max_steps):
        debug(f"External apply: processing step {step+1}/{max_steps}")

        if not detect_and_wait_for_captcha(driver):
            debug("External apply: CAPTCHA not solved in time; stopping for this job.")
            break

        try:
            container = driver.find_element(By.TAG_NAME, "body")
        except Exception as e:
            debug(
                f"External apply: could not locate <body>; assuming redirect/finish: {e!r}"
            )
            break

        # Upload resume / cover letter / merged PDF
        try:
            upload_resume_in_container(
                container,
                tailored_resume,
                cover_letter_pdf=cover_letter_pdf,
                merged_resume_pdf=merged_resume,
            )
        except Exception as e:
            debug(f"External apply: error uploading resume: {e!r}")

        # Fill simple profile fields
        try:
            fill_basic_fields_in_container(container, profile)
        except Exception as e:
            debug(
                f"External apply: error filling basic fields on external site: {e!r}"
            )

        # Gemini: answer remaining questions (text, dropdowns, yes/no, MCQs, etc.)
        try:
            ensure_form_answers_applied_and_recover(
                driver=driver,
                container=container,
                resume_plain=resume_plain,
                applicant_profile=profile,
                job_description=job_description,
                gemini_api_key=gemini_api_key,
                job_index=job_index,
                step_index=step,
                job_title=job_title,
                mode="external",
            )
        except Exception as e:
            debug(
                f"External apply: ensure_form_answers_applied_and_recover failed: {e!r}"
            )

        # Click Next/Submit/Apply and capture the label
        clicked_label: Optional[str] = None
        try:
            clicked_label = click_next_or_submit_in_container(
                container,
                mode="external",
                return_label=True,
            )
        except Exception as e:
            debug(f"External apply: error clicking Next/Submit: {e!r}")
            clicked_label = None

        if clicked_label:
            clicked_any_submit = True
            label_lower = clicked_label.lower()
            debug(f"External apply: clicked button '{clicked_label}'")
            time.sleep(5.0)

            # After clicking, check if we are stuck with empty fields
            try:
                ext_container = driver.find_element(By.TAG_NAME, "body")
            except Exception:
                ext_container = None

            if ext_container is not None:
                try:
                    if is_any_field_empty(ext_container):
                        debug(
                            "External apply: after Next/Submit, some fields/MCQs/"
                            "dropdowns still appear empty; invoking Gemini recovery "
                            "for stuck external step."
                        )
                        try_gemini_page_recovery(
                            driver=driver,
                            container=ext_container,
                            gemini_api_key=gemini_api_key,
                            problem_description=(
                                "Clicked Next/Submit on an external apply step but the "
                                "page did not seem to advance and some fields/MCQs/"
                                "dropdowns are still empty."
                            ),
                            phase="external-apply-stuck",
                            max_steps=3,
                        )
                except Exception as e:
                    debug(f"Error while checking for remaining empty fields on external site: {e!r}")

            # Decide if this looked like a final submit/preview/done/finish/review button
            final_tokens = ("submit", "preview", "done", "finish", "review")
            if any(tok in label_lower for tok in final_tokens):
                final_clicked = True
                debug(
                    "External apply: clicked a final 'submit/preview/done/finish/review' "
                    "button; treating this as end of the application."
                )
                break
        else:
            debug(
                "External apply: no obvious submit/next/apply button clicked on this step; "
                "attempting Gemini-based page recovery."
            )
            try:
                try_gemini_page_recovery(
                    driver=driver,
                    container=container,
                    gemini_api_key=gemini_api_key,
                    problem_description="External apply step has no obvious Next/Submit/Apply button.",
                    phase="external-apply",
                    max_steps=3,
                )
            except Exception as e:
                debug(f"External apply: Gemini-based page recovery failed on external apply: {e!r}")

            # Try once more to click a submit/next/apply button after recovery
            try:
                container = driver.find_element(By.TAG_NAME, "body")
                clicked_label = click_next_or_submit_in_container(
                    container,
                    mode="external",
                    return_label=True,
                )
            except Exception as e:
                debug(
                    f"External apply: error re-trying Next/Submit after Gemini "
                    f"recovery on external site: {e!r}"
                )
                clicked_label = None

            if clicked_label:
                clicked_any_submit = True
                label_lower = clicked_label.lower()
                debug(f"External apply (after recovery): clicked button '{clicked_label}'")
                time.sleep(5.0)

                final_tokens = ("submit", "preview", "done", "finish", "review")
                if any(tok in label_lower for tok in final_tokens):
                    final_clicked = True
                    debug(
                        "External apply: clicked a final 'submit/preview/done/finish/review' "
                        "button after recovery; treating this as end of the application."
                    )
                    break
            else:
                debug("External apply: still no submit/next/apply button after Gemini recovery; stopping external apply flow.")
                break

    else:
        # Loop finished normally (no break) → hit the step cap
        hit_step_cap = True
        debug(
            f"External apply reached max_steps={max_steps} without clicking a final "
            "'submit/preview/done/finish/review' button; leaving external tab open."
        )

    # --------- 7) Clean up windows/tabs ----------

    try:
        handles = set(driver.window_handles)

        if final_clicked:
            # Only auto-close the external portal when we clearly hit a final button
            if new_window in handles:
                try:
                    driver.switch_to.window(new_window)
                    driver.close()
                    debug(
                        "External apply: closed external portal after final submit/preview/done/finish/review."
                    )
                except Exception as e:
                    debug(
                        f"External apply: failed to close external portal cleanly: {e!r}"
                    )
            if original_window in driver.window_handles:
                try:
                    driver.switch_to.window(original_window)
                except Exception as e:
                    debug(
                        f"External apply: failed to switch back to LinkedIn window: {e!r}"
                    )
        else:
            # Never auto-close unless we are sure; just go back to LinkedIn.
            if original_window in driver.window_handles:
                try:
                    driver.switch_to.window(original_window)
                except Exception as e:
                    debug(
                        f"External apply: failed to switch back to LinkedIn window: {e!r}"
                    )

            if hit_step_cap:
                debug(
                    "External apply did not reach a clear final submit/preview/done/finish/review "
                    f"button within {max_steps} steps; leaving external tab open for manual review."
                )
            elif clicked_any_submit:
                debug(
                    "External apply clicked some Apply/Next/Submit buttons but no clear final "
                    "submit/preview/done/finish/review action. Leaving the external portal "
                    "open so you can review or finish manually."
                )
            else:
                debug(
                    "External apply flow did not manage to click any Apply/Submit buttons."
                )

    except Exception:
        # best-effort cleanup only
        pass

    # We treat external auto-apply as successful only if we clearly clicked a final button.
    return final_clicked
# Where we persist the Gemini key between runs (alongside this script)
GEMINI_KEY_FILE = BASE_DIR / "gemini_api_key.txt"

# In‑process cache so we don't keep re‑reading the file
_CACHED_GEMINI_KEY: Optional[str] = None




def card_looks_already_applied(card) -> bool:
    """
    Best-effort check on the LinkedIn job card itself for an 'Applied' badge
    in the left-hand list.

    Returns True if we find a small 'Applied' / 'You applied' label
    inside the card, False otherwise.
    """
    try:
        spans = card.find_elements(By.TAG_NAME, "span")
    except Exception:
        spans = []

    for sp in spans:
        try:
            t = (sp.text or "").strip().lower()
        except Exception:
            continue
        if not t:
            continue

        # Use fairly specific patterns so we don't match 'Applied Materials'
        if t == "applied":
            return True
        if t.startswith("applied on "):
            return True
        if t.startswith("you applied"):
            return True
        if "already applied" in t:
            return True

    # Fallback: scan full card text for clear phrases
    try:
        whole = (card.text or "").lower()
    except Exception:
        whole = ""

    for phrase in ("you applied", "applied on", "already applied"):
        if phrase in whole:
            return True

    return False


def is_job_already_applied_on_linkedin(driver: webdriver.Chrome) -> bool:
    """
    Check the currently open LinkedIn job details view to see if it indicates
    that you already applied for this job.

    We look for phrases like 'You applied', 'Applied on', 'Already applied',
    'Application submitted', etc. in the visible page text.

    Returns True if it looks already applied, False otherwise.
    """
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        text = (body.text or "").lower()
    except Exception:
        return False

    phrases = [
        "you applied",
        "applied on",
        "already applied",
        "you have applied",
        "you have already applied",
        "you previously applied",
        "application submitted",
        "your application was sent",
        "your application has been submitted",
        "thanks for applying",
        "Applied"
    ]

    for p in phrases:
        if p in text:
            debug(
                f"is_job_already_applied_on_linkedin: found phrase '{p}' – "
                "treating this job as already applied."
            )
            return True

    return False




def process_jobs(
    driver: webdriver.Chrome,
    base_resume_pdf: Path,
    profile: Dict[str, Any],
    resume_plain: str,
    gemini_api_key: str,
    max_jobs: int,
) -> None:
    """
    Iterate over job cards and try to auto-apply to each job.

    Behaviour:

    - Only counts a job as 'attempted' once we have its description.
    - Uses the updated open_job_and_get_description that verifies the
      detail pane actually matches the selected card.
    - Skips jobs that already show an 'Applied' badge on the LEFT job card.
    - Skips jobs where the RIGHT job details header clearly says you already
      applied (e.g. 'Applied 2 days ago · See application').
    - Only creates a job description file for jobs that are NOT already applied.
    - Slightly shorter delays between jobs.
    - ✅ Counts a job as successfully auto‑applied only if at least one of:
        * Easy Apply flow returned success,
        * External apply flow returned success,
        * LinkedIn shows an 'Application submitted' style confirmation,
        * External portal shows a generic 'application received' message.
    """

    # First try to find job cards once, to see if there are any results at all
    initial_cards = find_job_cards(driver, log=True)
    if not initial_cards:
        return

    attempted_count = 0
    applied_count = 0

    for idx in range(len(initial_cards)):
        if attempted_count >= max_jobs:
            debug(f"Max jobs attempted: {attempted_count}")
            break

        # Re-fetch cards each time so we don't use stale elements
        cards = find_job_cards(driver, log=False)
        if idx >= len(cards):
            debug("Job cards list shrank after refresh; stopping.")
            break

        card = cards[idx]

        # ---- NEW: skip jobs that already show 'Applied' on the LEFT card ----
        try:
            if card_looks_already_applied(card):
                debug(
                    f"Job #{idx+1} already shows 'Applied' on the left card; "
                    "skipping this job before opening details."
                )
                continue
        except Exception:
            # If detection fails for any reason, just carry on normally
            pass

        job_title = extract_job_title(card)
        company = extract_company_name(card)
        debug(
            f"Processing job #{idx+1}: '{job_title}' at '{company}'"
        )

        # Open job, get description from the RIGHT job pane
        description = open_job_and_get_description(driver, card, idx)
        if not description:
            debug(f"No description found for job #{idx+1}, skipping.")
            continue

        # ---- NEW: check the RIGHT job details header for 'Applied' ----
        try:
            if job_detail_looks_already_applied(driver):
                debug(
                    f"Job #{idx+1} ('{job_title}' at '{company}') already looks "
                    "applied in the job details header; skipping this job."
                )
                continue
        except Exception:
            # If detection explodes, just continue as normal
            pass

        # Only now count this as an "attempted" job
        attempted_count += 1
        debug(f"Attempting auto-apply for job #{idx+1} (attempt #{attempted_count}).")

        # Only create job description files for NOT-applied jobs
        job_desc_path = write_job_description_file(job_title, company, description, idx)

        easy_success = False
        external_success = False

        # ---- Try Easy Apply first ----
        try:
            easy_success = easy_apply_for_job(
                driver=driver,
                base_resume_pdf=base_resume_pdf,
                job_desc_path=job_desc_path,
                profile=profile,
                resume_plain=resume_plain,
                job_description=description,
                gemini_api_key=gemini_api_key,
                job_index=idx,
                job_title=job_title,
            )
        except Exception as e:
            debug(f"Easy Apply flow failed for job #{idx+1}: {e!r}")
            easy_success = False

        # ---- If Easy Apply not available / fails, try external apply next ----
        if not easy_success:
            try:
                external_success = external_apply_for_job(
                    driver=driver,
                    base_resume_pdf=base_resume_pdf,
                    job_desc_path=job_desc_path,
                    profile=profile,
                    resume_plain=resume_plain,
                    job_description=description,
                    gemini_api_key=gemini_api_key,
                    job_index=idx,
                    job_title=job_title,
                )
            except Exception as e:
                debug(f"External apply flow failed for job #{idx+1}: {e!r}")
                external_success = False

        # ---- Extra check 1: does LinkedIn show "Application submitted"? ----
        linkedin_confirmed = False
        try:
            linkedin_confirmed = detect_linkedin_application_confirmation(
                driver, timeout=3.0
            )
        except Exception as e:
            debug(
                f"Error while checking LinkedIn submission confirmation for job "
                f"#{idx+1}: {e!r}"
            )

        # ---- Extra check 2: generic external portal confirmation (non‑LinkedIn) ----
        portal_confirmed = False
        try:
            portal_confirmed = detect_generic_application_confirmation(
                driver, timeout=3.0
            )
        except Exception as e:
            debug(
                f"Error while checking generic submission confirmation for job "
                f"#{idx+1}: {e!r}"
            )

        # Final decision: treat as success ONLY if at least one of these is true
        success = bool(
            easy_success
            or external_success
            or linkedin_confirmed
            or portal_confirmed
        )

        if success:
            applied_count += 1
            # Minimal logging to avoid spam
            if linkedin_confirmed and not (easy_success or external_success):
                debug(
                    f"✅ Detected LinkedIn 'Application submitted' confirmation for job "
                    f"#{idx+1}; marking as successfully auto-applied even though the "
                    f"internal flow reported failure."
                )
            elif portal_confirmed and not (easy_success or external_success):
                debug(
                    f"✅ Detected generic 'application submitted' text on an external "
                    f"portal for job #{idx+1}; marking as successfully auto-applied."
                )
            else:
                # You can leave this as pass if you don't want per-job logs
                pass
        else:
            # You asked earlier not to print the big warning; keep it silent.
            pass

        # Shorter delay between jobs to speed things up
        time.sleep(2.5)

    # Clean final summary can be printed here if you want
    # (you previously chose not to show a big summary)


    debug(
        f"Finished job loop. New jobs attempted (not already applied): {attempted_count}, "
        f"successfully applied: {applied_count}."
    )








def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gemini-assisted LinkedIn AI/Robotics job auto-applier."
    )
    parser.add_argument(
        "--resume-pdf",
        required=True,
        type=Path,
        help="Path to your base resume PDF (used by resume_and_cover_maker.py).",
    )
    parser.add_argument(
        "--applicant-json",
        type=Path,
        default=BASE_DIR / "applicant_info.json",
        help="Path to JSON file with applicant info (default: applicant_info.json in this folder).",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=3,
        help="Maximum number of jobs to try auto-applying for.",
    )
    parser.add_argument(
        "--keywords",
        type=str,
        default="artificial intelligence,robotics,machine learning",
        help="Comma-separated job search keywords.",
    )
    parser.add_argument(
        "--location",
        type=str,
        default="Worldwide",
        help="Location for job search (e.g., 'United States', 'Germany', 'Remote').",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser in headless mode.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    # -------- parse CLI arguments --------
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    # -------- LinkedIn credentials from env --------
    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")

    if not email or not password:
        raise RuntimeError(
            "Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD environment variables before running."
        )

     # -------- Gemini API key: env + disk + interactive prompt --------
    gemini_api_key = resolve_gemini_api_key_from_env_or_disk(interactive=True)
    if not gemini_api_key:
        debug(
            "Gemini API key is not available. Gemini-assisted features will be disabled "
            "until a key is provided."
        )
    else:
        debug("Gemini API key resolved successfully (env or gemini_api_key.txt).")


    # -------- CLI args: resume, profile, search settings --------
    base_resume_pdf = args.resume_pdf.resolve()
    applicant_profile = load_applicant_profile(args.applicant_json)
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    debug(f"Using base resume: {base_resume_pdf}")
    debug(f"Applicant profile file: {args.applicant_json}")
    debug(f"Job keywords: {keywords}")
    debug(f"Job location: {args.location}")
    debug(f"Max jobs to auto-apply: {args.max_jobs}")

    if not base_resume_pdf.exists():
        raise FileNotFoundError(f"Base resume PDF not found at {base_resume_pdf}")

    # -------- Extract resume text for Gemini --------
    debug("Extracting plain text from base resume for Gemini...")
    resume_plain = rcm.extract_text_from_pdf(base_resume_pdf).strip()

    # -------- Start browser and run the job loop --------
    debug("Starting browser...")
    driver = create_driver(headless=args.headless)

    try:
        # Log into LinkedIn
        login_to_linkedin(driver, email, password, gemini_api_key=gemini_api_key)

        # Open job search and process jobs
        open_jobs_search(driver, keywords, args.location)
        process_jobs(
            driver=driver,
            base_resume_pdf=base_resume_pdf,
            profile=applicant_profile,
            resume_plain=resume_plain,
            gemini_api_key=gemini_api_key,
            max_jobs=args.max_jobs,
        )
    finally:
        debug("Closing browser...")
        driver.quit()


if __name__ == "__main__":
    main()
