# Current status

Updated: 2026-09-12.

## Current task

- Current task: `docs/tasks/2026-09-12-material-replication.md`.
- State: **implemented; theme-driven Douyin material replication workflow added**.
- Command: `material-replication run/inspect/doctor`; guide frozen in the task file, ≥20 offline tests added.
- Boundary: local credentials, Cookie files, browser profiles, models, runtime data, outputs and local integration history remain untracked; signed media URLs are never persisted.

## Reliable current capabilities

- Collect configured Douyin accounts and keyword-search metadata through the project-owned MediaCrawler adapter.
- Preserve raw interaction evidence, consolidate related videos and expose separate heat and delivery rankings.
- Download a collected video's media URL for bounded OCR/ASR enrichment; direct share-link downloading is not yet a standalone workbench flow.
- Build dated Markdown/JSON packages, material manifests and OpenMontage-facing snapshots.
- Discover bounded public-web leads and find specific visual references for a user-selected news script.
- Launch the Windows workbench with the Chinese `.bat`/`.cmd` entrypoints.
- Run a themed Douyin material replication: expand keywords, collect a candidate pool, pick one script replica plus 2~4 face-free material clips and publish an atomic `MM.DD<主题>复刻视频/` delivery folder.

## Product limitations

- Autonomous daily-news discovery is not a reliable finished product. The 2026-09-03 source-day package had 17 Douyin and 20 public-web candidates but zero strict public-reader cards.
- That run spent about 29 minutes; six core Douyin searches failed, 24/24 article-detail fetches timed out, and all content/localization model batches failed. Fallback output therefore remained title-like.
- Douyin remains attention evidence rather than factual evidence. Public-web results remain discovery leads unless the existing detail gates succeed.
- Material replication still needs live Douyin login to collect and the `data/models/face` YuNet model to gate faces; without the model it degrades to `unavailable` and still delivers with warnings. OpenCV 5.0 requires an ASCII model path, bridged automatically.

## Repository evidence

- Initial snapshot commit: `04958e1`.
- `third_party/MediaCrawler` is a submodule at `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`, tracking `https://github.com/NanmiCoder/MediaCrawler.git`.
- Pre-publish verification: 298 pytest cases passed, `compileall` passed, CLI doctor passed for the offline pipeline, and the staged secret-pattern scan returned no matches.
- Doctor intentionally did not inspect authentication; live collection still depends on the local project browser login.
- Material replication verification: 342 pytest cases passed (2 launcher cases deselected), `compileall` and `audit_handoff.py` passed; offline e2e sliced 3~8s `-c copy` clips from synthetic videos.

## Next direction

- Validate material replication on one real theme with manual usable-rate sampling before expanding scope; keep autonomous news-discovery expansion frozen.
