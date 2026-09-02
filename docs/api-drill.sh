#!/usr/bin/env bash
# 简道云 API 调用链演练 —— Sprint 0 环境搭建第 2 项。
# 默认全程只读；batch_create 写入需显式 --write。
#
#   export JDY_API_KEY="你的密钥"
#   ./docs/api-drill.sh            # 只读：app/list → entry/list → widget/list → data/list
#   ./docs/api-drill.sh --write    # 额外跑一次 batch_create（写入 1 条测试数据）
set -uo pipefail

BASE="https://api.jiandaoyun.com/api/v5"
WRITE=0
[ "${1:-}" = "--write" ] && WRITE=1

if [ -z "${JDY_API_KEY:-}" ]; then
  if [ -f "$HOME/.jdy/config.json" ]; then
    JDY_API_KEY=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.jdy/config.json"))).get("api_key",""))')
  fi
fi
if [ -z "${JDY_API_KEY:-}" ]; then
  echo "缺少密钥：export JDY_API_KEY=... 或写入 ~/.jdy/config.json" >&2
  exit 1
fi

jdy() { # jdy <path> <json-body>
  curl -sS -X POST "$BASE$1" \
    -H "Authorization: Bearer $JDY_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$2"
}
pick() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)" 2>/dev/null; }
step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

step "1/5 app/list （30次/秒）"
APPS=$(jdy /app/list '{"limit":100}'); echo "$APPS" | head -c 600; echo
APP_ID=$(echo "$APPS" | pick '["apps"][0]["app_id"]')
[ -z "$APP_ID" ] && { echo "拿不到 app_id，后续步骤中止——先确认密钥权限范围" >&2; exit 1; }
echo "→ app_id=$APP_ID"

step "2/5 entry/list （30次/秒）"
ENTRIES=$(jdy /app/entry/list "{\"app_id\":\"$APP_ID\"}"); echo "$ENTRIES" | head -c 600; echo
ENTRY_ID=$(echo "$ENTRIES" | pick '["forms"][0]["entry_id"]')
[ -z "$ENTRY_ID" ] && { echo "拿不到 entry_id，中止" >&2; exit 1; }
echo "→ entry_id=$ENTRY_ID"

step "3/5 widget/list （30次/秒）—— 字段结构，jdy-doc 的数据源"
jdy /app/entry/widget/list "{\"app_id\":\"$APP_ID\",\"entry_id\":\"$ENTRY_ID\"}" | head -c 1200; echo

step "4/5 data/list （30次/秒，limit 默认只有 10）"
jdy /app/entry/data/list "{\"app_id\":\"$APP_ID\",\"entry_id\":\"$ENTRY_ID\",\"limit\":3}" | head -c 1200; echo
echo "→ 翻页用最后一条的 _id 作 data_id 游标，没有 offset 分页"

step "5/5 batch_create （10次/秒，≤100条）"
if [ "$WRITE" -ne 1 ]; then
  echo "已跳过（只读模式）。确认要写入测试数据时加 --write 重跑。"
  exit 0
fi
echo "⚠️  将向 app_id=$APP_ID entry_id=$ENTRY_ID 写入 1 条测试数据。"
printf "确认？输入 yes 继续："; read -r ok
[ "$ok" != "yes" ] && { echo "已取消"; exit 0; }
TXN="drill-$(date +%s)"
jdy /app/entry/data/batch_create \
  "{\"app_id\":\"$APP_ID\",\"entry_id\":\"$ENTRY_ID\",\"transaction_id\":\"$TXN\",\"data_list\":[{}]}" | head -c 800; echo
echo "→ transaction_id=$TXN（1 小时内同 ID 重复提交会覆盖，这是重试的正确姿势）"
echo "→ 注意：API 写入绕过必填/重复值校验，且不回推 webhook——去表单里核对这条数据长什么样"
