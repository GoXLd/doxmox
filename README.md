# doxmox

Monitoring repository for:

- `https://pve.proxmox.com/pve-docs/pve-admin-guide.html`

Every 8 hours, GitHub Actions:

1. downloads and normalizes page content,
2. compares it with the previous snapshot,
3. saves a diff on changes,
4. updates a static changelog page (`docs/index.html`),
5. sends a Telegram notification if a real change is detected.

## Repository layout

- `src/monitor.py` - page fetch, normalization, diff generation, changelog rendering.
- `data/latest.html` - latest normalized snapshot.
- `data/history.json` - change history for changelog page.
- `data/last_result.json` - ephemeral run payload (not committed).
- `docs/changes/*.diff` - saved diffs.
- `docs/index.html` - static changelog page (for GitHub Pages).
- `.github/workflows/monitor.yml` - scheduled workflow.

## Setup

### 1) Enable Actions

Push repository and make sure Actions are enabled.

### 2) Configure notification secrets

In `Settings -> Secrets and variables -> Actions`, add:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DOMAIN` (for example `doxmox.com`)

`DOMAIN` is used to build public links in Telegram notifications:

- diff page: `https://<DOMAIN>/changes/<timestamp>.html`
- changelog page: `https://<DOMAIN>/index.html`

If these secrets are missing, workflow still runs and stores changes, but skips Telegram notifications.

### 3) Enable GitHub Pages

In `Settings -> Pages`:

- Source: `Deploy from a branch`
- Branch: your default branch
- Folder: `/docs`

After first successful run, changelog page is served from GitHub Pages.

### 4) Optional custom domain

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

### Delete from website (admin-only)

`docs/index.html` now supports admin deletion from the UI:

1. press `Esc` three times to reveal admin controls;
2. click `Admin login`;
3. paste GitHub token;
4. either click `Delete` on one row, or tick multiple `Select` checkboxes and click `Delete selected`.

The page checks GitHub permissions and enables deletion only for users with `admin` access to the repository.

The delete request triggers GitHub workflow:

- `.github/workflows/history-admin-delete.yml`

Token requirements:

- access to this repository;
- API scopes/permissions for repository and Actions (`repo` + `workflow` for classic PAT, or equivalent fine-grained permissions).
