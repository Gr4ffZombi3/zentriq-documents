#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


TARGET_PROXY = "proxy_pass http://127.0.0.1:8000;"


def find_matching_brace(text: str, open_brace_index: int) -> int:
    depth = 0
    for index in range(open_brace_index, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("Unmatched brace in nginx config")


def iter_server_blocks(text: str) -> list[tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    for match in re.finditer(r"(?m)^(\s*)server\s*\{", text):
        open_brace_index = text.find("{", match.start())
        close_brace_index = find_matching_brace(text, open_brace_index)
        blocks.append((match.start(), close_brace_index + 1))
    return blocks


def render_static_block(indent: str) -> str:
    inner = indent + "    "
    return (
        f"{indent}location /static/ {{\n"
        f"{inner}proxy_pass http://127.0.0.1:8000;\n"
        f"{inner}proxy_set_header Host $host;\n"
        f"{inner}proxy_set_header X-Real-IP $remote_addr;\n"
        f"{inner}proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        f"{inner}proxy_set_header X-Forwarded-Proto $scheme;\n"
        f"{indent}}}\n"
    )


def rewrite_named_location_block(server_block: str, location_name: str) -> str:
    pattern = re.compile(rf"(?m)^(\s*)location\s+{re.escape(location_name)}\s*\{{")
    match = pattern.search(server_block)
    if match is None:
        return server_block

    open_brace_index = server_block.find("{", match.start())
    close_brace_index = find_matching_brace(server_block, open_brace_index)
    replacement = render_static_block(match.group(1))
    return server_block[: match.start()] + replacement + server_block[close_brace_index + 1 :]


def insert_static_location(server_block: str) -> str:
    match = re.search(r"(?m)^(\s*)location\s+/\s*\{", server_block)
    if match is not None:
        indent = match.group(1)
        replacement = render_static_block(indent)
        return server_block[: match.start()] + replacement + "\n" + server_block[match.start() :]

    closing_match = re.search(r"(?m)^(\s*)\}\s*$", server_block)
    if closing_match is None:
        return server_block
    indent = closing_match.group(1) + "    "
    replacement = "\n" + render_static_block(indent) + closing_match.group(1)
    return server_block[: closing_match.start()] + replacement + server_block[closing_match.start() :]


def rewrite_server_block(server_block: str) -> str:
    if TARGET_PROXY not in server_block:
        return server_block

    if re.search(r"(?m)^\s*location\s+/static/\s*\{", server_block):
        return rewrite_named_location_block(server_block, "/static/")

    return insert_static_location(server_block)


def rewrite_config(text: str) -> str:
    blocks = iter_server_blocks(text)
    if not blocks:
        raise ValueError("No nginx server blocks found")

    rewritten_parts: list[str] = []
    last_index = 0
    touched = False

    for start, end in blocks:
        rewritten_parts.append(text[last_index:start])
        original_block = text[start:end]
        updated_block = rewrite_server_block(original_block)
        if updated_block != original_block:
            touched = True
        rewritten_parts.append(updated_block)
        last_index = end

    rewritten_parts.append(text[last_index:])
    rewritten = "".join(rewritten_parts)
    if TARGET_PROXY not in rewritten:
        raise ValueError("No Zentriq proxy server block found in nginx config")
    if not touched and "/static/" not in rewritten:
        raise ValueError("Unable to place /static/ location into nginx config")
    return rewritten


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair Zentriq nginx server blocks without touching SSL setup.")
    parser.add_argument("--input", required=True, dest="input_path")
    parser.add_argument("--output", required=True, dest="output_path")
    args = parser.parse_args()

    source_text = Path(args.input_path).read_text(encoding="utf-8")
    rewritten = rewrite_config(source_text)
    Path(args.output_path).write_text(rewritten, encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - deploy script output
        print(f"[repair-nginx] FEHLER: {exc}", file=sys.stderr)
        raise
