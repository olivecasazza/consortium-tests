#!/usr/bin/env python3
"""zai_review.py — generic Z.AI (GLM) review gate for GitHub Actions.

Reads an items JSON file plus free-form review instructions, asks a Z.AI
GLM model for a STRICT JSON response, validates that it parses, and writes
the resulting plan to a file. Deliberately schema-agnostic: whatever shape
the instructions demand is what lands in the plan file — the consumer of
the plan validates the shape (fail-loud gate).

Used by the reusable workflow `.github/workflows/ai-item-review.yml`, but
equally runnable standalone:

    ZAI_API_KEY=... python3 zai_review.py \
        --items items.json --instructions "Review these, output strict JSON" \
        --model glm-5.2 --out plan.json

Exit codes: 0 plan written, 1 model never returned valid JSON,
            2 usage/API/IO error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = os.environ.get("ZAI_API_BASE", "https://api.z.ai/api/paas/v4")
MAX_ITEMS_BYTES = 512 * 1024  # guard against blowing the model context

SYSTEM = ("You are a precise review assistant running inside a CI gate. "
          "Respond with STRICT JSON only: no prose, no markdown fences, "
          "no trailing commentary. Match the JSON shape requested in the "
          "user instructions exactly.")


def chat(api_key: str, model: str, messages: list[dict]) -> str:
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=json.dumps({"model": model, "messages": messages,
                         "temperature": 0.2}).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "Accept-Language": "en-US,en"},
        method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        payload = json.load(r)
    return payload["choices"][0]["message"]["content"]


def extract_json(text: str):
    """Parse strict JSON from a model reply, tolerating code fences and
    leading/trailing chatter. Raises ValueError if nothing parses."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", t, re.S)
    candidates = [m.group(1)] if m else []
    candidates.append(t)
    start = min((i for i in (t.find("{"), t.find("[")) if i >= 0),
                default=-1)
    if start >= 0:
        end = max(t.rfind("}"), t.rfind("]"))
        if end > start:
            candidates.append(t[start:end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no parseable JSON in model reply ({len(t)} chars)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True, help="items JSON file")
    ap.add_argument("--instructions", default=None,
                    help="review instructions (must demand strict JSON)")
    ap.add_argument("--instructions-file", default=None,
                    help="read instructions from this file instead")
    ap.add_argument("--model", default="glm-5.2", help="Z.AI model id")
    ap.add_argument("--out", required=True, help="plan JSON output path")
    ap.add_argument("--retries", type=int, default=2,
                    help="extra attempts after an unparseable reply")
    args = ap.parse_args()

    api_key = os.environ.get("ZAI_API_KEY", "")
    if not api_key:
        print("ZAI_API_KEY is not set", file=sys.stderr)
        return 2
    instructions = args.instructions
    if args.instructions_file:
        instructions = Path(args.instructions_file).read_text()
    if not instructions or not instructions.strip():
        print("no instructions given (--instructions/--instructions-file)",
              file=sys.stderr)
        return 2
    items_text = Path(args.items).read_text()
    if len(items_text.encode()) > MAX_ITEMS_BYTES:
        print(f"items file too large ({len(items_text.encode())} bytes > "
              f"{MAX_ITEMS_BYTES}); trim it before review", file=sys.stderr)
        return 2

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"{instructions.strip()}\n\nITEMS:\n{items_text}"},
    ]
    attempts = 1 + max(0, args.retries)
    for attempt in range(1, attempts + 1):
        try:
            reply = chat(api_key, args.model, messages)
        except urllib.error.HTTPError as e:
            print(f"Z.AI API error: HTTP {e.code} {e.read()[:500]!r}",
                  file=sys.stderr)
            return 2
        except (urllib.error.URLError, TimeoutError, KeyError,
                json.JSONDecodeError) as e:
            print(f"Z.AI request failed: {e}", file=sys.stderr)
            return 2
        try:
            plan = extract_json(reply)
        except ValueError as e:
            print(f"attempt {attempt}/{attempts}: {e}", file=sys.stderr)
            if attempt == attempts:
                print("model never returned valid JSON — gate FAILED",
                      file=sys.stderr)
                return 1
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content":
                             "That reply was not valid strict JSON. Respond "
                             "again with ONLY the JSON object/array requested "
                             "— no prose, no markdown fences."})
            continue
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, indent=2))
        size = len(plan) if isinstance(plan, (list, dict)) else "?"
        print(f"plan written to {out} "
              f"({size} top-level entries, attempt {attempt})")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
