"""
Send a prompt to the distributed LLM pipeline and display the inference result.

Usage:
  mise run predict                          # Enter prompt via input()
  mise run predict:demo                     # Send demo prompt "Hello!"
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.request

from common import ClusterConfig


def get_prompt(cli_prompt: str | None) -> str:
    """Accept prompt from CLI argument or input()."""

    if cli_prompt:
        return cli_prompt
    return input("Prompt: ").strip()


def send_prompt_http(config: ClusterConfig, prompt: str) -> str:
    """Send prompt via HTTP POST /predict and return the result string."""

    try:
        ip = __import__("socket").gethostbyname(config.master_addr)
    except __import__("socket").gaierror:
        ip = "127.0.0.1"
    if ip.startswith("127."):
        ip = "127.0.0.1"

    url = f"http://{ip}:8082/predict"
    body = json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=720) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result.get("result", "")


# Python script to run remotely (passed as base64-encoded)
_REMOTE_SCRIPT = (
    "import os, json, urllib.request, base64\n"
    "d = base64.b64decode(os.environ['PROMPT_B64'])\n"
    "r = urllib.request.Request('http://localhost:8082/predict', data=d,\n"
    "    headers={'Content-Type': 'application/json'}, method='POST')\n"
    "print(json.loads(urllib.request.urlopen(r, timeout=720).read().decode())['result'])\n"
)
_REMOTE_SCRIPT_B64 = base64.b64encode(_REMOTE_SCRIPT.encode("utf-8")).decode("ascii")


def send_prompt_ssh(config: ClusterConfig, prompt: str) -> str:
    """Execute HTTP POST on management node's Docker container and get the result."""

    body = json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8")
    prompt_b64 = base64.b64encode(body).decode("ascii")

    # Execute script inside Docker container (base64-encoded)
    # Pass data via -e directly (avoids variable expansion issues)
    cmd = (
        "LC_ALL=C.UTF-8 docker exec -i -e PROMPT_B64=\""
        + prompt_b64
        + "\" distributed-llm python3 -c "
        "'import base64,os,sys; exec(base64.b64decode(\""
        + _REMOTE_SCRIPT_B64
        + "\").decode())'"
    )

    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
         config.master_addr, cmd],
        capture_output=True, text=True, encoding="utf-8", timeout=730,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        print(f"[ERROR] SSH error: {err}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Send a prompt to the distributed LLM pipeline",
    )
    parser.add_argument("--config", "-c", default="config.json",
                        help="Path to config.json")
    parser.add_argument("--http", action="store_true",
                        help="Connect directly via HTTP (not SSH)")
    parser.add_argument("--prompt", "-p",
                        help="Prompt text (skip input())")
    args = parser.parse_args()

    config = ClusterConfig.load(args.config)

    prompt = get_prompt(args.prompt)
    if not prompt:
        print("[ERROR] Empty prompt", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Sending to {config.master_addr}:8082...", file=sys.stderr)

    if args.http:
        result = send_prompt_http(config, prompt)
    else:
        result = send_prompt_ssh(config, prompt)

    print(result)


if __name__ == "__main__":
    main()
