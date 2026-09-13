# Current binding decisions

## Context authority

- Current executable code/config/tests outrank prose.
- The active task contract is named by `docs/handoff/README.md`; the previous Windows lock/handoff hardening guide is historical.
- `CURRENT_STATUS.md` owns volatile facts and next actions. This file owns current rationale. Old task guides and the append-only process document do not define current state.

## Windows process safety

- PID liveness is a query, never a signal. Windows uses `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` and `GetExitCodeProcess`; handles are always closed.
- Access-denied PID queries are treated conservatively as alive so an active lock is not deleted.
- POSIX platforms continue to use `os.kill(pid, 0)` because signal 0 has existence-probe semantics there.
- The first process-related regression runs in an independent Windows process group. A failed child must not terminate the Codex app server.

## Job execution

- Jobs use mutually exclusive locks, atomic state files, finite retries, and detached workers where UI independence is required.
- Scheduler management remains explicit. Read-only query is safe; install, run-now, and uninstall are state-changing operations.

## Project browser lifecycle

- Douyin login is manual in the dedicated persistent-profile Chrome on CDP port 9223. Credentials, Cookie values, QR content and profile data never enter project inputs or outputs.
- Browser startup is single-instance and lease-protected. Only project-port page targets may be converged or closed; no name-based Chrome termination is permitted.
- Authentication challenges keep one project page open. Normal completion may close the project browser only when no other live project lease exists; closing never deletes the persistent profile and does not guarantee permanent login.

## Evidence and generation

- News facts are source-gated before model structuring. Model output may rewrite supported material but cannot promote unsupported claims.
- LLM and browser failures degrade to deterministic output instead of discarding collected data.
- Media work is selected after global metadata budgeting and is temporary by default.
- The 02:00 exchange accepts a `local_ranked_snapshot` only when the source artifact is a project-produced ranking/report pinned by SHA-256. It is heat evidence only; public interactions and source titles never promote facts.
- A single ordinary media article is a recent candidate, not a heat signal. Duplicate social descriptions of one event are merged before the exchange record is emitted; `measured_multi_signal` requires multiple independently retained signals, while research candidates cannot enter the hotspot rank.
- A fact-ready story without a truly usable image stays partial. Article-length text page screenshots, social UI, generic product/brand imagery and anti-bot workarounds are rejected rather than used to satisfy a primary-image count.

## Handoff maintenance

- The mandatory context is a router, not a project history.
- Current conclusions replace obsolete ones; chat transcripts are never copied into handoff files.
- Character budgets are enforced by `docs/handoff/context-policy.json` and `scripts/audit_handoff.py` through pytest.
