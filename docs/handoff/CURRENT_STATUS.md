# Current status

Updated: 2026-09-15.

## Current task

- `docs/tasks/2026-09-14-material-hit-rate-optimization.md`.
- Hit-rate gates are **enabled in the shipped config**: `relevance_gate`
  (`min_subject_hits=1`, `min_hit_ratio=0.05`), `visual_verify` (3-frame RapidOCR on file name + OCR
  only, never the title; `conclusive=false` drops nothing), `material_replica.max_age_days=90`.
- `theme_keywords` / `theme_subject_terms` per-theme overrides (`93c76da`) allow the generic queries
  platform users actually type without silently widening the admission gate.
- Command: `material-replication run --theme <T> --pool-size <N> --business-date <YYYY-MM-DD>`.

## Reliable current capabilities

- Collect Douyin account/keyword metadata via the project-owned MediaCrawler adapter; preserve raw
  interaction evidence and expose separate heat and delivery rankings.
- Bounded OCR/ASR enrichment; dated Markdown/JSON packages, material manifests, OpenMontage snapshots.
- Bounded public-web leads plus visual references for a user-selected script.
- Themed replication publishing an atomic `MM.DD<主题>复刻视频/` folder.
- Pluggable discovery: Douyin / yt-dlp / bilibili. `sources` stays off until multi-source dispatch lands.
- Pre-download subject-term admission, plus per-theme keyword and subject-term overrides.

## Product limitations

- Autonomous daily-news discovery is not a reliable finished product (2026-09-03 package: zero strict cards).
- Douyin is attention evidence, not factual evidence.
- Replication needs live Douyin login and `data/models/face` YuNet; without it, `unavailable` + warnings.
- The 2026-09-14 batch proved `status=done` + `degraded=false` + correct volume can still mean off-topic
  or stale content. The gates above exist to close that gap.

### Residual defects

1. `expand_keywords` adds a multi-word head as one phrase, which never matches under AND semantics
   (false-negative degradation); deferred, as fixing it touches lane allocation and 11 snapshots.
2. No multi-source dispatch on the download path — both chains read `media_urls[id]` directly
   (`replication_selection.py:1696`, `:1282`); `CompositeMediaResolver` gates `sources`.
3. Subject terms do not separate exact brand/model terms from category aliases, so a bare category word
   can admit a competitor clip; fixing it forces a batch re-run.
4. `max_age_days` is a single global key; the five-tier freshness policy needs per-category keys.
5. The offline pre-gate model missed the face/speech/visual gates and over-estimated one theme ~4x.

## Repository evidence

- `third_party/MediaCrawler` submodule `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`.
- Adapters `447ba87`; theme limit 12 -> 24 `9082bd8`; gate chain `6449e29` -> `90097cf` -> `286223e`
  -> `9a94ed4` -> `93c76da`.
- Repo-root data loss: LF-only launchers deleted all 10 top-level tracked files three times; fixed and
  guarded in `0341c0e`/`aedcdad`/`47143aa` (RUNTIME_SAFETY).
- Full suite 2026-09-15: 827 tests, 0 failures, no deselect.

## Next direction

- Re-run the affected batch after any subject-term exactness change. Keep news discovery frozen.
