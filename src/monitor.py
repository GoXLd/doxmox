#!/usr/bin/env python3
"""Monitor a target web page for content changes and update changelog artifacts."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_URL = "https://pve.proxmox.com/pve-docs/pve-admin-guide.html"
SNAPSHOT_FILE = "latest.html"
STATE_FILE = "state.json"
HISTORY_FILE = "history.json"
DEFAULT_RESULT_FILE = "last_result.json"
DEFAULT_AUTHOR_NAME = "Alexandre VANDEMOORTELE"
DEFAULT_AUTHOR_URL = "https://vande.fr/"


@dataclass
class Result:
    timestamp: str
    url: str
    changed: bool
    bootstrap: bool
    old_hash: str | None
    new_hash: str
    diff_file: str | None
    added_lines: int
    removed_lines: int
    history_count: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "url": self.url,
            "changed": self.changed,
            "bootstrap": self.bootstrap,
            "old_hash": self.old_hash,
            "new_hash": self.new_hash,
            "diff_file": self.diff_file,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "history_count": self.history_count,
            "error": self.error,
        }


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def save_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(payload)


def build_session() -> requests.Session:
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retries)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "doxmox-monitor/1.0 (+github-actions)"})
    return session


def normalize_html(raw_html: str) -> str:
    normalized_input = raw_html.replace("\r\n", "\n").replace("\r", "\n")
    soup = BeautifulSoup(normalized_input, "html.parser")

    # Limit comparison to meaningful document zones to reduce layout/JS noise.
    selected = []
    for section_id in ("header", "toc", "content", "footer"):
        node = soup.find(id=section_id)
        if node:
            selected.append(str(node))

    if selected:
        fragment = "\n".join(selected)
    elif soup.body:
        fragment = str(soup.body)
    else:
        fragment = raw_html

    fragment_soup = BeautifulSoup(fragment, "html.parser")
    for tag in fragment_soup.find_all(["script", "noscript"]):
        tag.decompose()

    text = fragment_soup.prettify()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return text + "\n"


def build_diff(previous: str, current: str) -> tuple[str, int, int]:
    previous_lines = previous.splitlines()
    current_lines = current.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            previous_lines,
            current_lines,
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )

    added = 0
    removed = 0
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1

    return "\n".join(diff_lines) + ("\n" if diff_lines else ""), added, removed


def format_event_row(event: dict[str, Any]) -> str:
    timestamp = event.get("timestamp", "-")
    old_hash = (event.get("old_hash") or "-")[:12]
    new_hash = (event.get("new_hash") or "-")[:12]
    added = event.get("added_lines", 0)
    removed = event.get("removed_lines", 0)
    diff_file = event.get("diff_file")

    if diff_file:
        html_file = diff_html_path(diff_file)
        html_href = docs_href_from_path(html_file)
        raw_href = docs_href_from_path(diff_file)
        link = (
            f'<a href="{html.escape(html_href)}" target="_blank" rel="noopener noreferrer" data-i18n="view">View</a> | '
            f'<a href="{html.escape(raw_href)}" target="_blank" rel="noopener noreferrer" data-i18n="raw">Raw</a>'
        )
    else:
        link = "-"

    return (
        "<tr>"
        f"<td>{timestamp}</td>"
        f"<td><code>{old_hash}</code></td>"
        f"<td><code>{new_hash}</code></td>"
        f"<td>+{added} / -{removed}</td>"
        f"<td>{link}</td>"
        "</tr>"
    )


def docs_href_from_path(path: str) -> str:
    return path[5:] if path.startswith("docs/") else path


def diff_html_path(diff_file: str) -> str:
    return str(Path(diff_file).with_suffix(".html").as_posix())


def resolve_author_url(docs_dir: Path, override: str | None) -> str:
    if override:
        return override

    cname_path = docs_dir / "CNAME"
    if cname_path.exists():
        domain = cname_path.read_text(encoding="utf-8").strip()
        if domain:
            if domain.startswith(("http://", "https://")):
                return domain
            return f"https://{domain}"

    return DEFAULT_AUTHOR_URL


def render_diff_html(diff_path: Path, diff_text: str, author_name: str, author_url: str) -> None:
    generated_at = now_utc_iso()
    title = f"{diff_path.name} - Diff Viewer"
    raw_href = diff_path.name
    lines = []
    for line in diff_text.splitlines():
        css_class = "ctx"
        if line.startswith("--- ") or line.startswith("+++ "):
            css_class = "meta"
        elif line.startswith("@@ "):
            css_class = "hunk"
        elif line.startswith("+"):
            css_class = "add"
        elif line.startswith("-"):
            css_class = "del"

        safe_line = html.escape(line)
        if not safe_line:
            safe_line = " "
        lines.append(f'<span class="line {css_class}">{safe_line}</span>')

    if not lines:
        lines.append('<span class="line ctx" data-i18n="empty_diff">(empty diff)</span>')

    body = "\n".join(lines)
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f3f6fb;
      --card: #ffffff;
      --line: #dbe3ef;
      --text: #0f172a;
      --muted: #475569;
      --meta: #4f46e5;
      --hunk: #b45309;
      --add-bg: rgba(22, 163, 74, 0.13);
      --add: #166534;
      --del-bg: rgba(220, 38, 38, 0.12);
      --del: #991b1b;
      --accent: #0b5cab;
      --control-bg: #ffffff;
      --control-line: #c7d2e0;
      --control-text: #0f172a;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    }}
    :root[data-theme="dark"] {{
      --bg: #0b1020;
      --card: #0f172a;
      --line: #1e293b;
      --text: #dbe7ff;
      --muted: #9fb3d1;
      --meta: #a5b4fc;
      --hunk: #f59e0b;
      --add-bg: rgba(16, 185, 129, 0.18);
      --add: #6ee7b7;
      --del-bg: rgba(248, 113, 113, 0.2);
      --del: #fca5a5;
      --accent: #93c5fd;
      --control-bg: #111827;
      --control-line: #334155;
      --control-text: #dbe7ff;
      --shadow: 0 10px 30px rgba(2, 6, 23, 0.45);
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme]) {{
        --bg: #0b1020;
        --card: #0f172a;
        --line: #1e293b;
        --text: #dbe7ff;
        --muted: #9fb3d1;
        --meta: #a5b4fc;
        --hunk: #f59e0b;
        --add-bg: rgba(16, 185, 129, 0.18);
        --add: #6ee7b7;
        --del-bg: rgba(248, 113, 113, 0.2);
        --del: #fca5a5;
        --accent: #93c5fd;
        --control-bg: #111827;
        --control-line: #334155;
        --control-text: #dbe7ff;
        --shadow: 0 10px 30px rgba(2, 6, 23, 0.45);
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Menlo, Consolas, "Liberation Mono", monospace;
      background: var(--bg);
      color: var(--text);
    }}
    .wrap {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 20px 16px 36px;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
      margin-bottom: 10px;
    }}
    .control {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .control select {{
      border: 1px solid var(--control-line);
      border-radius: 8px;
      background: var(--control-bg);
      color: var(--control-text);
      padding: 6px 8px;
      font-size: 13px;
    }}
    .head {{
      margin-bottom: 12px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--card);
      box-shadow: var(--shadow);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 18px;
    }}
    p {{
      margin: 4px 0;
      color: var(--muted);
      font-size: 13px;
    }}
    a {{
      color: var(--accent);
    }}
    .nav {{
      margin-top: 8px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .diff {{
      margin: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--card);
      box-shadow: var(--shadow);
      overflow: auto;
      line-height: 1.45;
      font-size: 13px;
    }}
    .line {{
      display: block;
      white-space: pre;
    }}
    .line.meta {{ color: var(--meta); }}
    .line.hunk {{ color: var(--hunk); }}
    .line.add {{
      color: var(--add);
      background: var(--add-bg);
    }}
    .line.del {{
      color: var(--del);
      background: var(--del-bg);
    }}
    .footer {{
      margin-top: 16px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="toolbar">
      <label class="control">
        <span data-i18n="language">Language</span>
        <select id="lang-select">
          <option value="en">English</option>
          <option value="fr">Français</option>
          <option value="ru">Русский</option>
        </select>
      </label>
      <label class="control">
        <span data-i18n="theme">Theme</span>
        <select id="theme-select">
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </label>
    </div>
    <div class="head">
      <h1>{html.escape(diff_path.name)}</h1>
      <p><span data-i18n="generated">Generated (UTC):</span> {generated_at}</p>
      <p class="nav">
        <a href="../index.html" data-i18n="back_to_menu">Back to main menu</a>
        <a href="{html.escape(raw_href)}" target="_blank" rel="noopener noreferrer" data-i18n="open_raw_diff">Open raw .diff</a>
      </p>
    </div>
    <pre class="diff">{body}</pre>
    <footer class="footer">
      <span data-i18n="footer_by">Designed and implemented by</span>
      <a href="{html.escape(author_url)}" target="_blank" rel="noopener noreferrer">{html.escape(author_name)}</a>
    </footer>
  </div>
  <script>
    (() => {{
      const LANG_KEY = "doxmox-lang";
      const THEME_KEY = "doxmox-theme";
      const fallbackLang = "en";
      const i18n = {{
        en: {{
          language: "Language",
          theme: "Theme",
          theme_light: "Light",
          theme_dark: "Dark",
          generated: "Generated (UTC):",
          back_to_menu: "Back to main menu",
          open_raw_diff: "Open raw .diff",
          footer_by: "Designed and implemented by",
          empty_diff: "(empty diff)",
          language_en: "English",
          language_fr: "French",
          language_ru: "Russian"
        }},
        fr: {{
          language: "Langue",
          theme: "Theme",
          theme_light: "Clair",
          theme_dark: "Sombre",
          generated: "Généré (UTC) :",
          back_to_menu: "Retour au menu principal",
          open_raw_diff: "Ouvrir le .diff brut",
          footer_by: "Conçu et réalisé par",
          empty_diff: "(diff vide)",
          language_en: "Anglais",
          language_fr: "Français",
          language_ru: "Russe"
        }},
        ru: {{
          language: "Язык",
          theme: "Тема",
          theme_light: "Светлая",
          theme_dark: "Тёмная",
          generated: "Сгенерировано (UTC):",
          back_to_menu: "Назад в главное меню",
          open_raw_diff: "Открыть raw .diff",
          footer_by: "Разработано и реализовано",
          empty_diff: "(пустой diff)",
          language_en: "Английский",
          language_fr: "Французский",
          language_ru: "Русский"
        }}
      }};

      const langSelect = document.getElementById("lang-select");
      const themeSelect = document.getElementById("theme-select");

      function readLang() {{
        const saved = localStorage.getItem(LANG_KEY) || fallbackLang;
        return i18n[saved] ? saved : fallbackLang;
      }}

      function readTheme() {{
        const saved = localStorage.getItem(THEME_KEY);
        if (saved === "dark" || saved === "light") {{
          return saved;
        }}
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }}

      function t(lang, key) {{
        return (i18n[lang] && i18n[lang][key]) || i18n[fallbackLang][key] || key;
      }}

      function applyTheme(theme) {{
        document.documentElement.setAttribute("data-theme", theme);
        themeSelect.value = theme;
      }}

      function applyLanguage(lang) {{
        document.documentElement.lang = lang;
        langSelect.value = lang;
        document.querySelectorAll("[data-i18n]").forEach((node) => {{
          const key = node.getAttribute("data-i18n");
          node.textContent = t(lang, key);
        }});
        const langOptions = {{
          en: "language_en",
          fr: "language_fr",
          ru: "language_ru"
        }};
        for (const option of langSelect.options) {{
          option.textContent = t(lang, langOptions[option.value]);
        }}
        themeSelect.options[0].textContent = t(lang, "theme_light");
        themeSelect.options[1].textContent = t(lang, "theme_dark");
      }}

      const currentLang = readLang();
      const currentTheme = readTheme();
      applyLanguage(currentLang);
      applyTheme(currentTheme);

      langSelect.addEventListener("change", () => {{
        localStorage.setItem(LANG_KEY, langSelect.value);
        applyLanguage(langSelect.value);
      }});

      themeSelect.addEventListener("change", () => {{
        localStorage.setItem(THEME_KEY, themeSelect.value);
        applyTheme(themeSelect.value);
      }});
    }})();
  </script>
</body>
</html>
"""
    save_text(diff_path.with_suffix(".html"), page)


def ensure_diff_html_pages(history: list[dict[str, Any]], author_name: str, author_url: str) -> bool:
    created_any = False
    for event in history:
        diff_file = event.get("diff_file")
        if not diff_file:
            continue
        diff_path = Path(diff_file)
        if not diff_path.exists():
            continue
        html_path = diff_path.with_suffix(".html")
        if html_path.exists():
            continue
        diff_text = diff_path.read_text(encoding="utf-8")
        render_diff_html(diff_path, diff_text, author_name=author_name, author_url=author_url)
        created_any = True
    return created_any


def render_docs(
    history: list[dict[str, Any]],
    url: str,
    docs_dir: Path,
    author_name: str,
    author_url: str,
) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(format_event_row(event) for event in history)
    if not rows:
        rows = '<tr><td colspan="5" data-i18n="no_changes">No changes detected yet.</td></tr>'

    generated_at = now_utc_iso()
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Doxmox Changelog</title>
  <style>
    :root {{
      --bg: #f3f6fb;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --line: #dbe3ef;
      --accent: #0b5cab;
      --accent-soft: #e7f1fd;
      --th-bg: #f8fbff;
      --th-text: #1e293b;
      --control-bg: #ffffff;
      --control-line: #c7d2e0;
      --control-text: #0f172a;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    }}
    :root[data-theme="dark"] {{
      --bg: #0b1020;
      --card: #0f172a;
      --text: #dbe7ff;
      --muted: #9fb3d1;
      --line: #1e293b;
      --accent: #93c5fd;
      --accent-soft: #111827;
      --th-bg: #111827;
      --th-text: #dbe7ff;
      --control-bg: #111827;
      --control-line: #334155;
      --control-text: #dbe7ff;
      --shadow: 0 10px 30px rgba(2, 6, 23, 0.45);
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme]) {{
        --bg: #0b1020;
        --card: #0f172a;
        --text: #dbe7ff;
        --muted: #9fb3d1;
        --line: #1e293b;
        --accent: #93c5fd;
        --accent-soft: #111827;
        --th-bg: #111827;
        --th-text: #dbe7ff;
        --control-bg: #111827;
        --control-line: #334155;
        --control-text: #dbe7ff;
        --shadow: 0 10px 30px rgba(2, 6, 23, 0.45);
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Tahoma, sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    .wrap {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 24px 16px 48px;
    }}
    .toolbar {{
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }}
    .control {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .control select {{
      border: 1px solid var(--control-line);
      border-radius: 8px;
      background: var(--control-bg);
      color: var(--control-text);
      padding: 6px 8px;
      font-size: 13px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .head {{
      padding: 20px;
      background: var(--accent-soft);
      border-bottom: 1px solid var(--line);
    }}
    .telegram-subscribe {{
      margin-top: 8px;
    }}
    .telegram-subscribe a {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 600;
      text-decoration: none;
    }}
    .telegram-subscribe svg {{
      width: 16px;
      height: 16px;
      display: block;
      fill: currentColor;
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ margin: 4px 0; color: var(--muted); }}
    a {{ color: var(--accent); }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      text-align: left;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      background: var(--th-bg);
      color: var(--th-text);
      font-weight: 600;
      position: sticky;
      top: 0;
    }}
    code {{ font-size: 12px; }}
    @media (max-width: 860px) {{
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--line); }}
      td {{
        border: 0;
        padding: 8px 14px;
      }}
    }}
    .footer {{
      margin-top: 16px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="toolbar">
      <label class="control">
        <span data-i18n="language">Language</span>
        <select id="lang-select">
          <option value="en">English</option>
          <option value="fr">Français</option>
          <option value="ru">Русский</option>
        </select>
      </label>
      <label class="control">
        <span data-i18n="theme">Theme</span>
        <select id="theme-select">
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </label>
    </div>
    <div class="card">
      <div class="head">
        <h1 data-i18n="title">Proxmox VE Admin Guide Changelog</h1>
        <p><span data-i18n="source">Source:</span> <a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">{html.escape(url)}</a></p>
        <p><span data-i18n="generated">Generated (UTC):</span> {generated_at}</p>
        <p class="telegram-subscribe">
          <a href="https://t.me/proxmox_update" target="_blank" rel="noopener noreferrer">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M21.94 4.79a1.5 1.5 0 0 0-1.66-.24L3.54 11.28a1.5 1.5 0 0 0 .12 2.8l3.96 1.33 1.33 3.96a1.5 1.5 0 0 0 2.8.12l6.73-16.74a1.5 1.5 0 0 0-.24-1.66 1.5 1.5 0 0 0-1.66-.24L9.74 12.26l2 2a1 1 0 1 1-1.42 1.42l-2-2L18.8 6.2l-8.42 8.42a1 1 0 0 0-.24.39l-.88 2.61-.76-2.25a1 1 0 0 0-.63-.63l-2.25-.76 2.61-.88a1 1 0 0 0 .39-.24L19.04 4.2l-7.48 10.48 2 2a1 1 0 0 1-1.42 1.42l-2-2-7.41 7.41a1.5 1.5 0 0 0 2.34 1.83l16.74-6.73a1.5 1.5 0 0 0 .24-1.66 1.5 1.5 0 0 0-.24-1.66Z"></path>
            </svg>
            <span data-i18n="subscribe_telegram">Subscribe on Telegram</span>
          </a>
        </p>
      </div>
      <table>
        <thead>
          <tr>
            <th data-i18n="timestamp">Timestamp (UTC)</th>
            <th data-i18n="old_hash">Old Hash</th>
            <th data-i18n="new_hash">New Hash</th>
            <th data-i18n="line_delta">Line Delta</th>
            <th data-i18n="details">Details</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>
    <footer class="footer">
      <span data-i18n="footer_by">Designed and implemented by</span>
      <a href="{html.escape(author_url)}" target="_blank" rel="noopener noreferrer">{html.escape(author_name)}</a>
    </footer>
  </div>
  <script>
    (() => {{
      const LANG_KEY = "doxmox-lang";
      const THEME_KEY = "doxmox-theme";
      const fallbackLang = "en";
      const i18n = {{
        en: {{
          title: "Proxmox VE Admin Guide Changelog",
          source: "Source:",
          generated: "Generated (UTC):",
          timestamp: "Timestamp (UTC)",
          old_hash: "Old Hash",
          new_hash: "New Hash",
          line_delta: "Line Delta",
          details: "Details",
          view: "View",
          raw: "Raw",
          no_changes: "No changes detected yet.",
          language: "Language",
          theme: "Theme",
          theme_light: "Light",
          theme_dark: "Dark",
          footer_by: "Designed and implemented by",
          subscribe_telegram: "Subscribe on Telegram",
          language_en: "English",
          language_fr: "French",
          language_ru: "Russian"
        }},
        fr: {{
          title: "Journal des changements du guide Proxmox VE Admin",
          source: "Source :",
          generated: "Généré (UTC) :",
          timestamp: "Horodatage (UTC)",
          old_hash: "Ancien hash",
          new_hash: "Nouveau hash",
          line_delta: "Delta de lignes",
          details: "Details",
          view: "Voir",
          raw: "Brut",
          no_changes: "Aucun changement detecte pour le moment.",
          language: "Langue",
          theme: "Theme",
          theme_light: "Clair",
          theme_dark: "Sombre",
          footer_by: "Conçu et réalisé par",
          subscribe_telegram: "S'abonner sur Telegram",
          language_en: "Anglais",
          language_fr: "Français",
          language_ru: "Russe"
        }},
        ru: {{
          title: "Журнал изменений руководства Proxmox VE Admin",
          source: "Источник:",
          generated: "Сгенерировано (UTC):",
          timestamp: "Временная метка (UTC)",
          old_hash: "Старый хэш",
          new_hash: "Новый хэш",
          line_delta: "Изменение строк",
          details: "Детали",
          view: "Открыть",
          raw: "Raw",
          no_changes: "Изменения пока не обнаружены.",
          language: "Язык",
          theme: "Тема",
          theme_light: "Светлая",
          theme_dark: "Тёмная",
          footer_by: "Разработано и реализовано",
          subscribe_telegram: "Подписаться в Telegram",
          language_en: "Английский",
          language_fr: "Французский",
          language_ru: "Русский"
        }}
      }};

      const langSelect = document.getElementById("lang-select");
      const themeSelect = document.getElementById("theme-select");

      function readLang() {{
        const saved = localStorage.getItem(LANG_KEY) || fallbackLang;
        return i18n[saved] ? saved : fallbackLang;
      }}

      function readTheme() {{
        const saved = localStorage.getItem(THEME_KEY);
        if (saved === "dark" || saved === "light") {{
          return saved;
        }}
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }}

      function t(lang, key) {{
        return (i18n[lang] && i18n[lang][key]) || i18n[fallbackLang][key] || key;
      }}

      function applyTheme(theme) {{
        document.documentElement.setAttribute("data-theme", theme);
        themeSelect.value = theme;
      }}

      function applyLanguage(lang) {{
        document.documentElement.lang = lang;
        langSelect.value = lang;
        document.querySelectorAll("[data-i18n]").forEach((node) => {{
          const key = node.getAttribute("data-i18n");
          node.textContent = t(lang, key);
        }});
        const langOptions = {{
          en: "language_en",
          fr: "language_fr",
          ru: "language_ru"
        }};
        for (const option of langSelect.options) {{
          option.textContent = t(lang, langOptions[option.value]);
        }}
        themeSelect.options[0].textContent = t(lang, "theme_light");
        themeSelect.options[1].textContent = t(lang, "theme_dark");
      }}

      const currentLang = readLang();
      const currentTheme = readTheme();
      applyLanguage(currentLang);
      applyTheme(currentTheme);

      langSelect.addEventListener("change", () => {{
        localStorage.setItem(LANG_KEY, langSelect.value);
        applyLanguage(langSelect.value);
      }});

      themeSelect.addEventListener("change", () => {{
        localStorage.setItem(THEME_KEY, themeSelect.value);
        applyTheme(themeSelect.value);
      }});
    }})();
  </script>
</body>
</html>
"""
    save_text(docs_dir / "index.html", page)


def process(
    url: str,
    state_dir: Path,
    changes_dir: Path,
    docs_dir: Path,
    result_file: Path,
    author_name: str,
    author_url: str | None,
) -> int:
    timestamp = now_utc_iso()
    state_dir.mkdir(parents=True, exist_ok=True)
    changes_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = state_dir / SNAPSHOT_FILE
    state_path = state_dir / STATE_FILE
    history_path = state_dir / HISTORY_FILE
    resolved_author_url = resolve_author_url(docs_dir, author_url)

    try:
        session = build_session()
        response = session.get(url, timeout=(10, 90))
        response.raise_for_status()
        raw_html = response.content.decode("utf-8", errors="replace")
        normalized = normalize_html(raw_html)
    except Exception as exc:
        result = Result(
            timestamp=timestamp,
            url=url,
            changed=False,
            bootstrap=False,
            old_hash=None,
            new_hash="",
            diff_file=None,
            added_lines=0,
            removed_lines=0,
            history_count=0,
            error=str(exc),
        )
        save_json(result_file, result.to_dict())
        return 1

    new_hash = sha256_text(normalized)
    old_snapshot = snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else None
    old_hash = sha256_text(old_snapshot) if old_snapshot is not None else None
    bootstrap = old_snapshot is None
    changed = old_snapshot is None or old_hash != new_hash
    history: list[dict[str, Any]] = load_json(history_path, [])

    diff_file: str | None = None
    added_lines = 0
    removed_lines = 0

    if changed:
        save_text(snapshot_path, normalized)
        save_json(
            state_path,
            {
                "url": url,
                "hash": new_hash,
                "updated_at": timestamp,
            },
        )

        if not bootstrap and old_snapshot is not None:
            diff_text, added_lines, removed_lines = build_diff(old_snapshot, normalized)
            ts_for_file = timestamp.replace(":", "").replace("-", "").replace("Z", "").replace("T", "T")
            diff_path = changes_dir / f"{ts_for_file}Z.diff"
            save_text(diff_path, diff_text)
            render_diff_html(
                diff_path,
                diff_text,
                author_name=author_name,
                author_url=resolved_author_url,
            )
            diff_file = str(diff_path.as_posix())

            event = {
                "timestamp": timestamp,
                "old_hash": old_hash,
                "new_hash": new_hash,
                "diff_file": diff_file,
                "added_lines": added_lines,
                "removed_lines": removed_lines,
            }
            history.insert(0, event)
            save_json(history_path, history)
        elif bootstrap:
            save_json(history_path, history)

    created_diff_pages = ensure_diff_html_pages(
        history,
        author_name=author_name,
        author_url=resolved_author_url,
    )
    index_path = docs_dir / "index.html"
    if changed or created_diff_pages or not index_path.exists():
        render_docs(
            history,
            url,
            docs_dir,
            author_name=author_name,
            author_url=resolved_author_url,
        )

    result = Result(
        timestamp=timestamp,
        url=url,
        changed=changed,
        bootstrap=bootstrap,
        old_hash=old_hash,
        new_hash=new_hash,
        diff_file=diff_file,
        added_lines=added_lines,
        removed_lines=removed_lines,
        history_count=len(history),
        error=None,
    )
    save_json(result_file, result.to_dict())
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--state-dir", default="data")
    parser.add_argument("--changes-dir", default="docs/changes")
    parser.add_argument("--docs-dir", default="docs")
    parser.add_argument("--result-file", default=f"data/{DEFAULT_RESULT_FILE}")
    parser.add_argument("--author-name", default=DEFAULT_AUTHOR_NAME)
    parser.add_argument("--author-url", default=DEFAULT_AUTHOR_URL)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return process(
        url=args.url,
        state_dir=Path(args.state_dir),
        changes_dir=Path(args.changes_dir),
        docs_dir=Path(args.docs_dir),
        result_file=Path(args.result_file),
        author_name=args.author_name,
        author_url=args.author_url,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
