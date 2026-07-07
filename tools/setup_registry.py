"""
Private Docker registry setup script.

Runs on the management server to start an HTTP (no TLS) Docker Registry v2.
Nodes access images via SSH tunnel.

Usage:
  uv run python tools/setup_registry.py
"""

from __future__ import annotations

import sys

from common import ClusterConfig, log, ssh_run


def start_registry(config: ClusterConfig) -> None:
    """
    Start Docker Registry v2 on the management server (HTTP / no TLS).

    Stops and removes existing container if present, then starts a new one.
    Data persists at /var/lib/registry.
    """

    log("INFO", "Starting Docker Registry v2...")

    registry_data_dir = "/var/lib/registry"
    master = config.master_addr
    user = config.ssh_user

    # Clean up existing registry container
    result = ssh_run(
        user, master,
        "docker ps -a --format '{{.Names}}'",
        capture=True, check=False,
    )
    if "secure-registry" in (result.stdout or ""):
        log("INFO", "Stopping and removing existing registry container...")
        ssh_run(user, master, "docker stop secure-registry", check=False)
        ssh_run(user, master, "docker rm secure-registry", check=False)

    ssh_run(user, master, f"mkdir -p {registry_data_dir}")

    log("INFO", "Starting registry container...")
    log("INFO", f"  Port: {config.registry_port} (HTTP)")
    log("INFO", f"  Data: {registry_data_dir}")

    ssh_run(
        user, master,
        f"docker run -d --name secure-registry --restart=always "
        f"-p {config.registry_port}:5000 "
        f"-v {registry_data_dir}:/var/lib/registry registry:2",
    )

    log("INFO", "Registry started.")
    log("INFO", f"Test: curl http://{config.master_addr}:{config.registry_port}/v2/_catalog")


def main() -> None:
    """Set up a Docker registry on the management server."""

    config = ClusterConfig()

    log("STEP", "=" * 40)
    log("INFO", "Private Docker Registry setup starting")
    log("INFO", f"  Master: {config.master_addr}")
    log("INFO", f"  Port: {config.registry_port} (HTTP)")
    log("STEP", "=" * 40)

    start_registry(config)

    log("STEP", "=" * 40)
    log("INFO", "Registry setup complete")
    log("INFO", "Next steps:")
    log("INFO", "  1. Build:       mise run build")
    log("INFO", "  2. Split model: mise run split:models")
    log("INFO", "  3. Deploy:      mise run deploy")
    log("STEP", "=" * 40)


if __name__ == "__main__":
    main()
