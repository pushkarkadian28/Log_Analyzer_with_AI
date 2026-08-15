# Log Analyzer

Submission for the Accuknox DevOps Trainee Practical Assessment, Problem Statement 2 (Objective 3: Log File Analyzer).

Reads any log file, standard web server access logs (Apache/Nginx) or anything else, JSON logs, application logs, syslog-style logs, Kubernetes/container logs, custom formats, whatever you point it at, and produces a short AI-generated summary: what service the log belongs to, what errors happened and why, how to fix them, and anything else worth noticing.

## What it does

- Works on any log format. Recognizes Apache/Nginx access logs (Common/Combined Log Format) directly and pulls out status codes and request counts without spending any tokens on that part.
- For anything else, JSON logs, application logs, syslog, Kubernetes logs, custom in-house formats, sends a sample of the raw log to the AI, which detects the format itself and analyzes it accordingly. Nothing needs to be a recognized format for this script to produce a useful summary.
- Produces one short, four-part summary: Service, Errors (and why they happened), Fix, and Notable (anything else worth flagging, like a single IP dominating traffic or repeated auth failures, even if it isn't a hard error).
- Keeps token usage low: capped response length, capped and sampled input for large files, and only the minimum context sent per request.

There's no offline mode. An AI provider key is required to run this script.

## Requirements

- Python 3.8 or later
- No external packages, standard library only
- A free Gemini API key (or a paid Anthropic key, if you'd rather use that)

## Setup

1. Copy the example env file:

```
cp .env.example .env
```

2. Get a free Gemini key: go to aistudio.google.com, sign in with any Google account, click "Get API Key" in the left menu, generate one. No credit card, no expiration.

3. Paste it into `.env`:

```
GEMINI_API_KEY=your_key_here
```

The `.env` file is loaded automatically, nothing else to configure. Real environment variables, if set, always take priority over the file.

## Usage

```
python3 log_analyzer.py /path/to/access.log
```

Show more top paths in the stats line (default is 5):

```
python3 log_analyzer.py /path/to/access.log --top 10
```

Write the report to a file as well as printing it:

```
python3 log_analyzer.py /path/to/access.log --output report.txt
```

Use Anthropic instead of Gemini:

```
python3 log_analyzer.py /path/to/access.log --provider anthropic
```

(requires `ANTHROPIC_API_KEY` set in `.env` instead, and is a paid API, unlike Gemini's free tier)

Test it right away with the included sample log:

```
python3 log_analyzer.py sample_access.log
```

## Example output

```
============================================================
LOG SUMMARY
============================================================
Format: Apache/Nginx access log
Total requests: 9  (skipped/unparseable lines: 1)
Status codes: {'200': 5, '404': 2, '500': 1, '502': 1}

Service: web application backend served behind an nginx/Apache access log.
Errors: 404s on /missing-page and /old-page.html, likely broken links or
removed pages. 500 on /api/data suggests an unhandled exception in that
endpoint. 502 on the same path suggests the backend was briefly unreachable.
Fix: add redirects for the removed pages, check application logs for the
/api/data stack trace at that timestamp, and confirm the backend service
was healthy and reachable at the time of the 502.
Notable: 192.168.1.10 accounts for the majority of requests in this
sample, worth a quick check if that's expected traffic or a single
client hammering the server.
============================================================
```

## Non-standard logs

If the log doesn't match Common/Combined Log Format, the script says so and sends a sample of the raw content to the AI instead:

```
============================================================
LOG SUMMARY
============================================================
Format: non-standard, analyzed via AI directly
Total lines: 1

Service: an "orders" service, based on a JSON-structured log line.
Errors: a database timeout error.
Fix: check the database connection pool and query performance for the
orders service around the time this occurred.
============================================================
```

For large files, only a sample is sent (spread evenly across the file, not just the start) to keep token usage down. A note in the output says when this happened.

## Model used

Defaults to `gemini-flash-lite-latest`, Google's smallest and cheapest current model, well suited to a short structured summary like this. Override it if needed:

```
GEMINI_MODEL=gemini-2.5-flash
```

in your `.env` file.

## Security note

Never commit your `.env` file or hardcode a key in the script. `.env` should be excluded via `.gitignore` before pushing this to a public repository.
