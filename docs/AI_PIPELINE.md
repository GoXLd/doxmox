# AI Pipeline Design

This document explains how AI changelog generation works in this repository, why specific settings were chosen, and how to operate/tune the system safely.

## Goals

1. Convert large unified diffs into structured release notes.
2. Keep output useful for both operators and newcomers.
3. Support multilingual output (`en`, `ru`, `fr`).
4. Keep latency and token usage bounded.
5. Recover safely from model/API failures without breaking main monitoring flow.

## Runtime Components

The AI pipeline is implemented in [src/monitor.py](/Users/goxld/doxmox/doxmox/src/monitor.py) and uses Cloudflare APIs directly.

1. Workers AI REST API for LLM inference and embeddings.
2. Vectorize for optional retrieval context (RAG-like enrichment).
3. GitHub Actions workflows for scheduled runs and manual backfill.

## Model Selection

Default model configuration is hardcoded at the top of [src/monitor.py](/Users/goxld/doxmox/doxmox/src/monitor.py):

1. Analysis model: `@cf/openai/gpt-oss-120b`
2. Fallback analysis model: `@cf/moonshotai/kimi-k2.6`
3. Translation model: `@cf/zai-org/glm-4.7-flash`
4. Embedding model: `@cf/baai/bge-m3`

Rationale:

1. `gpt-oss-120b` is used for high-quality technical synthesis from noisy diff chunks.
2. `kimi-k2.6` is a robustness fallback when primary JSON generation fails.
3. `glm-4.7-flash` is used only for translation to keep cost/latency lower than full re-analysis per language.
4. `bge-m3` provides multilingual embeddings suitable for documentation retrieval.

## End-to-End Flow

### Scheduled monitor flow

1. Fetch and normalize source HTML.
2. Detect content change via snapshot hash.
3. Build unified diff (`.diff`) when change exists.
4. Optionally index normalized snapshot chunks into Vectorize.
5. Optionally query Vectorize using diff embedding to retrieve relevant source context.
6. Run AI summarization and translations.
7. Persist event payload in `data/history.json` under `event["ai"]`.
8. Render:
   - `docs/changes/<timestamp>.html` (code diff page)
   - `docs/changes/<timestamp>.changelog.html` (AI summary page)
   - `docs/index.html` (table links `Changelog | Code Diff`)

### Decoupled translation-only flow

When `summary.en` already exists, translations can be regenerated independently:

1. Read existing `event["ai"]["summary"]["en"]`.
2. Call translation model only.
3. Update `summary.ru` and `summary.fr` without touching `summary.en`.
4. Re-render changelog pages and index.

This avoids unnecessary full EN changelog regeneration when only translation quality is being fixed.

### Backfill flow

Backfill (`--history-ai-backfill*`) reprocesses existing diff files and refreshes site pages.

1. Select history entries by selector(s) or `all`.
2. Skip entries with `ai.status=ok` unless `--history-ai-force`.
3. Regenerate `event["ai"]` and changelog pages.
4. Always refresh `docs/index.html` and reconcile missing pages, even if `updated=0`.

## Prompting and Output Contract

### Chunked analysis

Large diff text is trimmed/chunked before model calls:

1. `DEFAULT_AI_MAX_DIFF_CHARS = 380000`
2. `DEFAULT_AI_CHUNK_CHARS = 40000`
3. chunk overlap for analysis: `1800` chars

Each chunk produces condensed technical notes. A reduce step then builds final JSON.

### Structured JSON contract

The reduce step expects strict JSON:

1. `overview`
2. `professional_assessment`
3. `newcomer_explainer`
4. `changes[]` with:
   - `title`
   - `details`
   - `impact`
   - `recommended_action`
   - `severity` (`high|medium|low`)

Parser behavior:

1. Markdown code fences are stripped if present.
2. Best-effort JSON fragment extraction is applied.
3. Payload is normalized and capped (`changes[:18]`).
4. If primary model output is invalid, fallback model is used.

## Multilingual Strategy

The pipeline first builds canonical `en` JSON, then translates the same JSON into `ru` and `fr`.

Benefits:

1. Consistent structure/meaning across languages.
2. Lower token usage vs. independent tri-lingual analyses.
3. Reduced drift in technical terminology.

If translation fails, `en` still ships and language pages fall back to available data.

## Vectorize Design

Vectorize is optional but recommended.

### Indexing

Snapshot text is chunked and embedded:

1. `DEFAULT_VECTORIZE_CHUNK_CHARS = 2200`
2. chunk overlap: `300` chars
3. `DEFAULT_VECTORIZE_MAX_CHUNKS = 96`

Each record stores:

1. vector values
2. metadata: `doc_hash`, `chunk_index`, `namespace`, `text` (truncated)

### Retrieval

1. Diff query text is trimmed (`24000` chars) and embedded.
2. Vectorize query uses `topK` (or fallback `count`) with `DEFAULT_VECTORIZE_QUERY_TOP_K = 8`.
3. Retrieved metadata text is deduplicated and injected into reduce prompt as context.

## Configuration Surface

Main flags are defined in [src/monitor.py](/Users/goxld/doxmox/doxmox/src/monitor.py):

1. `--ai-enable`
2. `--ai-analysis-model`
3. `--ai-fallback-model`
4. `--ai-translation-model`
5. `--ai-embedding-model`
6. `--ai-max-diff-chars`
7. `--ai-chunk-chars`
8. `--vectorize-index`
9. `--vectorize-namespace`
10. `--vectorize-chunk-chars`
11. `--vectorize-max-chunks`
12. `--vectorize-query-top-k`

Secrets/env:

1. `CLOUDFLARE_ACCOUNT_ID`
2. `CLOUDFLARE_AUTH_TOKEN`
3. `CLOUDFLARE_VECTORIZE_INDEX` (optional)

Required account token permissions:

1. `Workers AI Edit`
2. `Vectorize Edit`

## Failure Modes and Safety

1. Missing AI credentials: monitor continues; AI is marked skipped/error.
2. Invalid JSON from primary analysis model: automatic fallback model retry.
3. Translation failure: English summary still published.
4. Vectorize unavailable: summarization continues without retrieval context.
5. Backfill with no updated entries: docs still re-render to keep site state consistent.

## Operational Notes

1. `history-ai-backfill.yml` is for manual regeneration of historical entries.
2. `monitor.yml` runs on schedule and commits generated artifacts.
3. GitHub Pages reflects changes only after a commit touches `docs` and is deployed.

## Tuning Guide

Use these levers if quality/cost/latency need adjustment:

1. Increase `--ai-chunk-chars` to reduce call count and cost, at risk of weaker local detail extraction.
2. Decrease `--ai-max-diff-chars` to cap cost on very large updates.
3. Increase `--vectorize-query-top-k` for broader context, at risk of noisier prompts.
4. Disable Vectorize for minimal infra complexity when source context is not needed.
5. Switch `--ai-analysis-model` only with controlled comparison on representative diffs.
