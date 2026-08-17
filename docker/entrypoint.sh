#!/usr/bin/env sh
# Role switch for the one felix image (see DEPLOY.md §2). $ROLE picks which
# process this container runs:
#
#   web    → the API + built React UI on :8000        (public — gets a DuckDNS name)
#   watch  → the CDC metrics watcher, standalone       (no public port — no DuckDNS)
#   sample → the interactive checkout demo service    (public — gets a DuckDNS name)
#
# The deployed demo runs ONE task with ROLE=web and FELIX_RUN_WATCHER=1: the
# API process also starts the CDC watcher on a background thread, sharing the
# single in-memory embedding model (DEPLOY.md §4) — so web+watch cost one ~4GB
# task, not two. The standalone `watch` role stays for local dev / a future
# split; it is not used by the merged deploy.
#
# Public roles self-register their DuckDNS name on boot so the link survives a
# task restart (Fargate hands out a fresh public IP each time). The final server
# runs under `exec` so it becomes PID 1 and receives SIGTERM directly — uvicorn's
# lifespan (web) and the CLI's SIGTERM trap (watch) both shut down cleanly.
set -eu

# Point DuckDNS at this container's public IP. DuckDNS reads the caller's source
# IP when ip= is empty, so no IP lookup is needed here. Best-effort: a failed
# update must not stop the container from serving. The token stays out of logs
# (we echo only the domain) and out of the process table — the request URL is
# fed to curl on stdin via `-K -`, not as an argv element readable via /proc.
update_duckdns() {
    if [ -n "${DUCKDNS_DOMAIN:-}" ]; then
        echo "[entrypoint] updating DuckDNS record for $DUCKDNS_DOMAIN"
        if ! printf 'url = "https://www.duckdns.org/update?domains=%s&token=%s&ip="\n' \
            "$DUCKDNS_DOMAIN" "${DUCKDNS_TOKEN:-}" \
            | curl -fsS -o /dev/null -K -; then
            echo "[entrypoint] warning: DuckDNS update failed — continuing"
        fi
    fi
}

case "${ROLE:-}" in
    web)
        update_duckdns
        exec python -m src serve --host 0.0.0.0 --port 8000
        ;;
    watch)
        exec python -m src watch
        ;;
    sample)
        update_duckdns
        exec python -m sample_project.server
        ;;
    *)
        echo "[entrypoint] error: ROLE must be one of: web, watch, sample (got '${ROLE:-}')" >&2
        exit 1
        ;;
esac
