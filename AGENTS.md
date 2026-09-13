# copy_skill agent contract

## Mandatory context routing

1. Always read this file first.
2. For every project task, next read `docs/handoff/README.md` and `docs/handoff/CURRENT_STATUS.md`.
3. Before inspecting source, load at most the one topic file routed by the handoff README:
   - product/data constraints: `PRODUCT_RULES.md`
   - binding architecture choices: `DECISIONS.md`
   - source entrypoints and commands: `CODE_MAP.md`
   - locks, workers, Windows or test-process safety: `RUNTIME_SAFETY.md`
4. Use the current task guide named in the handoff README. Other `docs/tasks/*.md` files are historical contracts unless the current handoff explicitly promotes them.

Never read `docs/项目开发过程文档.md` when planning, implementing, diagnosing, or reviewing a later task. It is append-only integration history for agents working in other projects, not current implementation truth. Do not broadly scan the repository or old task guides before following the routes above.

Current code, current configuration, executable tests, the promoted task guide, and current handoff files are authoritative in that order. If they conflict, stop and report the concrete conflict instead of silently choosing an older statement.

## Development document workflow

For every task that changes code, configuration, Skill instructions, dependencies, deployment state, or handoff contracts:

1. Before changes, create one task-specific guide under `docs/tasks/` that freezes goal, scope, decisions, deliverables, exclusions, safety boundaries, and acceptance tests.
2. Implement and test against that guide. Record only necessary deviations.
3. After implementation and verification, update the current handoff facts and append one dated section to `docs/项目开发过程文档.md` without reading or rewriting its previous content.

Ordinary read-only exploration, explanation, and repeated monitoring do not update task or handoff documents.

## Runtime safety

- On Windows, never use `os.kill(pid, 0)` or a console signal to probe PID existence. Follow `docs/handoff/RUNTIME_SAFETY.md`.
- After changing locks, subprocesses, workers, schedulers, or Windows process handling, run the first focused and full pytest passes in an independent process group so a regression cannot terminate the Codex host.
- All network, browser, model, and subprocess operations must have finite budgets and timeouts.

## Repository boundaries

- `third_party/MediaCrawler` is an isolated upstream checkout. Put project-owned adapters under `src/`, `config/`, and `skills/`.
- Never expose or commit Cookie values, API keys, browser profiles, or local authentication material. Do not read `.env.local` or Cookie files for routine status work.
- Do not write into `D:\codex_work\OpenMontage` unless the user explicitly authorizes it. Default integration uses generated snapshots in this repository.
- Douyin material is discovery and attention evidence, never factual evidence for a news claim.

## Handoff maintenance

- Replace obsolete current conclusions; do not append chat transcripts or routine activity logs.
- Update `CURRENT_STATUS.md` only after a material implementation, decision, blocker, ownership, or next-step change.
- Run `python scripts/audit_handoff.py --root .` after changing `AGENTS.md` or `docs/handoff/`.
