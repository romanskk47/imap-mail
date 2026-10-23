"""Optional lossless review projection; never changes frozen MIME or journals."""

from __future__ import annotations

import copy

from .core import digest


def compact_review(value):
    """Keep complete plain/HTML bodies once, metadata, changes and exact hashes.

    This is opt-in. Legacy full results and text-only MCP JSON consumers remain
    unchanged. HTML is not inferred equivalent to plain text or silently omitted.
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
    if isinstance(result.get("before"), dict):
        result["before"] = compact_review(result["before"])
    result["response_view"] = "compact"
    result["review_completeness"] = {
        "body_truncated": False,
        "html_preserved": result.get("body_html") is not None,
        "attachment_manifest_preserved": True,
        "duplicated_mime_body_contents_omitted": omitted,
        "additional_mime_body_contents_preserved": additional,
    }
    return result
