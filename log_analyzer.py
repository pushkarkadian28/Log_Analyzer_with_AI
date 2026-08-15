#!/usr/bin/env python3
"""
log_analyzer.py

Analyzes log files (Apache/Nginx access logs, JSON logs, application logs,
syslog-style logs, or anything else) and produces a short AI-generated
summary covering: what service/component the log belongs to, what errors
occurred and why, and how to fix them.

Reads GEMINI_API_KEY (or ANTHROPIC_API_KEY) from a .env file in the script's
directory, or from the environment directly. No external packages required,
the .env loader is built in.

Usage:
  python3 log_analyzer.py /var/log/nginx/access.log
  python3 log_analyzer.py /path/to/any.log --top 5
  python3 log_analyzer.py /path/to/any.log --output report.txt
  python3 log_analyzer.py /path/to/any.log --provider anthropic
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

LOG_PATTERN = re.compile(
    r'(?P<ip>\S+) \S+ \S+ \[(?P<timestamp>[^\]]+)\] '
    r'"(?P<method>\S+) (?P<path>\S+) \S+" '
    r'(?P<status>\d{3}) (?P<size>\S+)'
)

# Cap on how much raw log text gets sent per prompt, kept small to limit token usage.
MAX_RAW_LOG_CHARS = 12000

# Cap on the AI response length.
MAX_RESPONSE_TOKENS = 1000


def load_env_file():
    """Load KEY=VALUE pairs from a .env file next to this script, if one
    exists. Does not override variables already set in the environment,
    so real env vars always take priority over the file."""
    env_path = SCRIPT_DIR / ".env"
    if not env_path.is_file():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_log(filepath: Path):
    """Parse the log file against Common/Combined Log Format. Returns
    matched records and a count of lines that didn't match, rather than
    failing outright, since real logs often mix in a few bad lines."""
    records = []
    skipped = 0

    with filepath.open("r", errors="replace") as f:
        for line in f:
            match = LOG_PATTERN.search(line)
            if match:
                records.append(match.groupdict())
            else:
                skipped += 1

    return records, skipped


def build_raw_sample(lines, max_chars):
    """Return a text sample of the log for AI analysis. Uses the whole
    file if it fits under max_chars, otherwise takes an even sample of
    lines spread across the file so nothing important is missed just
    because it's not near the start."""
    full_text = "".join(lines)
    if len(full_text) <= max_chars:
        return full_text, len(lines)

    avg_line_len = max(len(full_text) / max(len(lines), 1), 1)
    sample_line_count = max(int(max_chars / avg_line_len), 1)
    step = max(len(lines) // sample_line_count, 1)
    sampled = lines[::step][:sample_line_count]
    return "".join(sampled), len(sampled)


SUMMARY_INSTRUCTIONS = (
    "Give a short, plain-text summary (no markdown) with exactly four parts:\n"
    "1. Service: one line naming what service, app, or system this log appears to be from.\n"
    "2. Errors: the distinct errors found and why each one most likely happened. "
    "One or two lines per error, be specific, not generic.\n"
    "3. Fix: a concrete suggested fix for each error.\n"
    "4. Notable: any other important information worth flagging that isn't a "
    "hard error, security concerns (repeated auth failures, a single IP "
    "dominating traffic, suspicious paths being probed), performance issues "
    "(unusually large response sizes, slow-looking patterns), or anything "
    "else that stands out. If nothing stands out, write \"None.\"\n"
    "Keep the whole thing brief and to the point."
)


def summarize_structured_log(status_counts, path_counts, ip_counts, total, provider, api_key):
    """Build a short summary for a log that matched Common/Combined Log
    Format, using parsed counts rather than raw text, which keeps the
    prompt small. Includes IP and full status distribution, not just
    errors, so the AI can flag notable patterns (e.g. one IP dominating
    traffic) even when nothing is technically an error."""
    error_codes = {
        code: count for code, count in status_counts.items() if code.startswith(("4", "5"))
    }
    top_paths = [path for path, _ in path_counts.most_common(5)]
    top_ips = ip_counts.most_common(5)

    if not error_codes:
        prompt = (
            f"This is a web server access log. Total requests: {total}. "
            f"Status codes: {dict(sorted(status_counts.items()))}. "
            f"Most-requested paths: {top_paths}. Top IPs by request count: {top_ips}.\n\n"
            + SUMMARY_INSTRUCTIONS
            + "\n\nThere are no 4xx/5xx errors, so part 2 should say \"None\" and part 3 "
              "should say \"n/a\". Focus mainly on part 4, Notable, for anything worth flagging."
        )
    else:
        prompt = (
            f"This is a web server access log. Total requests: {total}. "
            f"HTTP error status codes and counts: {json.dumps(error_codes)}. "
            f"Full status code breakdown: {dict(sorted(status_counts.items()))}. "
            f"Most-requested paths: {top_paths}. Top IPs by request count: {top_ips}.\n\n"
            + SUMMARY_INSTRUCTIONS
        )
    return call_ai(prompt, provider, api_key)


def summarize_unstructured_log(filepath, provider, api_key):
    """Fallback for logs that don't match Common/Combined Log Format.
    Sends a capped, sampled slice of the raw log to the AI."""
    with filepath.open("r", errors="replace") as f:
        lines = f.readlines()

    if not lines:
        return None, 0

    sample_text, sampled_count = build_raw_sample(lines, MAX_RAW_LOG_CHARS)
    note = (
        f"(Sample of {sampled_count}/{len(lines)} lines, evenly spread across the file.)\n\n"
        if sampled_count < len(lines) else ""
    )

    prompt = f"Here is a log file:\n\n{note}{sample_text}\n\n{SUMMARY_INSTRUCTIONS}"
    return call_ai(prompt, provider, api_key), len(lines)


def call_ai(prompt, provider, api_key):
    if provider == "gemini":
        return _call_gemini(prompt, api_key)
    return _call_anthropic(prompt, api_key)


def _call_anthropic(prompt, api_key):
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": MAX_RESPONSE_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
            return "\n".join(text_blocks) if text_blocks else None
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError) as e:
        print(f"Anthropic API call failed ({e}).", file=sys.stderr)
        return None


def _call_gemini(prompt, api_key):
    # Free tier, sign in at aistudio.google.com with any Google account and generate a key under "Get API Key".
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": MAX_RESPONSE_TOKENS},
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            text_blocks = [p["text"] for p in parts if "text" in p]
            return "\n".join(text_blocks) if text_blocks else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(
                f"Gemini API call failed (404: model '{model}' not found or not enabled for your key). "
                f"Check available models at https://generativelanguage.googleapis.com/v1beta/models?key=YOUR_KEY",
                file=sys.stderr,
            )
        else:
            print(f"Gemini API call failed (HTTP {e.code}: {e.reason}).", file=sys.stderr)
        return None
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        print(f"Gemini API call failed ({e}).", file=sys.stderr)
        return None


def main():
    load_env_file()

    parser = argparse.ArgumentParser(description="Summarize any log file: service, errors, and fixes.")
    parser.add_argument("logfile", type=Path, help="Path to the log file")
    parser.add_argument("--top", type=int, default=5, help="Number of top paths/IPs to include in stats (default: 5)")
    parser.add_argument("--output", type=Path, default=None, help="Write report to this file instead of only printing")
    parser.add_argument(
        "--provider",
        choices=["gemini", "anthropic"],
        default="gemini",
        help="AI provider to use. Gemini (default) has a free tier, sign in at aistudio.google.com, "
             "no credit card needed. Set GEMINI_API_KEY. Anthropic requires a paid key, set ANTHROPIC_API_KEY.",
    )
    args = parser.parse_args()

    if not args.logfile.is_file():
        print(f"Error: {args.logfile} not found or not a file.", file=sys.stderr)
        sys.exit(1)

    if args.provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        print(
            f"No API key found for provider '{args.provider}'. Set it in a .env file next to this "
            f"script (GEMINI_API_KEY=... or ANTHROPIC_API_KEY=...) or as an environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    records, skipped = parse_log(args.logfile)

    lines = []
    lines.append("=" * 60)
    lines.append("LOG SUMMARY")
    lines.append("=" * 60)

    if records:
        total = len(records)
        status_counts = Counter(r["status"] for r in records)
        path_counts = Counter(r["path"] for r in records)
        ip_counts = Counter(r["ip"] for r in records)
        lines.append(f"Format: Apache/Nginx access log")
        lines.append(f"Total requests: {total}  (skipped/unparseable lines: {skipped})")
        lines.append(f"Status codes: {dict(sorted(status_counts.items()))}")
        lines.append("")
        summary = summarize_structured_log(status_counts, path_counts, ip_counts, total, args.provider, api_key)
    else:
        summary, total_lines = summarize_unstructured_log(args.logfile, args.provider, api_key)
        lines.append("Format: non-standard, analyzed via AI directly")
        lines.append(f"Total lines: {total_lines}")
        lines.append("")

    if summary is None:
        print("\n".join(lines))
        print("AI summary failed. See error above.", file=sys.stderr)
        sys.exit(1)

    lines.append(summary)
    lines.append("=" * 60)
    report = "\n".join(lines)

    print(report)

    if args.output:
        args.output.write_text(report + "\n")
        print(f"\nReport written to {args.output}")


if __name__ == "__main__":
    main()
