#!/usr/bin/env python3
"""Render a safe, label-driven Suno Create console script from JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


JS_TEMPLATE = r'''(() => {
  const song = __SONG_PAYLOAD__;
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const norm = (value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
  const visible = (element) =>
    Boolean(element && element.getClientRects().length &&
      getComputedStyle(element).visibility !== "hidden");

  function clickExact(text) {
    if (!text) return null;
    const wanted = norm(text);
    const selectors = 'button,[role="button"],[role="radio"],[role="tab"],label';
    const element = [...document.querySelectorAll(selectors)]
      .filter(visible)
      .find((candidate) => norm(candidate.textContent) === wanted);
    if (element) element.click();
    return element;
  }

  function describe(element) {
    const parts = [
      element.getAttribute("aria-label"),
      element.getAttribute("placeholder"),
      element.getAttribute("name"),
      element.id,
    ];

    if (element.id) {
      parts.push(document.querySelector(
        `label[for="${CSS.escape(element.id)}"]`
      )?.textContent);
    }

    let parent = element.parentElement;
    for (let depth = 0; parent && depth < 4; depth += 1, parent = parent.parentElement) {
      const text = norm(parent.innerText);
      if (text.length < 800) parts.push(text);
    }
    return norm(parts.filter(Boolean).join(" "));
  }

  function controls() {
    const selector = [
      "textarea",
      'input:not([type="hidden"]):not([type="range"])',
      '[contenteditable="true"]',
      '[role="textbox"]',
    ].join(",");
    return [...document.querySelectorAll(selector)].filter(visible);
  }

  function controlNearLabel(required, rejected = []) {
    const wanted = required.map(norm);
    const unwanted = rejected.map(norm);
    const direct = controls().find((element) => {
      const info = describe(element);
      return wanted.some((term) => info.includes(term)) &&
        !unwanted.some((term) => info.includes(term));
    });
    if (direct) return direct;

    const label = [...document.querySelectorAll("label,span,p,div")]
      .filter(visible)
      .find((element) => {
        const text = norm(element.textContent);
        return wanted.some((term) => text === term || text.startsWith(`${term} `));
      });

    let parent = label;
    for (let depth = 0; parent && depth < 6; depth += 1, parent = parent.parentElement) {
      const candidate = [...parent.querySelectorAll(
        'textarea,input:not([type="hidden"]):not([type="range"]),[contenteditable="true"],[role="textbox"]'
      )].find(visible);
      if (candidate && !unwanted.some((term) => describe(candidate).includes(term))) {
        return candidate;
      }
    }
    return null;
  }

  function setControl(element, value) {
    if (!element || value == null) return false;
    element.focus();

    if (element.isContentEditable ||
        (!("value" in element) && element.getAttribute("role") === "textbox")) {
      element.textContent = String(value);
      element.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: String(value),
      }));
    } else {
      let prototype = Object.getPrototypeOf(element);
      let setter;
      while (prototype && !setter) {
        setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
        prototype = Object.getPrototypeOf(prototype);
      }
      if (setter) setter.call(element, String(value));
      else element.value = String(value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }

    element.blur();
    return true;
  }

  function findSlider(label) {
    const wanted = norm(label);
    const sliders = [...document.querySelectorAll('input[type="range"],[role="slider"]')]
      .filter(visible);
    const direct = sliders.find((slider) => describe(slider).includes(wanted));
    if (direct) return direct;

    const textElement = [...document.querySelectorAll("label,span,p,div")]
      .filter(visible)
      .find((element) => norm(element.textContent) === wanted);
    let parent = textElement;
    for (let depth = 0; parent && depth < 6; depth += 1, parent = parent.parentElement) {
      const slider = parent.querySelector?.('input[type="range"],[role="slider"]');
      if (visible(slider)) return slider;
    }
    return null;
  }

  function setSlider(label, value) {
    const slider = findSlider(label);
    if (!slider) return false;
    slider.focus();

    if (slider.matches('input[type="range"]')) {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(slider, String(value));
      slider.dispatchEvent(new Event("input", { bubbles: true }));
      slider.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      slider.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
      for (let step = 0; step < value; step += 1) {
        slider.dispatchEvent(new KeyboardEvent("keydown", {
          key: "ArrowRight",
          bubbles: true,
        }));
      }
    }

    slider.blur();
    return true;
  }

  return (async () => {
    clickExact("Advanced");
    await wait(400);

    if (song.model) clickExact(song.model);
    if (song.instrumental) clickExact("Instrumental");
    else clickExact("Write");
    await wait(250);

    const results = {
      title: setControl(controlNearLabel(["title", "song name"]), song.title),
      styles: setControl(
        controlNearLabel(["style of music", "styles"], ["exclude"]),
        song.styles
      ),
      exclude: setControl(
        controlNearLabel(["exclude", "styles to exclude"]),
        song.exclude
      ),
      lyrics: song.instrumental ? "instrumental" : setControl(
        controlNearLabel(["lyrics", "enter your own lyrics"]),
        song.lyrics
      ),
      vocalGender: song.vocal_gender ? Boolean(clickExact(song.vocal_gender)) : "unchanged",
      duration: Boolean(clickExact(song.duration)),
      weirdness: setSlider("Weirdness", song.weirdness),
      styleInfluence: setSlider("Style Influence", song.style_influence),
    };

    console.table(results);
    console.info("Suno fields filled. Review them, then click Create manually.");
    return results;
  })();
})();
'''


def integer_percent(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number from 0 through 100")
    result = round(value)
    if not 0 <= result <= 100:
        raise ValueError(f"{field} must be a number from 0 through 100")
    return result


def nonempty_text(spec: dict[str, Any], field: str) -> str:
    value = spec.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def normalize_spec(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("song specification must be a JSON object")

    instrumental = raw.get("instrumental", False)
    if not isinstance(instrumental, bool):
        raise ValueError("instrumental must be true or false")

    gender = raw.get("vocal_gender")
    if gender not in (None, "Male", "Female"):
        raise ValueError('vocal_gender must be "Male", "Female", or null')

    duration = raw.get("duration", "Auto")
    if duration not in ("Auto", "Custom"):
        raise ValueError('duration must be "Auto" or "Custom"')

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model must be a non-empty string or null")

    lyrics = raw.get("lyrics", "")
    if not instrumental and (not isinstance(lyrics, str) or not lyrics.strip()):
        raise ValueError("lyrics must be a non-empty string for a vocal song")
    if not isinstance(lyrics, str):
        raise ValueError("lyrics must be a string")

    exclude = raw.get("exclude", "")
    if not isinstance(exclude, str):
        raise ValueError("exclude must be a string")

    return {
        "title": nonempty_text(raw, "title"),
        "lyrics": lyrics,
        "styles": nonempty_text(raw, "styles"),
        "exclude": exclude,
        "instrumental": instrumental,
        "vocal_gender": gender,
        "weirdness": integer_percent(raw.get("weirdness"), "weirdness", 35),
        "style_influence": integer_percent(
            raw.get("style_influence"), "style_influence", 80
        ),
        "duration": duration,
        "model": model,
    }


def read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a Suno Advanced Create console script from a song JSON file."
    )
    parser.add_argument("input", nargs="?", default="-", help="JSON file, or - for stdin")
    parser.add_argument("-o", "--output", help="write JavaScript to this file")
    args = parser.parse_args()

    try:
        spec = normalize_spec(read_json(args.input))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))

    payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    rendered = JS_TEMPLATE.replace("__SONG_PAYLOAD__", payload)

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
