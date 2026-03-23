#!/usr/bin/env python3
"""Recreate qanot-bot containers with the latest image, preserving config."""

import json
import subprocess
import sys

SKIP_ENV = {
    "PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
    "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION",
    "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256",
}


def main():
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=qanot-bot-", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]

    if not names:
        print("   No bot containers found.")
        return

    for name in names:
        info_raw = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True,
        )
        info = json.loads(info_raw.stdout)[0]

        # Extract env vars
        env_args = []
        for e in info["Config"].get("Env", []):
            key = e.split("=", 1)[0]
            if key not in SKIP_ENV:
                env_args.extend(["-e", e])

        # Extract volumes
        vol_args = []
        for m in info.get("Mounts", []):
            src = m.get("Source", "")
            dst = m.get("Destination", "")
            mode = m.get("Mode", "rw") or "rw"
            if src and dst:
                vol_args.extend(["-v", f"{src}:{dst}:{mode}"])

        # Extract port bindings
        port_args = []
        port_bindings = info.get("HostConfig", {}).get("PortBindings") or {}
        for container_port, bindings in port_bindings.items():
            if not bindings:
                continue
            for b in bindings:
                host_port = b.get("HostPort", "")
                host_ip = b.get("HostIp", "")
                if host_port:
                    port_num = container_port.split("/")[0]
                    if host_ip:
                        port_args.extend(["-p", f"{host_ip}:{host_port}:{port_num}"])
                    else:
                        port_args.extend(["-p", f"{host_port}:{port_num}"])

        # Extract network
        networks = list(info.get("NetworkSettings", {}).get("Networks", {}).keys())
        net = networks[0] if networks else "qanot-cloud-net"

        # Stop and remove
        subprocess.run(["docker", "stop", name], capture_output=True)
        subprocess.run(["docker", "rm", name], capture_output=True)

        # Recreate
        cmd = [
            "docker", "run", "-d", "--name", name, "--user", "1000:1000",
            *env_args, *vol_args, *port_args,
            "--memory=256m", "--cpus=0.25", "--pids-limit=100",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            f"--network={net}", "--restart=unless-stopped",
            "qanot-bot:latest",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"   {name} recreated")
        else:
            print(f"   {name} FAILED: {r.stderr.strip()[:200]}")


if __name__ == "__main__":
    main()
