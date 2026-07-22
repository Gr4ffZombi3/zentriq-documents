from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
POST_DEPLOY_SCRIPT = ROOT_DIR / "deploy" / "post-deploy.sh"
BASH_CANDIDATES = (
    Path(r"C:\Program Files\Git\bin\bash.exe"),
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
)


def _bash_path() -> Path:
    for candidate in BASH_CANDIDATES:
        if candidate.exists():
            return candidate
    raise AssertionError("Git Bash wurde fuer den Shell-Skript-Test nicht gefunden.")


def _run_bash(command: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["POST_DEPLOY_SCRIPT"] = str(POST_DEPLOY_SCRIPT)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(_bash_path()), "-lc", command],
        cwd=ROOT_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_extract_last_header_value_prefers_final_response_header():
    header_blob = (
        "HTTP/1.1 301 Moved Permanently\r\n"
        "Content-Type: text/html\r\n"
        "Location: https://www.zentriqai.de/static/css/app.css\r\n"
        "\r\n"
        "HTTP/2 200\r\n"
        "Content-Type: text/css\r\n"
        "Content-Length: 81983\r\n"
        "\r\n"
    )
    command = """
source "$(cygpath -u "$POST_DEPLOY_SCRIPT")"
printf '%s' "$TEST_HEADERS" | extract_last_header_value "Content-Type"
"""
    result = _run_bash(command, {"TEST_HEADERS": header_blob})
    assert result.stdout.strip() == "text/css"


def test_curl_final_headers_uses_get_with_dumped_headers():
    command = """
source "$(cygpath -u "$POST_DEPLOY_SCRIPT")"
curl() { printf '%s' "$*"; }
curl_final_headers --resolve "www.zentriqai.de:80:127.0.0.1" --resolve "www.zentriqai.de:443:127.0.0.1" "http://www.zentriqai.de/static/css/app.css"
"""
    result = _run_bash(command)
    args = result.stdout
    assert "-D -" in args
    assert "-o /dev/null" in args
    assert "-L" in args
    assert "-I" not in args


def test_curl_content_type_uses_final_response_metadata():
    command = """
source "$(cygpath -u "$POST_DEPLOY_SCRIPT")"
curl() { printf '%s' "$*"; }
curl_content_type --resolve "www.zentriqai.de:80:127.0.0.1" --resolve "www.zentriqai.de:443:127.0.0.1" "http://www.zentriqai.de/static/css/app.css"
"""
    result = _run_bash(command)
    args = result.stdout
    assert "%{content_type}" in args
    assert "-o /dev/null" in args
    assert "-L" in args
    assert "-I" not in args


def test_post_deploy_uses_80kb_css_threshold():
    script_text = POST_DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "CSS_MIN_BYTES=80000" in script_text
