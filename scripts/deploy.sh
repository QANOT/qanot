#!/usr/bin/env bash
# Qanot AI — production deploy script
# Usage: ./scripts/deploy.sh [--rebuild]
set -euo pipefail

SERVER="root@46.62.250.72"
REMOTE_DIR="/opt/qanot-bot"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Qanot Deploy ==="
echo "Local:  $LOCAL_DIR"
echo "Remote: $SERVER:$REMOTE_DIR"

# 1. Sync source code
echo ""
echo "[1/4] Syncing source code..."
rsync -az --delete \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='tests/' \
    --exclude='scripts/' \
    --exclude='landing/' \
    --exclude='mahalla/' \
    --exclude='docs/' \
    --exclude='claudedocs/' \
    --exclude='.claude/' \
    "$LOCAL_DIR/qanot/" "$SERVER:$REMOTE_DIR/qanot/"

rsync -az --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$LOCAL_DIR/plugins/" "$SERVER:$REMOTE_DIR/plugins/"

rsync -az --delete \
    --exclude='__pycache__' \
    "$LOCAL_DIR/templates/" "$SERVER:$REMOTE_DIR/templates/"

echo "   Source synced."

# 2. Build Docker image
echo ""
echo "[2/4] Building Docker image..."
BUILD_FLAG=""
if [[ "${1:-}" == "--rebuild" ]]; then
    BUILD_FLAG="--no-cache"
fi
ssh "$SERVER" "cd $REMOTE_DIR && docker build $BUILD_FLAG -t qanot-bot:latest . 2>&1 | tail -3"
echo "   Image built."

# 3. Verify image
echo ""
echo "[3/4] Verifying image..."
ssh "$SERVER" "docker run --rm qanot-bot:latest python -c 'from qanot.agent import Agent; print(\"qanot.agent OK\")'"
ssh "$SERVER" "docker run --rm qanot-bot:latest python -c 'from qanot.registry import ToolRegistry; print(\"qanot.registry OK\")'"
ssh "$SERVER" "docker run --rm qanot-bot:latest python -c 'from plugins.ibox.plugin import QanotPlugin; print(\"ibox plugin OK\")'"
echo "   All modules verified."

# 4. Restart running bot containers
echo ""
echo "[4/4] Restarting bot containers..."
BOTS=$(ssh "$SERVER" "docker ps --filter 'name=qanot-bot-' --format '{{.Names}}'" 2>/dev/null || true)
if [ -n "$BOTS" ]; then
    for bot in $BOTS; do
        ssh "$SERVER" "docker restart $bot" 2>/dev/null
        echo "   Restarted: $bot"
    done
else
    echo "   No running bots found."
fi

echo ""
echo "=== Deploy complete ==="
