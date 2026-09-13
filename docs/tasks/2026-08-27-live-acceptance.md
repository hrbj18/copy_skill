# Production live-environment acceptance

Date: 2026-08-27

## Goal

Validate the smallest production-representative path for LLM availability, one
real HTTPS news source, bounded Douyin metadata discovery, and report generation
without enabling any recurring system action.

## Scope and deliverables

- Run the handoff audit and the necessary regression tests before live access.
- Run `llm-doctor` against the configured relay and model without printing,
  copying, or inspecting API keys.
- Use exactly one real HTTPS news source for connection, parsing, and date-boundary
  smoke tests.
- Attempt one minimal Douyin metadata collection capped at 10 records.
- Generate one small acceptance report from the obtained data and inspect its
  fields, source attribution, failure degradation, and factual boundaries.
- Scan task outputs for API keys, cookies, signed media URLs, browser profiles,
  and authentication material.
- Fix defects discovered inside this acceptance path when safe, then rerun the
  focused checks that prove the fix.
- Update current handoff facts after verification and append one dated integration
  entry without reading prior integration history.

## Frozen decisions

- Douyin is discovery and attention evidence only. It must not establish or
  corroborate a factual news claim.
- News facts must retain an HTTPS source reference and publication-date evidence.
- A missing live dependency must degrade to an explicit unavailable/partial result,
  never fabricated content or silent success.
- Any login, QR-code, CAPTCHA, or interactive authentication requirement is a hard
  pause for user action; no bypass is attempted.
- Windows process-related tests run first in an independent process group if this
  task changes process, worker, scheduler, subprocess, or lock handling.

## Safety and resource limits

- Do not install, enable, modify, or trigger the Windows scheduled task.
- Do not read `.env.local`, cookie files, browser profiles, or raw authentication
  storage. Commands and captured output must not reveal secret values.
- Use one HTTPS news origin only, at most 10 parsed news records, finite request
  timeouts, and no broad crawling.
- Fetch at most 10 Douyin metadata records. Do not download video, images, audio,
  or signed media assets; do not run batch media processing; do not make high-rate
  or repeated retry traffic.
- Model health checks and report generation must use the smallest useful request,
  finite timeout, and bounded output. No unbounded agent/model loop is allowed.
- All network, browser, model, and subprocess operations require explicit finite
  time budgets. Stop repeated attempts after the first actionable authentication
  or configuration blocker.
- Do not modify `third_party/MediaCrawler` or write to
  `D:\codex_work\OpenMontage`.

## Exclusions

- Windows schedule installation or activation.
- Production-scale crawling, batch media downloads, transcription, rendering, or
  paid load/performance testing.
- Authentication bypass, credential repair, account setup, or persistent browser
  profile changes.
- Using Douyin content as factual evidence for a news claim.

## Acceptance checks

1. Handoff audit exits successfully.
2. Required isolated/focused regression tests pass before live access.
3. `llm-doctor` confirms a reachable configured relay and usable model, or reports
   a precise sanitized blocker without exposing a key.
4. One HTTPS news source completes bounded connection and parsing checks, and the
   date-boundary test includes or excludes records correctly at the cutoff.
5. Douyin returns no more than 10 metadata records without media downloads, or the
   run pauses immediately at a clearly reported human-login requirement.
6. A small report is produced from obtained data with expected fields and source
   labels; unavailable inputs degrade explicitly; Douyin remains non-factual
   discovery evidence.
7. A scan of generated artifacts and captured task output finds no API key, Cookie,
   bearer token, signed media URL, browser authentication data, or credential path.
8. Relevant focused tests and, when code changes warrant it, the full regression
   suite pass after fixes.
9. `CURRENT_STATUS.md` states every check result, login requirement, schedule
   readiness, and remaining blockers.

## Pass condition

The task passes only when checks 1-9 have evidence, no credential or signed-media
material appears in task artifacts, no scheduler is installed or enabled, and any
partial live result is explicitly classified rather than presented as success.

