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
import time
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
DEFAULT_AI_TRANSLATION_MODEL = "@cf/openai/gpt-oss-120b"
DEFAULT_AI_BRIEF_MODEL = "@cf/openai/gpt-oss-20b"
DEFAULT_AI_BRIEF_FALLBACK_MODEL = "@cf/zai-org/glm-4.7-flash"
DEFAULT_AI_EMBEDDING_MODEL = "@cf/baai/bge-m3"
DEFAULT_AI_FALLBACK_MODEL = "@cf/moonshotai/kimi-k2.6"
DEFAULT_AI_MAX_DIFF_CHARS = 380_000
DEFAULT_AI_CHUNK_CHARS = 40_000
DEFAULT_AI_REQUIRE_FULL_TRANSLATIONS = True
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
    short_summary: str | None = None
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
            "short_summary": self.short_summary,
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
    brief_model: str
    brief_fallback_model: str
    embedding_model: str
    max_diff_chars: int
    chunk_chars: int
    vectorize_index: str | None
    vectorize_namespace: str
    vectorize_chunk_chars: int
    vectorize_max_chunks: int
    vectorize_query_top_k: int
    require_full_translations: bool


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


def looks_like_ai_envelope_text(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "chatcmpl-" in lowered:
        return True
    if '"object"' in lowered and '"chat.completion"' in lowered:
        return True
    if text.startswith("{") and '"result"' in lowered and '"choices"' in lowered:
        return True
    if text.startswith("{") and '"success"' in lowered and '"errors"' in lowered:
        return True
    if "please provide the text to translate" in lowered:
        return True
    if "veuillez fournir le texte" in lowered:
        return True
    if "пожалуйста, предоставьте текст" in lowered:
        return True
    return False


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


def format_exception_message(exc: Exception, max_len: int = 600) -> str:
    message = str(exc).strip()
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        if response is not None:
            status = response.status_code
            reason = (response.reason or "").strip()
            body = response.text.strip()
            if len(body) > max_len:
                body = body[:max_len] + "...[truncated]"
            parts = [f"HTTP {status}"]
            if reason:
                parts.append(reason)
            if body:
                parts.append(body)
            message = " | ".join(parts)
    if not message:
        message = exc.__class__.__name__
    return " ".join(message.split())


def is_retryable_translation_error(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        if response is not None and response.status_code in {408, 429, 500, 502, 503, 504}:
            return True
    message = str(exc).lower()
    if "request timeout" in message or '"code":3046' in message or '"code":3007' in message:
        return True
    return False


def split_text_for_translation(text: str, max_len: int = 480) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    if len(value) <= max_len:
        return [value]

    blocks = [chunk.strip() for chunk in re.split(r"\n{2,}", value) if chunk.strip()]
    pieces: list[str] = []

    def append_with_limit(chunk: str) -> None:
        if len(chunk) <= max_len:
            pieces.append(chunk)
            return
        sentences = re.split(r"(?<=[.!?])\s+", chunk)
        current = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > max_len:
                for i in range(0, len(sentence), max_len):
                    part = sentence[i : i + max_len].strip()
                    if part:
                        if current:
                            pieces.append(current)
                            current = ""
                        pieces.append(part)
                continue
            candidate = f"{current} {sentence}".strip() if current else sentence
            if len(candidate) > max_len:
                if current:
                    pieces.append(current)
                current = sentence
            else:
                current = candidate
        if current:
            pieces.append(current)

    for block in blocks:
        append_with_limit(block)

    return pieces if pieces else [value]


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
    def content_to_text(content: Any) -> str | None:
        def append_text(parts: list[str], value: Any) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text:
                    parts.append(text)
                return
            if isinstance(value, dict):
                # Common OpenAI-compatible variants:
                # {"text":"..."} or {"text":{"value":"..."}}
                text_field = value.get("text")
                if isinstance(text_field, str):
                    append_text(parts, text_field)
                elif isinstance(text_field, dict):
                    append_text(parts, text_field.get("value"))
                # Harmony/Responses-like wrapper:
                # {"content":"..."} or {"content":[...]}
                append_text(parts, value.get("content"))
                append_text(parts, value.get("value"))
                return
            if isinstance(value, list):
                for item in value:
                    append_text(parts, item)

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                append_text(parts, item)
            joined = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
            return joined or None
        if isinstance(content, dict):
            parts: list[str] = []
            append_text(parts, content)
            joined = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
            if joined:
                return joined
        return None

    result = payload.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        response = result.get("response")
        if isinstance(response, str):
            return response
        response_text = content_to_text(response)
        if response_text:
            return response_text
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
                    content_text = content_to_text(content)
                    if content_text:
                        return content_text
                text = first.get("text")
                if isinstance(text, str):
                    return text
    response = payload.get("response")
    if isinstance(response, str):
        return response
    payload_response_text = content_to_text(response)
    if payload_response_text:
        return payload_response_text
    return ""


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
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.2,
) -> str:
    if not config.account_id or not config.api_token:
        raise RuntimeError("Cloudflare AI credentials are not configured")
    payload = {
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "stream": False,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format
    result = workers_ai_run(session, config.account_id, config.api_token, model, payload)
    return workers_ai_extract_text(result).strip()


def workers_ai_chat_completion(
    session: requests.Session,
    config: AIConfig,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 1400,
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    if not config.account_id or not config.api_token:
        raise RuntimeError("Cloudflare AI credentials are not configured")
    payload = {
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "stream": False,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format
    return workers_ai_run(session, config.account_id, config.api_token, model, payload)


def workers_ai_extract_refusal(payload: dict[str, Any]) -> str | None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    refusal = message.get("refusal")
    if refusal is None:
        return None
    if isinstance(refusal, str):
        return refusal.strip() or "(empty refusal)"
    if isinstance(refusal, dict):
        try:
            return json.dumps(refusal, ensure_ascii=False)
        except Exception:
            return str(refusal)
    return str(refusal)


def workers_ai_extract_translation_text(payload: dict[str, Any]) -> str | None:
    result = payload.get("result")
    if isinstance(result, dict):
        translated = result.get("translated_text")
        if isinstance(translated, str) and translated.strip():
            return translated.strip()
        response = result.get("response")
        if isinstance(response, str) and response.strip():
            return response.strip()
    if isinstance(result, str) and result.strip():
        return result.strip()
    translated = payload.get("translated_text")
    if isinstance(translated, str) and translated.strip():
        return translated.strip()
    response = payload.get("response")
    if isinstance(response, str) and response.strip():
        return response.strip()
    return None


def workers_ai_translate_text(
    session: requests.Session,
    config: AIConfig,
    model: str,
    text: str,
    target_lang: str,
    source_lang: str = "english",
    allow_split: bool = True,
) -> str:
    value = str(text or "").strip()
    if not value:
        return ""

    payload = {"text": value, "source_lang": source_lang, "target_lang": target_lang}
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            raw = workers_ai_run(session, config.account_id or "", config.api_token or "", model, payload)
            translated = workers_ai_extract_translation_text(raw)
            if translated and not looks_like_ai_envelope_text(translated):
                return translated
            preview = json.dumps(raw, ensure_ascii=False)[:280]
            raise RuntimeError(f"Invalid translation response payload. Preview: {preview}")
        except Exception as exc:
            last_error = exc
            if not is_retryable_translation_error(exc) or attempt == 2:
                break
            time.sleep(1.2 * (2**attempt))

    if allow_split and last_error is not None and is_retryable_translation_error(last_error) and len(value) > 420:
        parts = split_text_for_translation(value, max_len=420)
        translated_parts: list[str] = []
        for part in parts:
            translated_parts.append(
                workers_ai_translate_text(
                    session=session,
                    config=config,
                    model=model,
                    text=part,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    allow_split=False,
                )
            )
        return "\n".join(piece.strip() for piece in translated_parts if piece and piece.strip())

    if last_error is not None:
        raise last_error
    raise RuntimeError("Unknown translation error")


def workers_ai_translate_text_with_llm(
    session: requests.Session,
    config: AIConfig,
    model: str,
    text: str,
    target_lang_label: str,
    allow_split: bool = True,
) -> str:
    value = str(text or "").strip()
    if not value:
        return ""

    system = "You are a professional technical translator. Return only translated text."
    user = (
        f"Translate the following text to {target_lang_label}.\n"
        "Keep Proxmox terms precise. Preserve meaning exactly.\n"
        "Output only the translated text, without JSON, markdown, or commentary.\n\n"
        + value
    )
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            raw = workers_ai_chat_completion(
                session=session,
                config=config,
                model=model,
                messages=ai_messages(system, user),
                max_tokens=2200,
                temperature=0.0,
            )
            refusal = workers_ai_extract_refusal(raw)
            if refusal:
                raise RuntimeError(f"refusal={refusal}")
            translated = workers_ai_extract_text(raw).strip()
            if translated and not looks_like_ai_envelope_text(translated):
                return translated
            preview = json.dumps(raw, ensure_ascii=False)[:280]
            raise RuntimeError(f"Invalid LLM translation response payload. Preview: {preview}")
        except Exception as exc:
            last_error = exc
            if not is_retryable_translation_error(exc) or attempt == 2:
                break
            time.sleep(1.2 * (2**attempt))

    if allow_split and last_error is not None and is_retryable_translation_error(last_error) and len(value) > 420:
        parts = split_text_for_translation(value, max_len=420)
        translated_parts: list[str] = []
        for part in parts:
            translated_parts.append(
                workers_ai_translate_text_with_llm(
                    session=session,
                    config=config,
                    model=model,
                    text=part,
                    target_lang_label=target_lang_label,
                    allow_split=False,
                )
            )
        return "\n".join(piece.strip() for piece in translated_parts if piece and piece.strip())

    if last_error is not None:
        raise last_error
    raise RuntimeError("Unknown LLM translation error")


def workers_ai_embeddings(
    session: requests.Session,
    config: AIConfig,
    texts: list[str],
) -> list[list[float]]:
    if not texts:
        return []
    if not config.account_id or not config.api_token:
        raise RuntimeError("Cloudflare AI credentials are not configured")
    # Workers AI embedding models may differ in accepted payload shape.
    # Try common variants before failing.
    bulk_variants: list[dict[str, Any]] = [{"text": texts}, {"input": texts}]
    if len(texts) == 1:
        bulk_variants.extend([{"text": texts[0]}, {"input": texts[0]}])

    for payload in bulk_variants:
        try:
            result = workers_ai_run(session, config.account_id, config.api_token, config.embedding_model, payload)
            vectors = workers_ai_extract_embeddings(result)
            if vectors:
                if len(texts) == 1:
                    return [vectors[0]]
                if len(vectors) == len(texts):
                    return vectors
                if vectors:
                    # Some providers return one vector even for batched input.
                    return vectors
        except Exception:
            continue

    fallback_vectors: list[list[float]] = []
    for text in texts:
        parsed_single: list[list[float]] = []
        for payload in ({"text": [text]}, {"input": [text]}, {"text": text}, {"input": text}):
            try:
                single = workers_ai_run(
                    session,
                    config.account_id,
                    config.api_token,
                    config.embedding_model,
                    payload,
                    timeout_seconds=120,
                )
                parsed_single = workers_ai_extract_embeddings(single)
                if parsed_single:
                    break
            except Exception:
                continue
        if not parsed_single:
            raise RuntimeError("Unable to parse embedding response payload")
        fallback_vectors.append(parsed_single[0])
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


def extract_release_version(summary_json: dict[str, Any]) -> str | None:
    if not isinstance(summary_json, dict):
        return None
    for key in ("overview", "professional_assessment", "newcomer_explainer"):
        value = str(summary_json.get(key) or "")
        match = re.search(r"\b(Proxmox\s+VE\s+\d+(?:\.\d+){1,3})\b", value, re.IGNORECASE)
        if match:
            return match.group(1).replace("proxmox ve", "Proxmox VE")

    changes = summary_json.get("changes")
    if isinstance(changes, list):
        for item in changes:
            if not isinstance(item, dict):
                continue
            text = f"{item.get('title') or ''} {item.get('details') or ''}"
            match = re.search(r"\b(\d+\.\d+\.\d+)\b", text)
            if match:
                return f"Proxmox VE {match.group(1)}"
    return None


def fallback_compact_summary(summary_json: dict[str, Any]) -> str:
    version = extract_release_version(summary_json) or "Proxmox VE update"
    version_num_match = re.search(r"(\d+(?:\.\d+){1,3})", version)
    version_num = version_num_match.group(1) if version_num_match else ""
    tags: list[str] = []
    seen: set[str] = set()
    for item in summary_json.get("changes", []) if isinstance(summary_json.get("changes"), list) else []:
        if not isinstance(item, dict):
            continue
        title = re.sub(r"\s+", " ", str(item.get("title") or "").strip())
        title = title.strip(" .")
        if not title:
            continue
        title = re.sub(r"^new\s+", "", title, flags=re.IGNORECASE)
        title = re.sub(r"^version bump to\s+", "", title, flags=re.IGNORECASE)
        title = title.split(":", 1)[0].strip(" .")
        if version_num and title.strip().lower() in {version_num.lower(), f"proxmox ve {version_num}".lower()}:
            continue
        key = title.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        tags.append(title)
        if len(tags) >= 8:
            break
    suffix = ", ".join(tags) if tags else "documentation and configuration updates"
    return f"{version}: {suffix}"


def normalize_compact_summary(text: str, version_hint: str | None) -> str:
    compact = text.strip().strip('`"')
    compact = re.sub(r"\s+", " ", compact).strip().rstrip(".")
    if not compact:
        return ""

    if version_hint:
        if not compact.lower().startswith(version_hint.lower()):
            right = compact.split(":", 1)[1].strip() if ":" in compact else compact
            compact = f"{version_hint}: {right}"
        else:
            # Drop duplicated immediate version token:
            # "Proxmox VE 9.1.4: 9.1.4, ..." -> "Proxmox VE 9.1.4: ..."
            version_num_match = re.search(r"(\d+(?:\.\d+){1,3})", version_hint)
            if version_num_match and ":" in compact:
                version_num = version_num_match.group(1)
                left, right = compact.split(":", 1)
                cleaned_right = right.strip()
                cleaned_right = re.sub(
                    rf"^(?:proxmox\s+ve\s+)?{re.escape(version_num)}\s*[,:\-]?\s*",
                    "",
                    cleaned_right,
                    flags=re.IGNORECASE,
                )
                compact = f"{left.strip()}: {cleaned_right.strip()}" if cleaned_right.strip() else left.strip()
    elif not compact.lower().startswith("proxmox ve"):
        compact = f"Proxmox VE update: {compact}"

    if len(compact) > 240:
        compact = compact[:237].rsplit(" ", 1)[0].rstrip(",;") + "..."
    return compact


def generate_compact_summary(
    session: requests.Session,
    config: AIConfig,
    summary_json: dict[str, Any],
) -> dict[str, Any]:
    version_hint = extract_release_version(summary_json)
    fallback = fallback_compact_summary(summary_json)
    prompt_payload = {
        "version_hint": version_hint,
        "overview": summary_json.get("overview") or "",
        "change_titles": [
            str(item.get("title") or "").strip()
            for item in summary_json.get("changes", [])
            if isinstance(item, dict)
        ][:12],
    }
    system = (
        "You write ultra-compact Proxmox changelog headlines. "
        "Return one plain-text line only, no markdown."
    )
    user = (
        "Create one short headline in this style:\n"
        "Proxmox VE X.Y.Z: tag1, tag2, tag3, ...\n"
        "Rules:\n"
        "- Start with version_hint if present.\n"
        "- Keep 4-8 concise technology tags.\n"
        "- English only.\n"
        "- Max 220 chars.\n"
        "- No trailing period.\n\n"
        f"{json.dumps(prompt_payload, ensure_ascii=False)}"
    )

    model_used = config.brief_model
    try:
        raw = workers_ai_chat_text(
            session,
            config,
            config.brief_model,
            ai_messages(system, user),
            max_tokens=120,
        )
    except Exception:
        model_used = config.brief_fallback_model
        try:
            raw = workers_ai_chat_text(
                session,
                config,
                config.brief_fallback_model,
                ai_messages(system, user),
                max_tokens=120,
            )
        except Exception:
            raw = fallback
            model_used = "fallback"

    if looks_like_ai_envelope_text(raw):
        raw = ""
    text = normalize_compact_summary(raw, version_hint)
    if not text:
        text = normalize_compact_summary(fallback, version_hint)
        model_used = "fallback"
    return {
        "text": text,
        "version": version_hint or "",
        "model": model_used,
        "generated_at": now_utc_iso(),
    }


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


def extract_translation_payload(parsed: Any, language: str) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {}
    lowered = {str(k).strip().lower(): v for k, v in parsed.items()}
    candidates = {
        "ru": ["ru", "russian", "русский"],
        "fr": ["fr", "french", "francais", "français"],
    }
    for key in candidates.get(language, [language]):
        if key in lowered:
            normalized = normalize_summary_payload(lowered[key])
            if normalized.get("overview"):
                return normalized
    translations_node = lowered.get("translations")
    if isinstance(translations_node, dict):
        nested = extract_translation_payload(translations_node, language)
        if nested:
            return nested
    return {}


def translate_single_language_summary(
    session: requests.Session,
    config: AIConfig,
    summary_json: dict[str, Any],
    language: str,
) -> dict[str, Any]:
    model_name = str(config.translation_model or "").strip()
    if "m2m100-1.2b" in model_name:
        target_lang = {"ru": "russian", "fr": "french"}.get(language, language)
        soft_failures: list[str] = []

        def tr(text: str, field_name: str, allow_english_fallback: bool = True) -> str:
            value = str(text or "").strip()
            if not value:
                return ""
            try:
                return workers_ai_translate_text(
                    session=session,
                    config=config,
                    model=model_name,
                    text=value,
                    target_lang=target_lang,
                )
            except Exception as exc:
                detail = f"{field_name}: {format_exception_message(exc)}"
                if allow_english_fallback:
                    soft_failures.append(detail)
                    return value
                raise RuntimeError(detail) from exc

        translated_changes: list[dict[str, str]] = []
        for idx, change in enumerate(summary_json.get("changes", []), start=1):
            if not isinstance(change, dict):
                continue
            translated_changes.append(
                {
                    "title": tr(str(change.get("title") or ""), f"changes[{idx}].title"),
                    "details": tr(str(change.get("details") or ""), f"changes[{idx}].details"),
                    "impact": tr(str(change.get("impact") or ""), f"changes[{idx}].impact"),
                    "recommended_action": tr(
                        str(change.get("recommended_action") or ""),
                        f"changes[{idx}].recommended_action",
                    ),
                    "severity": str(change.get("severity") or "").strip(),
                }
            )

        translated_summary = {
            "overview": tr(str(summary_json.get("overview") or ""), "overview"),
            "professional_assessment": tr(
                str(summary_json.get("professional_assessment") or ""),
                "professional_assessment",
            ),
            "newcomer_explainer": tr(str(summary_json.get("newcomer_explainer") or ""), "newcomer_explainer"),
            "changes": translated_changes,
        }

        if soft_failures:
            print(
                "[translate][warn] "
                + language
                + " used EN fallback for "
                + str(len(soft_failures))
                + " fields: "
                + " | ".join(soft_failures[:4])
            )

        return translated_summary

    model = str(config.translation_model or "").strip()
    if not model:
        raise RuntimeError("Translation model is not configured")

    language_label = {"ru": "Russian", "fr": "French"}.get(language, language)
    soft_failures: list[str] = []

    def tr_llm(text: str, field_name: str, allow_english_fallback: bool = True) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        try:
            return workers_ai_translate_text_with_llm(
                session=session,
                config=config,
                model=model,
                text=value,
                target_lang_label=language_label,
            )
        except Exception as exc:
            detail = f"{field_name}: {format_exception_message(exc)}"
            if allow_english_fallback:
                soft_failures.append(detail)
                return value
            raise RuntimeError(detail) from exc

    translated_changes: list[dict[str, str]] = []
    for idx, change in enumerate(summary_json.get("changes", []), start=1):
        if not isinstance(change, dict):
            continue
        translated_changes.append(
            {
                "title": tr_llm(str(change.get("title") or ""), f"changes[{idx}].title"),
                "details": tr_llm(str(change.get("details") or ""), f"changes[{idx}].details"),
                "impact": tr_llm(str(change.get("impact") or ""), f"changes[{idx}].impact"),
                "recommended_action": tr_llm(
                    str(change.get("recommended_action") or ""),
                    f"changes[{idx}].recommended_action",
                ),
                "severity": str(change.get("severity") or "").strip(),
            }
        )

    translated_summary = {
        "overview": tr_llm(str(summary_json.get("overview") or ""), "overview"),
        "professional_assessment": tr_llm(
            str(summary_json.get("professional_assessment") or ""),
            "professional_assessment",
        ),
        "newcomer_explainer": tr_llm(str(summary_json.get("newcomer_explainer") or ""), "newcomer_explainer"),
        "changes": translated_changes,
    }
    if soft_failures:
        print(
            "[translate][warn] "
            + language
            + " used EN fallback for "
            + str(len(soft_failures))
            + " fields: "
            + " | ".join(soft_failures[:4])
        )
    return translated_summary


def translate_summary_bundle(
    session: requests.Session,
    config: AIConfig,
    summary_json: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    translations: dict[str, Any] = {}
    error_details: dict[str, str] = {}

    for language in ("ru", "fr"):
        try:
            fallback_translation = translate_single_language_summary(session, config, summary_json, language)
            if fallback_translation:
                translations[language] = fallback_translation
            else:
                error_details[f"{language}"] = f"Missing or invalid {language} payload in translation response"
        except Exception as exc:
            error_details[f"{language}"] = format_exception_message(exc)
            continue

    translation_status = {
        "required": ["ru", "fr"],
        "present": sorted(translations.keys()),
        "missing": sorted(language for language in ("ru", "fr") if language not in translations),
        "errors": error_details,
    }
    if config.require_full_translations and translation_status["missing"]:
        details = " | ".join(f"{key}: {value}" for key, value in error_details.items() if value)
        details_suffix = f" Details: {details}" if details else ""
        raise RuntimeError(
            "Missing required translations: "
            + ", ".join(translation_status["missing"])
            + ". Re-run translation or verify translation model availability."
            + details_suffix
        )

    return translations, translation_status


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

    translations, translation_status = translate_summary_bundle(
        session=session,
        config=config,
        summary_json=summary_json,
    )

    return {
        "status": "ok",
        "generated_at": now_utc_iso(),
        "analysis_model": analysis_model_used,
        "translation_model": config.translation_model,
        "embedding_model": config.embedding_model,
        "translation_status": translation_status,
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


def row_accent_color(row_index: int) -> str:
    palette = [
        "#38bdf8",
        "#22c55e",
        "#f97316",
        "#a855f7",
        "#eab308",
        "#ef4444",
        "#14b8a6",
        "#3b82f6",
        "#84cc16",
        "#f43f5e",
    ]
    if row_index < 0:
        return palette[0]
    return palette[row_index % len(palette)]


def compact_brief_text(value: str, max_len: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rsplit(" ", 1)[0].rstrip(",;") + "..."


def localized_brief_text(event: dict[str, Any], language: str, fallback_text: str) -> str:
    if language == "en":
        return fallback_text
    ai_node = event.get("ai") if isinstance(event.get("ai"), dict) else {}
    summary_node = ai_node.get("summary") if isinstance(ai_node.get("summary"), dict) else {}
    lang_summary = summary_node.get(language) if isinstance(summary_node.get(language), dict) else {}
    overview = compact_brief_text(str(lang_summary.get("overview") or ""))
    if overview and not looks_like_ai_envelope_text(overview):
        return overview
    return fallback_text


def has_localized_brief(event: dict[str, Any], language: str) -> bool:
    ai_node = event.get("ai") if isinstance(event.get("ai"), dict) else {}
    summary_node = ai_node.get("summary") if isinstance(ai_node.get("summary"), dict) else {}
    lang_summary = summary_node.get(language) if isinstance(summary_node.get(language), dict) else {}
    overview = compact_brief_text(str(lang_summary.get("overview") or ""))
    return bool(overview and not looks_like_ai_envelope_text(overview))


def format_event_row(event: dict[str, Any], row_index: int) -> str:
    timestamp = event.get("timestamp", "-")
    timestamp_iso = str(timestamp)
    try:
        dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        timestamp_display = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        timestamp_display = timestamp_iso
    new_hash = (event.get("new_hash") or "-")[:12]
    added = event.get("added_lines", 0)
    removed = event.get("removed_lines", 0)
    diff_file = event.get("diff_file")

    if diff_file:
        html_href = docs_href_from_path(diff_html_path(diff_file))
        changelog_href = docs_href_from_path(diff_changelog_path(diff_file))
        link = (
            f'<a href="{html.escape(changelog_href)}" target="_blank" rel="noopener noreferrer" data-i18n="human_changelog">Human Changelog</a> | '
            f'<a href="{html.escape(html_href)}" target="_blank" rel="noopener noreferrer" data-i18n="code_diff">Code diff</a>'
        )
    else:
        link = "-"
    row_id = event_row_id(event)
    selector = html.escape(str(timestamp))
    brief_node = event.get("brief") if isinstance(event.get("brief"), dict) else {}
    brief_text_en = compact_brief_text(str(brief_node.get("text") or ""))
    if not brief_text_en:
        ai_node = event.get("ai") if isinstance(event.get("ai"), dict) else {}
        summary_node = ai_node.get("summary") if isinstance(ai_node.get("summary"), dict) else {}
        summary_en = summary_node.get("en") if isinstance(summary_node.get("en"), dict) else {}
        if summary_en:
            brief_text_en = compact_brief_text(fallback_compact_summary(summary_en))
    brief_text_fr = localized_brief_text(event, "fr", brief_text_en)
    brief_text_ru = localized_brief_text(event, "ru", brief_text_en)
    brief_fr_localized = has_localized_brief(event, "fr")
    brief_ru_localized = has_localized_brief(event, "ru")

    details_cell = (
        f"{link} "
        '<label class="row-select-wrap" hidden>'
        '<input type="checkbox" class="row-select" data-role="row-select"> '
        '<span data-i18n="select_entry">Select</span>'
        "</label>"
    )

    main_row_class = "has-brief" if brief_text_en else ""
    accent = row_accent_color(row_index)
    main_row = (
        f'<tr data-event-id="{row_id}" data-event-row="main" data-history-selector="{selector}" class="{main_row_class}" style="--row-accent: {accent};">'
        f'<td><time class="event-time hint-tooltip" datetime="{html.escape(timestamp_iso, quote=True)}" data-iso="{html.escape(timestamp_iso, quote=True)}" data-tooltip="" tabindex="0">{timestamp_display}</time></td>'
        f"<td><code>{new_hash}</code></td>"
        f'<td class="col-line-delta">+{added} / -{removed}</td>'
        f'<td class="details-cell">{details_cell}</td>'
        "</tr>"
    )
    if not brief_text_en:
        return main_row

    brief_attr_en = html.escape(brief_text_en, quote=True)
    brief_attr_fr = html.escape(brief_text_fr, quote=True)
    brief_attr_ru = html.escape(brief_text_ru, quote=True)
    brief_row = (
        f'<tr data-event-id="{row_id}" data-event-row="brief" data-history-selector="{selector}" class="brief-row" style="--row-accent: {accent};">'
        '<td colspan="4" class="brief-cell">'
        f'<div class="row-brief" data-brief-en="{brief_attr_en}" data-brief-fr="{brief_attr_fr}" data-brief-ru="{brief_attr_ru}" '
        f'data-brief-fr-localized="{"1" if brief_fr_localized else "0"}" data-brief-ru-localized="{"1" if brief_ru_localized else "0"}">'
        f'<span class="row-brief-text">{html.escape(brief_text_en)}</span>'
        "</div>"
        "</td>"
        "</tr>"
    )
    return main_row + brief_row


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
    generated_at_iso = now_utc_iso()
    generated_at = format_utc_display(generated_at_iso)
    license_url = f"https://github.com/{DEFAULT_GITHUB_REPO}/blob/{DEFAULT_GITHUB_REF}/LICENSE"
    page_title = f"{diff_path.name} - Human Changelog"
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

    error_block = ""
    if str(ai_data.get("status")) == "error":
        error_message = html.escape(str(ai_data.get("error") or "AI generation failed"))
        error_block = (
            '<div class="error-banner">'
            f"<strong>AI generation error:</strong> {error_message}"
            "</div>"
        )

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
    .error-banner {{
      margin-top: 14px;
      padding: 10px 12px;
      border: 1px solid #ef4444;
      border-radius: 8px;
      background: rgba(239, 68, 68, 0.12);
      color: #fecaca;
      font-size: 14px;
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
    .license-note {{
      border-bottom: 1px dotted currentColor;
      cursor: help;
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
        <h1 data-i18n="title">Human Changelog</h1>
        <p><span data-i18n="last_check">Last Check:</span> <time id="last-check-at" class="hint-tooltip" datetime="{generated_at_iso}" data-iso="{generated_at_iso}" data-tooltip="">{generated_at}</time></p>
        <p><a href="../index.html" data-i18n="back_menu">Back to main menu</a> | <a href="{html.escape(code_diff_href)}" data-i18n="open_diff">Open code diff</a></p>
      </div>
      {error_block}
      {section_html(summary_en, "en")}
      {section_html(summary_fr, "fr")}
      {section_html(summary_ru, "ru")}
      <div class="footer">
        <div><span data-i18n="model_analysis">Analysis model:</span> {html.escape(str(ai_data.get("analysis_model") or "-"))}</div>
        <div><span data-i18n="model_translation">Translation model:</span> {html.escape(str(ai_data.get("translation_model") or "-"))}</div>
        <div><span data-i18n="author">Author</span> <a href="{html.escape(author_url)}" target="_blank" rel="noopener noreferrer">{html.escape(author_name)}</a></div>
        <div><a href="{html.escape(license_url)}" target="_blank" rel="noopener noreferrer" data-i18n="footer_license">Apache-2.0</a> · <span class="license-note" data-i18n="footer_rights" data-i18n-title="footer_rights_hint" tabindex="0">Some rights reserved.</span></div>
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
          title: "Human Changelog",
          generated: "Generated:",
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
          author: "Author",
          footer_license: "Apache-2.0",
          footer_rights: "Some rights reserved.",
          footer_rights_hint: "Code in this repository is licensed under Apache License 2.0. Source Proxmox documentation/content remains under its own copyright and terms."
        }},
        fr: {{
          language: "Langue",
          theme: "Theme",
          title: "Journal des changements",
          generated: "Généré :",
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
          author: "Auteur",
          footer_license: "Apache-2.0",
          footer_rights: "Certains droits réservés.",
          footer_rights_hint: "Le code de ce dépôt est sous licence Apache License 2.0. La documentation/contenu Proxmox source reste soumis à ses propres droits et conditions."
        }},
        ru: {{
          language: "Язык",
          theme: "Тема",
          title: "Журнал изменений",
          generated: "Сгенерировано:",
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
          author: "Автор",
          footer_license: "Apache-2.0",
          footer_rights: "Некоторые права защищены.",
          footer_rights_hint: "Код этого репозитория лицензирован по Apache License 2.0. Исходная документация/контент Proxmox регулируются их собственными правами и условиями."
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
        document.querySelectorAll("[data-i18n-title]").forEach((node) => {{
          const key = node.getAttribute("data-i18n-title");
          node.title = t(lang, key);
        }});
        document.querySelectorAll("[data-lang-block]").forEach((node) => {{
          node.hidden = node.getAttribute("data-lang-block") !== lang;
        }});
      }}
      function applyTheme(theme) {{
        document.documentElement.setAttribute("data-theme", theme);
        themeSelect.value = theme;
      }}
      function formatLocalDateTime(isoValue) {{
        const date = new Date(isoValue);
        if (Number.isNaN(date.getTime())) return "";
        const pad = (num) => String(num).padStart(2, "0");
        return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ${{pad(date.getHours())}}:${{pad(date.getMinutes())}}:${{pad(date.getSeconds())}}`;
      }}
      function applyGeneratedTime() {{
        const generatedNode = document.getElementById("generated-at");
        if (!(generatedNode instanceof HTMLElement)) return;
        const iso = generatedNode.getAttribute("data-iso") || generatedNode.getAttribute("datetime") || "";
        const local = formatLocalDateTime(iso);
        if (local) generatedNode.textContent = local;
      }}
      const currentLang = readLang();
      const currentTheme = readTheme();
      applyLanguage(currentLang);
      applyTheme(currentTheme);
      applyGeneratedTime();
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
    generated_at_iso = now_utc_iso()
    generated_at = format_utc_display(generated_at_iso)
    title = f"{diff_path.name} - Diff Viewer"
    changelog_href = diff_path.with_suffix(".changelog.html").name
    license_url = f"https://github.com/{DEFAULT_GITHUB_REPO}/blob/{DEFAULT_GITHUB_REF}/LICENSE"
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
    .license-note {{
      border-bottom: 1px dotted currentColor;
      cursor: help;
    }}
    .hint-tooltip {{
      position: relative;
    }}
    .hint-tooltip::after {{
      content: attr(data-tooltip);
      position: absolute;
      left: 0;
      bottom: calc(100% + 10px);
      max-width: min(420px, 75vw);
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(15, 23, 42, 0.96);
      color: #f8fafc;
      font-size: 12px;
      line-height: 1.4;
      white-space: normal;
      box-shadow: 0 8px 20px rgba(2, 6, 23, 0.3);
      opacity: 0;
      visibility: hidden;
      transform: translateY(2px);
      transition: opacity 120ms ease, transform 120ms ease;
      transition-delay: 250ms;
      z-index: 20;
      pointer-events: none;
    }}
    .hint-tooltip::before {{
      content: "";
      position: absolute;
      left: 14px;
      bottom: calc(100% + 4px);
      border-width: 6px;
      border-style: solid;
      border-color: rgba(15, 23, 42, 0.96) transparent transparent transparent;
      opacity: 0;
      visibility: hidden;
      transform: translateY(2px);
      transition: opacity 120ms ease, transform 120ms ease;
      transition-delay: 250ms;
      z-index: 20;
      pointer-events: none;
    }}
    .hint-tooltip:hover::after,
    .hint-tooltip:hover::before,
    .hint-tooltip:focus-visible::after,
    .hint-tooltip:focus-visible::before {{
      opacity: 1;
      visibility: visible;
      transform: translateY(0);
    }}
    .license-note {{
      border-bottom: 1px dotted currentColor;
      cursor: help;
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
      <p><span data-i18n="generated">Generated:</span> <time id="generated-at" datetime="{generated_at_iso}" data-iso="{generated_at_iso}">{generated_at}</time></p>
      <p class="nav">
        <a href="../index.html" data-i18n="back_to_menu">Back to main menu</a>
        <a href="{html.escape(changelog_href)}" target="_blank" rel="noopener noreferrer" data-i18n="open_changelog">Open changelog</a>
      </p>
    </div>
    <pre class="diff">{body}</pre>
    <footer class="footer">
      <span data-i18n="footer_by">Author</span>
      <a href="{html.escape(author_url)}" target="_blank" rel="noopener noreferrer">{html.escape(author_name)}</a>
      · <a href="{html.escape(license_url)}" target="_blank" rel="noopener noreferrer" data-i18n="footer_license">Apache-2.0</a>
      · <span class="license-note" data-i18n="footer_rights" data-i18n-title="footer_rights_hint" tabindex="0">Some rights reserved.</span>
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
          generated: "Generated:",
          back_to_menu: "Back to main menu",
          open_changelog: "Open changelog",
          footer_by: "Author",
          footer_license: "Apache-2.0",
          footer_rights: "Some rights reserved.",
          footer_rights_hint: "Code in this repository is licensed under Apache License 2.0. Source Proxmox documentation/content remains under its own copyright and terms.",
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
          generated: "Généré :",
          back_to_menu: "Retour au menu principal",
          open_changelog: "Ouvrir le changelog",
          footer_by: "Auteur",
          footer_license: "Apache-2.0",
          footer_rights: "Certains droits réservés.",
          footer_rights_hint: "Le code de ce dépôt est sous licence Apache License 2.0. La documentation/contenu Proxmox source reste soumis à ses propres droits et conditions.",
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
          generated: "Сгенерировано:",
          back_to_menu: "Назад в главное меню",
          open_changelog: "Открыть changelog",
          footer_by: "Автор",
          footer_license: "Apache-2.0",
          footer_rights: "Некоторые права защищены.",
          footer_rights_hint: "Код этого репозитория лицензирован по Apache License 2.0. Исходная документация/контент Proxmox регулируются их собственными правами и условиями.",
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
        document.querySelectorAll("[data-i18n-title]").forEach((node) => {{
          const key = node.getAttribute("data-i18n-title");
          node.title = t(lang, key);
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

      function formatLocalDateTime(isoValue) {{
        const date = new Date(isoValue);
        if (Number.isNaN(date.getTime())) return "";
        const pad = (num) => String(num).padStart(2, "0");
        return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ${{pad(date.getHours())}}:${{pad(date.getMinutes())}}:${{pad(date.getSeconds())}}`;
      }}

      function formatTimezoneHint(date) {{
        const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "Local time";
        const offsetMinutes = -date.getTimezoneOffset();
        const sign = offsetMinutes >= 0 ? "+" : "-";
        const absMinutes = Math.abs(offsetMinutes);
        const hh = String(Math.floor(absMinutes / 60)).padStart(2, "0");
        const mm = String(absMinutes % 60).padStart(2, "0");
        return `${{zone}} (UTC${{sign}}${{hh}}:${{mm}})`;
      }}

      function applyGeneratedTime() {{
        const generatedNode = document.getElementById("generated-at");
        if (!(generatedNode instanceof HTMLElement)) return;
        const iso = generatedNode.getAttribute("data-iso") || generatedNode.getAttribute("datetime") || "";
        const date = new Date(iso);
        if (Number.isNaN(date.getTime())) return;
        const local = formatLocalDateTime(iso);
        if (local) generatedNode.textContent = local;
        generatedNode.dataset.tooltip = formatTimezoneHint(date);
      }}

      function applyEventTimes() {{
        document.querySelectorAll(".event-time").forEach((node) => {{
          if (!(node instanceof HTMLElement)) return;
          const iso = node.getAttribute("data-iso") || node.getAttribute("datetime") || "";
          const date = new Date(iso);
          if (Number.isNaN(date.getTime())) return;
          const local = formatLocalDateTime(iso);
          if (local) node.textContent = local;
          node.dataset.tooltip = formatTimezoneHint(date);
        }});
      }}

      const currentLang = readLang();
      const currentTheme = readTheme();
      applyLanguage(currentLang);
      applyTheme(currentTheme);
      applyGeneratedTime();

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
    rows = "\n".join(format_event_row(event, idx) for idx, event in enumerate(history))

    generated_at_iso = now_utc_iso()
    generated_at = format_utc_display(generated_at_iso)
    license_url = f"https://github.com/{github_repo}/blob/{github_ref}/LICENSE"
    github_repo_url = f"https://github.com/{github_repo}"
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Doxmox Changelog</title>
  <style>
    :root,
    :root[data-theme="dark"] {{
      --bg: #0d0f12;
      --card: #1a1d22;
      --text: #e6e8eb;
      --muted: #a3acb8;
      --line: rgba(255, 255, 255, 0.08);
      --accent: #ff6a00;
      --accent-soft: #2a2f37;
      --th-bg: #2a2f37;
      --th-text: #f4f5f7;
      --control-bg: #2a2f37;
      --control-line: rgba(255, 255, 255, 0.14);
      --control-text: #e6e8eb;
      --telegram: #229ed9;
      --telegram-hover: #1d8fc4;
      --telegram-text: #ffffff;
      --radius: 18px;
      --shadow: 0 10px 30px rgba(0, 0, 0, 0.34);
    }}
    :root[data-theme="light"] {{
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
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Inter", "Segoe UI", Tahoma, sans-serif;
      color: var(--text);
      background: radial-gradient(1200px 600px at 80% -200px, rgba(255, 106, 0, 0.12), transparent 55%), var(--bg);
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
      border-radius: 10px;
      background: var(--control-bg);
      color: var(--control-text);
      padding: 6px 8px;
      font-size: 13px;
    }}
    .control-btn {{
      border: 1px solid var(--control-line);
      border-radius: 10px;
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
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .head {{
      padding: 20px;
      background: linear-gradient(180deg, color-mix(in srgb, var(--accent-soft) 92%, transparent), color-mix(in srgb, var(--card) 86%, transparent));
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
    h1 {{
      margin: 0 0 8px;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0.01em;
      font-family: "Exo 2", "Segoe UI", Tahoma, sans-serif;
    }}
    p {{ margin: 4px 0; color: var(--muted); }}
    a {{ color: var(--accent); }}
    .row-select-wrap {{
      color: var(--muted);
      font-size: 12px;
      user-select: none;
    }}
    .row-select {{
      vertical-align: middle;
    }}
    .row-brief {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      position: relative;
      padding-left: 16px;
    }}
    .row-brief::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 1px;
      bottom: 1px;
      width: 3px;
      border-radius: 3px;
      background: color-mix(in srgb, var(--row-accent, var(--accent)) 76%, #ffffff 24%);
    }}
    tr.has-brief > td {{
      border-bottom: 0;
      padding-bottom: 8px;
    }}
    tr[data-event-row="main"].has-brief > td:first-child {{
      border-left: 4px solid var(--row-accent, var(--accent));
      padding-left: 10px;
      box-shadow: inset 0 -1px 0 color-mix(in srgb, var(--row-accent, var(--accent)) 35%, transparent);
    }}
    tr[data-event-row="main"].has-brief > td.details-cell {{
      position: relative;
      padding-left: 20px;
    }}
    tr[data-event-row="main"].has-brief > td.details-cell::before {{
      content: "";
      position: absolute;
      left: 8px;
      top: 10px;
      bottom: 10px;
      width: 3px;
      border-radius: 3px;
      background: color-mix(in srgb, var(--row-accent, var(--accent)) 78%, #ffffff 22%);
    }}
    .brief-row td {{
      background: color-mix(in srgb, var(--accent-soft) 36%, transparent);
      border-bottom: 1px solid var(--line);
      padding-top: 10px;
      padding-bottom: 12px;
    }}
    .brief-cell {{
      padding-left: 14px;
      padding-right: 14px;
      border-left: 4px solid color-mix(in srgb, var(--row-accent, var(--accent)) 70%, #ffffff 30%);
      box-shadow: inset 0 1px 0 color-mix(in srgb, var(--row-accent, var(--accent)) 28%, transparent);
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
    .col-line-delta {{
      white-space: nowrap;
      min-width: 190px;
    }}
    code {{
      font-size: 12px;
      font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
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
    .license-note {{
      border-bottom: 1px dotted currentColor;
      cursor: help;
    }}
    .github-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 18px;
      height: 18px;
      vertical-align: text-bottom;
      color: var(--muted);
      text-decoration: none;
      transition: color 120ms ease;
    }}
    .github-link:hover {{
      color: var(--accent);
    }}
    .github-link:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
      border-radius: 4px;
    }}
    .github-link svg {{
      width: 16px;
      height: 16px;
      fill: currentColor;
      display: block;
    }}
    .hint-tooltip {{
      position: relative;
      cursor: help;
    }}
    .hint-tooltip::after {{
      content: attr(data-tooltip);
      position: absolute;
      left: 0;
      bottom: calc(100% + 10px);
      max-width: min(420px, 75vw);
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(15, 23, 42, 0.96);
      color: #f8fafc;
      font-size: 12px;
      line-height: 1.4;
      white-space: normal;
      box-shadow: 0 8px 20px rgba(2, 6, 23, 0.3);
      opacity: 0;
      visibility: hidden;
      transform: translateY(2px);
      transition: opacity 120ms ease, transform 120ms ease;
      transition-delay: 250ms;
      z-index: 20;
      pointer-events: none;
    }}
    .hint-tooltip::before {{
      content: "";
      position: absolute;
      left: 14px;
      bottom: calc(100% + 4px);
      border-width: 6px;
      border-style: solid;
      border-color: rgba(15, 23, 42, 0.96) transparent transparent transparent;
      opacity: 0;
      visibility: hidden;
      transform: translateY(2px);
      transition: opacity 120ms ease, transform 120ms ease;
      transition-delay: 250ms;
      z-index: 20;
      pointer-events: none;
    }}
    .hint-tooltip:hover::after,
    .hint-tooltip:hover::before,
    .hint-tooltip:focus-visible::after,
    .hint-tooltip:focus-visible::before {{
      opacity: 1;
      visibility: visible;
      transform: translateY(0);
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
      </div>
      <div class="toolbar-right">
        <a class="telegram-link" href="https://t.me/proxmox_update" target="_blank" rel="noopener noreferrer">
          <span class="tg-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <circle class="tg-circle" cx="12" cy="12" r="11"></circle>
              <path class="tg-plane" d="M17.8 7.2 5.8 11.8c-.8.3-.8.7-.1.9l3 .9 1.2 3.5c.1.4.3.6.6.6.2 0 .4-.1.7-.3l1.7-1.7 2.8 2.1c.5.3 1 .2 1.2-.6l2.1-9c.2-.9-.3-1.3-1.2-1zM10 13.2l5.8-4c.3-.2.6.1.3.3l-4.8 4.5-.2 2 .9-2.8z"></path>
            </svg>
          </span>
          <span data-i18n="subscribe_telegram">Notifications</span>
        </a>
      </div>
    </div>
    <div class="card">
      <div class="head">
        <h1 data-i18n="title">Proxmox VE Admin Guide Changelog</h1>
        <p><span data-i18n="source">Source:</span> <a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">{html.escape(url)}</a></p>
        <p><span data-i18n="last_check">Last Check:</span> <time id="last-check-at" class="hint-tooltip" datetime="{generated_at_iso}" data-iso="{generated_at_iso}" data-tooltip="">{generated_at}</time></p>
      </div>
      <table>
        <thead>
          <tr>
            <th data-i18n="timestamp">Timestamp</th>
            <th data-i18n="hash">Hash</th>
            <th class="col-line-delta" data-i18n="line_delta">Line Delta</th>
            <th data-i18n="details">Details</th>
          </tr>
        </thead>
        <tbody>
          {rows}
          <tr data-empty-row="true" style="display:none;"><td colspan="4" data-i18n="no_changes">No changes detected yet.</td></tr>
        </tbody>
      </table>
    </div>
    <footer class="footer">
      <span data-i18n="footer_by">Author</span>
      <a href="{html.escape(author_url)}" target="_blank" rel="noopener noreferrer">{html.escape(author_name)}</a>
      · <span><span data-i18n="generated">Generated:</span> <time id="generated-at" class="hint-tooltip" datetime="{generated_at_iso}" data-iso="{generated_at_iso}" data-tooltip="">{generated_at}</time></span>
      · <a href="{html.escape(license_url)}" target="_blank" rel="noopener noreferrer" data-i18n="footer_license">Apache-2.0</a>
      · <span class="license-note hint-tooltip" data-i18n="footer_rights" data-i18n-title="footer_rights_hint" data-tooltip="" tabindex="0">Some rights reserved.</span>
      · <a class="github-link" href="{html.escape(github_repo_url)}" target="_blank" rel="noopener noreferrer" aria-label="GitHub repository" title="GitHub repository"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8a8 8 0 0 0 5.47 7.59c.4.07.55-.17.55-.38v-1.33c-2.22.48-2.69-1.07-2.69-1.07-.36-.93-.89-1.18-.89-1.18-.73-.49.06-.48.06-.48.81.06 1.23.83 1.23.83.72 1.23 1.88.88 2.34.67.07-.52.28-.88.5-1.08-1.77-.2-3.64-.89-3.64-3.95 0-.87.31-1.58.82-2.14-.08-.2-.36-1.01.08-2.1 0 0 .67-.21 2.2.82A7.64 7.64 0 0 1 8 4.84c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.09.16 1.9.08 2.1.51.56.82 1.27.82 2.14 0 3.07-1.88 3.75-3.67 3.95.29.25.54.74.54 1.49v2.21c0 .22.15.46.55.38A8 8 0 0 0 16 8c0-4.42-3.58-8-8-8Z"></path></svg></a>
    </footer>
  </div>
  <script>
    (() => {{
      const LANG_KEY = "doxmox-lang";
      const THEME_KEY = "doxmox-theme";
      const fallbackLang = "en";
      const GITHUB_REPO = "{html.escape(github_repo)}";
      const GITHUB_ADMIN_WORKFLOW = "{html.escape(github_admin_workflow)}";
      const i18n = {{
        en: {{
          title: "Proxmox VE Admin Guide Changelog",
          source: "Source:",
          last_check: "Last Check:",
          generated: "Generated:",
          timestamp: "Timestamp",
          hash: "Hash",
          line_delta: "Line Delta",
          details: "Details",
          human_changelog: "Human Changelog",
          code_diff: "Code Diff",
          no_changes: "No changes detected yet.",
          language: "Language",
          theme: "Theme",
          theme_light: "Light",
          theme_dark: "Dark",
          select_entry: "Select",
          copy_selected: "Copy selected",
          open_delete_workflow: "Open delete workflow",
          tools_status_off: "Tools: off",
          tools_status_on: "Tools: on",
          copy_selected_empty: "Select at least one entry first.",
          copy_selected_done: "Selectors copied. Paste them into the 'selectors' field in GitHub workflow.",
          copy_selected_failed: "Clipboard copy failed. Use manual copy from prompt.",
          footer_by: "Author",
          footer_license: "Apache-2.0",
          footer_rights: "Some rights reserved.",
          footer_rights_hint: "Code in this repository is licensed under Apache License 2.0. Source Proxmox documentation/content remains under its own copyright and terms.",
          subscribe_telegram: "Notifications",
          language_en: "English",
          language_fr: "French",
          language_ru: "Russian"
        }},
        fr: {{
          title: "Journal des changements du guide Proxmox VE Admin",
          source: "Source :",
          last_check: "Dernière vérification :",
          generated: "Généré :",
          timestamp: "Horodatage",
          hash: "Hash",
          line_delta: "Delta de lignes",
          details: "Details",
          human_changelog: "Human Changelog",
          code_diff: "Code Diff",
          no_changes: "Aucun changement detecte pour le moment.",
          language: "Langue",
          theme: "Theme",
          theme_light: "Clair",
          theme_dark: "Sombre",
          select_entry: "Selectionner",
          copy_selected: "Copier la selection",
          open_delete_workflow: "Ouvrir le workflow de suppression",
          tools_status_off: "Outils : off",
          tools_status_on: "Outils : on",
          copy_selected_empty: "Selectionnez au moins une entree.",
          copy_selected_done: "Selecteurs copies. Collez-les dans le champ 'selectors' du workflow GitHub.",
          copy_selected_failed: "Echec de copie dans le presse-papiers. Utilisez la copie manuelle.",
          footer_by: "Auteur",
          footer_license: "Apache-2.0",
          footer_rights: "Certains droits réservés.",
          footer_rights_hint: "Le code de ce dépôt est sous licence Apache License 2.0. La documentation/contenu Proxmox source reste soumis à ses propres droits et conditions.",
          subscribe_telegram: "Notifications",
          language_en: "Anglais",
          language_fr: "Français",
          language_ru: "Russe"
        }},
        ru: {{
          title: "Журнал изменений руководства Proxmox VE Admin",
          source: "Источник:",
          last_check: "Последняя проверка:",
          generated: "Сгенерировано:",
          timestamp: "Временная метка",
          hash: "Хэш",
          line_delta: "Изменение строк",
          details: "Детали",
          human_changelog: "Human Changelog",
          code_diff: "Code Diff",
          no_changes: "Изменения пока не обнаружены.",
          language: "Язык",
          theme: "Тема",
          theme_light: "Светлая",
          theme_dark: "Тёмная",
          select_entry: "Выбрать",
          copy_selected: "Скопировать выбранное",
          open_delete_workflow: "Открыть workflow удаления",
          tools_status_off: "Инструменты: выкл",
          tools_status_on: "Инструменты: вкл",
          copy_selected_empty: "Сначала выберите хотя бы одну запись.",
          copy_selected_done: "Селекторы скопированы. Вставьте их в поле 'selectors' в GitHub workflow.",
          copy_selected_failed: "Не удалось скопировать в буфер. Используйте ручное копирование.",
          footer_by: "Автор",
          footer_license: "Apache-2.0",
          footer_rights: "Некоторые права защищены.",
          footer_rights_hint: "Код этого репозитория лицензирован по Apache License 2.0. Исходная документация/контент Proxmox регулируются их собственными правами и условиями.",
          subscribe_telegram: "Уведомления",
          language_en: "Английский",
          language_fr: "Французский",
          language_ru: "Русский"
        }}
      }};

      const langSelect = document.getElementById("lang-select");
      const themeSelect = document.getElementById("theme-select");
      const adminPanel = document.getElementById("admin-panel");
      const openWorkflowButton = document.getElementById("open-workflow");
      const deleteSelectedButton = document.getElementById("delete-selected");
      const adminStatus = document.getElementById("admin-status");
      const tableBody = document.querySelector("tbody");
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

      function ensureEmptyRow() {{
        let row = tableBody.querySelector('tr[data-empty-row="true"]');
        if (row) return row;
        row = document.createElement("tr");
        row.setAttribute("data-empty-row", "true");
        const cell = document.createElement("td");
        cell.colSpan = 4;
        cell.setAttribute("data-i18n", "no_changes");
        cell.textContent = i18n[fallbackLang].no_changes;
        row.appendChild(cell);
        tableBody.appendChild(row);
        return row;
      }}

      function refreshEmptyRow() {{
        const mainRows = [...tableBody.querySelectorAll('tr[data-event-id][data-event-row="main"]')];
        const emptyRow = ensureEmptyRow();
        emptyRow.style.display = mainRows.length === 0 ? "" : "none";
        updateBatchDeleteState();
      }}

      function formatLocalDateTime(isoValue) {{
        const date = new Date(isoValue);
        if (Number.isNaN(date.getTime())) return "";
        const pad = (num) => String(num).padStart(2, "0");
        return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ${{pad(date.getHours())}}:${{pad(date.getMinutes())}}:${{pad(date.getSeconds())}}`;
      }}

      function applyLocalizedTime(nodeId) {{
        const timeNode = document.getElementById(nodeId);
        if (!(timeNode instanceof HTMLElement)) return;
        const iso = timeNode.getAttribute("data-iso") || timeNode.getAttribute("datetime") || "";
        const local = formatLocalDateTime(iso);
        if (local) timeNode.textContent = local;
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
        document.querySelectorAll("[data-i18n-title]").forEach((node) => {{
          const key = node.getAttribute("data-i18n-title");
          const value = t(lang, key);
          node.setAttribute("data-tooltip", value);
          node.setAttribute("aria-label", value);
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
        document.querySelectorAll(".row-brief").forEach((node) => {{
          if (!(node instanceof HTMLElement)) return;
          const textNode = node.querySelector(".row-brief-text");
          if (!(textNode instanceof HTMLElement)) return;
          const value = node.getAttribute(`data-brief-${{lang}}`) || node.getAttribute("data-brief-en") || "";
          if (value) textNode.textContent = value;
        }});
      }}

      const currentLang = readLang();
      const currentTheme = readTheme();
      applyLanguage(currentLang);
      applyTheme(currentTheme);
      applyLocalizedTime("last-check-at");
      applyLocalizedTime("generated-at");
      applyEventTimes();
      setToolsMode(false);
      refreshEmptyRow();

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

      tableBody.addEventListener("change", (event) => {{
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        if (!target.matches(".row-select")) return;
        updateBatchDeleteState();
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
        brief_model=args.ai_brief_model,
        brief_fallback_model=args.ai_brief_fallback_model,
        embedding_model=args.ai_embedding_model,
        max_diff_chars=max(10_000, int(args.ai_max_diff_chars)),
        chunk_chars=max(5_000, int(args.ai_chunk_chars)),
        vectorize_index=(args.vectorize_index.strip() if args.vectorize_index else None),
        vectorize_namespace=args.vectorize_namespace.strip() if args.vectorize_namespace else "default",
        vectorize_chunk_chars=max(500, int(args.vectorize_chunk_chars)),
        vectorize_max_chunks=max(8, int(args.vectorize_max_chunks)),
        vectorize_query_top_k=max(1, int(args.vectorize_query_top_k)),
        require_full_translations=bool(args.ai_require_full_translations),
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
        previous_ai = event.get("ai") if isinstance(event.get("ai"), dict) else None
        if isinstance(previous_ai, dict) and previous_ai.get("status") == "ok" and not force:
            continue
        diff_path = Path(str(diff_file))
        if not diff_path.exists():
            continue

        diff_text = diff_path.read_text(encoding="utf-8")
        try:
            try:
                contexts = maybe_fetch_vector_context(session, ai_config, diff_text)
            except Exception:
                contexts = []
            event["ai"] = summarize_diff_with_ai(session, ai_config, diff_text, contexts)
        except Exception as exc:
            if isinstance(previous_ai, dict) and previous_ai.get("status") == "ok":
                preserved = dict(previous_ai)
                preserved["last_attempt_error"] = str(exc)
                preserved["last_attempt_at"] = now_utc_iso()
                event["ai"] = preserved
            else:
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


def history_ai_translate(
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
        print("History is empty; nothing to translate.")
        return 0
    if not ai_config.enabled:
        print("AI is disabled. Re-run with --ai-enable.")
        return 1
    if not ai_config.account_id or not ai_config.api_token:
        print("Cloudflare credentials are missing for translation.")
        return 1

    selected = set(value.strip() for value in selectors if value.strip())
    if not process_all and not selected:
        print("No selectors were supplied. Use --history-ai-translate or --history-ai-translate-all.")
        return 1

    resolved_author_url = resolve_author_url(docs_dir, author_url)
    session = build_session()
    updated = 0
    eligible = 0
    failed = 0
    skipped_missing_en = 0

    for event in history:
        diff_file = event.get("diff_file")
        if not diff_file:
            continue
        if not process_all and not any(history_entry_matches(event, value) for value in selected):
            continue

        ai_payload = event.get("ai") if isinstance(event.get("ai"), dict) else {}
        summary_node = ai_payload.get("summary") if isinstance(ai_payload.get("summary"), dict) else {}
        summary_en = summary_node.get("en") if isinstance(summary_node.get("en"), dict) else {}
        if not summary_en or not summary_en.get("overview"):
            skipped_missing_en += 1
            continue

        has_ru = isinstance(summary_node.get("ru"), dict) and bool(summary_node.get("ru", {}).get("overview"))
        has_fr = isinstance(summary_node.get("fr"), dict) and bool(summary_node.get("fr", {}).get("overview"))
        if has_ru and has_fr and not force:
            continue

        eligible += 1
        try:
            translations, translation_status = translate_summary_bundle(
                session=session,
                config=ai_config,
                summary_json=summary_en,
            )
        except Exception as exc:
            ai_payload["translation_error"] = str(exc)
            ai_payload["translation_attempt_at"] = now_utc_iso()
            event["ai"] = ai_payload
            failed += 1
            selector = str(event.get("timestamp") or diff_file or "unknown")
            print(f"[translate][error] {selector}: {ai_payload['translation_error']}")
            continue

        merged_summary = {"en": summary_en, **translations}
        ai_payload["status"] = "ok"
        ai_payload["generated_at"] = ai_payload.get("generated_at") or now_utc_iso()
        ai_payload["translation_generated_at"] = now_utc_iso()
        ai_payload["translation_model"] = ai_config.translation_model
        ai_payload["translation_status"] = translation_status
        ai_payload["summary"] = merged_summary
        ai_payload["summary_text"] = {
            "en": format_changes_text(summary_en.get("changes", [])),
            "ru": format_changes_text(translations.get("ru", {}).get("changes", [])),
            "fr": format_changes_text(translations.get("fr", {}).get("changes", [])),
        }
        ai_payload.pop("translation_error", None)
        ai_payload.pop("translation_error_details", None)
        event["ai"] = ai_payload

        diff_path = Path(str(diff_file))
        if diff_path.exists():
            render_changelog_html(
                diff_path,
                event.get("ai", {}),
                author_name=author_name,
                author_url=resolved_author_url,
            )
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
    print(
        f"AI translation completed for {updated} entries."
        + (f" Skipped without EN summary: {skipped_missing_en}." if skipped_missing_en else "")
    )
    if eligible > 0 and updated == 0:
        print(
            f"AI translation failed: 0/{eligible} entries were updated"
            + (f" ({failed} errors)." if failed else ".")
        )
        return 2
    return 0


def history_ai_brief(
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
        print("History is empty; nothing to summarize.")
        return 0
    if not ai_config.enabled:
        print("AI is disabled. Re-run with --ai-enable.")
        return 1
    if not ai_config.account_id or not ai_config.api_token:
        print("Cloudflare credentials are missing for compact summaries.")
        return 1

    selected = set(value.strip() for value in selectors if value.strip())
    if not process_all and not selected:
        print("No selectors were supplied. Use --history-ai-brief or --history-ai-brief-all.")
        return 1

    resolved_author_url = resolve_author_url(docs_dir, author_url)
    session = build_session()
    updated = 0
    skipped_missing_en = 0

    for event in history:
        diff_file = event.get("diff_file")
        if not diff_file:
            continue
        if not process_all and not any(history_entry_matches(event, value) for value in selected):
            continue

        ai_payload = event.get("ai") if isinstance(event.get("ai"), dict) else {}
        summary_node = ai_payload.get("summary") if isinstance(ai_payload.get("summary"), dict) else {}
        summary_en = summary_node.get("en") if isinstance(summary_node.get("en"), dict) else {}
        if not summary_en or not summary_en.get("overview"):
            skipped_missing_en += 1
            continue

        previous_brief = event.get("brief") if isinstance(event.get("brief"), dict) else {}
        if previous_brief.get("text") and not force:
            continue

        try:
            event["brief"] = generate_compact_summary(session, ai_config, summary_en)
        except Exception as exc:
            event["brief"] = {
                "text": normalize_compact_summary(fallback_compact_summary(summary_en), extract_release_version(summary_en)),
                "model": "fallback",
                "version": extract_release_version(summary_en) or "",
                "generated_at": now_utc_iso(),
                "error": str(exc),
            }

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
    print(
        f"AI compact summary completed for {updated} entries."
        + (f" Skipped without EN summary: {skipped_missing_en}." if skipped_missing_en else "")
    )
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
            short_summary=None,
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
    short_summary: str | None = None

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
                previous_ai = event.get("ai") if isinstance(event.get("ai"), dict) else None
                try:
                    try:
                        contexts = maybe_fetch_vector_context(session, ai_config, diff_text)
                    except Exception:
                        contexts = []
                    ai_payload = summarize_diff_with_ai(session, ai_config, diff_text, contexts)
                    if vectorize_sync:
                        ai_payload["vectorize_sync"] = vectorize_sync
                    event["ai"] = ai_payload
                    summary_node = ai_payload.get("summary") if isinstance(ai_payload.get("summary"), dict) else {}
                    summary_en = summary_node.get("en") if isinstance(summary_node.get("en"), dict) else {}
                    if ai_payload.get("status") == "ok" and summary_en:
                        try:
                            event["brief"] = generate_compact_summary(session, ai_config, summary_en)
                        except Exception as brief_exc:
                            event["brief"] = {
                                "text": normalize_compact_summary(
                                    fallback_compact_summary(summary_en),
                                    extract_release_version(summary_en),
                                ),
                                "model": "fallback",
                                "version": extract_release_version(summary_en) or "",
                                "generated_at": now_utc_iso(),
                                "error": str(brief_exc),
                            }
                except Exception as exc:
                    if isinstance(previous_ai, dict) and previous_ai.get("status") == "ok":
                        preserved = dict(previous_ai)
                        preserved["last_attempt_error"] = str(exc)
                        preserved["last_attempt_at"] = now_utc_iso()
                        event["ai"] = preserved
                    else:
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
            brief_node = event.get("brief") if isinstance(event.get("brief"), dict) else {}
            short_summary = str(brief_node.get("text") or "").strip() or None
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
        short_summary=short_summary,
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
    parser.add_argument("--ai-brief-model", default=DEFAULT_AI_BRIEF_MODEL)
    parser.add_argument("--ai-brief-fallback-model", default=DEFAULT_AI_BRIEF_FALLBACK_MODEL)
    parser.add_argument("--ai-embedding-model", default=DEFAULT_AI_EMBEDDING_MODEL)
    parser.add_argument("--ai-max-diff-chars", type=int, default=DEFAULT_AI_MAX_DIFF_CHARS)
    parser.add_argument("--ai-chunk-chars", type=int, default=DEFAULT_AI_CHUNK_CHARS)
    parser.add_argument("--vectorize-index", default=None)
    parser.add_argument("--vectorize-namespace", default="doxmox-admin-guide")
    parser.add_argument("--vectorize-chunk-chars", type=int, default=DEFAULT_VECTORIZE_CHUNK_CHARS)
    parser.add_argument("--vectorize-max-chunks", type=int, default=DEFAULT_VECTORIZE_MAX_CHUNKS)
    parser.add_argument("--vectorize-query-top-k", type=int, default=DEFAULT_VECTORIZE_QUERY_TOP_K)
    parser.add_argument(
        "--ai-require-full-translations",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_AI_REQUIRE_FULL_TRANSLATIONS,
        help="Require ru/fr translations when AI is enabled and credentials are present.",
    )
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
    parser.add_argument(
        "--history-ai-translate",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="Translate existing EN AI summaries to ru/fr for selected history items.",
    )
    parser.add_argument(
        "--history-ai-translate-all",
        action="store_true",
        help="Translate existing EN AI summaries to ru/fr for all history entries.",
    )
    parser.add_argument(
        "--history-ai-translate-force",
        action="store_true",
        help="Regenerate ru/fr translations even if they already exist.",
    )
    parser.add_argument(
        "--history-ai-brief",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="Generate short EN compact summary from existing EN AI changelog for selected history items.",
    )
    parser.add_argument(
        "--history-ai-brief-all",
        action="store_true",
        help="Generate short EN compact summary from existing EN AI changelog for all history entries.",
    )
    parser.add_argument(
        "--history-ai-brief-force",
        action="store_true",
        help="Regenerate compact summary even if it already exists.",
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
    if args.history_ai_translate_all or args.history_ai_translate:
        return history_ai_translate(
            state_dir=Path(args.state_dir),
            docs_dir=Path(args.docs_dir),
            github_repo=args.github_repo,
            github_ref=args.github_ref,
            github_admin_workflow=args.github_admin_workflow,
            selectors=args.history_ai_translate,
            process_all=args.history_ai_translate_all,
            force=args.history_ai_translate_force,
            author_name=args.author_name,
            author_url=args.author_url,
            ai_config=ai_config,
        )
    if args.history_ai_brief_all or args.history_ai_brief:
        return history_ai_brief(
            state_dir=Path(args.state_dir),
            docs_dir=Path(args.docs_dir),
            github_repo=args.github_repo,
            github_ref=args.github_ref,
            github_admin_workflow=args.github_admin_workflow,
            selectors=args.history_ai_brief,
            process_all=args.history_ai_brief_all,
            force=args.history_ai_brief_force,
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
