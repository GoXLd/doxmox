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

### 2) Configure Telegram secrets

In `Settings -> Secrets and variables -> Actions`, add:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

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
