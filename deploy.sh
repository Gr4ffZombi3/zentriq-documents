#!/usr/bin/env bash
# Einstiegspunkt fuer Deployments: `git pull && ./deploy.sh` auf dem Server.
# Delegiert vollstaendig an deploy/update.sh (siehe dort fuer die einzelnen Schritte,
# oder DEPLOYMENT.md fuer die vollstaendige Dokumentation).
set -euo pipefail
cd "$(dirname "$0")"
exec ./deploy/update.sh
