#!/usr/bin/env bash
# Qanot AI — production deploy script
# Usage: ./scripts/deploy.sh [qanot|cloud|all] [--rebuild]
#
# qanot  — deploy framework (bot image + restart containers)
# cloud  — deploy qanotcloud (platform + connect app + miniapp)
# all    — deploy both (default)
set -euo pipefail

SERVER="root@46.62.250.72"
TARGET="${1:-all}"
REBUILD=""
for arg in "$@"; do [[ "$arg" == "--rebuild" ]] && REBUILD="--no-cache"; done

# ═══════════════════════════════════════
# QANOT FRAMEWORK
# ═══════════════════════════════════════
deploy_qanot() {
    local REMOTE="/opt/qanot-bot"
    local LOCAL="$(cd "$(dirname "$0")/.." && pwd)"

    echo "=== Qanot Framework Deploy ==="

    echo "[1/4] Syncing source..."
    rsync -az --delete \
        --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
        --exclude='.git' --exclude='tests/' --exclude='scripts/' \
        --exclude='docs/' --exclude='claudedocs/' --exclude='.claude/' \
        "$LOCAL/qanot/" "$SERVER:$REMOTE/qanot/"
    rsync -az --delete --exclude='__pycache__' --exclude='*.pyc' \
        "$LOCAL/plugins/" "$SERVER:$REMOTE/plugins/"
    rsync -az --delete --exclude='__pycache__' \
        "$LOCAL/templates/" "$SERVER:$REMOTE/templates/"
    echo "   Done."

    echo "[2/4] Building image..."
    ssh "$SERVER" "cd $REMOTE && docker build $REBUILD -t qanot-bot:latest . 2>&1 | tail -3"
    echo "   Done."

    echo "[3/4] Verifying..."
    ssh "$SERVER" "docker run --rm qanot-bot:latest python -c '
from qanot.agent import Agent
from qanot.registry import ToolRegistry
print(\"core OK\")
'" 2>&1
    echo "   Done."

    echo "[4/4] Restarting bots..."
    local BOTS
    BOTS=$(ssh "$SERVER" "docker ps --filter 'name=qanot-bot-' --format '{{.Names}}'" 2>/dev/null || true)
    if [ -n "$BOTS" ]; then
        for bot in $BOTS; do
            ssh "$SERVER" "docker restart $bot" 2>/dev/null
            echo "   $bot"
        done
    else
        echo "   No running bots."
    fi
    echo ""
}

# ═══════════════════════════════════════
# QANOTCLOUD PLATFORM
# ═══════════════════════════════════════
deploy_cloud() {
    local REMOTE="/root/qanotcloud"

    echo "=== QanotCloud Deploy ==="

    echo "[1/4] Pulling latest code..."
    ssh "$SERVER" "cd $REMOTE && git pull origin main 2>&1 | tail -3"
    echo "   Done."

    echo "[2/4] Rebuilding platform..."
    ssh "$SERVER" "cd $REMOTE/docker && docker compose build platform $REBUILD 2>&1 | tail -3"
    ssh "$SERVER" "cd $REMOTE/docker && docker compose up -d platform 2>&1 | tail -2"
    echo "   Done."

    echo "[3/4] Updating connect app + miniapp..."
    ssh "$SERVER" "cp $REMOTE/connect/app.py /var/www/qanot.topkey.uz/connect/app.py 2>/dev/null || true"
    ssh "$SERVER" "cp $REMOTE/miniapp/connect/index.html /var/www/plane.topkey.uz/miniapp/connect/index.html 2>/dev/null || true"
    # Restart connect app
    ssh "$SERVER" "fuser -k 8090/tcp 2>/dev/null; sleep 1; cd /var/www/qanot.topkey.uz/connect && source /root/.env.connect 2>/dev/null && nohup uvicorn app:app --host 127.0.0.1 --port 8090 > /var/log/qanot-connect.log 2>&1 &"
    echo "   Done."

    echo "[4/4] Verifying..."
    sleep 3
    local STATUS
    STATUS=$(ssh "$SERVER" "docker logs docker-platform-1 --since=5s 2>&1 | grep -c 'startup complete'" 2>/dev/null || echo "0")
    if [ "$STATUS" -gt 0 ]; then
        echo "   Platform OK"
    else
        echo "   Platform may need attention — check logs"
    fi
    echo ""
}

# ═══════════════════════════════════════
# RUN
# ═══════════════════════════════════════
case "$TARGET" in
    qanot)  deploy_qanot ;;
    cloud)  deploy_cloud ;;
    all)    deploy_qanot; deploy_cloud ;;
    *)      echo "Usage: $0 [qanot|cloud|all] [--rebuild]"; exit 1 ;;
esac

echo "=== Deploy complete ==="
