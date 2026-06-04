"""
分散LLMパイプラインにプロンプトを送信し、推論結果を表示する。

使用法:
  mise run predict                          # input() でプロンプトを入力
  mise run predict:demo                     # デモ用「こんにちは！」送信
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

from common import ClusterConfig


def get_prompt(cli_prompt: str | None) -> str:
    """コマンドライン引数または input() でプロンプトを受け取る。"""

    if cli_prompt:
        return cli_prompt
    return input("プロンプト: ").strip()


def send_prompt_http(config: ClusterConfig, prompt: str) -> str:
    """HTTP POST /predict にプロンプトを送信し、結果文字列を返す。"""

    try:
        ip = __import__("socket").gethostbyname(config.master_addr)
    except __import__("socket").gaierror:
        ip = "127.0.0.1"
    if ip.startswith("127."):
        ip = "127.0.0.1"

    url = f"http://{ip}:8082/predict"
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result.get("result", "")


def send_prompt_ssh(config: ClusterConfig, prompt: str) -> str:
    """管理ノード上で curl を実行し、結果を取得する（SSH経由）。"""

    body = json.dumps({"prompt": prompt})
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        config.master_addr,
        "curl -s --max-time 300 http://localhost:8082/predict "
        "-X POST -H 'Content-Type: application/json' -d @-",
    ]
    result = subprocess.run(
        cmd, input=body, capture_output=True, text=True, timeout=310,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        print(f"Error via SSH: {err}", file=sys.stderr)
        sys.exit(1)
    resp = json.loads(result.stdout.strip())
    return resp.get("result", "")


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
                        help="Prompt text (skip input() prompt)")
    args = parser.parse_args()

    config = ClusterConfig.load(args.config)

    prompt = get_prompt(args.prompt)
    if not prompt:
        print("Error: empty prompt", file=sys.stderr)
        sys.exit(1)

    print(f"Sending to {config.master_addr}:8082 ...", file=sys.stderr)

    if args.http:
        result = send_prompt_http(config, prompt)
    else:
        result = send_prompt_ssh(config, prompt)

    print(result)


if __name__ == "__main__":
    main()
