#!/usr/bin/env python3
"""
record_web_actions_firefox.py

Simple macro recorder for ONE purpose:
- Open a web page in Firefox
- Record mouse clicks, text input changes, and keydown events
- Save them to a JSON file

Usage example:

  (dobot_env) python record_web_actions_firefox.py --url "https://www.google.com" --out "macro.json"

Requirements:
  - Python 3
  - selenium:  pip install selenium
  - Firefox installed (Ubuntu Snap is OK)
  - geckodriver installed, e.g. /snap/bin/geckodriver  (you have this)
"""

import argparse
import json
import time
from typing import Any, Dict, List

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService


# ---------- JavaScript injected into the page to record events ----------

RECORD_JS = r"""
(function () {
  if (window._macroRecorderInstalled) {
    return;
  }
  window._macroRecorderInstalled = true;
  window._macroEvents = [];

  function cssPath(el) {
    if (!(el instanceof Element)) return null;
    var path = [];
    while (el && el.nodeType === Node.ELEMENT_NODE) {
      var selector = el.nodeName.toLowerCase();

      // If element has an ID, use that as a shortcut and stop.
      if (el.id) {
        selector += "#" + el.id;
        path.unshift(selector);
        break;
      } else {
        // nth-of-type for siblings of the same tag
        var sib = el, nth = 1;
        while (sib = sib.previousElementSibling) {
          if (sib.nodeName.toLowerCase() === selector) nth++;
        }
        if (nth !== 1) {
          selector += ":nth-of-type(" + nth + ")";
        }
      }
      path.unshift(selector);
      el = el.parentNode;
    }
    return path.join(" > ");
  }

  // Record clicks anywhere on the page
  document.addEventListener(
    "click",
    function (e) {
      try {
        var el = e.target;
        var selector = cssPath(el);
        window._macroEvents.push({
          type: "click",
          selector: selector,
          timestamp: Date.now()
        });
      } catch (err) {
        console.error("Macro recorder click error:", err);
      }
    },
    true
  );

  // Record text/selection for inputs, textareas, selects
  function recordInputEvent(e) {
    try {
      var el = e.target;
      if (!el || !el.tagName) return;
      var tag = el.tagName.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") {
        var selector = cssPath(el);
        window._macroEvents.push({
          type: "input",
          selector: selector,
          value: el.value,
          timestamp: Date.now()
        });
      }
    } catch (err) {
      console.error("Macro recorder input error:", err);
    }
  }

  document.addEventListener("change", recordInputEvent, true);
  document.addEventListener("input", recordInputEvent, true);

  // Record keydown events (useful for Enter/Tab/etc.)
  document.addEventListener(
    "keydown",
    function (e) {
      try {
        var el = e.target;
        var selector = cssPath(el);
        window._macroEvents.push({
          type: "keydown",
          selector: selector,
          key: e.key,
          code: e.code,
          timestamp: Date.now()
        });
      } catch (err) {
        console.error("Macro recorder keydown error:", err);
      }
    },
    true
  );
})();
"""


# ---------- Helpers ----------

def inject_recorder(driver: webdriver.Firefox) -> None:
    """Inject the JS recorder into the current page."""
    driver.execute_script(RECORD_JS)


def fetch_events(driver: webdriver.Firefox) -> List[Dict[str, Any]]:
    """Fetch recorded events from the page."""
    try:
        events = driver.execute_script("return (window._macroEvents || []);")
        if not isinstance(events, list):
            return []
        return events
    except Exception as e:
        print(f"[macro] Could not fetch events from page: {e!r}")
        return []


def save_events_to_json(events: List[Dict[str, Any]], out_path: str) -> None:
    """Save events list to a JSON file."""
    try:
        events.sort(key=lambda ev: ev.get("timestamp", 0))
    except Exception:
        pass

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, ensure_ascii=False)

    print(f"[macro] Saved {len(events)} events to {out_path}")


# ---------- Main CLI ----------

def main():
    parser = argparse.ArgumentParser(
        description="Record mouse/keyboard actions on a web page in Firefox and save to JSON."
    )
    parser.add_argument("--url", required=True, help="URL to open for recording.")
    parser.add_argument(
        "--out",
        default="macro.json",
        help="Path to output JSON file (default: macro.json)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Firefox headless (not recommended for manual interaction).",
    )
    parser.add_argument(
        "--geckodriver-path",
        default="/snap/bin/geckodriver",
        help="Path to geckodriver (default: /snap/bin/geckodriver). "
             "Run `which geckodriver` to confirm.",
    )

    args = parser.parse_args()

    # Set up Firefox driver
    options = FirefoxOptions()
    if args.headless:
        options.add_argument("-headless")

    # Use the geckodriver that matches your Firefox
    service = FirefoxService(executable_path=args.geckodriver_path)
    print(f"[macro] Using geckodriver: {args.geckodriver_path}")

    driver = webdriver.Firefox(service=service, options=options)

    try:
        print(f"[macro] Opening {args.url}")
        driver.get(args.url)

        # Give page a moment to load
        time.sleep(3)

        print("[macro] Injecting recorder JS...")
        inject_recorder(driver)
        print(
            "\n[m a c r o   r e c o r d e r   (Firefox)]\n"
            "  - A Firefox window should be open on your URL.\n"
            "  - Perform the actions you want to record:\n"
            "      * Mouse clicks\n"
            "      * Typing into inputs/textareas\n"
            "      * Pressing keys (Enter/Tab/etc.)\n"
            "  - When you are finished, come back to this terminal and press ENTER.\n"
        )
        input(">>> Press ENTER here to stop recording and save JSON... ")

        events = fetch_events(driver)
        print(f"[macro] Retrieved {len(events)} recorded events from page.")
        save_events_to_json(events, args.out)

    finally:
        print("[macro] Closing browser.")
        driver.quit()


if __name__ == "__main__":
    main()
