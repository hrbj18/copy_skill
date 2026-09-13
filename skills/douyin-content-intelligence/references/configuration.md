# Configuration

The maintained configuration is `config/content_intelligence.json` at the project root.

## Accounts

Each `benchmark_accounts` entry has `id`, `name`, `url`, `category`, and `enabled`. Prefer a full creator URL or `sec_user_id` when MediaCrawler cannot resolve a short Douyin ID. Do not store account login credentials here.

## Categories

`categories` maps a stable category ID to deterministic title/search keywords. Current stable IDs are `daily_news`, `ai_models`, `software_tools`, `hardware_products`, `open_source`, `interesting_tech`, and `unclassified`.

## MediaCrawler

`media_crawler.root` and `media_crawler.python` point to the isolated upstream deployment. `raw_output` is local untrusted input. Keep comment collection disabled unless a later task specifically needs comment mining.

`state_path` stores the atomic, local deduplication ledger. Repeating the same target date is idempotent; a work ID observed under another target date is reported without silently deleting the current candidate.

Real collection may open or attach Chrome. Use a dedicated browser identity when possible. QR, phone verification, CAPTCHA, or risk prompts require human handling.

## Resource retention

`materials.retention.keep_video` is false by default. `temp_root` contains disposable MP4, audio chunks, and OCR frames; `quota_bytes` and `ttl_hours` bound it. `cache_root` contains durable transcript parts, final transcript, OCR text, technical metadata, and semantic analysis. Never point `temp_root` at the project root, a user profile, or another broad directory.

Use `--keep-video` only after a downstream consumer explicitly selects the original. `cleanup-temp` never deletes outside `temp_root`.

## Transcription and OCR

`transcription.chunk_seconds` controls sequential ASR checkpoints. Completed part JSON files support resume after interruption. `ocr` controls low-frequency keyframes for no-speech videos; frames are deleted after recognition.

## LLM

`materials.llm.base_url` is the non-secret OpenAI-compatible `/v1` endpoint. `model` may be empty for `/models` discovery. Chunk size, timeout, retries, and analysis version are explicit. Put the API key only in an ignored project `.env.local` or process environment:

Prefer HTTPS. A non-local HTTP endpoint sends the bearer key without transport encryption and therefore requires the explicit `allow_insecure_http` acknowledgement; the doctor/report exposes this risk without exposing the key.

```text
DOUYIN_LLM_API_KEY=...
DOUYIN_LLM_BASE_URL=...   # optional override
DOUYIN_LLM_MODEL=...      # optional override
```

Do not place secrets in JSON, Skill files, CLI arguments, reports, or Markdown. Use `llm-doctor` to verify presence and authentication without printing the key.
