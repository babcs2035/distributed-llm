"""
分散LLMパイプラインにプロンプトを送信し、推論結果を表示する。

使用法:
  mise run predict                          # input() でプロンプトを入力
  mise run predict:demo                     # デモ用「こんにちは！」送信
"""

from __future__ import annotations

import base64
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
    body = json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result.get("result", "")


# リモートで実行するPythonスクリプト（base64エンコード済みで渡す）
_REMOTE_SCRIPT = (
    "import os, json, urllib.request, base64\n"
    "d = base64.b64decode(os.environ['PROMPT_B64'])\n"
    "r = urllib.request.Request('http://localhost:8082/predict', data=d,\n"
    "    headers={'Content-Type': 'application/json'}, method='POST')\n"
    "print(json.loads(urllib.request.urlopen(r, timeout=300).read().decode())['result'])\n"
)
_REMOTE_SCRIPT_B64 = base64.b64encode(_REMOTE_SCRIPT.encode("utf-8")).decode("ascii")


def send_prompt_ssh(config: ClusterConfig, prompt: str) -> str:
    """管理ノードの Docker コンテナ上でHTTP POSTし、結果を取得する。"""

    body = json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8")
    prompt_b64 = base64.b64encode(body).decode("ascii")

    # Dockerコンテナ内でスクリプトを実行（base64エンコード済み）
    # データを直接-eで渡す（変数展開の問題回避）
    cmd = (
        "LC_ALL=C docker exec -i -e PROMPT_B64=\""
        + prompt_b64
        + "\" llm-node python3 -c "
        "'import base64,os,sys; exec(base64.b64decode(\""
        + _REMOTE_SCRIPT_B64
        + "\").decode())'"
    )

    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
         config.master_addr, cmd],
        capture_output=True, text=True, timeout=310,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        print(f"Error via SSH: {err}", file=sys.stderr)
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
                        help="Connect directly via HTTP (not HTTP)")
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
