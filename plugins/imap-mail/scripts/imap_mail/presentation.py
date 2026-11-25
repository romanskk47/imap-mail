"""Optional lossless review projection; never changes frozen MIME or journals."""

from __future__ import annotations

import copy

from .core import digest


QUOTE_EXCERPT_LINES = 15


def _elide_quote(result):
    """Replace the server-derived quoted original with a hashed summary.

    The authored text and signature stay complete. The quote is copied from the
    source message (readable with mail_read) and its full rendering remains in
    response_view=full and mail_preview_read. Split only at the offset recorded
    when the message was composed, never by searching the text.
    """
    quote = result.get("quote")
    body = result.get("body_text")
    if not isinstance(quote, dict) or not isinstance(body, str):
        return False
    offset = quote.get("rendered_offset")
    if not isinstance(offset, int) or not 0 < offset <= len(body):
        return False
    rendered = body[offset:]
    result["body_text"] = body[:offset].rstrip()
    lines = rendered.splitlines()
    result["quoted_original"] = {
        "elided": True, "format": quote.get("format"), "chars": quote.get("chars"),
        "lines": quote.get("lines"), "sha256": quote.get("sha256"),
        "rendered_sha256": digest(rendered.encode("utf-8")),
        "excerpt": "\n".join(lines[:QUOTE_EXCERPT_LINES]),
        "excerpt_truncated": len(lines) > QUOTE_EXCERPT_LINES,
        "full_view": "mail_preview_read with response_view=full",
    }
    return True


def _summarize_before(before):
    """The pre-edit bodies are the unchanged source; keep hashes, not copies."""
    for name in ("body_text", "body_html"):
        value = before.get(name)
        if isinstance(value, str):
            before[name] = {"elided": True, "chars": len(value),
                            "sha256": digest(value.encode("utf-8"))}
    return before


def compact_review(value):
    """Keep authored bodies once, metadata, changes and exact hashes.

    The MCP default; CLI/Python callers and response_view=full keep the legacy
    full result. HTML is not inferred equivalent to plain text or silently
    omitted. Only server-derived content (the quoted original and the unchanged
    pre-edit body) is summarized, always with hashes.
    """
    result = copy.deepcopy(value)
    if not isinstance(result, dict):
        return result
    if "rendered_body_text" in result:
        result["body_text"] = result.pop("rendered_body_text")
        result["body_text_scope"] = "complete_rendered_message"
    if "rendered_body_html" in result:
        result["body_html"] = result.pop("rendered_body_html")
    signature = result.get("signature")
    if isinstance(signature, dict):
        for name in ("plain", "html"):
            if name in signature:
                content = signature[name]
                signature[name + "_sha256"] = digest(content.encode("utf-8"))
                # A creation preview has no full HTML body field: retain its
                # HTML signature so links/markup are not hidden by projection.
                if name == "plain" or result.get("body_html") is not None:
                    signature.pop(name)
        signature["content_location"] = "complete_message_bodies"
    omitted = 0
    additional = 0
    for part in result.get("mime_parts", []):
        # Body contents remain available in body_text/body_html, while part
        # metadata, MIME position, byte length and hashes stay reviewable.
        if not isinstance(part, dict) or "content" not in part:
            continue
        canonical = (result.get("body_text") if part.get("type") == "text/plain"
                     else result.get("body_html") if part.get("type") == "text/html"
                     else None)
        normalize = lambda text: text.replace("\r\n", "\n").replace("\r", "\n")
        if isinstance(canonical, str) and normalize(canonical) == normalize(part["content"]):
            part.pop("content")
            omitted += 1
        else:
            # Preserve alternative text/HTML bodies not represented by the
            # canonical fields. A compact review must not hide disagreements.
            additional += 1
    quoted = _elide_quote(result)
    if isinstance(result.get("before"), dict):
        result["before"] = _summarize_before(compact_review(result["before"]))
    result["response_view"] = "compact"
    result["review_completeness"] = {
        "body_truncated": "quoted_original_only" if quoted else False,
        "html_preserved": result.get("body_html") is not None,
        "attachment_manifest_preserved": True,
        "duplicated_mime_body_contents_omitted": omitted,
        "additional_mime_body_contents_preserved": additional,
    }
    return result
