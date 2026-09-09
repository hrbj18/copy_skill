# Data contracts

## Accepted input

The normalizer accepts JSON arrays, JSONL rows, or objects containing `videos`, `items`, `aweme_list`, `contents`, `data`, or `result`. It recognizes both MediaCrawler's flattened Douyin fields and common nested `author` / `statistics` fields.

MediaCrawler's maintained flattened fields include `aweme_id`, `title`, `create_time`, `creator_hash`, `nickname`, `liked_count`, `collected_count`, `comment_count`, `share_count`, `aweme_url`, and `source_keyword`.

## Output

`run` creates:

- `hotboard.json`: `captured_at`, `target_date`, `items[]` with OpenMontage-compatible `word`, `hotScore`, and `url`.
- `benchmark_accounts.json`: `captured_at`, `target_date`, `videos[]` with `title`, `account_name`, `play_count`, and `share_url` plus preserved engagement fields.
- `content_candidates.json`: complete normalized records, scores and explanations.
- `run_report.json`: counts, exclusions, duplicates and warnings.

`play_count: 0` is emitted only for compatibility when upstream omitted play count. The same item carries `play_count_missing: true`; scoring does not interpret the missing measurement as zero value.

OpenMontage must use these as heat/topic signals. A title or transcript is not sufficient evidence for a factual claim.

## Markdown material package

`collect-materials` and `build-materials` create an isolated package at `output/materials/<run-id>/`:

- `summary.md`: ranked value summaries, core information, best timestamped moments, extension angles, verification reminders, source URL, and relative material links.
- `videos/<account-id>-<video-id>.md`: semantic analysis, source/heat metadata, original caption, timestamped OCR/transcript, processing state, retention state, and fact-check constraints.
- `run_report.json`: machine-readable status, selected/completed counts, transcript/OCR/analysis states, storage cleanup metrics, non-secret LLM status, warnings, and errors.

Durable text checkpoints stay under ignored `data/cache/materials/<run-id>/`. Disposable media stays under `data/temp/materials/` and is deleted after success; `data/media/<run-id>/` receives MP4 only for explicit keep-video runs. Temporary signed URLs and API keys are never emitted in Markdown or reports. The package is a downstream material input, not verified news evidence.
