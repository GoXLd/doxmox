#!/usr/bin/env python3
"""Monitor a target web page for content changes and update changelog artifacts."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
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
DEFAULT_AUTHOR_NAME = "GoXLd"
DEFAULT_AUTHOR_URL = "https://vande.fr/posts/doxmox/"
DEFAULT_GITHUB_REPO = "GoXLd/doxmox"
DEFAULT_GITHUB_REF = "main"
DEFAULT_GITHUB_ADMIN_WORKFLOW = "history-admin-delete.yml"
DEFAULT_AI_ANALYSIS_MODEL = "@cf/openai/gpt-oss-120b"
DEFAULT_AI_TRANSLATION_MODEL = "@cf/zai-org/glm-4.7-flash"
DEFAULT_AI_EMBEDDING_MODEL = "@cf/baai/bge-m3"
DEFAULT_AI_FALLBACK_MODEL = "@cf/moonshotai/kimi-k2.6"
DEFAULT_AI_MAX_DIFF_CHARS = 380_000
DEFAULT_AI_CHUNK_CHARS = 40_000
DEFAULT_VECTORIZE_MAX_CHUNKS = 96
DEFAULT_VECTORIZE_CHUNK_CHARS = 2_200
DEFAULT_VECTORIZE_QUERY_TOP_K = 8
DEFAULT_CLOUDFLARE_ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
DEFAULT_CLOUDFLARE_TOKEN_ENV = "CLOUDFLARE_AUTH_TOKEN"


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


@dataclass
class AIConfig:
    enabled: bool
    account_id: str | None
    api_token: str | None
    analysis_model: str
    fallback_model: str
    translation_model: str
    embedding_model: str
    max_diff_chars: int
    chunk_chars: int
    vectorize_index: str | None
    vectorize_namespace: str
    vectorize_chunk_chars: int
    vectorize_max_chunks: int
    vectorize_query_top_k: int


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_utc_display(timestamp: str) -> str:
    """Render ISO UTC timestamp as explicit 24-hour display string."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return timestamp


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


def split_text_chunks(text: str, chunk_size: int, overlap: int = 0) -> list[str]:
    if chunk_size <= 0:
        return [text] if text else []
    if overlap < 0:
        overlap = 0
    if overlap >= chunk_size:
        overlap = chunk_size // 4

    parts: list[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        parts.append(text[start:end])
        if end >= text_len:
            break
        start = end - overlap
    return parts


def strip_code_fences(payload: str) -> str:
    text = payload.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def extract_json_fragment(payload: str) -> str | None:
    text = strip_code_fences(payload)
    if not text:
        return None
    if text.startswith("{") and text.endswith("}"):
        return text
    if text.startswith("[") and text.endswith("]"):
        return text

    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and obj_end != -1 and obj_start < obj_end:
        return text[obj_start : obj_end + 1]

    arr_start = text.find("[")
    arr_end = text.rfind("]")
    if arr_start != -1 and arr_end != -1 and arr_start < arr_end:
        return text[arr_start : arr_end + 1]
    return None


def parse_json_payload(payload: str) -> Any | None:
    fragment = extract_json_fragment(payload)
    if not fragment:
        return None
    try:
        return json.loads(fragment)
    except json.JSONDecodeError:
        return None


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


def trim_for_prompt(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return f"{text[:head]}\n\n...[truncated for token budget]...\n\n{text[-tail:]}"


def ai_messages(system: str, user: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def workers_ai_run(
    session: requests.Session,
    account_id: str,
    api_token: str,
    model: str,
    payload: dict[str, Any],
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    response = session.post(url, headers=headers, json=payload, timeout=(12, timeout_seconds))
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("success") is False:
        errors = data.get("errors") or []
        details = "; ".join(str(item.get("message", item)) for item in errors if isinstance(item, dict))
        raise RuntimeError(details or "Workers AI request failed")
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Workers AI response payload")
    return data


def workers_ai_extract_text(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        response = result.get("response")
        if isinstance(response, str):
            return response
        if isinstance(response, list):
            return "\n".join(str(item) for item in response)
        output_text = result.get("output_text")
        if isinstance(output_text, str):
            return output_text
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                text = first.get("text")
                if isinstance(text, str):
                    return text
    response = payload.get("response")
    if isinstance(response, str):
        return response
    return json.dumps(payload, ensure_ascii=False)


def workers_ai_extract_embeddings(payload: dict[str, Any]) -> list[list[float]]:
    result = payload.get("result")
    candidates: list[Any] = []
    if isinstance(result, dict):
        candidates.append(result.get("data"))
        candidates.append(result.get("response"))
        candidates.append(result.get("embeddings"))
    candidates.append(result)

    for candidate in candidates:
        if isinstance(candidate, list):
            if candidate and all(isinstance(x, (int, float)) for x in candidate):
                return [[float(x) for x in candidate]]
            vectors: list[list[float]] = []
            for item in candidate:
                if isinstance(item, dict):
                    emb = item.get("embedding") or item.get("values")
                    if isinstance(emb, list) and all(isinstance(x, (int, float)) for x in emb):
                        vectors.append([float(x) for x in emb])
                elif isinstance(item, list) and all(isinstance(x, (int, float)) for x in item):
                    vectors.append([float(x) for x in item])
            if vectors:
                return vectors
    return []


def workers_ai_chat_text(
    session: requests.Session,
    config: AIConfig,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 1400,
) -> str:
    if not config.account_id or not config.api_token:
        raise RuntimeError("Cloudflare AI credentials are not configured")
    payload = {
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "stream": False,
        "temperature": 0.2,
    }
    result = workers_ai_run(session, config.account_id, config.api_token, model, payload)
    return workers_ai_extract_text(result).strip()


def workers_ai_embeddings(
    session: requests.Session,
    config: AIConfig,
    texts: list[str],
) -> list[list[float]]:
    if not texts:
        return []
    if not config.account_id or not config.api_token:
        raise RuntimeError("Cloudflare AI credentials are not configured")
    payload = {"text": texts}
    result = workers_ai_run(session, config.account_id, config.api_token, config.embedding_model, payload)
    vectors = workers_ai_extract_embeddings(result)
    if vectors:
        return vectors

    fallback_vectors: list[list[float]] = []
    for text in texts:
        single = workers_ai_run(
            session,
            config.account_id,
            config.api_token,
            config.embedding_model,
            {"text": [text]},
            timeout_seconds=120,
        )
        parsed = workers_ai_extract_embeddings(single)
        if not parsed:
            raise RuntimeError("Unable to parse embedding response payload")
        fallback_vectors.append(parsed[0])
    return fallback_vectors


def vectorize_upsert(
    session: requests.Session,
    config: AIConfig,
    records: list[dict[str, Any]],
) -> None:
    if not config.account_id or not config.api_token or not config.vectorize_index:
        return
    if not records:
        return
    endpoint = (
        f"https://api.cloudflare.com/client/v4/accounts/{config.account_id}"
        f"/vectorize/v2/indexes/{config.vectorize_index}/upsert"
    )
    headers = {
        "Authorization": f"Bearer {config.api_token}",
        "Content-Type": "application/x-ndjson",
    }
    payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    response = session.post(endpoint, headers=headers, data=payload.encode("utf-8"), timeout=(10, 240))
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and body.get("success") is False:
        raise RuntimeError("Vectorize upsert failed")


def vectorize_query_context(
    session: requests.Session,
    config: AIConfig,
    query_vector: list[float],
) -> list[dict[str, Any]]:
    if not config.account_id or not config.api_token or not config.vectorize_index:
        return []
    endpoint = (
        f"https://api.cloudflare.com/client/v4/accounts/{config.account_id}"
        f"/vectorize/v2/indexes/{config.vectorize_index}/query"
    )
    headers = {
        "Authorization": f"Bearer {config.api_token}",
        "Content-Type": "application/json",
    }
    payload = {"vector": query_vector, "topK": config.vectorize_query_top_k, "returnMetadata": "all"}
    response = session.post(endpoint, headers=headers, json=payload, timeout=(10, 120))
    if response.status_code >= 400:
        # Fallback for API variants using count instead of topK.
        payload = {"vector": query_vector, "count": config.vectorize_query_top_k}
        response = session.post(endpoint, headers=headers, json=payload, timeout=(10, 120))
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        return []
    result = body.get("result")
    if isinstance(result, dict):
        matches = result.get("matches")
        if isinstance(matches, list):
            return [match for match in matches if isinstance(match, dict)]
    return []


def build_vectorize_records(
    chunks: list[str],
    vectors: list[list[float]],
    snapshot_hash: str,
    namespace: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for idx, (chunk, vector) in enumerate(zip(chunks, vectors, strict=False)):
        chunk_id = hashlib.sha1(f"{snapshot_hash}:{idx}:{chunk[:128]}".encode("utf-8")).hexdigest()
        records.append(
            {
                "id": chunk_id,
                "values": vector,
                "metadata": {
                    "doc_hash": snapshot_hash,
                    "chunk_index": idx,
                    "namespace": namespace,
                    "text": chunk[:1600],
                },
            }
        )
    return records


def extract_context_texts(matches: list[dict[str, Any]]) -> list[str]:
    collected: list[str] = []
    seen = set()
    for match in matches:
        metadata = match.get("metadata")
        if not isinstance(metadata, dict):
            continue
        text = metadata.get("text")
        if not isinstance(text, str):
            continue
        trimmed = text.strip()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        collected.append(trimmed)
    return collected


def format_changes_text(changes: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for idx, item in enumerate(changes, start=1):
        title = str(item.get("title") or "").strip()
        details = str(item.get("details") or "").strip()
        impact = str(item.get("impact") or "").strip()
        action = str(item.get("recommended_action") or "").strip()
        severity = str(item.get("severity") or "").strip()
        if title:
            lines.append(f"{idx}. {title}")
        if details:
            lines.append(f"   - {details}")
        if impact:
            lines.append(f"   - Impact: {impact}")
        if action:
            lines.append(f"   - Action: {action}")
        if severity:
            lines.append(f"   - Severity: {severity}")
    return "\n".join(lines).strip()


def normalize_summary_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    changes = payload.get("changes")
    normalized_changes: list[dict[str, Any]] = []
    if isinstance(changes, list):
        for item in changes[:18]:
            if isinstance(item, dict):
                normalized_changes.append(
                    {
                        "title": str(item.get("title") or "").strip(),
                        "details": str(item.get("details") or "").strip(),
                        "impact": str(item.get("impact") or "").strip(),
                        "recommended_action": str(item.get("recommended_action") or "").strip(),
                        "severity": str(item.get("severity") or "").strip(),
                    }
                )
    return {
        "overview": str(payload.get("overview") or "").strip(),
        "professional_assessment": str(payload.get("professional_assessment") or "").strip(),
        "newcomer_explainer": str(payload.get("newcomer_explainer") or "").strip(),
        "changes": normalized_changes,
    }


def summarize_diff_with_ai(
    session: requests.Session,
    config: AIConfig,
    diff_text: str,
    source_context: list[str],
) -> dict[str, Any]:
    if not config.enabled:
        return {"status": "skipped", "reason": "ai_disabled"}
    if not config.account_id or not config.api_token:
        return {"status": "skipped", "reason": "missing_credentials"}

    trimmed_diff = trim_for_prompt(diff_text, config.max_diff_chars)
    chunks = split_text_chunks(trimmed_diff, chunk_size=config.chunk_chars, overlap=1800)
    chunk_notes: list[str] = []

    system = (
        "You are a senior Proxmox VE technical editor. "
        "Identify meaningful behavior/documentation changes from diff chunks. "
        "Write concise, precise technical notes."
    )
    for idx, chunk in enumerate(chunks, start=1):
        user = (
            f"Chunk {idx}/{len(chunks)} from a unified diff.\n"
            "Return short bullet notes of significant changes only. "
            "Ignore cosmetic whitespace.\n\n"
            f"{chunk}"
        )
        note = workers_ai_chat_text(session, config, config.analysis_model, ai_messages(system, user), max_tokens=700)
        chunk_notes.append(note)

    reduce_user = (
        "Build final changelog JSON for Proxmox newcomers and operators.\n"
        "Return STRICT JSON object with keys:\n"
        "overview (string), professional_assessment (string), newcomer_explainer (string),\n"
        "changes (array of objects with title, details, impact, recommended_action, severity).\n"
        "Keep 4-12 changes.\n"
        "Severity must be one of: high, medium, low.\n"
        "Use simple language but keep technical accuracy.\n\n"
        "Context excerpts from source documentation:\n"
        f"{chr(10).join(source_context) if source_context else '(no vector context)'}\n\n"
        "Notes from chunk analysis:\n"
        f"{chr(10).join(chunk_notes)}"
    )
    reduce_system = (
        "You are a release notes expert. Output valid JSON only. "
        "No markdown. No explanation outside JSON."
    )

    summary_raw: str | None = None
    summary_json: dict[str, Any] | None = None
    analysis_model_used = config.analysis_model
    try:
        summary_raw = workers_ai_chat_text(
            session,
            config,
            config.analysis_model,
            ai_messages(reduce_system, reduce_user),
            max_tokens=2200,
        )
        parsed = parse_json_payload(summary_raw)
        summary_json = normalize_summary_payload(parsed)
        if not summary_json.get("overview"):
            raise RuntimeError("Invalid JSON payload from analysis model")
    except Exception:
        analysis_model_used = config.fallback_model
        summary_raw = workers_ai_chat_text(
            session,
            config,
            config.fallback_model,
            ai_messages(reduce_system, reduce_user),
            max_tokens=2200,
        )
        parsed = parse_json_payload(summary_raw)
        summary_json = normalize_summary_payload(parsed)

    if not summary_json:
        raise RuntimeError("AI summary generation failed")

    translation_user = (
        "Translate this JSON summary to Russian and French.\n"
        "Return STRICT JSON with keys ru and fr, each preserving the same structure:\n"
        "overview, professional_assessment, newcomer_explainer, changes[].\n"
        "Keep technical terms correct for Proxmox.\n\n"
        f"{json.dumps(summary_json, ensure_ascii=False)}"
    )
    translation_system = "You are a professional technical translator. Output valid JSON only."
    translations: dict[str, Any] = {}
    try:
        translated = workers_ai_chat_text(
            session,
            config,
            config.translation_model,
            ai_messages(translation_system, translation_user),
            max_tokens=2600,
        )
        parsed_translations = parse_json_payload(translated)
        if isinstance(parsed_translations, dict):
            ru = normalize_summary_payload(parsed_translations.get("ru"))
            fr = normalize_summary_payload(parsed_translations.get("fr"))
            if ru:
                translations["ru"] = ru
            if fr:
                translations["fr"] = fr
    except Exception:
        translations = {}

    return {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "analysis_model": analysis_model_used,
        "translation_model": config.translation_model,
        "embedding_model": config.embedding_model,
        "summary": {"en": summary_json, **translations},
        "summary_text": {
            "en": format_changes_text(summary_json.get("changes", [])),
            "ru": format_changes_text(translations.get("ru", {}).get("changes", [])),
            "fr": format_changes_text(translations.get("fr", {}).get("changes", [])),
        },
    }


def ai_summary_for_language(ai_data: dict[str, Any], language: str) -> dict[str, Any]:
    summary = ai_data.get("summary")
    if not isinstance(summary, dict):
        return {}
    lang_payload = summary.get(language)
    if isinstance(lang_payload, dict):
        return lang_payload
    fallback = summary.get("en")
    if isinstance(fallback, dict):
        return fallback
    return {}


def maybe_index_snapshot_in_vectorize(
    session: requests.Session,
    config: AIConfig,
    normalized_snapshot: str,
    snapshot_hash: str,
) -> dict[str, Any]:
    if not config.vectorize_index:
        return {"status": "skipped", "reason": "vectorize_not_configured"}
    if not config.account_id or not config.api_token:
        return {"status": "skipped", "reason": "missing_credentials"}

    chunks = split_text_chunks(
        normalized_snapshot,
        chunk_size=config.vectorize_chunk_chars,
        overlap=300,
    )[: config.vectorize_max_chunks]
    if not chunks:
        return {"status": "skipped", "reason": "empty_snapshot"}

    vectors = workers_ai_embeddings(session, config, chunks)
    records = build_vectorize_records(chunks, vectors, snapshot_hash=snapshot_hash, namespace=config.vectorize_namespace)
    vectorize_upsert(session, config, records)
    return {"status": "ok", "chunks_indexed": len(records)}


def maybe_fetch_vector_context(
    session: requests.Session,
    config: AIConfig,
    diff_text: str,
) -> list[str]:
    if not config.vectorize_index:
        return []
    if not config.account_id or not config.api_token:
        return []

    query_text = trim_for_prompt(diff_text, 24_000)
    query_vector_payload = workers_ai_embeddings(session, config, [query_text])
    if not query_vector_payload:
        return []
    matches = vectorize_query_context(session, config, query_vector_payload[0])
    contexts = extract_context_texts(matches)
    return contexts[: config.vectorize_query_top_k]


def event_row_id(event: dict[str, Any]) -> str:
    payload = "|".join(
        [
            str(event.get("timestamp", "")),
            str(event.get("old_hash", "")),
            str(event.get("new_hash", "")),
            str(event.get("added_lines", 0)),
            str(event.get("removed_lines", 0)),
            str(event.get("diff_file", "")),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def format_event_row(event: dict[str, Any]) -> str:
    timestamp = event.get("timestamp", "-")
    timestamp_display = format_utc_display(str(timestamp))
    old_hash = (event.get("old_hash") or "-")[:12]
    new_hash = (event.get("new_hash") or "-")[:12]
    added = event.get("added_lines", 0)
    removed = event.get("removed_lines", 0)
    diff_file = event.get("diff_file")

    if diff_file:
        html_href = docs_href_from_path(diff_html_path(diff_file))
        changelog_href = docs_href_from_path(diff_changelog_path(diff_file))
        link = (
            f'<a href="{html.escape(changelog_href)}" target="_blank" rel="noopener noreferrer" data-i18n="changelog">Changelog</a> | '
            f'<a href="{html.escape(html_href)}" target="_blank" rel="noopener noreferrer" data-i18n="code_diff">Code diff</a>'
        )
    else:
        link = "-"
    row_id = event_row_id(event)
    selector = html.escape(str(timestamp))
    details_cell = (
        f"{link} "
        '<button type="button" class="row-remove" data-action="hide-row" data-i18n="hide_entry">Hide</button> '
        '<label class="row-select-wrap" hidden>'
        '<input type="checkbox" class="row-select" data-role="row-select"> '
        '<span data-i18n="select_entry">Select</span>'
        "</label>"
    )

    return (
        f'<tr data-event-id="{row_id}" data-history-selector="{selector}">'
        f"<td>{timestamp_display}</td>"
        f"<td><code>{old_hash}</code></td>"
        f"<td><code>{new_hash}</code></td>"
        f"<td>+{added} / -{removed}</td>"
        f"<td>{details_cell}</td>"
        "</tr>"
    )


def docs_href_from_path(path: str) -> str:
    return path[5:] if path.startswith("docs/") else path


def diff_html_path(diff_file: str) -> str:
    return str(Path(diff_file).with_suffix(".html").as_posix())


def diff_changelog_path(diff_file: str) -> str:
    path = Path(diff_file)
    return str(path.with_suffix(".changelog.html").as_posix())


def render_changelog_html(
    diff_path: Path,
    ai_data: dict[str, Any],
    author_name: str,
    author_url: str,
) -> None:
    generated_at = format_utc_display(now_utc_iso())
    page_title = f"{diff_path.name} - AI Changelog"
    code_diff_href = diff_path.with_suffix(".html").name

    if not ai_data:
        ai_data = {"status": "skipped", "reason": "ai_data_not_available"}
    summary_en = ai_summary_for_language(ai_data, "en")
    summary_ru = ai_summary_for_language(ai_data, "ru")
    summary_fr = ai_summary_for_language(ai_data, "fr")

    def render_change_list(summary: dict[str, Any]) -> str:
        changes = summary.get("changes")
        if not isinstance(changes, list) or not changes:
            return '<li data-i18n="no_items">No items yet.</li>'
        items = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            title = html.escape(str(change.get("title") or "").strip())
            details = html.escape(str(change.get("details") or "").strip())
            impact = html.escape(str(change.get("impact") or "").strip())
            action = html.escape(str(change.get("recommended_action") or "").strip())
            severity = html.escape(str(change.get("severity") or "").strip())
            chunks = [f"<strong>{title}</strong>" if title else ""]
            if details:
                chunks.append(f"<div>{details}</div>")
            if impact:
                chunks.append(f"<div><em>Impact:</em> {impact}</div>")
            if action:
                chunks.append(f"<div><em>Action:</em> {action}</div>")
            if severity:
                chunks.append(f"<div><em>Severity:</em> {severity}</div>")
            items.append(f"<li>{''.join(chunks)}</li>")
        return "\n".join(items) if items else '<li data-i18n="no_items">No items yet.</li>'

    def section_html(summary: dict[str, Any], lang_code: str) -> str:
        overview = html.escape(str(summary.get("overview") or ""))
        assessment = html.escape(str(summary.get("professional_assessment") or ""))
        newcomer = html.escape(str(summary.get("newcomer_explainer") or ""))
        return f"""
        <section class="lang-block" data-lang-block="{lang_code}">
          <h2 data-i18n="summary_title">Summary</h2>
          <p><strong data-i18n="overview">Overview:</strong> {overview or '-'}</p>
          <p><strong data-i18n="assessment">Assessment:</strong> {assessment or '-'}</p>
          <p><strong data-i18n="newcomer">For newcomers:</strong> {newcomer or '-'}</p>
          <h3 data-i18n="changes_title">Change List</h3>
          <ol>
            {render_change_list(summary)}
          </ol>
        </section>
        """

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page_title)}</title>
  <style>
    :root {{
      --bg: #f3f6fb;
      --card: #ffffff;
      --line: #dbe3ef;
      --text: #0f172a;
      --muted: #475569;
      --accent: #0b5cab;
    }}
    :root[data-theme="dark"] {{
      --bg: #0b1020;
      --card: #0f172a;
      --line: #1e293b;
      --text: #dbe7ff;
      --muted: #9fb3d1;
      --accent: #93c5fd;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, sans-serif;
    }}
    .wrap {{
      max-width: 1000px;
      margin: 0 auto;
      padding: 20px 16px 40px;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      justify-content: flex-end;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }}
    .toolbar select {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 6px 8px;
      background: var(--card);
      color: var(--text);
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px;
    }}
    .head p {{
      color: var(--muted);
      margin: 6px 0;
    }}
    a {{
      color: var(--accent);
    }}
    .lang-block {{
      margin-top: 20px;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }}
    .lang-block[hidden] {{
      display: none;
    }}
    ol {{
      padding-left: 20px;
    }}
    li {{
      margin: 10px 0;
      line-height: 1.45;
    }}
    .footer {{
      margin-top: 20px;
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="toolbar">
      <label>
        <span data-i18n="language">Language</span>
        <select id="lang-select">
          <option value="en">English</option>
          <option value="fr">Français</option>
          <option value="ru">Русский</option>
        </select>
      </label>
      <label>
        <span data-i18n="theme">Theme</span>
        <select id="theme-select">
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </label>
    </div>
    <div class="card">
      <div class="head">
        <h1 data-i18n="title">AI Changelog</h1>
        <p><span data-i18n="generated">Generated (UTC):</span> {generated_at}</p>
        <p><a href="../index.html" data-i18n="back_menu">Back to main menu</a> | <a href="{html.escape(code_diff_href)}" data-i18n="open_diff">Open code diff</a></p>
      </div>
      {section_html(summary_en, "en")}
      {section_html(summary_fr, "fr")}
      {section_html(summary_ru, "ru")}
      <div class="footer">
        <div><span data-i18n="model_analysis">Analysis model:</span> {html.escape(str(ai_data.get("analysis_model") or "-"))}</div>
        <div><span data-i18n="model_translation">Translation model:</span> {html.escape(str(ai_data.get("translation_model") or "-"))}</div>
        <div><span data-i18n="author">Author</span> <a href="{html.escape(author_url)}" target="_blank" rel="noopener noreferrer">{html.escape(author_name)}</a></div>
      </div>
    </div>
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
          title: "AI Changelog",
          generated: "Generated (UTC):",
          back_menu: "Back to main menu",
          open_diff: "Open code diff",
          summary_title: "Summary",
          overview: "Overview:",
          assessment: "Assessment:",
          newcomer: "For newcomers:",
          changes_title: "Change list",
          no_items: "No items yet.",
          model_analysis: "Analysis model:",
          model_translation: "Translation model:",
          author: "Author"
        }},
        fr: {{
          language: "Langue",
          theme: "Theme",
          title: "Journal IA",
          generated: "Généré (UTC) :",
          back_menu: "Retour au menu principal",
          open_diff: "Ouvrir le diff de code",
          summary_title: "Résumé",
          overview: "Aperçu :",
          assessment: "Évaluation :",
          newcomer: "Pour les débutants :",
          changes_title: "Liste des changements",
          no_items: "Aucun élément.",
          model_analysis: "Modèle d'analyse :",
          model_translation: "Modèle de traduction :",
          author: "Auteur"
        }},
        ru: {{
          language: "Язык",
          theme: "Тема",
          title: "AI Changelog",
          generated: "Сгенерировано (UTC):",
          back_menu: "Назад в главное меню",
          open_diff: "Открыть code diff",
          summary_title: "Сводка",
          overview: "Обзор:",
          assessment: "Оценка:",
          newcomer: "Для новичков:",
          changes_title: "Список изменений",
          no_items: "Пока нет пунктов.",
          model_analysis: "Модель анализа:",
          model_translation: "Модель перевода:",
          author: "Автор"
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
        if (saved === "dark" || saved === "light") return saved;
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }}
      function t(lang, key) {{
        return (i18n[lang] && i18n[lang][key]) || i18n[fallbackLang][key] || key;
      }}
      function applyLanguage(lang) {{
        document.documentElement.lang = lang;
        langSelect.value = lang;
        document.querySelectorAll("[data-i18n]").forEach((node) => {{
          const key = node.getAttribute("data-i18n");
          node.textContent = t(lang, key);
        }});
        document.querySelectorAll("[data-lang-block]").forEach((node) => {{
          node.hidden = node.getAttribute("data-lang-block") !== lang;
        }});
      }}
      function applyTheme(theme) {{
        document.documentElement.setAttribute("data-theme", theme);
        themeSelect.value = theme;
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
    save_text(diff_path.with_suffix(".changelog.html"), page)


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
    generated_at = format_utc_display(now_utc_iso())
    title = f"{diff_path.name} - Diff Viewer"
    changelog_href = diff_path.with_suffix(".changelog.html").name
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
      --telegram: #229ed9;
      --telegram-hover: #1d8fc4;
      --telegram-text: #ffffff;
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
    .control-btn {{
      border: 1px solid var(--control-line);
      border-radius: 8px;
      background: var(--control-bg);
      color: var(--control-text);
      padding: 6px 10px;
      font-size: 13px;
      cursor: pointer;
    }}
    .control-btn:hover {{
      border-color: var(--accent);
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
        <a href="{html.escape(changelog_href)}" target="_blank" rel="noopener noreferrer" data-i18n="open_changelog">Open changelog</a>
      </p>
    </div>
    <pre class="diff">{body}</pre>
    <footer class="footer">
      <span data-i18n="footer_by">Author</span>
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
          open_changelog: "Open changelog",
          footer_by: "Author",
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
          open_changelog: "Ouvrir le changelog",
          footer_by: "Auteur",
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
          open_changelog: "Открыть changelog",
          footer_by: "Автор",
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
            pass
        else:
            diff_text = diff_path.read_text(encoding="utf-8")
            render_diff_html(diff_path, diff_text, author_name=author_name, author_url=author_url)
            created_any = True

        changelog_path = diff_path.with_suffix(".changelog.html")
        if not changelog_path.exists():
            ai_payload = event.get("ai")
            if not isinstance(ai_payload, dict):
                ai_payload = {"status": "skipped", "reason": "ai_data_not_available"}
            render_changelog_html(diff_path, ai_payload, author_name=author_name, author_url=author_url)
            created_any = True
    return created_any


def render_docs(
    history: list[dict[str, Any]],
    url: str,
    docs_dir: Path,
    author_name: str,
    author_url: str,
    github_repo: str,
    github_ref: str,
    github_admin_workflow: str,
) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(format_event_row(event) for event in history)

    generated_at = format_utc_display(now_utc_iso())
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
      --telegram: #229ed9;
      --telegram-hover: #1d8fc4;
      --telegram-text: #ffffff;
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
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }}
    .toolbar-left {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-start;
    }}
    .toolbar-right {{
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
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
    .control-btn {{
      border: 1px solid var(--control-line);
      border-radius: 8px;
      background: var(--control-bg);
      color: var(--control-text);
      padding: 6px 10px;
      font-size: 13px;
      cursor: pointer;
    }}
    .control-btn:hover {{
      border-color: var(--accent);
    }}
    .admin-status {{
      color: var(--muted);
      font-size: 12px;
    }}
    .admin-panel {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .admin-panel[hidden] {{
      display: none !important;
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
    .telegram-link {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      border-radius: 999px;
      border: 1px solid rgba(13, 90, 127, 0.5);
      background: var(--telegram);
      color: var(--telegram-text) !important;
      font-weight: 600;
      font-size: 15px;
      line-height: 1.2;
      text-decoration: none;
      box-shadow: 0 6px 16px rgba(34, 158, 217, 0.35);
      transition: box-shadow 120ms ease, background 120ms ease;
    }}
    .telegram-link:hover {{
      background: var(--telegram-hover);
      box-shadow: 0 8px 20px rgba(34, 158, 217, 0.45);
    }}
    .telegram-link:focus-visible {{
      outline: 2px solid rgba(133, 213, 245, 0.9);
      outline-offset: 2px;
    }}
    .telegram-link svg {{
      width: 22px;
      height: 22px;
      display: block;
      flex: 0 0 auto;
    }}
    .telegram-link .tg-icon {{
      width: 22px;
      height: 22px;
      flex: 0 0 22px;
      display: inline-flex;
      align-items: center;
      justify-content: flex-start;
      overflow: hidden;
    }}
    .telegram-link .tg-circle {{
      fill: #229ed9;
    }}
    .telegram-link .tg-plane {{
      fill: #ffffff;
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ margin: 4px 0; color: var(--muted); }}
    a {{ color: var(--accent); }}
    .row-remove {{
      border: 1px solid var(--control-line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--control-text);
      padding: 2px 8px;
      font-size: 12px;
      cursor: pointer;
    }}
    .row-remove:hover {{
      border-color: #dc2626;
      color: #dc2626;
    }}
    .row-select-wrap {{
      color: var(--muted);
      font-size: 12px;
      user-select: none;
    }}
    .row-select {{
      vertical-align: middle;
    }}
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
      .toolbar {{
        gap: 8px;
      }}
      .toolbar-left {{
        width: 100%;
      }}
      .toolbar-right {{
        width: 100%;
        justify-content: flex-start;
      }}
      .telegram-link {{
        max-width: 100%;
        width: auto;
        font-size: 14px;
      }}
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
      <div class="toolbar-left">
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
        <span id="admin-panel" class="admin-panel" hidden>
          <button type="button" id="open-workflow" class="control-btn" data-i18n="open_delete_workflow">Open delete workflow</button>
          <button type="button" id="delete-selected" class="control-btn" data-i18n="copy_selected" disabled>Copy selected</button>
          <span id="admin-status" class="admin-status" data-i18n="tools_status_off">Tools: off</span>
        </span>
        <button type="button" id="reset-hidden" class="control-btn" data-i18n="reset_hidden">Reset hidden</button>
      </div>
      <div class="toolbar-right">
        <a class="telegram-link" href="https://t.me/proxmox_update" target="_blank" rel="noopener noreferrer">
          <span class="tg-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <circle class="tg-circle" cx="12" cy="12" r="11"></circle>
              <path class="tg-plane" d="M17.8 7.2 5.8 11.8c-.8.3-.8.7-.1.9l3 .9 1.2 3.5c.1.4.3.6.6.6.2 0 .4-.1.7-.3l1.7-1.7 2.8 2.1c.5.3 1 .2 1.2-.6l2.1-9c.2-.9-.3-1.3-1.2-1zM10 13.2l5.8-4c.3-.2.6.1.3.3l-4.8 4.5-.2 2 .9-2.8z"></path>
            </svg>
          </span>
          <span data-i18n="subscribe_telegram">Subscribe on Telegram</span>
        </a>
      </div>
    </div>
    <div class="card">
      <div class="head">
        <h1 data-i18n="title">Proxmox VE Admin Guide Changelog</h1>
        <p><span data-i18n="source">Source:</span> <a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">{html.escape(url)}</a></p>
        <p><span data-i18n="generated">Generated (UTC):</span> {generated_at}</p>
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
          <tr data-empty-row="true" style="display:none;"><td colspan="5" data-i18n="no_changes">No changes detected yet.</td></tr>
        </tbody>
      </table>
    </div>
    <footer class="footer">
      <span data-i18n="footer_by">Author</span>
      <a href="{html.escape(author_url)}" target="_blank" rel="noopener noreferrer">{html.escape(author_name)}</a>
    </footer>
  </div>
  <script>
    (() => {{
      const LANG_KEY = "doxmox-lang";
      const THEME_KEY = "doxmox-theme";
      const HIDDEN_ROWS_KEY = "doxmox-hidden-events";
      const fallbackLang = "en";
      const GITHUB_REPO = "{html.escape(github_repo)}";
      const GITHUB_ADMIN_WORKFLOW = "{html.escape(github_admin_workflow)}";
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
          changelog: "Changelog",
          code_diff: "Code Diff",
          no_changes: "No changes detected yet.",
          language: "Language",
          theme: "Theme",
          theme_light: "Light",
          theme_dark: "Dark",
          reset_hidden: "Reset hidden",
          hide_entry: "Hide",
          select_entry: "Select",
          copy_selected: "Copy selected",
          open_delete_workflow: "Open delete workflow",
          tools_status_off: "Tools: off",
          tools_status_on: "Tools: on",
          copy_selected_empty: "Select at least one entry first.",
          copy_selected_done: "Selectors copied. Paste them into the 'selectors' field in GitHub workflow.",
          copy_selected_failed: "Clipboard copy failed. Use manual copy from prompt.",
          footer_by: "Author",
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
          changelog: "Changelog",
          code_diff: "Code Diff",
          no_changes: "Aucun changement detecte pour le moment.",
          language: "Langue",
          theme: "Theme",
          theme_light: "Clair",
          theme_dark: "Sombre",
          reset_hidden: "Reinitialiser les caches",
          hide_entry: "Masquer",
          select_entry: "Selectionner",
          copy_selected: "Copier la selection",
          open_delete_workflow: "Ouvrir le workflow de suppression",
          tools_status_off: "Outils : off",
          tools_status_on: "Outils : on",
          copy_selected_empty: "Selectionnez au moins une entree.",
          copy_selected_done: "Selecteurs copies. Collez-les dans le champ 'selectors' du workflow GitHub.",
          copy_selected_failed: "Echec de copie dans le presse-papiers. Utilisez la copie manuelle.",
          footer_by: "Auteur",
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
          changelog: "Changelog",
          code_diff: "Code Diff",
          no_changes: "Изменения пока не обнаружены.",
          language: "Язык",
          theme: "Тема",
          theme_light: "Светлая",
          theme_dark: "Тёмная",
          reset_hidden: "Сбросить скрытые",
          hide_entry: "Скрыть",
          select_entry: "Выбрать",
          copy_selected: "Скопировать выбранное",
          open_delete_workflow: "Открыть workflow удаления",
          tools_status_off: "Инструменты: выкл",
          tools_status_on: "Инструменты: вкл",
          copy_selected_empty: "Сначала выберите хотя бы одну запись.",
          copy_selected_done: "Селекторы скопированы. Вставьте их в поле 'selectors' в GitHub workflow.",
          copy_selected_failed: "Не удалось скопировать в буфер. Используйте ручное копирование.",
          footer_by: "Автор",
          subscribe_telegram: "Подписаться в Telegram",
          language_en: "Английский",
          language_fr: "Французский",
          language_ru: "Русский"
        }}
      }};

      const langSelect = document.getElementById("lang-select");
      const themeSelect = document.getElementById("theme-select");
      const resetHiddenButton = document.getElementById("reset-hidden");
      const adminPanel = document.getElementById("admin-panel");
      const openWorkflowButton = document.getElementById("open-workflow");
      const deleteSelectedButton = document.getElementById("delete-selected");
      const adminStatus = document.getElementById("admin-status");
      const tableBody = document.querySelector("tbody");
      const hiddenRows = new Set();
      let adminPanelUnlocked = false;
      let escHitCounter = 0;
      let escTimer = null;

      function unlockAdminPanel() {{
        adminPanelUnlocked = true;
        adminPanel.hidden = false;
      }}

      function workflowUrl() {{
        return `https://github.com/${{GITHUB_REPO}}/actions/workflows/${{encodeURIComponent(GITHUB_ADMIN_WORKFLOW)}}`;
      }}

      function selectedSelectors() {{
        const selected = new Set();
        tableBody.querySelectorAll('tr[data-event-id] .row-select:checked').forEach((checkbox) => {{
          const row = checkbox.closest("tr[data-event-id]");
          if (!row) return;
          const selector = row.getAttribute("data-history-selector");
          if (selector) selected.add(selector);
        }});
        return [...selected];
      }}

      function updateBatchDeleteState() {{
        deleteSelectedButton.disabled = !adminPanelUnlocked || selectedSelectors().length === 0;
      }}

      function setToolsMode(enabled) {{
        if (enabled) unlockAdminPanel();
        adminPanel.hidden = !adminPanelUnlocked;
        document.querySelectorAll(".row-select-wrap").forEach((node) => {{
          node.hidden = !enabled;
        }});
        if (!enabled) {{
          tableBody.querySelectorAll(".row-select").forEach((checkbox) => {{
            checkbox.checked = false;
          }});
        }}
        const lang = readLang();
        adminStatus.textContent = enabled ? t(lang, "tools_status_on") : t(lang, "tools_status_off");
        updateBatchDeleteState();
      }}

      async function copyToClipboard(value) {{
        if (navigator.clipboard && window.isSecureContext) {{
          await navigator.clipboard.writeText(value);
          return;
        }}
        const textArea = document.createElement("textarea");
        textArea.value = value;
        textArea.style.position = "fixed";
        textArea.style.opacity = "0";
        textArea.style.left = "-9999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        const success = document.execCommand("copy");
        document.body.removeChild(textArea);
        if (!success) {{
          throw new Error("copy failed");
        }}
      }}

      function loadHiddenRows() {{
        try {{
          const raw = localStorage.getItem(HIDDEN_ROWS_KEY);
          if (!raw) return;
          const parsed = JSON.parse(raw);
          if (!Array.isArray(parsed)) return;
          parsed.forEach((id) => {{
            if (typeof id === "string" && id) hiddenRows.add(id);
          }});
        }} catch {{
          // ignore invalid localStorage payload
        }}
      }}

      function saveHiddenRows() {{
        localStorage.setItem(HIDDEN_ROWS_KEY, JSON.stringify([...hiddenRows]));
      }}

      function ensureEmptyRow() {{
        let row = tableBody.querySelector('tr[data-empty-row="true"]');
        if (row) return row;
        row = document.createElement("tr");
        row.setAttribute("data-empty-row", "true");
        const cell = document.createElement("td");
        cell.colSpan = 5;
        cell.setAttribute("data-i18n", "no_changes");
        cell.textContent = i18n[fallbackLang].no_changes;
        row.appendChild(cell);
        tableBody.appendChild(row);
        return row;
      }}

      function applyHiddenRows() {{
        const rows = [...tableBody.querySelectorAll("tr[data-event-id]")];
        let visible = 0;
        rows.forEach((row) => {{
          const id = row.getAttribute("data-event-id");
          const isHidden = id && hiddenRows.has(id);
          row.style.display = isHidden ? "none" : "";
          if (isHidden) {{
            const checkbox = row.querySelector(".row-select");
            if (checkbox) checkbox.checked = false;
          }}
          if (!isHidden) visible += 1;
        }});
        const emptyRow = ensureEmptyRow();
        emptyRow.style.display = visible === 0 ? "" : "none";
        updateBatchDeleteState();
      }}

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
      loadHiddenRows();
      applyLanguage(currentLang);
      applyTheme(currentTheme);
      setToolsMode(false);
      applyHiddenRows();

      document.addEventListener("keydown", (event) => {{
        if (event.key !== "Escape") return;
        escHitCounter += 1;
        if (escTimer) clearTimeout(escTimer);
        escTimer = setTimeout(() => {{
          escHitCounter = 0;
          escTimer = null;
        }}, 1300);
        if (escHitCounter >= 3) {{
          unlockAdminPanel();
          escHitCounter = 0;
          if (escTimer) {{
            clearTimeout(escTimer);
            escTimer = null;
          }}
          setToolsMode(true);
        }}
      }});

      tableBody.addEventListener("click", (event) => {{
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        const button = target.closest('button[data-action]');
        if (!button) return;
        const action = button.getAttribute("data-action");
        const row = button.closest("tr[data-event-id]");
        if (!row) return;

        if (action === "hide-row") {{
          const id = row.getAttribute("data-event-id");
          if (!id) return;
          hiddenRows.add(id);
          saveHiddenRows();
          applyHiddenRows();
          return;
        }}
      }});

      tableBody.addEventListener("change", (event) => {{
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        if (!target.matches(".row-select")) return;
        updateBatchDeleteState();
      }});

      resetHiddenButton.addEventListener("click", () => {{
        hiddenRows.clear();
        localStorage.removeItem(HIDDEN_ROWS_KEY);
        applyHiddenRows();
      }});

      openWorkflowButton.addEventListener("click", () => {{
        window.open(workflowUrl(), "_blank", "noopener,noreferrer");
      }});

      deleteSelectedButton.addEventListener("click", async () => {{
        const selectors = selectedSelectors();
        if (!selectors.length) {{
          window.alert(t(readLang(), "copy_selected_empty"));
          return;
        }}
        const payload = selectors.join("\\n");
        const lang = readLang();
        deleteSelectedButton.disabled = true;
        try {{
          await copyToClipboard(payload);
          window.alert(t(lang, "copy_selected_done"));
        }} catch {{
          window.prompt("Copy selectors manually:", payload);
          window.alert(t(lang, "copy_selected_failed"));
        }} finally {{
          updateBatchDeleteState();
        }}
      }});

      langSelect.addEventListener("change", () => {{
        localStorage.setItem(LANG_KEY, langSelect.value);
        applyLanguage(langSelect.value);
        setToolsMode(adminPanelUnlocked);
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


def history_entry_matches(event: dict[str, Any], selector: str) -> bool:
    value = selector.strip()
    if not value:
        return False

    timestamp = str(event.get("timestamp") or "")
    diff_file = str(event.get("diff_file") or "")
    diff_name = Path(diff_file).name if diff_file else ""
    diff_stem = Path(diff_file).stem if diff_file else ""

    if value.startswith("ts:"):
        return timestamp == value[3:]
    if value.startswith("diff:"):
        needle = value[5:]
        return needle in {diff_file, diff_name, diff_stem}

    return value in {timestamp, diff_file, diff_name, diff_stem}


def prune_history(
    state_dir: Path,
    docs_dir: Path,
    selectors: list[str],
    delete_all: bool,
    delete_artifacts: bool,
    author_name: str,
    author_url: str | None,
    github_repo: str,
    github_ref: str,
    github_admin_workflow: str,
) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    history_path = state_dir / HISTORY_FILE
    state_path = state_dir / STATE_FILE
    history: list[dict[str, Any]] = load_json(history_path, [])

    if not history:
        print("History is empty; nothing to delete.")
        return 0

    selector_values = [value.strip() for value in selectors if value.strip()]
    if not delete_all and not selector_values:
        print("No selectors provided. Use --history-delete or --history-delete-all.")
        return 1

    if delete_all:
        removed = history
        kept: list[dict[str, Any]] = []
    else:
        removed = []
        kept = []
        for event in history:
            if any(history_entry_matches(event, selector) for selector in selector_values):
                removed.append(event)
            else:
                kept.append(event)

        if not removed:
            print("No matching history entries found.")
            return 1

    save_json(history_path, kept)

    if delete_artifacts:
        for event in removed:
            diff_file = event.get("diff_file")
            if not diff_file:
                continue
            diff_path = Path(str(diff_file))
            html_path = diff_path.with_suffix(".html")
            changelog_path = diff_path.with_suffix(".changelog.html")
            if diff_path.exists():
                diff_path.unlink()
            if html_path.exists():
                html_path.unlink()
            if changelog_path.exists():
                changelog_path.unlink()

    state_payload = load_json(state_path, {})
    url = state_payload.get("url") or DEFAULT_URL
    resolved_author_url = resolve_author_url(docs_dir, author_url)
    render_docs(
        kept,
        url,
        docs_dir,
        author_name=author_name,
        author_url=resolved_author_url,
        github_repo=github_repo,
        github_ref=github_ref,
        github_admin_workflow=github_admin_workflow,
    )

    print(f"Removed {len(removed)} entr{'y' if len(removed) == 1 else 'ies'} from {history_path}.")
    if delete_artifacts:
        print("Deleted linked diff/html artifacts for removed entries (if present).")
    return 0


def build_ai_config_from_args(args: argparse.Namespace) -> AIConfig:
    account_id = args.ai_account_id or os.environ.get(args.ai_account_env)
    api_token = os.environ.get(args.ai_api_token_env)
    return AIConfig(
        enabled=bool(args.ai_enable),
        account_id=account_id.strip() if isinstance(account_id, str) and account_id.strip() else None,
        api_token=api_token.strip() if isinstance(api_token, str) and api_token.strip() else None,
        analysis_model=args.ai_analysis_model,
        fallback_model=args.ai_fallback_model,
        translation_model=args.ai_translation_model,
        embedding_model=args.ai_embedding_model,
        max_diff_chars=max(10_000, int(args.ai_max_diff_chars)),
        chunk_chars=max(5_000, int(args.ai_chunk_chars)),
        vectorize_index=(args.vectorize_index.strip() if args.vectorize_index else None),
        vectorize_namespace=args.vectorize_namespace.strip() if args.vectorize_namespace else "default",
        vectorize_chunk_chars=max(500, int(args.vectorize_chunk_chars)),
        vectorize_max_chunks=max(8, int(args.vectorize_max_chunks)),
        vectorize_query_top_k=max(1, int(args.vectorize_query_top_k)),
    )


def history_ai_backfill(
    state_dir: Path,
    docs_dir: Path,
    github_repo: str,
    github_ref: str,
    github_admin_workflow: str,
    selectors: list[str],
    process_all: bool,
    force: bool,
    author_name: str,
    author_url: str | None,
    ai_config: AIConfig,
) -> int:
    history_path = state_dir / HISTORY_FILE
    history: list[dict[str, Any]] = load_json(history_path, [])
    if not history:
        print("History is empty; nothing to backfill.")
        return 0
    if not ai_config.enabled:
        print("AI is disabled. Re-run with --ai-enable.")
        return 1
    if not ai_config.account_id or not ai_config.api_token:
        print("Cloudflare credentials are missing for AI backfill.")
        return 1

    selected = set(value.strip() for value in selectors if value.strip())
    if not process_all and not selected:
        print("No selectors were supplied. Use --history-ai-backfill or --history-ai-backfill-all.")
        return 1

    resolved_author_url = resolve_author_url(docs_dir, author_url)
    session = build_session()
    updated = 0
    for event in history:
        diff_file = event.get("diff_file")
        if not diff_file:
            continue
        if not process_all:
            if not any(history_entry_matches(event, value) for value in selected):
                continue
        ai_payload = event.get("ai")
        if isinstance(ai_payload, dict) and ai_payload.get("status") == "ok" and not force:
            continue
        diff_path = Path(str(diff_file))
        if not diff_path.exists():
            continue

        diff_text = diff_path.read_text(encoding="utf-8")
        try:
            contexts = maybe_fetch_vector_context(session, ai_config, diff_text)
            event["ai"] = summarize_diff_with_ai(session, ai_config, diff_text, contexts)
        except Exception as exc:
            event["ai"] = {
                "status": "error",
                "generated_at": now_utc_iso(),
                "error": str(exc),
            }

        render_changelog_html(diff_path, event.get("ai", {}), author_name=author_name, author_url=resolved_author_url)
        updated += 1

    save_json(history_path, history)
    state_payload = load_json(state_dir / STATE_FILE, {})
    url = state_payload.get("url") or DEFAULT_URL
    ensure_diff_html_pages(history, author_name=author_name, author_url=resolved_author_url)
    render_docs(
        history,
        url,
        docs_dir,
        author_name=author_name,
        author_url=resolved_author_url,
        github_repo=github_repo,
        github_ref=github_ref,
        github_admin_workflow=github_admin_workflow,
    )

    if updated:
        print(f"AI backfill completed for {updated} entries.")
    else:
        print("No entries required AI backfill. Index/docs were still refreshed.")
    return 0


def process(
    url: str,
    state_dir: Path,
    changes_dir: Path,
    docs_dir: Path,
    result_file: Path,
    author_name: str,
    author_url: str | None,
    github_repo: str,
    github_ref: str,
    github_admin_workflow: str,
    ai_config: AIConfig,
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
    vectorize_sync: dict[str, Any] | None = None

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
        if ai_config.enabled:
            try:
                vectorize_sync = maybe_index_snapshot_in_vectorize(session, ai_config, normalized, new_hash)
            except Exception as exc:
                vectorize_sync = {"status": "error", "error": str(exc)}

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
            if ai_config.enabled:
                try:
                    contexts = maybe_fetch_vector_context(session, ai_config, diff_text)
                    ai_payload = summarize_diff_with_ai(session, ai_config, diff_text, contexts)
                    if vectorize_sync:
                        ai_payload["vectorize_sync"] = vectorize_sync
                    event["ai"] = ai_payload
                except Exception as exc:
                    event["ai"] = {
                        "status": "error",
                        "generated_at": now_utc_iso(),
                        "error": str(exc),
                    }

            render_changelog_html(
                diff_path,
                event.get("ai", {"status": "skipped", "reason": "ai_not_enabled"}),
                author_name=author_name,
                author_url=resolved_author_url,
            )
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
            github_repo=github_repo,
            github_ref=github_ref,
            github_admin_workflow=github_admin_workflow,
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
    parser.add_argument("--github-repo", default=DEFAULT_GITHUB_REPO)
    parser.add_argument("--github-ref", default=DEFAULT_GITHUB_REF)
    parser.add_argument("--github-admin-workflow", default=DEFAULT_GITHUB_ADMIN_WORKFLOW)
    parser.add_argument("--ai-enable", action="store_true", help="Enable Workers AI changelog generation.")
    parser.add_argument("--ai-account-id", default=None, help="Cloudflare account id for Workers AI/Vectorize.")
    parser.add_argument("--ai-account-env", default=DEFAULT_CLOUDFLARE_ACCOUNT_ENV)
    parser.add_argument("--ai-api-token-env", default=DEFAULT_CLOUDFLARE_TOKEN_ENV)
    parser.add_argument("--ai-analysis-model", default=DEFAULT_AI_ANALYSIS_MODEL)
    parser.add_argument("--ai-fallback-model", default=DEFAULT_AI_FALLBACK_MODEL)
    parser.add_argument("--ai-translation-model", default=DEFAULT_AI_TRANSLATION_MODEL)
    parser.add_argument("--ai-embedding-model", default=DEFAULT_AI_EMBEDDING_MODEL)
    parser.add_argument("--ai-max-diff-chars", type=int, default=DEFAULT_AI_MAX_DIFF_CHARS)
    parser.add_argument("--ai-chunk-chars", type=int, default=DEFAULT_AI_CHUNK_CHARS)
    parser.add_argument("--vectorize-index", default=None)
    parser.add_argument("--vectorize-namespace", default="doxmox-admin-guide")
    parser.add_argument("--vectorize-chunk-chars", type=int, default=DEFAULT_VECTORIZE_CHUNK_CHARS)
    parser.add_argument("--vectorize-max-chunks", type=int, default=DEFAULT_VECTORIZE_MAX_CHUNKS)
    parser.add_argument("--vectorize-query-top-k", type=int, default=DEFAULT_VECTORIZE_QUERY_TOP_K)
    parser.add_argument(
        "--history-delete",
        action="append",
        default=[],
        metavar="SELECTOR",
        help=(
            "Delete history entry by selector. Supports exact timestamp, diff path/name, "
            "or prefixes ts:<timestamp> and diff:<file>. Repeatable."
        ),
    )
    parser.add_argument(
        "--history-delete-all",
        action="store_true",
        help="Delete all entries from data/history.json.",
    )
    parser.add_argument(
        "--history-delete-artifacts",
        action="store_true",
        help="Also delete linked docs/changes/*.diff and *.html files for removed entries.",
    )
    parser.add_argument(
        "--history-ai-backfill",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="Backfill AI changelog for selected history items.",
    )
    parser.add_argument(
        "--history-ai-backfill-all",
        action="store_true",
        help="Backfill AI changelog for all history entries.",
    )
    parser.add_argument(
        "--history-ai-force",
        action="store_true",
        help="Regenerate AI payload even if entry already has ai.status=ok.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    ai_config = build_ai_config_from_args(args)
    if args.history_delete_all or args.history_delete:
        return prune_history(
            state_dir=Path(args.state_dir),
            docs_dir=Path(args.docs_dir),
            selectors=args.history_delete,
            delete_all=args.history_delete_all,
            delete_artifacts=args.history_delete_artifacts,
            author_name=args.author_name,
            author_url=args.author_url,
            github_repo=args.github_repo,
            github_ref=args.github_ref,
            github_admin_workflow=args.github_admin_workflow,
        )
    if args.history_ai_backfill_all or args.history_ai_backfill:
        return history_ai_backfill(
            state_dir=Path(args.state_dir),
            docs_dir=Path(args.docs_dir),
            github_repo=args.github_repo,
            github_ref=args.github_ref,
            github_admin_workflow=args.github_admin_workflow,
            selectors=args.history_ai_backfill,
            process_all=args.history_ai_backfill_all,
            force=args.history_ai_force,
            author_name=args.author_name,
            author_url=args.author_url,
            ai_config=ai_config,
        )

    return process(
        url=args.url,
        state_dir=Path(args.state_dir),
        changes_dir=Path(args.changes_dir),
        docs_dir=Path(args.docs_dir),
        result_file=Path(args.result_file),
        author_name=args.author_name,
        author_url=args.author_url,
        github_repo=args.github_repo,
        github_ref=args.github_ref,
        github_admin_workflow=args.github_admin_workflow,
        ai_config=ai_config,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
