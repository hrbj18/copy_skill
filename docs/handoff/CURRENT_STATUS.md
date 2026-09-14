# Current status

Updated: 2026-09-14.

## Current task

- Current task: `docs/tasks/2026-09-14-multi-source-material-acquisition.md`.
- State: theme-driven material replication with pluggable multi-source discovery; adapters `douyin`/`ytdlp`/`bilibili` landed.
- Not yet approved: multi-source aggregation, hit-ratio gate, delivered-bytes filtering, `Candidate.source`.
- Command: `material-replication run/inspect/doctor`; signed media URLs are never persisted.

## Reliable current capabilities

- Collect configured Douyin accounts and keyword-search metadata through the project-owned MediaCrawler adapter.
- Preserve raw interaction evidence, consolidate related videos and expose separate heat and delivery rankings.
- Download a collected video's media URL for bounded OCR/ASR enrichment.
- Build dated Markdown/JSON packages, material manifests and OpenMontage-facing snapshots.
- Discover bounded public-web leads and find specific visual references for a user-selected news script.
- Launch the Windows workbench with the Chinese `.bat`/`.cmd` entrypoints.
- Run a themed replication: expand keywords, collect a candidate pool, pick one script replica plus 2~4 face-free material clips and publish an atomic `MM.DD<主题>复刻视频/` folder.
- Discover material through pluggable sources: Douyin crawler, yt-dlp (YouTube `ytsearch`) and bilibili (stdlib wbi search, no login). Douyin covers mass-interest/domestic topics; bilibili covers overseas niche frontier hardware.

## Product limitations

- Autonomous daily-news discovery is not a reliable finished product (the 2026-09-03 source-day package held zero strict public-reader cards).
- Douyin is attention evidence, not factual evidence; public-web results stay leads until detail gates succeed.
- Material replication still needs live Douyin login and the `data/models/face` YuNet model; without the model it degrades to `unavailable` and still delivers with warnings.
- The gate judgement is still zero-only: a 25%-hit run reported `warnings: []`. Hit-ratio gating is not yet approved, and a volume-only delivery gate can be satisfied by off-topic clips.

## Repository evidence

- `third_party/MediaCrawler` is a submodule at `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`, tracking `https://github.com/NanmiCoder/MediaCrawler.git`.
- Source adapters landed in `447ba87`; delivery-folder theme limit raised 12 -> 24 in `9082bd8`.
- Handoff verification: `audit_handoff.py` 7/7 OK; `tests/test_handoff.py` 3 passed.
- Full suite: 733 passed (736 collected); 3 pre-existing, unrelated failures remain in `tests/test_workbench_launcher.py` (environment-sensitive; deselected by project convention).

## Next direction

- Validate material replication on one real theme with manual usable-rate sampling; keep autonomous news-discovery expansion frozen.
