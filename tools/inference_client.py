"""
管理ノードから分散LLMパイプラインに推論リクエストを送信するクライアント。

SSHトンネル経由で管理ノードの8080ポートにアクセスする。

使用法:
  uv run python tools/inference_client.py "こんにちは、世界"
  echo '{"prompt": "テスト"}' | uv run python tools/inference_client.py --stdin
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request


DEFAULT_HOST = "wafl-ctrl1"
DEFAULT_SSH_USER = "denjo"
DEFAULT_SSH_PORT = 22
HTTP_PORT = 8080
TIMEOUT_SECONDS = 300


def send_request(host: str, port: int, prompt: str) -> str:
    """SSHトンネル経由でパイプラインに推論リクエストを送信し、結果を返す。"""

    payload = json.dumps({"prompt": prompt}).encode("utf-8")
    url = f"http://127.0.0.1:{port}/predict"

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    print(f"[i] Sending request to {url} ...", flush=True)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        if resp.status != 200:
            body = resp.read().decode()
            print(f"[x] Error {resp.status}: {body}", file=sys.stderr)
            sys.exit(1)
        return json.loads(resp.read().decode())["result"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a prompt to the distributed LLM pipeline via SSH tunnel",
    )
    parser.add_argument("prompt", nargs="?", help="Prompt text to send")
    parser.add_argument("--stdin", action="store_true", help="Read prompt from stdin")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Master host (default: {DEFAULT_HOST})")
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER, help=f"SSH user (default: {DEFAULT_SSH_USER})")
    parser.add_argument("--ssh-port", type=int, default=DEFAULT_SSH_PORT, help=f"SSH port (default: {DEFAULT_SSH_PORT})")
    parser.add_argument("--port", type=int, default=HTTP_PORT, help=f"HTTP port on remote (default: {HTTP_PORT})")
    args = parser.parse_args()

    if args.prompt:
        prompt = args.prompt
    elif args.stdin:
        prompt = sys.stdin.read().strip()
    else:
        parser.print_help()
        sys.exit(1)

    if not prompt:
        print("[x] Error: empty prompt", file=sys.stderr)
        sys.exit(1)

    # SSHトンネル確立
    tunnel = subprocess.Popen(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-L", f"{args.port}:127.0.0.1:{args.port}",
            f"-p {args.ssh_port}",
            f"{args.ssh_user}@{args.host}",
            "-N",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # トンネル確立を待つ
        for _ in range(30):
            time.sleep(0.5)
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect(("127.0.0.1", args.port))
                break
            except ConnectionRefusedError:
                pass
            finally:
                s.close()
        else:
            print(f"[x] Error: failed to establish SSH tunnel to {args.host}:{args.port}", file=sys.stderr)
            sys.exit(1)

        result = send_request("127.0.0.1", args.port, prompt)
        print(f"[result] {result}", flush=True)
    except Exception:
        print(f"[x] Error: {sys.exc_info()[1]}", file=sys.stderr)
        sys.exit(1)
    finally:
        tunnel.terminate()
        tunnel.wait()


if __name__ == "__main__":
    main()
