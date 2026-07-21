#!/usr/bin/env bash

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$DEPLOY_DIR/.." && pwd)}"

NGINX_SITES_AVAILABLE_DIR="${NGINX_SITES_AVAILABLE_DIR:-/etc/nginx/sites-available}"
NGINX_SITES_ENABLED_DIR="${NGINX_SITES_ENABLED_DIR:-/etc/nginx/sites-enabled}"
NGINX_CONF_D_DIR="${NGINX_CONF_D_DIR:-/etc/nginx/conf.d}"

load_app_env() {
    if [ -f "$APP_DIR/.env" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$APP_DIR/.env"
        set +a
    fi
}

normalize_host() {
    local raw="${1:-}"
    raw="${raw#http://}"
    raw="${raw#https://}"
    raw="${raw%%/*}"
    raw="${raw%%;*}"
    raw="${raw%%:*}"
    raw="${raw#\"}"
    raw="${raw%\"}"
    raw="${raw#\'}"
    raw="${raw%\'}"
    raw="${raw#\[}"
    raw="${raw%\]}"
    case "$raw" in
        ""|"_"|"localhost"|"127.0.0.1")
            return 1
            ;;
    esac
    printf '%s\n' "$raw"
}

extract_scheme_from_url() {
    local raw="${1:-}"
    if [[ "$raw" =~ ^([A-Za-z][A-Za-z0-9+.-]*):// ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    return 1
}

first_server_name_from_file() {
    local file_path="${1:-}"
    [ -f "$file_path" ] || return 1
    awk '
        BEGIN { IGNORECASE = 1 }
        $1 == "server_name" {
            for (i = 2; i <= NF; i++) {
                gsub(/;/, "", $i)
                if ($i != "" && $i != "_" && $i != "localhost" && $i != "127.0.0.1") {
                    print $i
                    exit
                }
            }
        }
    ' "$file_path"
}

resolve_explicit_public_host() {
    load_app_env
    local candidate
    for candidate in "${PUBLIC_HOST:-}" "${DOMAIN:-}" "${SERVER_NAME:-}"; do
        if candidate="$(normalize_host "$candidate" 2>/dev/null)"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    for candidate in "${PUBLIC_URL:-}" "${APP_URL:-}"; do
        if candidate="$(normalize_host "$candidate" 2>/dev/null)"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

resolve_nginx_site_file() {
    local -a candidates=()
    local candidate

    if [ -n "${NGINX_SITE_FILE:-}" ] && [ -f "$NGINX_SITE_FILE" ]; then
        printf '%s\n' "$NGINX_SITE_FILE"
        return 0
    fi

    if [ -L "$NGINX_SITES_ENABLED_DIR/zentriq" ]; then
        readlink -f "$NGINX_SITES_ENABLED_DIR/zentriq"
        return 0
    fi

    if [ -f "$NGINX_SITES_AVAILABLE_DIR/zentriq" ]; then
        printf '%s\n' "$NGINX_SITES_AVAILABLE_DIR/zentriq"
        return 0
    fi

    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        candidates+=("$(readlink -f "$candidate")")
    done < <(
        grep -Rsl "127\\.0\\.0\\.1:8000" \
            "$NGINX_SITES_ENABLED_DIR" \
            "$NGINX_SITES_AVAILABLE_DIR" \
            "$NGINX_CONF_D_DIR" 2>/dev/null | sort -u
    )

    if [ "${#candidates[@]}" -eq 1 ]; then
        printf '%s\n' "${candidates[0]}"
        return 0
    fi

    if [ "${#candidates[@]}" -gt 1 ]; then
        local -a zentriq_named=()
        local base_name
        for candidate in "${candidates[@]}"; do
            base_name="$(basename "$candidate")"
            if [[ "$base_name" == *zentriq* ]]; then
                zentriq_named+=("$candidate")
            fi
        done
        if [ "${#zentriq_named[@]}" -eq 1 ]; then
            printf '%s\n' "${zentriq_named[0]}"
            return 0
        fi

        local resolved_host
        resolved_host="$(resolve_explicit_public_host 2>/dev/null || true)"
        if [ -n "$resolved_host" ]; then
            local -a host_matches=()
            for candidate in "${candidates[@]}"; do
                if grep -Eq "(^|[[:space:]])server_name[[:space:]].*${resolved_host//./\\.}([[:space:];]|$)" "$candidate"; then
                    host_matches+=("$candidate")
                fi
            done
            if [ "${#host_matches[@]}" -eq 1 ]; then
                printf '%s\n' "${host_matches[0]}"
                return 0
            fi
        fi
    fi

    return 1
}

resolve_public_host() {
    local candidate
    if candidate="$(resolve_explicit_public_host 2>/dev/null || true)"; then
        if [ -n "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi

    local site_file
    site_file="$(resolve_nginx_site_file 2>/dev/null || true)"
    if [ -n "$site_file" ]; then
        candidate="$(first_server_name_from_file "$site_file" 2>/dev/null || true)"
        if candidate="$(normalize_host "$candidate" 2>/dev/null)"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi

    return 1
}

resolve_public_scheme() {
    load_app_env
    local scheme
    for scheme in "${PUBLIC_URL:-}" "${APP_URL:-}"; do
        if scheme="$(extract_scheme_from_url "$scheme" 2>/dev/null)"; then
            printf '%s\n' "$scheme"
            return 0
        fi
    done

    local site_file host
    site_file="$(resolve_nginx_site_file 2>/dev/null || true)"
    host="$(resolve_public_host 2>/dev/null || true)"
    if [ -n "$site_file" ] && [ -n "$host" ]; then
        if awk -v host="$host" '
            BEGIN { IGNORECASE = 1; in_server = 0; depth = 0; host_match = 0; ssl_match = 0 }
            /server[[:space:]]*\{/ {
                in_server = 1
            }
            in_server {
                if ($1 == "server_name") {
                    for (i = 2; i <= NF; i++) {
                        gsub(/;/, "", $i)
                        if ($i == host) {
                            host_match = 1
                        }
                    }
                }
                if ($1 == "listen") {
                    for (i = 2; i <= NF; i++) {
                        gsub(/;/, "", $i)
                        if ($i == "443" || $i ~ /^443$/ || $i ~ /^443ssl$/ || $i ~ /:443$/) {
                            ssl_match = 1
                        }
                    }
                }
                depth += gsub(/\{/, "{")
                depth -= gsub(/\}/, "}")
                if (depth <= 0) {
                    if (host_match && ssl_match) {
                        print "https"
                        exit
                    }
                    in_server = 0
                    depth = 0
                    host_match = 0
                    ssl_match = 0
                }
            }
        ' "$site_file" | grep -q '^https$'; then
            printf '%s\n' "https"
            return 0
        fi
    fi

    printf '%s\n' "http"
}

resolve_public_base_url() {
    load_app_env
    local url host scheme
    for url in "${PUBLIC_URL:-}" "${APP_URL:-}"; do
        if [ -n "$url" ]; then
            printf '%s\n' "${url%/}"
            return 0
        fi
    done

    host="$(resolve_public_host 2>/dev/null || true)"
    [ -n "$host" ] || return 1
    scheme="$(resolve_public_scheme)"
    printf '%s://%s\n' "$scheme" "$host"
}
