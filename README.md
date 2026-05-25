# doxmox

Monitoring repository for:

- `https://pve.proxmox.com/pve-docs/pve-admin-guide.html`

Every 2 hours, GitHub Actions:

1. downloads and normalizes page content,
2. compares it with the previous snapshot,
3. saves a diff on changes,
4. generates an AI text changelog (`en`, `ru`, `fr`) for each diff,
5. updates a static changelog page (`docs/index.html`),
6. sends a Telegram notification if a real change is detected.

## Repository layout

- `src/monitor.py` - page fetch, normalization, diff generation, changelog rendering.
- `data/latest.html` - latest normalized snapshot.
- `data/history.json` - change history for changelog page.
- `data/last_result.json` - ephemeral run payload (not committed).
- `docs/changes/*.diff` - saved diffs.
- `docs/changes/*.changelog.html` - AI summary pages for each change.
- `docs/index.html` - static changelog page (for GitHub Pages).
- `docs/AI_PIPELINE.md` - AI architecture, model choices, RAG flow, and tuning notes.
- `.github/workflows/monitor.yml` - scheduled workflow.
- `.github/workflows/history-ai-backfill.yml` - manual AI summary backfill.
- `.github/workflows/history-ai-translate.yml` - manual translation-only run (`en` -> `ru/fr`).

## Setup

### 1) Enable Actions

Push repository and make sure Actions are enabled.

### 2) Configure notification secrets

In `Settings -> Secrets and variables -> Actions`, add:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DOMAIN` (for example `doxmox.com`)
- `CLOUDFLARE_ACCOUNT_ID` (for Workers AI/Vectorize API calls)
- `CLOUDFLARE_AUTH_TOKEN` (token with Workers AI + Vectorize permissions)
- `CLOUDFLARE_VECTORIZE_INDEX` (optional; enables snapshot context retrieval)

Cloudflare token must be created for the same account as `CLOUDFLARE_ACCOUNT_ID`.
Minimum account-level permissions for this project:

- `Workers AI Edit`
- `Vectorize Edit`

Never commit tokens to git or shell history. If a token is exposed, revoke it immediately and issue a new one.

`DOMAIN` is used to build public links in Telegram notifications:

- diff page: `https://<DOMAIN>/changes/<timestamp>.html`
- changelog page: `https://<DOMAIN>/index.html`

If these secrets are missing, workflow still runs and stores changes, but skips Telegram notifications.

### 3) Create Vectorize index (recommended)

For better AI context quality, create a Vectorize index once:

```bash
wr-dox vectorize create doxmox-admin-guide-index \
  --dimensions=1024 \
  --metric=cosine \
  --description "RAG context for doxmox changelog summaries"
```

Then set:

- `CLOUDFLARE_VECTORIZE_INDEX=doxmox-admin-guide-index`

`--dimensions=1024` and `--metric=cosine` are required for the current embedding model (`@cf/baai/bge-m3`) used by this repository.

### 4) Enable GitHub Pages

In `Settings -> Pages`:

- Source: `Deploy from a branch`
- Branch: your default branch
- Folder: `/docs`

After first successful run, changelog page is served from GitHub Pages.

### 5) Optional custom domain

If you want a custom domain:

1. add `docs/CNAME` with your domain (for example `status.example.com`);
2. configure DNS records at your registrar according to GitHub Pages docs.

## Manual run

Run locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/monitor.py
```

Trigger workflow manually from Actions tab using `workflow_dispatch`.

## Cloudflare multi-account helper (optional)

If you manage multiple Cloudflare accounts, keep token out of shell files and use macOS Keychain:

```bash
security add-generic-password -a "$USER" -s CLOUDFLARE_API_TOKEN_DOX -w 'YOUR_TOKEN' -U
```

Add this helper to `~/.zshrc`:

```bash
wr-dox() {
  local token
  token="$(security find-generic-password -a "$USER" -s CLOUDFLARE_API_TOKEN_DOX -w)" || {
    echo "Token not found in Keychain: CLOUDFLARE_API_TOKEN_DOX" >&2
    return 1
  }

  CLOUDFLARE_ACCOUNT_ID="1a180b334afa0d013606923bd52e7b7c" \
  CLOUDFLARE_API_TOKEN="$token" \
  npx wrangler "$@"
}
```

Check account context:

```bash
wr-dox whoami
wr-dox vectorize list
```

## Manage history entries

You can remove records from `data/history.json` with built-in CLI options:

```bash
# delete one entry by timestamp
python src/monitor.py --history-delete "2026-04-10T08:28:42Z"

# delete by diff filename (basename is supported)
python src/monitor.py --history-delete "20260410T082842Z.diff"

# delete several entries in one command
python src/monitor.py --history-delete "20260410T082842Z.diff" --history-delete "2026-04-08T22:17:17Z"

# delete all history entries
python src/monitor.py --history-delete-all

# optional: also remove linked docs/changes artifacts for removed entries
python src/monitor.py --history-delete "20260410T082842Z.diff" --history-delete-artifacts
```

After deletion, `docs/index.html` is regenerated automatically.

## AI changelog backfill

Rebuild AI text summaries for existing history entries:

```bash
# one entry
python src/monitor.py --ai-enable --history-ai-backfill "20260521T130040Z.diff"

# several entries
python src/monitor.py --ai-enable \
  --history-ai-backfill "20260521T130040Z.diff" \
  --history-ai-backfill "2026-05-20T05:19:06Z"

# all entries
python src/monitor.py --ai-enable --history-ai-backfill-all

# force regenerate even if ai.status=ok
python src/monitor.py --ai-enable --history-ai-backfill-all --history-ai-force
```

Required environment variables for local runs:

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_AUTH_TOKEN`

Optional for Vectorize-based context retrieval:

- `CLOUDFLARE_VECTORIZE_INDEX`

## AI translation-only mode

Use this when English changelog already exists and you only need to (re)generate `ru/fr` translations.

```bash
# one entry
python src/monitor.py --ai-enable --history-ai-translate "20260521T130040Z.diff"

# several entries
python src/monitor.py --ai-enable \
  --history-ai-translate "20260521T130040Z.diff" \
  --history-ai-translate "2026-05-20T05:19:06Z"

# all entries with EN summary
python src/monitor.py --ai-enable --history-ai-translate-all

# force overwrite existing ru/fr
python src/monitor.py --ai-enable --history-ai-translate-all --history-ai-translate-force
```

This mode does not regenerate `summary.en`; it only updates translations and re-renders docs pages.

### Delete from website (safe flow, no tokens in browser)

`docs/index.html` no longer asks for GitHub tokens and does not call GitHub API from the browser.

To delete entries:

1. press `Esc` three times to reveal maintenance tools;
2. tick `Select` checkboxes for entries you want to remove;
3. click `Copy selected` (copies newline-separated selectors);
4. click `Open delete workflow`;
5. in GitHub Actions (`.github/workflows/history-admin-delete.yml`) paste copied selectors into the `selectors` input and run the workflow.

Authentication happens only in GitHub UI/session, not in page JavaScript.
