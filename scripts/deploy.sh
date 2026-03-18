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
    local REMOTE="/opt/qanot-bot"
    local LOCAL="$(cd "$(dirname "$0")/.." && pwd)"

    echo "=== Qanot Framework Deploy ==="

    echo "[1/5] Syncing source..."
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

    echo "[2/5] Building image..."
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
    ssh "$SERVER" 'python3 << '"'"'PYSCRIPT'"'"'
import subprocess, json

# Find all qanot-bot containers
result = subprocess.run(
    ["docker", "ps", "-a", "--filter", "name=qanot-bot-", "--format", "{{.Names}}"],
    capture_output=True, text=True
)
names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]

if not names:
    print("   No bot containers found.")
else:
    for name in names:
        # Get container config
        info = json.loads(subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True
        ).stdout)[0]

        # Extract env vars (skip system ones)
        skip = {"PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
                "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL",
                "PYTHON_GET_PIP_SHA256"}
        env_args = []
        for e in info["Config"].get("Env", []):
            key = e.split("=", 1)[0]
            if key not in skip:
                env_args.extend(["-e", e])

        # Extract volumes
        vol_args = []
        for m in info.get("Mounts", []):
            mode = m.get("Mode", "rw") or "rw"
            vol_args.extend(["-v", f"{m['Source']}:{m['Destination']}:{mode}"])

        # Extract network
        networks = list(info.get("NetworkSettings", {}).get("Networks", {}).keys())
        net = networks[0] if networks else "qanot-cloud-net"

        # Stop and remove
        subprocess.run(["docker", "stop", name], capture_output=True)
        subprocess.run(["docker", "rm", name], capture_output=True)

        # Recreate
        cmd = ["docker", "run", "-d", "--name", name, "--user", "1000:1000"] + \
              env_args + vol_args + \
              ["--memory=256m", "--cpus=0.25", "--pids-limit=100",
               "--cap-drop=ALL", "--security-opt=no-new-privileges",
               f"--network={net}", "--restart=unless-stopped",
               "qanot-bot:latest"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"   {name} recreated")
        else:
            print(f"   {name} FAILED: {r.stderr[:100]}")
PYSCRIPT
'
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
