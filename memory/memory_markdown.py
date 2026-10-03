from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

from memory.errors import MemoryError

SECTION_PATTERN = re.compile(
    r"<!-- SECTION:(?P<name>[A-Z0-9_ -]+) -->\s*(?P<content>.*?)\s*<!-- /SECTION:(?P=name) -->",
    re.DOTALL,
)
MARKER_PATTERN = re.compile(r"<!-- ?/?SECTION:[A-Z0-9_ -]+ ?-->", re.IGNORECASE)


def parse_sections(markdown_text: str) -> dict[str, dict[str, Any]]:
    text = str(markdown_text or "")
    out: dict[str, dict[str, Any]] = {}
    for match in SECTION_PATTERN.finditer(text):
        name = match.group("name").strip().upper()
        out[name] = {
            "section": name,
            "heading": _find_heading_before(text, match.start()) or name.replace("_", " ").title(),
            "content": (match.group("content") or "").strip(),
            "full_start": match.start(),
            "full_end": match.end(),
            "content_start": match.start("content"),
            "content_end": match.end("content"),
        }
    return out


def extract_context_blocks(markdown_text: str) -> list[dict[str, str]]:
    sections = parse_sections(markdown_text)
    if sections:
        return [dict(item) for item in sections.values() if str(item.get("content") or "").strip()]
    stripped = strip_section_markers(markdown_text).strip()
    if not stripped:
        return []
    return [{"section": "", "heading": "", "content": stripped}]


def strip_section_markers(markdown_text: str) -> str:
    return MARKER_PATTERN.sub("", str(markdown_text or ""))


def replace_section(markdown_text: str, section_name: str, content: str, *, heading_label: str | None = None) -> str:
    text = str(markdown_text or "")
    section_name = _normalize_section_name(section_name)
    normalized = _normalize_block(content)
    sections = parse_sections(text)
    current = sections.get(section_name)
    if current:
        return text[: current["content_start"]] + normalized + text[current["content_end"] :]

    block = _build_section_block(section_name, heading_label or section_name.replace("_", " ").title(), normalized)
    return text.rstrip() + "\n\n" + block if text.strip() else block


def append_section_note(markdown_text: str, section_name: str, note: str, *, heading_label: str | None = None) -> str:
    note_line = _normalize_note_line(note)
    text = str(markdown_text or "")
    sections = parse_sections(text)
    current = sections.get(_normalize_section_name(section_name))
    if not current:
        return replace_section(text, section_name, note_line, heading_label=heading_label)
    content = current["content"].strip()
    merged = note_line if not content else content + "\n" + note_line
    return replace_section(text, section_name, merged, heading_label=heading_label)


def append_log_entry(markdown_text: str, section_name: str, content: str, *, heading_label: str | None = None) -> str:
    entry = str(content or "").replace("\r\n", "\n").strip()
    if not entry:
        raise MemoryError("memory_empty_log_entry", "Log entry content must not be empty.", 400)
    text = str(markdown_text or "")
    sections = parse_sections(text)
    current = sections.get(_normalize_section_name(section_name))
    if not current:
        return replace_section(text, section_name, entry, heading_label=heading_label)
    existing = current["content"].strip()
    merged = entry if not existing else existing + "\n\n" + entry
    return replace_section(text, section_name, merged, heading_label=heading_label)


def mark_stale(markdown_text: str, section_name: str, note: str, *, heading_label: str | None = None) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return append_section_note(
        markdown_text,
        section_name,
        f"> Veraltet-Hinweis ({today}): {str(note or '').strip()}",
        heading_label=heading_label,
    )


def apply_operation(
    markdown_text: str,
    *,
    strategy: str,
    section_name: str,
    content: str,
    heading_label: str | None = None,
) -> str:
    op = str(strategy or "").strip().lower()
    if op == "replace_section":
        return replace_section(markdown_text, section_name, content, heading_label=heading_label)
    if op in {"append_section_note", "append"}:
        return append_section_note(markdown_text, section_name, content, heading_label=heading_label)
    if op == "append_log_entry":
        return append_log_entry(markdown_text, section_name, content, heading_label=heading_label)
    if op == "mark_stale":
        return mark_stale(markdown_text, section_name, content, heading_label=heading_label)
    raise MemoryError("memory_strategy_invalid", f"Unsupported write strategy: {strategy}", 400)


def render_markdown(markdown_text: str) -> str:
    text = strip_section_markers(markdown_text).replace("\r\n", "\n").strip("\n")
    if not text:
        return "<p class=\"memory-empty\">Kein Inhalt vorhanden.</p>"

    lines = text.split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    quote_lines: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            paragraph_text = " ".join(paragraph).strip()
            if paragraph_text == "_Noch offen._":
                out.append("<p class=\"memory-empty\">Noch offen. Dieser Bereich wurde bisher noch nicht beschrieben.</p>")
            else:
                out.append(f"<p>{_render_inline(paragraph_text)}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            out.append("<ul>" + "".join(f"<li>{_render_inline(item)}</li>" for item in list_items) + "</ul>")
            list_items = []

    def flush_quote() -> None:
        nonlocal quote_lines
        if quote_lines:
            out.append("<blockquote>" + "".join(f"<p>{_render_inline(item)}</p>" for item in quote_lines) + "</blockquote>")
            quote_lines = []

    def flush_code() -> None:
        nonlocal code_lines
        if code_lines:
            out.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            code_lines = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            flush_quote()
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            flush_quote()
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            flush_list()
            flush_quote()
            level = min(6, len(stripped) - len(stripped.lstrip("#")))
            out.append(f"<h{level}>{_render_inline(stripped[level:].strip())}</h{level}>")
            continue
        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            flush_quote()
            list_items.append(stripped[2:].strip())
            continue
        if stripped.startswith("> "):
            flush_paragraph()
            flush_list()
            quote_lines.append(stripped[2:].strip())
            continue
        paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    flush_quote()
    if in_code:
        flush_code()
    return "\n".join(out)


def _find_heading_before(text: str, position: int) -> str:
    prefix = text[:position]
    for line in reversed(prefix.splitlines()):
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip()
    return ""


def _normalize_section_name(section_name: str) -> str:
    return str(section_name or "").strip().upper()


def _normalize_block(content: str) -> str:
    text = str(content or "").replace("\r\n", "\n").strip()
    if not text:
        text = "_Noch offen._"
    return text + "\n"


def _normalize_note_line(note: str) -> str:
    text = str(note or "").strip()
    if not text:
        raise MemoryError("memory_empty_note", "Section note content must not be empty.", 400)
    if text.startswith(("-", ">", "*")):
        return text
    return f"- {text}"


def _build_section_block(section_name: str, heading_label: str, normalized_content: str) -> str:
    return (
        f"## {heading_label}\n"
        f"<!-- SECTION:{section_name} -->\n"
        f"{normalized_content}"
        f"<!-- /SECTION:{section_name} -->\n"
    )


def _render_inline(text: str) -> str:
    escaped = html.escape(str(text or ""))
    escaped = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", lambda m: f"<em>{m.group(1)}</em>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: f"<a href=\"{m.group(2)}\">{m.group(1)}</a>", escaped)
    return escaped
