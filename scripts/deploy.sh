#!/usr/bin/env bash
# Qanot AI — production deploy script
# Usage: ./scripts/deploy.sh [qanot|cloud|all] [--rebuild]
set -euo pipefail

SERVER="root@46.62.250.72"
TARGET="${1:-all}"
REBUILD=""
for arg in "$@"; do [[ "$arg" == "--rebuild" ]] && REBUILD="--no-cache"; done

# ═══════════════════════════════════════
# QANOT FRAMEWORK
# ═══════════════════════════════════════
deploy_qanot() {
    local REMOTE="/root/qanotai"
    local LOCAL="$(cd "$(dirname "$0")/.." && pwd)"

    echo "=== Qanot Framework Deploy ==="

    echo "[1/5] Pushing to remotes..."
    cd "$LOCAL"
    git push origin main 2>&1 | tail -2
    git push qanot main 2>&1 | tail -2
    echo "   Done."

    echo "[2/5] Pulling & building image on server..."
    ssh "$SERVER" "cd $REMOTE && git pull origin main 2>&1 | tail -3"
    ssh "$SERVER" "cd $REMOTE && docker build $REBUILD -t qanot-bot:latest . 2>&1 | tail -3"
    echo "   Done."

    echo "[3/5] Verifying..."
    ssh "$SERVER" "docker run --rm qanot-bot:latest python -c '
from qanot.agent import Agent
from qanot.registry import ToolRegistry
print(\"core OK\")
'" 2>&1
    echo "   Done."

    echo "[4/5] Recreating bot containers..."
    scp -q "$(cd "$(dirname "$0")" && pwd)/recreate_containers.py" "$SERVER:/tmp/recreate_containers.py"
    ssh "$SERVER" "python3 /tmp/recreate_containers.py"
    echo "   Done."

    echo "[5/5] Health check..."
    sleep 8
    ssh "$SERVER" 'for name in $(docker ps --filter "name=qanot-bot-" --format "{{.Names}}" 2>/dev/null); do
        status=$(docker inspect "$name" --format "{{.State.Status}}" 2>/dev/null)
        echo "   $name: $status"
    done'
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
    ssh "$SERVER" "cp -r $REMOTE/miniapp/connect/ /var/www/plane.topkey.uz/miniapp/connect/ 2>/dev/null || true"
    ssh "$SERVER" "systemctl restart qanot-connect 2>/dev/null || (fuser -k 8090/tcp 2>/dev/null; sleep 1; cd /var/www/qanot.topkey.uz/connect && source /root/.env.connect 2>/dev/null && nohup uvicorn app:app --host 127.0.0.1 --port 8090 > /var/log/qanot-connect.log 2>&1 &)"
    echo "   Done."

    echo "[4/4] Verifying..."
    sleep 3
    ssh "$SERVER" "curl -s -o /dev/null -w 'platform: %{http_code}' http://localhost:8010/docs 2>/dev/null; echo ''"
    ssh "$SERVER" "curl -s -o /dev/null -w 'connect:  %{http_code}' http://localhost:8090/ibox?user_id=test 2>/dev/null; echo ''"
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
