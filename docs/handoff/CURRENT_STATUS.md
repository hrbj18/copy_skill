# Current status

Updated: 2026-09-03.

## Current task

- Current task: `docs/tasks/2026-09-03-public-web-discovery-lane.md`.
- State: **implemented; code, full regression, diagnostics and real source-day acceptance passed for the new candidate lane**.
- Boundary: only `D:\work\copy_skill`; no OpenMontage, scheduler, credential, paid-production or upstream-third-party changes.

## Current V2.6 contract

- Douyin remains the attention lane. Raw interactions, `heat_score`, `heat_rank`, delivery priority and `truth_status: not_checked` are not recalculated from public-web results.
- A bounded 12-query public-web matrix supplements the 14 configured public sources. It discovers leads only; no search result, source count or model result is called factual verification or platform heat.
- `全网选题候选池.md` / JSON provide a broad, independent 20-item OP selection pool. Every JSON candidate carries `discovery_audit`: query lead(s) or configured-source reference(s), plus `discovery_only_not_fact_verification`. `observed_heat_status` stays `unknown` unless raw Douyin evidence exists.
- The candidate export rejects routine developer logs, customer cases, commentary/predictions and slogan-only lines; strips publisher tails; preserves weak leads as explicitly labelled leads; and merges rows only when two named anchors match, retaining all audit references.
- `每日科技热点榜.md` remains strict: detail support, public-audience route and plain-language criteria still apply. Candidate expansion cannot pad it or lower its admission rules.

## Latest real evidence

- Final V2.6 source-day run: `run-20260902-eb9c942b1ec9`, target day 2026-09-02, 997.125 seconds. It collected 53 public-web leads and yielded exactly 20 selected candidate items after filtering/merging. All 20 have discovery audit; all retain `observed_heat_status: unknown`; no selected title matched developer-log/customer-case/slogan/media-tail checks.
- The full package is honestly `partial`: public reader cards remain 3/20. The candidate-pool status is independently `success`, and no partial run replaced the prior success pointer.
- `current.json` remains manifest-valid V2.4 success `run-20260902-1ea1b4cb7503`. Latest generated V2.6 candidate files are under `output\每日新闻素材\2026-09-02_每日素材\packs\run-20260902-eb9c942b1ec9\`.
- Full pytest, compileall, doctor and current-package inspect passed. Doctor intentionally does not access Cookies; live account outcomes remain variable and are reported by each run.

## Next direction

- Improve first-party/allowlisted article-detail coverage so more public events qualify for the strict reader list. Do not reinterpret search leads as truth, relax reader-list detail gates, alter Douyin heat, or promote a partial package just to hide sparse-day limits.
