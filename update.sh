#!/usr/bin/env sh
# ============================================================
#  MoneyPrinterTurbo 更新脚本（拉取最新代码）
#  Update source: https://github.com/PrayerT/MoneyPrinterTurbo
# ============================================================

set -e

CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$CURRENT_DIR"

REPO_URL="https://github.com/PrayerT/MoneyPrinterTurbo.git"
BRANCH="main"

if ! command -v git >/dev/null 2>&1; then
  echo "***** 未找到 git，无法更新。请先安装 git。 *****"
  exit 1
fi

if [ ! -d "$CURRENT_DIR/.git" ]; then
  echo "***** 当前目录不是 git 仓库，无法执行 git pull 更新。 *****"
  exit 1
fi

echo "***** 更新来源: $REPO_URL ($BRANCH) *****"

# 确保 origin 指向本仓库地址。
git remote set-url origin "$REPO_URL" 2>/dev/null || git remote add origin "$REPO_URL"

echo "***** 正在拉取最新代码... *****"
git pull origin "$BRANCH"

echo "***** 更新完成。 *****"
