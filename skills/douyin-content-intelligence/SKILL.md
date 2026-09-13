---
name: douyin-content-intelligence
description: Collect, validate, rank, transiently process, transcribe, OCR, semantically analyze, and export Douyin competitor-account and technology-topic intelligence as storage-bounded Markdown material packages. Use for benchmark-account collection, dedicated Chrome login, MediaCrawler diagnosis, high-value copy generation, LLM analysis, or temporary-media cleanup. Do not use for ordinary video rendering or unsourced factual news writing.
---

# Douyin Content Intelligence

Supply topic and heat signals to OpenMontage without treating competitor scripts as factual evidence.

## Route the request

- Environment or login diagnosis: run `python scripts/run.py doctor` from this skill directory. This is offline and does not read Cookie values.
- Configure accounts or categories: edit the project-level `config/content_intelligence.json`; read [configuration.md](references/configuration.md) first.
- Inspect collection commands: run `python scripts/run.py crawl-plan --mode creator|search|all`.
- Start a real collection only when the user requested it and browser use is authorized: run `python scripts/run.py crawl --mode creator|search --allow-browser`. Stop on QR login, CAPTCHA, phone verification, or account-risk prompts and let the user complete them.
- Preferred end-to-end project workflow: run `python scripts/run.py collect-materials --allow-browser`. It collects and validates each account, processes only ranked Top N videos sequentially, creates checkpointed local transcripts, OCRs no-speech videos, performs cached chunked LLM analysis, deletes temporary media, and writes rich Markdown.
- To separate stages, use `collect-creators --allow-browser [--run-id ID]`, then `build-materials --run-dir PATH`. `--keep-video` is opt-in for a downstream-selected original. Use `--skip-transcription` or `--skip-analysis` only for diagnosis or explicit degraded output.
- LLM diagnosis: run `python scripts/run.py llm-doctor`. It may call the configured `/models` endpoint but reports only secret presence and status, never the key.
- Storage maintenance: run `python scripts/run.py cleanup-temp`; it deletes only under the configured project temp root according to TTL and quota.
- Process existing MediaCrawler output: use `normalize`, `run`, or `export-openmontage`; read [data-contract.md](references/data-contract.md) when adapting a new input shape or downstream consumer.

## Invariants

- Keep MediaCrawler isolated at `third_party/MediaCrawler`; do not copy or patch its internals for ordinary adaptations.
- Never place API keys, Cookie values, browser profiles, or login material in Skill files, commands shown to the user, logs, fixtures, or versioned config.
- Prefer persistent dedicated Chrome state or QR login. Do not promise permanent cookie-free operation.
- Treat Douyin titles, engagement and transcripts as discovery/ranking signals. OpenMontage must obtain original-site evidence before freezing factual claims.
- Do not write directly into OpenMontage unless the user explicitly authorizes that mutation. Default output stays under this project's `output/openmontage/<date>/`.
- Use deterministic metadata scoring before any media or LLM work. LLM analysis runs only on the selected Top N and must be visible in the report.
- Signed media URLs are ephemeral secrets of the collection response: use them only for immediate download and never copy them into Markdown, reports, Skill files, or logs.
- MP4, extracted audio, and OCR frames are temporary by default. Keep transcript/OCR/analysis checkpoints and Markdown; retain an MP4 only with explicit `--keep-video`.
- A successful material run requires valid ffprobe metadata, a successful checkpointed transcript or successful OCR for no-speech content, and successful semantic analysis when LLM is enabled. Long audio and transcripts are processed sequentially in bounded chunks.
- Read [configuration.md](references/configuration.md) before changing retention or LLM settings. API keys belong only in `DOUYIN_LLM_API_KEY` or ignored `.env.local`.
- Preserve the previous successful output when a run fails; exporter writes atomically.
- The upstream MediaCrawler license is non-commercial learning/research. Flag this before commercial deployment or redistribution.
- Never read the append-only project development process document as task context. Use the current code, config, and the new task-specific guide instead.

## Completion checks

Run the project tests, validate this Skill with the bundled `quick_validate.py`, confirm the temp directory is within quota, and verify generated reports contain no API key or signed media URL.
