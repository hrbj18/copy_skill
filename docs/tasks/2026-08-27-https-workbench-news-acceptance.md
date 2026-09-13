# HTTPS repair and workbench news-broadcast acceptance

Date: 2026-08-27

## Goal

Eliminate unsafe HTTP model use without reading secrets, prove that the Tkinter
workbench's daily-news control follows the production daily-news task path, and
produce one bounded, user-visible Chinese technology-news broadcast for the
previous Beijing calendar day. Decide scheduler readiness separately and leave
the Windows scheduled task untouched.

## Scope

- Test the currently configured model relay's HTTPS variant with normal TLS
  verification and a finite request budget. Never disable verification or wrap
  remote HTTP in local TLS.
- If no compatible HTTPS endpoint is available through existing non-secret
  configuration mechanisms, disable model calls safely and emit a deterministic
  report that labels deep AI analysis unavailable.
- Verify the workbench daily-news action invokes the same `run_daily_news` task
  path used by CLI/scheduling and shows bounded progress, completion status,
  report path, and concise failures without requiring Voicebox.
- Run one unplanned, bounded previous-day report through that workbench task path:
  official HTTPS news only, 3-5 confirmed items when available, zero media
  downloads, and a 60-90 second Chinese narration.
- Add focused tests for every project-owned fix, then isolated full regression,
  sensitive artifact scanning, and read-only scheduler verification.

## Frozen safety boundaries

- Do not install, enable, modify, run, or trigger `CopySkillDailyTechNews`.
- Do not read or print `.env.local`, keys, Cookies, Bearer values, browser
  profiles, authentication stores, or arbitrary environment values.
- HTTPS checks use normal certificate validation, no insecure fallback, finite
  timeouts, no broad port scan, and at most the same configured host plus existing
  safe runtime configuration mechanisms.
- Do not modify `third_party/MediaCrawler`, write to `D:\codex_work\OpenMontage`,
  download media, or treat Douyin as factual news evidence.
- Any interactive authentication stops only that external step. All child process,
  network, browser, and model work has finite budgets.
- First focused and full tests after runtime changes run in independent Windows
  process groups. Delete only exact task-owned sensitive artifacts after resolved
  path checks.

## Exclusions

- New product features outside the daily-news workbench action, proxy/TLS
  workarounds, credential provisioning, dependency upgrades, production schedule
  activation, broad live crawling, and manual browser login.

## Acceptance conditions

1. HTTP relay use is either replaced by a verified HTTPS endpoint or disabled;
   no insecure model request remains in the manual workbench run.
2. Workbench daily-news action uses `run_daily_news`, publishes running/complete
   state and report path, and fails concisely without optional services.
3. A prior-day Chinese report contains 3-5 source-gated items if available,
   title/event/importance/date/official URL/verification status, plus a 60-90
   second deterministic or HTTPS-model-assisted narration.
4. Focused and full tests pass in isolated process groups; artifacts scan clean;
   scheduler remains uninstalled and unused.
5. Handoff and final report separate development readiness from scheduler
   readiness and request only the minimum HTTPS configuration if unavailable.

