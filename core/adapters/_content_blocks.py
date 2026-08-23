from __future__ import annotations


def flatten_anthropic_content(content) -> tuple[str, bool]:
    """Flatten Anthropic-style message content (string or list of content
    blocks) into (text, has_tool_call). Shared by claude_code and codewhale_tui,
    since both store Anthropic-API-shaped content blocks."""
    if isinstance(content, str):
        return content, False

    if not isinstance(content, list):
        return "", False

    text_parts: list[str] = []
    has_tool_call = False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "thinking":
            thinking = block.get("thinking", "")
            if thinking:
                text_parts.append(f"[thinking] {thinking}")
        elif block_type == "tool_use":
            has_tool_call = True
            name = block.get("name", "?")
            text_parts.append(f"[tool_use: {name}]")
        elif block_type == "tool_result":
            has_tool_call = True
            inner = block.get("content", "")
            if isinstance(inner, list):
                inner_text, _ = flatten_anthropic_content(inner)
            else:
                inner_text = str(inner)
            text_parts.append(f"[tool_result] {inner_text}")

    return "\n".join(p for p in text_parts if p), has_tool_call
