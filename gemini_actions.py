# gemini_actions.py
"""
Gemini-based action helper for browser automation.

Provides:
 - resolve_gemini_api_key_from_env_or_disk()
 - save_gemini_api_key_to_disk()
 - call_gemini_for_actions(...)

Usage:
  from gemini_actions import call_gemini_for_actions, resolve_gemini_api_key_from_env_or_disk

Then:
  response = call_gemini_for_actions(screenshot_bytes, page_html, model="gemini-2.5-flash")
  actions = response.get("actions", [])
"""

import os
import json
import base64
import time
from typing import Dict, Any, List, Optional

# ✅ use google-genai, same as other files
from google import genai
from google.genai import types

GEMINI_KEY_FILE = "gemini_api_key.txt"


def resolve_gemini_api_key_from_env_or_disk() -> Optional[str]:
    """Try env var, then disk file. Return None if not found."""
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return key
    try:
        if os.path.exists(GEMINI_KEY_FILE):
            with open(GEMINI_KEY_FILE, "r", encoding="utf-8") as fh:
                k = fh.read().strip()
            if k:
                return k
    except Exception:
        pass
    return None


def save_gemini_api_key_to_disk(key: str) -> None:
    try:
        with open(GEMINI_KEY_FILE, "w", encoding="utf-8") as fh:
            fh.write(key.strip())
    except Exception:
        pass


def _strip_code_fences(text: str) -> str:
    """Remove ``` fences if present and return inner content."""
    if not text:
        return text
    s = text.strip()
    if s.startswith("```"):
        parts = s.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.lower().startswith("json"):
                inner = inner[len("json"):].lstrip()
            return inner.strip()
    return s


def _extract_first_json(text: str) -> Optional[Dict[str, Any]]:
    """Find first {...} block and parse JSON. Return dict or None."""
    if not text:
        return None
    import re

    stripped = _strip_code_fences(text)
    m = re.search(r"(\{[\s\S]*\})", stripped)
    candidate = m.group(1) if m else stripped
    try:
        return json.loads(candidate)
    except Exception:
        try:
            cand2 = (
                candidate.replace("\u201c", '"')
                .replace("\u201d", '"')
                .replace("\u2018", "'")
                .replace("\u2019", "'")
            )
            return json.loads(cand2)
        except Exception:
            return None


def _validate_actions_shape(obj: Dict[str, Any]) -> bool:
    """
    Expected JSON shape:

    {
      "actions": [
        { "type": "mouse", "params": { "x": <int>, "y": <int>, "button": "left"|"right"|"middle" } },
        { "type": "keyboard", "params": { "text": "..." } },
        ...
      ],
      "explain": "short text"
    }
    """
    if not isinstance(obj, dict):
        return False
    actions = obj.get("actions")
    if not isinstance(actions, list):
        return False
    for a in actions:
        if not isinstance(a, dict):
            return False
        t = a.get("type")
        if t not in ("mouse", "keyboard"):
            return False
        params = a.get("params", {})
        if not isinstance(params, dict):
            return False
        if t == "mouse":
            if "x" not in params or "y" not in params:
                return False
            try:
                int(params["x"])
                int(params["y"])
            except Exception:
                return False
            btn = params.get("button", "left")
            if btn not in ("left", "right", "middle"):
                return False
        elif t == "keyboard":
            if "text" not in params:
                return False
            if not isinstance(params["text"], (str, int, float)):
                return False
    return True


def call_gemini_for_actions(
    screenshot_bytes: bytes,
    page_html: str,
    model: str = "gemini-2.5-flash",
    max_retries: int = 2,
) -> Dict[str, Any]:
    """
    Ask Gemini to suggest structured mouse/keyboard actions.

    Returns dict:
      { "actions": [...], "explain": "..." }

    If Gemini key appears exhausted/invalid, prompts once for a replacement key,
    saves it to gemini_api_key.txt, and retries.
    """
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

    prompt = f"""
You are an assistant that must RETURN EXACTLY one JSON object and NOTHING ELSE.
The JSON MUST have keys:
  - "actions": a list of action objects in sequence
  - "explain": a short text explanation (1-2 sentences)

Action object shapes:
  - Mouse click: {{ "type":"mouse", "params":{{ "x": <int>, "y": <int>, "button": "left"|"right"|"middle" }} }}
  - Keyboard type: {{ "type":"keyboard", "params":{{ "text":"..." }} }}

Rules:
- Coordinates must be viewport integers.
- Do NOT include any extra keys.
- If typing secrets (passwords), use text="<TYPE_SECRET_HERE>" as a placeholder.
- If unsure, return {{"actions": [], "explain": "I am unsure; manual intervention needed."}}

Inputs:
SCREENSHOT (base64, truncated): "{screenshot_b64[:400]}... (truncated)"
PAGE_HTML (DOM snapshot):
<<<HTML>>>
{page_html}
<<<END>>>
"""

    key = resolve_gemini_api_key_from_env_or_disk()
    if not key:
        print("Gemini API key not found. Paste a valid GEMINI_API_KEY (saved to gemini_api_key.txt):")
        key = input("GEMINI_API_KEY: ").strip()
        if not key:
            return {"actions": [], "explain": "No Gemini API key provided."}
        save_gemini_api_key_to_disk(key)
        os.environ["GEMINI_API_KEY"] = key

    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            client = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(api_version="v1alpha"),
            )
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=512,
                    response_mime_type="text/plain",
                ),
            )
            raw = (resp.text or "").strip()
            stripped = _strip_code_fences(raw)
            parsed = _extract_first_json(stripped)
            if parsed is None or not _validate_actions_shape(parsed):
                raise ValueError("Gemini response did not include valid actions JSON.")
            return parsed
        except Exception as e:
            last_exc = e
            msg = (str(e) or "").lower()
            print(f"[call_gemini_for_actions] attempt {attempt} failed: {e}")

            key_issue_tokens = [
                "api key",
                "apikey",
                "invalid api key",
                "unauthorized",
                "permission_denied",
                "quota",
                "exhausted",
                "billing",
                "403",
                "401",
            ]
            if any(tok in msg for tok in key_issue_tokens) and attempt == 1:
                print("Gemini key looks invalid/exhausted. Paste NEW GEMINI_API_KEY (or press Enter to abort):")
                new_key = input("NEW GEMINI_API_KEY: ").strip()
                if new_key:
                    save_gemini_api_key_to_disk(new_key)
                    os.environ["GEMINI_API_KEY"] = new_key
                    key = new_key
                    print("Saved new Gemini key; retrying.")
                    continue
                else:
                    return {"actions": [], "explain": "Gemini key invalid and no replacement provided."}
            time.sleep(1.0 * attempt)

    return {"actions": [], "explain": f"Gemini action request failed: {last_exc}"}
