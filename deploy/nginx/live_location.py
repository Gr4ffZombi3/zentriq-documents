#!/usr/bin/env python3
"""Fuegt die Location /live/ (zentriq-live, SSE) in jede Zentriq-Server-Konfiguration ein.

Idempotent: ist /live/ bereits vorhanden, bleibt die Datei unveraendert. Nur Server-Bloecke, die
an Gunicorn (127.0.0.1:8000) weiterleiten, werden angepasst (z. B. HTTP- und HTTPS-Block von
certbot). Aufruf ueber deploy/enable-live.sh - dort mit Sicherung, nginx -t und Ruecksicherung."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repair_site import TARGET_PROXY, iter_server_blocks  # noqa: E402


def render_live_block(indent: str) -> str:
    lines = [
        "# Live-Zusammenarbeit (zentriq-live, Server-Sent Events): ohne Pufferung, lange offen.",
        "location /live/ {",
        "    proxy_pass http://127.0.0.1:8001;",
        "    proxy_http_version 1.1;",
        '    proxy_set_header Connection "";',
        "    proxy_set_header Host $host;",
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "    proxy_buffering off;",
        "    proxy_cache off;",
        "    proxy_read_timeout 1h;",
        "    proxy_send_timeout 1h;",
        "}",
    ]
    return indent + ("\n" + indent).join(lines) + "\n"


def add_live(text: str) -> str:
    result = []
    last = 0
    for start, end in iter_server_blocks(text):
        block = text[start:end]
        if TARGET_PROXY in block and not re.search(r"(?m)^\s*location\s+/live/\s*\{", block):
            match = re.search(r"(?m)^([ \t]*)location\s+/\s*\{", block)
            if match is not None:
                block = block[: match.start()] + render_live_block(match.group(1)) + "\n" + block[match.start():]
        result.append(text[last:start])
        result.append(block)
        last = end
    result.append(text[last:])
    return "".join(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    text = Path(args.input).read_text(encoding="utf-8")
    Path(args.output).write_text(add_live(text), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
