#!/bin/sh
# GitHub のリポジトリ設定を一括で入れる（このリポジトリを持つアカウント＝管理者の gh で1回だけ。何度流しても同じ状態になる）。
# ★2026-10-23 時点で、これを書いた手元の環境には gh が無く、まだ1回も流していない（API の項目は公式文書で確かめた）。
#   初回は流したあと sh repo_settings/check.sh で1つずつ確かめ、画面（docs/リポジトリの設定.md）とも見比べる。
#   シェルの組み立て（管理者の確かめ・止まり方）だけは、偽の gh で試験している（tests/test_repo_settings.py）。
# 使い方: sh repo_settings/apply.sh OWNER/REPO
set -eu
REPO="${1:?使い方: sh repo_settings/apply.sh OWNER/REPO}"
case "$REPO" in
  */*) ;;
  *) echo "OWNER/REPO の形で渡してください" >&2; exit 2 ;;
esac
DIR=$(cd "$(dirname "$0")" && pwd)
api() { gh api -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "$@"; }

# ★gh のアカウントが、このリポジトリの管理者かを先に確かめる（違うアカウントでは何も入れない）。
#   gh に複数のアカウントが入っていると、リポジトリを持たない方のまま流れうる
LOGIN=$(api user --jq '.login' 2>/dev/null) || LOGIN=""
ADMIN=$(api "repos/$REPO" --jq '.permissions.admin' 2>/dev/null) || ADMIN=""
if [ "$ADMIN" != "true" ]; then
  echo "止めました: gh のアカウント（${LOGIN:-読めませんでした}）が $REPO の管理者だと確かめられませんでした。何も入れていません" >&2
  echo "  このリポジトリを持つアカウントに gh を切り替えてから流してください（gh auth status で見て、gh auth switch で切り替える）" >&2
  exit 2
fi
echo "gh のアカウント: ${LOGIN}（${REPO} の管理者）"

echo "1/9 一般設定（squash だけ・枝を消す）と secret scanning・push protection"
api --method PATCH "repos/$REPO" --input "$DIR/repo.json" >/dev/null
echo "2/9 Dependabot の警告と、脆弱性を直す PR"
api --method PUT "repos/$REPO/vulnerability-alerts" >/dev/null
api --method PUT "repos/$REPO/automated-security-fixes" >/dev/null
echo "3/9 非公開の脆弱性報告"
api --method PUT "repos/$REPO/private-vulnerability-reporting" >/dev/null
echo "4/9 CodeQL（default setup）"
api --method PATCH "repos/$REPO/code-scanning/default-setup" --input "$DIR/codeql.json" >/dev/null
echo "5/9 Actions: 許すアクションを絞り、SHA 固定を必須にする"
api --method PUT "repos/$REPO/actions/permissions" --input "$DIR/actions_permissions.json" >/dev/null
api --method PUT "repos/$REPO/actions/permissions/selected-actions" --input "$DIR/selected_actions.json" >/dev/null
echo "6/9 Actions: GITHUB_TOKEN は読むだけ・Actions に PR を承認させない"
api --method PUT "repos/$REPO/actions/permissions/workflow" --input "$DIR/workflow_permissions.json" >/dev/null
echo "7/9 Actions: fork からの PR は毎回承認・記録の保存は30日"
api --method PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" --input "$DIR/fork_pr_approval.json" >/dev/null
api --method PUT "repos/$REPO/actions/permissions/artifact-and-log-retention" --input "$DIR/retention.json" >/dev/null
echo "8/9 環境 production（main からだけデプロイできる）"
api --method PUT "repos/$REPO/environments/production" --input "$DIR/environment_production.json" >/dev/null
NAMES=$(api "repos/$REPO/environments/production/deployment-branch-policies" --jq '[.branch_policies[].name] | join(",")')
case ",$NAMES," in
  *,main,*) ;;
  *) api --method POST "repos/$REPO/environments/production/deployment-branch-policies" -f name=main -f type=branch >/dev/null ;;
esac
echo "9/9 ruleset「main を守る」"
ID=$(api "repos/$REPO/rulesets" --jq '[.[] | select(.name == "main を守る") | .id] | first // empty')
if [ -n "$ID" ]; then
  api --method PUT "repos/$REPO/rulesets/$ID" --input "$DIR/ruleset_main.json" >/dev/null
else
  api --method POST "repos/$REPO/rulesets" --input "$DIR/ruleset_main.json" >/dev/null
fi
echo "入れ終わりました。続けて sh repo_settings/check.sh $REPO で確かめてください"
