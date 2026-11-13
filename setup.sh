#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
SKILL_ROOT="$(cd "$(dirname "$0")" && pwd)"
echo "== douyin-auto-upload setup =="
echo "skill: $SKILL_ROOT"

DATA_ROOT=""
if [ "$#" -ge 1 ]; then
  DATA_ROOT="$1"
fi
if [ -z "$DATA_ROOT" ]; then
  printf "请输入数据根目录（建议非系统盘，例如 ~/DouyinAutoUpload）："
  read -r DATA_ROOT
fi
if [ -z "$DATA_ROOT" ]; then
  echo "未指定数据根" >&2
  exit 1
fi

PYTHON_BIN=""
for cand in python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
      PYTHON_BIN="$cand"
      break
    fi
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "未找到 Python 3.11+；请安装后重试" >&2
  exit 1
fi
"$PYTHON_BIN" -c "import sys; print('python', sys.version)"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then
  echo "未找到 Google Chrome：$CHROME" >&2
  exit 1
fi
echo "chrome: $CHROME"

if ! "$PYTHON_BIN" -c "import websocket" 2>/dev/null; then
  echo "安装 websocket-client ..."
  "$PYTHON_BIN" -m pip install --user -r "$SKILL_ROOT/requirements.txt"
fi

"$PYTHON_BIN" "$SKILL_ROOT/scripts/cli.py" --data-root "$DATA_ROOT" init
"$PYTHON_BIN" "$SKILL_ROOT/scripts/cli.py" doctor

echo ""
echo "== 安装完成 =="
echo "数据根：$DATA_ROOT"
echo "下一步：$PYTHON_BIN \"$SKILL_ROOT/scripts/cli.py\" account add --display \"账号名\" --slug your-account"
echo "然后：$PYTHON_BIN \"$SKILL_ROOT/scripts/cli.py\" account check --slug your-account --wait-login 300"
