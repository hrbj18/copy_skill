# Stable product rules

## Daily Douyin technology candidate pool

- V2 uses approved plus enabled `topic_radar`, then terms; ordinary candidates never enter. Generic platform, ecosystem, campaign and broad-skill tags cannot establish story identity; same-story merge needs a specific product/version or shared entity/date/specific phrase.
- Dedicated game searches are forbidden. Pure game release/review/guide/trial events are excluded before OCR/model/image work; ordinary metaphors such as “改变游戏规则” survive. Exclusions remain auditable and raw videos are preserved.
- Every candidate remains marked “copy_skill 未核验真实性，候选仅供 OP 选题与后续核验。” Facts and images never affect heat or delivery priority.
- `heat_score`/`heat_rank` use effective interactions, freshness and group-capped video/source/dual-lane coverage. Matrix posts and raw counts remain; only each group's highest-interaction video affects heat.
- `delivery_priority_score`/`delivery_rank` deterministically combine normalized heat 45%, strategic significance 25%, public relevance 20% and discussion value 10%, then penalize risk, tutorial promotion and vague titles. They use original title anchors only, never LLM summaries, facts, images or human fields; `rank` aliases `delivery_rank`.
- Only delivery rank 1-3 get bounded review-required images; rank 4+ gets zero. `partial` is immutable; consumers use `current/latest/READY/manifest` and OP verifies facts.
- Same-date `current` never regresses: candidates, target-day videos then Top3 images decide; rejected runs record outcome. No normal-workbench override.

## Douyin technology hotspot ranking

- Principle: **retain heat first, route by type second, gate facts last**. Source scarcity limits wording and readiness, never heat or creator-reference value.
- Heat, factual credibility and creative value are separate. A qualifying public-metadata topic remains in the unified ranking without an external news match.
- `verified_news` meets the official/multi-source threshold; evidence changes labels and wording, never heat. `creator_original` proves creator publication, not external claims. `unverified_claim` remains visible with a warning.
- Reports retain unified/evidence/type views, ordinary links and public interactions.

## Dual-track editorial board

- Every hotspot has independent heat, content-type, evidence and editorial-status fields. Heat ranks are immutable under official-source changes, human type overrides or optional model analysis.
- Content types are `news_lead`, creator review/experiment/tutorial/opinion, `mixed` and `uncertain`. Evidence is official/multi-source, creator-primary, unverified, conflicting or insufficient; type overrides cannot promote it. Editorial status keeps ready/research/rumor/manual/recoverable-ignore distinctions.
- Both exports share ranked records. Unverified news stays available for research; creator material stays available for tech talk while external claims remain in `claims_to_verify` and `do_not_claim`.
- Human overrides live in an atomic, project-owned state file keyed by stable topic ID. They never modify crawler artifacts and must support type routing, ignore/restore, pin/video-candidate flags and bounded notes with concurrent-write protection.

## Trusted news accounts

- User-curated accounts form a separate configured lane. `trusted_creator` may enter its account ranking without per-item corroboration, but never becomes `verified_official`; popularity cannot upgrade trust.
- Collection uses stable account ID, Beijing-time window and hard cap. Reports retain source wording and `claims_to_verify`; LLM supplies neither facts nor rank.
- Captions precede description, then bounded temporary ASR/OCR. Authentication stops collection; media and signed URLs are deleted. Model transport defaults to verified HTTPS; opt-in HTTP keeps a warning.

## Material replication workflow

- Theme → 3~6 in-domain keywords; folder `MM.DD<主题>复刻视频` (month not zero-padded, theme sanitized ≤12 chars) published atomically, never half-overwritten. Every folder carries a human `汇报文档.md` (`AGENTS.md`).
- The signed `video_download_url` is captured in memory before sanitization and never persisted; the pool keeps only `video_id`/`share_url`/`media_url_present`.
- `heat_score` = pool-normalized `digg+3*comment+5*share+4*collect` (+0.05*play when present); ties break by `(-score,-duration,video_id)`.
- Script replica: exactly one, top-10% heat (≥Top5), 30~300s, ASR ≥150 chars at ≥1.2 chars/s, face-gate exempt.
- Material replicas keep theme subject terms, freshness, technical validity, duplicate control and low-speech / visual checks as admission gates. Within that qualified pool, ordering is deterministic: theme hit → event directness → profile source/role bonus → heat → duration → `video_id`; author diversity remains and source diversity is a soft preference. `face_class` is descriptive only (never admission/main/legality), so `face_heavy`, hosts, interviewees and event subjects may ship.
- Theme, not source kind, comes first. Three explicit profiles (`person_or_company_event`, `official_notice_or_security_event`, `product_or_industry_trend`) choose source/role preferences without altering theme keywords, subject terms or event terms. `official_original` earns a bonus, yet hot on-theme `creator_commentary` can be main.
- Slice delivery uses deterministic 3~8s source-timeline intervals rather than face-free intervals, preventing a themed news/interview segment from being silently erased because people are on screen. Face detection remains existence-only: no embeddings, crops or identity storage.
- Deliveries add `00-素材目录.json`: one safe relative-path row per physical `02-主素材` / `03-辅助素材` entity, containing bytes, public candidate provenance, deterministic source/role/rights labels, score, duration and `face_class`. Signed download URLs, absolute / `..` paths, duplicate entities and uncovered manifest media fail validation. The complete folder caps at `200,000,000 bytes` and is blocked pre-publish. Multi-source resolve exists (`CompositeMediaResolver`); the former gap was labels, not dispatch.

## Resource and lifecycle guarantees

- Duplicate jobs use atomic locks; stale locks are recovered without signals. State exposes bounded progress/errors.
- Temporary media is deleted by default under TTL/quota; durable reports and approved caches remain.
- Network, browser, model, retry, concurrency and collection volumes are finite/configurable.

## Security and integration boundaries

- Secrets come only from ignored local environment state and never appear in committed config, reports, logs, task XML, fixtures, or diagnostics.
- `third_party/MediaCrawler` remains unmodified upstream code.
- OpenMontage is not modified without explicit authorization; this project produces stable integration snapshots.
