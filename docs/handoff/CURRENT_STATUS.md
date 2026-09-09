# Current status

Updated: 2026-09-09.

## Current task

- Current task: `docs/tasks/2026-09-09-github-private-snapshot.md`.
- State: **completed; private GitHub snapshot published and verified**.
- Repository: `https://github.com/hrbj18/copy_skill`, visibility `PRIVATE`, default branch `main`.
- Boundary: local credentials, Cookie files, browser profiles, models, runtime data, outputs and local integration history remain untracked.

## Reliable current capabilities

- Collect configured Douyin accounts and keyword-search metadata through the project-owned MediaCrawler adapter.
- Preserve raw interaction evidence, consolidate related videos and expose separate heat and delivery rankings.
- Download a collected video's media URL for bounded OCR/ASR enrichment; direct share-link downloading is not yet a standalone workbench flow.
- Build dated Markdown/JSON packages, material manifests and OpenMontage-facing snapshots.
- Discover bounded public-web leads and find specific visual references for a user-selected news script.
- Launch the Windows workbench with the Chinese `.bat`/`.cmd` entrypoints.

## Product limitations

- Autonomous daily-news discovery is not a reliable finished product. The 2026-09-03 source-day package had 17 Douyin and 20 public-web candidates but zero strict public-reader cards.
- That run spent about 29 minutes; six core Douyin searches failed, 24/24 article-detail fetches timed out, and all content/localization model batches failed. Fallback output therefore remained title-like.
- Douyin remains attention evidence rather than factual evidence. Public-web results remain discovery leads unless the existing detail gates succeed.

## Repository evidence

- Initial snapshot commit: `04958e1`.
- `third_party/MediaCrawler` is a submodule at `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`, tracking `https://github.com/NanmiCoder/MediaCrawler.git`.
- Pre-publish verification: 298 pytest cases passed, `compileall` passed, CLI doctor passed for the offline pipeline, and the staged secret-pattern scan returned no matches.
- Doctor intentionally did not inspect authentication; live collection still depends on the local project browser login.

## Next direction

- Freeze expansion of the autonomous news-discovery pipeline until a small keep/stop experiment proves it can beat the current manual workflow on time and usable-news count.
- Prioritize the demonstrated path: accept a selected news script, locate one or two accurate product/company visuals per story, download them into a dated folder, and generate a compact handoff manifest for OpenMontage.
- A future implementation task should add this script-to-material workflow to the workbench without coupling it to the slow full daily-news run.
