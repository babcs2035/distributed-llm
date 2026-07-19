"""pytest 共通設定: tools/ とリポジトリルートをモジュール検索パスへ追加する
（tools/ 内スクリプトの相対 import，およびルート直下の pipeline_inference.py の import に合わせる）．
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
