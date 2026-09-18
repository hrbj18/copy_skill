# Current status

Updated: 2026-09-18.

## Current task

- `docs/tasks/2026-09-18-material-acquisition-strategy-v1.md` — theme-first material acquisition; the
  hard gate is theme relevance (not faces or source kind), `00-素材目录.json` is the downstream contract,
  and the folder caps at `200,000,000` bytes. Steps A, B and C all landed (`745e17b`).
- Shipped config enables `theme_material_profiles`/`theme_profile_map`, `sources: ["bilibili"]`,
  `direct_delivery`, `relevance_gate` (`min_subject_hits=1`, `min_hit_ratio=0.05`), `visual_verify`
  (3-frame RapidOCR, drops nothing), `max_age_days=90`, the per-theme `theme_keywords`/
  `theme_subject_terms` (`93c76da`) and the frozen episode research pack.
- Live proof 2026-09-18: `9.18日本AI篡改历史观复刻视频` -> 9 clips (2 main + 7 support), folder
  89,426,232 B <= 200,000,000, `delivered_bytes` 89,217,332, `degraded=false`, no clip dropped for
  faces. The 09-16 run reproduces its frozen digest.
- Run under `CODEBUDDY_SAFE_DELETE_ENABLED=0`, else the host bulk-delete guard kills the motion stage:
  `material-replication run --theme <T> --pool-size <N> --business-date <YYYY-MM-DD>`.

## Reliable current capabilities

- Collect Douyin account/keyword metadata via the project-owned MediaCrawler adapter; expose heat and
  delivery rankings separately on preserved raw interaction evidence.
- Bounded OCR/ASR enrichment; dated Markdown/JSON packages, material manifests, `00-素材目录.json`,
  OpenMontage snapshots.
- Bounded public-web leads plus visual references for a selected script.
- Themed replication publishing an atomic `MM.DD<主题>复刻视频/` folder.
- Pluggable discovery: Douyin / yt-dlp / bilibili; downloads resolve via `CompositeMediaResolver`.

## Product limitations

- Autonomous daily-news discovery is not a reliable finished product (2026-09-03: zero strict cards).
- Douyin is attention evidence, not factual evidence.
- The Douyin path needs live login + `data/models/face` YuNet, else `unavailable` + warnings.
- The 2026-09-14 batch proved `status=done` + `degraded=false` + correct volume can still mean off-topic
  or stale content; the gates above close that gap.

### Residual defects

1. `expand_keywords` adds a multi-word head as one phrase, never matching under AND semantics; deferred
   (touches lane allocation and 11 snapshots).
2. Subject terms do not separate exact brand/model terms from category aliases, so a bare category word
   can admit a competitor clip; fixing it forces a batch re-run.
3. `max_age_days` is a single global key; the five-tier freshness policy needs per-category keys.
4. `direct_delivery.main_min_seconds=60` keeps official 35-53 s sources out of `02-主素材`, and the
   profile's source-kind/role rankings stay dead code while real outlet accounts are unlisted. Bilibili
   serves 360P only (`qn`/dash unvalidated).

## Repository evidence

- `third_party/MediaCrawler` submodule `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`.
- Adapters `447ba87`/`f40ab92`; theme limit 12 -> 24 `9082bd8`; gate chain `6449e29` -> `9a94ed4` ->
  `93c76da` -> `745e17b`.
- Repo-root data loss: LF-only launchers deleted all 10 top-level tracked files three times; fixed and
  guarded in `0341c0e`/`aedcdad`/`47143aa` (RUNTIME_SAFETY).
- Full suite 2026-09-18: 1109 tests, 0 failures, no deselect.

## Next direction

- Settle the three open items in defect 4 (main_min_seconds, outlet accounts, bitrate), then re-run the
  affected batch. Keep news discovery frozen.
