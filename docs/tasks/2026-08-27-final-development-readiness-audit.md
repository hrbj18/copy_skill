# Final development-readiness and runtime-safety audit

Date: 2026-08-27

## Goal

Produce evidence-backed, separate conclusions for (1) whether normal project
development may continue and (2) whether the Windows daily scheduled task may
be installed. Diagnose and minimally repair confirmed project-owned safety
defects without adding product features.

## Scope

- Establish a resumable phase ledger and final machine-readable and Markdown
  reports under `output/final-readiness-audit/`.
- Validate project entrypoints, configuration parsing, optional-service isolation,
  scheduler read-only state, process safety, bounded external-operation behavior,
  atomic output, and sensitive-artifact handling.
- Revalidate the prior live-acceptance conclusions: LLM relay scheme, raw Douyin
  output redaction/cleanup, daily reference ceiling, and final artifact scan.
- Add only minimal root-cause fixes and regression tests for confirmed defects.
- Run focused then full tests in independent Windows process groups after runtime
  changes, and perform one bounded, unplanned daily-path rehearsal only after
  prerequisite checks pass.

## Frozen safety boundaries

- Do not install, enable, modify, run, or trigger `CopySkillDailyTechNews`; only
  its read-only query is allowed.
- Do not read `.env.local`, Cookie files, browser profiles, API keys, Bearer
  values, or authentication stores; do not expose them in artifacts or output.
- Do not modify `third_party/MediaCrawler` or write to
  `D:\codex_work\OpenMontage`.
- Do not download media. Douyin work is metadata-only, one keyword, concurrency
  one, comments disabled, and at most 10 references.
- Use finite timeouts, retries, concurrency, output sizes, and process budgets.
  Any authentication, QR, CAPTCHA, or ambiguous destructive target stops that
  external operation and is recorded without bypassing it.
- On Windows, never use signal-based PID probing. First focused and full tests
  after runtime/process changes must be placed in independent process groups.
- Delete only precise files created by this task after their resolved paths are
  verified under the task-owned directory. Record any such deletion and how it
  can be regenerated.

## Exclusions

- New product capabilities, dependency upgrades, broad refactors, bulk crawling,
  load testing, live schedule installation, credential repair, and modifications
  to global Codex or optional-service configuration.
- Treating Douyin material as factual evidence for news claims.

## Phase contract

A. Record baseline entrypoint, configuration, optional-dependency, and
read-only-scheduler results.

B. Inspect targeted project-owned runtime-risk surfaces only: PID/locks,
subprocess and network budgets, unsafe shell/path operations, detached workbench
behavior, configuration degradation, logging, and artifact cleanup.

C. Revalidate and resolve where possible the LLM relay scheme, raw-artifact
sanitization, daily reference limit, and sensitive-output scan.

D. Run isolated focused and full regressions with finite total timeouts.

E. If A-D pass and external conditions permit, perform one bounded daily-path
rehearsal using one HTTPS news source, at most 10 retained records and 10 Douyin
metadata references; otherwise record the exact skip reason.

F. Update current handoff facts, append one process record without reading prior
history, and emit final Markdown and JSON reports.

## Acceptance conditions

1. Every phase has machine-readable status, bounded command evidence, and a
   human-readable summary.
2. Confirmed project-owned crash or secret-retention defects have minimal fixes
   with non-weakened regression coverage.
3. Optional unavailable services remain localized and non-fatal to unrelated
   commands and tests.
4. Focused and full isolated suites pass, or any failure has a reproducible
   development-blocking root cause and produces RED.
5. Final artifacts contain no API-key, cookie, bearer, signed-media, browser-auth,
   or credential-path material; scanner output names only sanitized locations and
   categories.
6. The final conclusion is exactly GREEN, YELLOW, or RED, separately states
   scheduler readiness, and proves the scheduler remains uninstalled and unused.

## Rollback principles

- Each change must be small and independently tested; revert only the exact
  audited change if it fails verification, never reset unrelated user work.
- When an external condition blocks verification, preserve safe evidence, mark it
  unavailable, and continue unrelated offline checks rather than retrying without
  limit.

