# Code map

## Primary entrypoints

- `src/douyin_intelligence/cli.py`: CLI router.
- `daily_hot_candidate_pool.py`: V2.4 producer, source-first official events, semantic routing, immutable READY publish and non-regressing promotion.
- `daily_material_exchange.py`: immutable READY/current/latest pointers, manifest checks and business-date consumer inspection.
- `workbench.py`: Tkinter control/status UI. `科技内容情报工作台.cmd` is the Windows launcher.
- `trusted_news.py`, `jobs.py`: fixed-account reports and other job orchestration.

## Data and evidence

- `official_major_events.py`, `news_sources.py`, `reporting.py`: bounded official indexes, same-day source events, Chinese source-bound cards and reports.
- `collector.py`, `search_collector.py`, `mediacrawler_runner.py`: project-only CDP browser lifecycle and bounded Douyin adapters.
- `story_consolidation.py`, `editorial_priority.py`, `story_enrichment.py`, `news_semantics.py`: story anchors, game scope, dual ranking, evidence enrichment and constrained headlines.
- `human_brief.py`: Markdown brief. `normalize.py`, `scoring.py`, `pipeline.py`, `exporter.py`: normalization, ranking and atomic snapshots.
- `account_pool.py`, `account_evaluation.py`, `account_pool_ui.py`: candidate lifecycle and 14-day metadata evaluation.
- `material_probe.py`, `visual_anchor.py`: official-source-gated images and specific visual anchors.

## Media, models and configuration

- `llm_analysis.py`, `local_secrets.py`: OpenAI-compatible structured analysis and secret-safe local configuration.
- `visual_ocr.py`, `media_tools.py`, `materials.py`, `material_pipeline.py`, `media_processing.py`: bounded OCR/media tooling, caches and cleanup.
- `replication_theme.py`, `replication_candidates.py`, `replication_selection.py`, `replication_script.py`, `replication_clips.py`, `replication_delivery.py`, `replication_pipeline.py`, `face_metrics.py`: theme-driven Douyin material replication (candidate pool, deterministic selection, heuristic script skeleton, face-free clip export, atomic delivery).
- `sources/__init__.py`, `sources/base.py`, `sources/douyin.py`, `sources/ytdlp.py`, `sources/bilibili.py`: pluggable source registry (`SourceAdapter`, `MediaResolver`, `DownloadTarget`) plus Douyin (crawler wrapper), yt-dlp (YouTube `ytsearch`) and bilibili (stdlib wbi search) adapters; multi-source aggregation (`source_aggregation.py`) not yet implemented.
- `episode_research_pack.py`, `workbench_research_pack.py`: cross-repo producer for the frozen
  `episode-research-pack-v1` contract (`publish_episode_research_pack`, `publish_from_delivery`,
  `build_research_semantic`, `build_fixture_pack`) plus the workbench `单期研究包` panel helpers
  (command builder, detached launch, log tail, status text).
- `config/content_intelligence.json`: non-secret V2.4 source, story, media and model budgets. `.env.local` and Cookie files are secrets; never read them for routine diagnosis.
- `.gitattributes`: pins `*.bat`/`*.cmd` to CRLF (Windows launcher safety); no other file is rewritten.

## Verification commands

- Full tests: `.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider` (run serially; never a bare `pytest .`); compile: `.venv\Scripts\python.exe -m compileall -q src tests`.
- Offline health and CLI: `.venv\Scripts\python.exe -m douyin_intelligence.cli doctor` and `--help`.
- V2 run: `.venv\Scripts\python.exe -m douyin_intelligence.cli daily-material-exchange run [--business-date YYYY-MM-DD]`; inspect: `daily-material-exchange inspect --business-date YYYY-MM-DD`.
- Material replication: `.venv\Scripts\python.exe -m douyin_intelligence.cli material-replication run --theme "苹果折叠屏手机" [--dry-run]`; `inspect --folder <交付目录>`; `doctor` (offline, no download).
- Episode research pack: `.venv\Scripts\python.exe -m douyin_intelligence.cli episode-research-pack build --delivery-folder <交付目录> [--theme T] [--business-date YYYY-MM-DD]`; tests `tests/test_episode_research_pack.py` (frozen fixture digests) and `tests/test_workbench.py`.
- Handoff audit: `.venv\Scripts\python.exe scripts/audit_handoff.py --root .`. Append history only with `scripts/append_process_record.py --root . --record <markdown>`.
- `promote-existing` is offline recovery; `simulate` is V1 compatibility only. Browser lifecycle tests live in `tests/test_browser_session.py`.
