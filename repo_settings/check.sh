#!/bin/sh
# GitHub のリポジトリ設定が雛形どおりかを、読むだけで確かめる（何も書き換えない）。
# ★2026-10-23 時点で未実行（apply.sh と同じ理由）。読めない項目は「読めない」と出し、「そのとおり」とは言わない。
#   シェルの組み立て（管理者の確かめ・三状態・終了コード）だけは、偽の gh で試験している（tests/test_repo_settings.py）。
# 使い方: sh repo_settings/check.sh OWNER/REPO
# 終了コード: 0=全部そのとおり / 1=違う所がある / 2=読めなかった所がある
# ★変数の直後に全角の文字を続けるときは ${name} の形で囲む（macOS の sh は、変数名の直後の全角の文字の1バイト目まで
#   変数名に読み、set -u で「unbound variable」になって終了コード127で落ちる。2026-10-23 に実測。試験が見張る）
set -u
REPO="${1:?使い方: sh repo_settings/check.sh OWNER/REPO}"
api() { gh api -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "$@"; }
NG=0
UNREAD=0

want() {  # want 名前 期待値 API のパス --jq 式
  name=$1
  expected=$2
  shift 2
  if got=$(api "$@" 2>/dev/null) && [ "$got" != "__UNREADABLE__" ]; then
    if [ "$got" = "$expected" ]; then
      echo "OK      $name"
    else
      echo "違う    ${name}（今: ${got} / 期待: ${expected}）"
      NG=1
    fi
  else
    echo "読めない $name"
    UNREAD=1
  fi
}

# ★管理者でないアカウントで読むと、権限が無いと返らない項目（security_and_analysis は管理者だけと GitHub の文書にある・
#   マージの設定・bypass_actors）が応答に出ず、Dependabot の警告は 404（「無効」と同じ形）が返りうる。
#   「違う」「無効」と読み違えないよう、先に確かめて止める
LOGIN=$(api user --jq '.login' 2>/dev/null) || LOGIN=""
ADMIN=$(api "repos/$REPO" --jq '.permissions.admin' 2>/dev/null) || ADMIN=""
if [ "$ADMIN" != "true" ]; then
  echo "読めない gh のアカウント（${LOGIN:-読めませんでした}）が $REPO の管理者だと確かめられませんでした。ほかの項目は見ていません"
  echo "         このリポジトリを持つアカウントに gh を切り替えてから流してください（gh auth status で見て、gh auth switch で切り替える）"
  exit 2
fi
echo "OK      gh のアカウント（${LOGIN}）は ${REPO} の管理者"

want "マージは squash だけ" "false true false" "repos/$REPO" --jq '"\(.allow_merge_commit) \(.allow_squash_merge) \(.allow_rebase_merge)"'
want "マージ後に枝を消す" "true" "repos/$REPO" --jq '.delete_branch_on_merge'
want "public である" "false" "repos/$REPO" --jq '.private'
want "secret scanning" "enabled" "repos/$REPO" --jq '.security_and_analysis.secret_scanning.status'
want "push protection" "enabled" "repos/$REPO" --jq '.security_and_analysis.secret_scanning_push_protection.status'
want "非公開の脆弱性報告" "true" "repos/$REPO/private-vulnerability-reporting" --jq '.enabled'
want "Dependabot の脆弱性を直す PR" "true" "repos/$REPO/automated-security-fixes" --jq '.enabled'
want "CodeQL" "configured" "repos/$REPO/code-scanning/default-setup" --jq '.state'
want "許すアクション・SHA 固定" "selected true" "repos/$REPO/actions/permissions" --jq '"\(.allowed_actions) \(.sha_pinning_required)"'
want "許すアクションの中身" "true false zizmorcore/zizmor-action@*" "repos/$REPO/actions/permissions/selected-actions" --jq '"\(.github_owned_allowed) \(.verified_allowed) \(.patterns_allowed | join(","))"'
want "GITHUB_TOKEN は読むだけ" "read false" "repos/$REPO/actions/permissions/workflow" --jq '"\(.default_workflow_permissions) \(.can_approve_pull_request_reviews)"'
want "fork の PR は毎回承認" "all_external_contributors" "repos/$REPO/actions/permissions/fork-pr-contributor-approval" --jq '.approval_policy'
want "production は main からだけ" "main" "repos/$REPO/environments/production/deployment-branch-policies" --jq '[.branch_policies[].name] | join(",")'

# Dependabot の警告: 有効なら 204、無効なら 404（gh は 404 で失敗を返すので、本文の HTTP の行で見分ける）
OUT=$(api -i "repos/$REPO/vulnerability-alerts" 2>&1)
case "$OUT" in
  *" 204"*) echo "OK      Dependabot の警告" ;;
  *" 404"*) echo "違う    Dependabot の警告（無効）"; NG=1 ;;
  *) echo "読めない Dependabot の警告"; UNREAD=1 ;;
esac

ID=$(api "repos/$REPO/rulesets" --jq '[.[] | select(.name == "main を守る") | .id] | first // empty' 2>/dev/null)
if [ -z "$ID" ]; then
  echo "違う    ruleset「main を守る」が見つかりません"
  NG=1
else
  want "ruleset が効いている" "active" "repos/$REPO/rulesets/$ID" --jq '.enforcement'
  want "ruleset の規則" "deletion,non_fast_forward,pull_request,required_linear_history,required_status_checks" "repos/$REPO/rulesets/$ID" --jq '[.rules[].type] | sort | join(",")'
  want "必須の検査" "test,wrangler-dry-run,zizmor" "repos/$REPO/rulesets/$ID" --jq '[.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context] | sort | join(",")'
  # bypass_actors は管理者の権限で読まないと応答に出ない。出ないのを「0人」と読まない
  want "素通りできる人がいない" "0" "repos/$REPO/rulesets/$ID" --jq 'if has("bypass_actors") then (.bypass_actors | length | tostring) else "__UNREADABLE__" end'
fi

if [ "$NG" -ne 0 ]; then exit 1; fi
if [ "$UNREAD" -ne 0 ]; then exit 2; fi
echo "全部そのとおりでした"
