#!/usr/bin/env python3
"""検算: 関所を1か所ずつ壊し、試験が赤になることを確かめる。

- 壊した版で試験が通ってしまったら「生き残り」＝その関所は試験に守られていない → 失敗
- 壊す場所が見つからない（または2か所以上ある）なら「型が古い」→ 失敗（黙って飛ばさない）
- 壊すのは一時フォルダに写した版だけ。元のファイルには触らない
- 新しい関所を足したら、ここに型を1つ足す（掟）

使い方: python3 -I tests/mutation_check.py
終了コード: 0=全部赤になった / 1=生き残り・型が古い / 2=壊す前の版で試験が通らない
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY_IGNORE = shutil.ignore_patterns(".git", "dist", "data", "node_modules", "__pycache__", ".wrangler")
TIMEOUT_SECONDS = 300

# (ファイル, 壊す前, 壊した後, 何の関所か)
MUTANTS = [
    # ---- check_dist.py（公開前の関所）----
    ("tools/check_dist.py", 'if tag == "script" and "src" not in a:', 'if tag == "script" and "src" not in a and False:', "本文つき <script> の検出"),
    ("tools/check_dist.py", 'if name.startswith("on"):', 'if name.startswith("on") and False:', "イベント属性の検出"),
    ("tools/check_dist.py", '                else:\n                    self.problems.append(f"{where}: 外部から読み込もうとしています: {name}={value!r}")', "                else:\n                    pass", "外部からの読み込みの検出"),
    ("tools/check_dist.py", "if tag in FORBIDDEN_TAGS:", "if False:", "使えないタグ（form・iframe・style 等）の検出"),
    ("tools/check_dist.py", '    "portal", "fencedframe", "svg", "math", "template", "noscript",', '    "portal", "fencedframe",', "svg・math・template・noscript の禁止"),
    ("tools/check_dist.py", "if BANNER_ATTR not in a:", "if False:", "帯が <body> の最初にあるか"),
    ("tools/check_dist.py", 'if BANNER_WORD not in "".join(scan.banner_text):', "if False:", "帯に「非公式」の文字があるか"),
    ("tools/check_dist.py", "        if self.banner_open is not None and not (self.banner_open and self.banner_open[-1][1]):", "        if self.banner_open is not None:", "隠れた所の文字を「非公式」に数えない"),
    ("tools/check_dist.py", "            if hiding and not allowed_hidden:", "            if False:", "帯の中の hidden・popover 等の検出"),
    ("tools/check_dist.py", "            elif tag not in BANNER_TAGS or closed:", "            elif closed:", "帯に使える要素の制限（details 等で畳ませない）"),
    ("tools/check_dist.py", '        if tag in ("html", "body") and HIDING_ATTRS & set(a):', "        if False:", "ページ全体を隠す属性の検出"),
    ("tools/check_dist.py", "    if not scan.body_seen:", "    if False:", "<body> の無いページの検出"),
    ("tools/check_dist.py", "                if not self._in_head:", "                if False:", "<meta> の CSP は <head> の中"),
    ("tools/check_dist.py", "    if scan._resource_before_csp:", "    if False:", "<meta> の CSP より前の読み込み"),
    ("tools/check_dist.py", "    for seq in HTML_FORBIDDEN_SEQUENCES:", "    for seq in ():", "コメント・CDATA の禁止（読み方のずれ）"),
    ("tools/check_dist.py", "        if raw != scan.seen_tags.get(tag, 0):", "        if False:", "文字列と検査器でタグの数が違う（読み方のずれ）"),
    ("tools/check_dist.py", '    if re.search(r"</script[^>]", lowered):', "    if False:", "</script の後ろの余計な文字"),
    ("tools/check_dist.py", "elif set(directives[name]) != need:", "elif False:", "CSP の必須の指令の値が完全一致か"),
    ("tools/check_dist.py", "        if name not in directives:\n", "        if False:\n", "CSP の必須の指令があるか"),
    ("tools/check_dist.py", "bad = [v for v in values if v not in CSP_ALLOWED[name]]", "bad = []", "CSP の許していない値の検出"),
    ("tools/check_dist.py", "if {k: sorted(v) for k, v in got.items()} != {k: sorted(v) for k, v in expected.items()}:", "if False:", "<meta> とヘッダの CSP の食い違い"),
    ("tools/check_dist.py", '            elif key in SECURITY_HEADER_NAMES and path != "/*":', "            elif False:", "安全のヘッダを別のパスで上書きさせない"),
    ("tools/check_dist.py", "            if key not in ALLOWED_HEADER_NAMES:", "            if False:", "_headers に書いてよいヘッダの制限（Refresh 等）"),
    ("tools/check_dist.py", '        if any((ord(ch) < 0x20 and ch != "\\t") or ord(ch) > 0x7E for ch in raw):', "        if False:", "_headers の制御文字（Cloudflare と読み方がずれる）"),
    ("tools/check_dist.py", "        if len(raw) > HEADERS_MAX_LINE:", "        if False:", "_headers の長すぎる行（Cloudflare に捨てられる）"),
    ("tools/check_dist.py", '        if line.startswith("/") or line.startswith("https://"):', '        if raw.startswith("/") or raw.startswith("https://"):', "字下げしたパスの行を Cloudflare と同じに読む"),
    ("tools/check_dist.py", '        if sep and key.strip().lower() == "max-age" and val.strip().isdigit():', '        if sep and key.strip().lower().endswith("max-age") and val.strip().isdigit():', "HSTS の max-age を指令として読む（x-max-age に当てない）"),
    ("tools/check_dist.py", "    if max_age is None or max_age < HSTS_MIN_MAX_AGE:", "    if max_age is None:", "HSTS の max-age の下限"),
    ("tools/check_dist.py", 'ALLOWED_NAMES = {"_headers"}', 'ALLOWED_NAMES = {"_headers", "_redirects"}', "_redirects を置かせない"),
    ("tools/check_dist.py", 'if any(part.startswith(".") for part in path.relative_to(dist).parts):', "if False:", "隠しファイルの検出"),
    ("tools/check_dist.py", '            problems.append(f"{rel}: シンボリックリンクは公開しません")', "            pass", "シンボリックリンクを公開しない"),
    ("tools/check_dist.py", "marks = [m for m in common.PLACEHOLDER_MARKS if m in text]", "marks = []", "公開用の未設定の印の検出"),
    ("tools/check_dist.py", "            for pattern, why in JS_FORBIDDEN:", "            for pattern, why in []:", "JS の innerHTML・保存・移動・帯の操作の検出"),
    ("tools/check_dist.py", "    for pattern, why in CSS_FORBIDDEN:", "    for pattern, why in []:", "CSS で帯を隠す・狙う・@import の検出"),
    ("tools/check_dist.py", "        if url and not is_same_origin(url):", "        if False:", "CSS から外部を読む url() の検出"),
    ("tools/check_dist.py", "        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:", "        elif False:", "banner.css・freshness.js の中身の固定"),
    ("tools/check_dist.py", "    if banner_css.resolve() not in files:", "    if False:", "全ページが banner.css を読むか"),
    ("tools/check_dist.py", "            if rels and rels <= LINK_NAV_RELS:", "            if rels & LINK_NAV_RELS:", "rel に canonical を混ぜた外部読み込み"),
    ("tools/check_dist.py", 'SRCSET_ATTRS = {"srcset", "imagesrcset"}', 'SRCSET_ATTRS = {"srcset"}', "imagesrcset の外部読み込み"),
    ("tools/check_dist.py",
     """    r"<\\s*([\\w.-]+:)?(script|foreignObject)\\b|\\son[a-z]+\\s*=|javascript:|<!|(href|src)\\s*=\\s*[\\"']\\s*(https?:)?//",""",
     """    r"<\\s*script|\\son[a-z]+\\s*=|javascript:|<\\s*foreignObject",""",
     "SVG の名前空間つき script・外部参照の検出"),
    ("tools/check_dist.py", 'if v.startswith("https://") or v.startswith("mailto:"):', "if True:", "リンク先の制限（javascript:・http:）"),
    ("tools/check_dist.py", 'if not {"noopener", "noreferrer"} <= rels:', "if False:", "外へのリンクの noopener"),
    ("tools/check_dist.py", "        return 2\n    print(\"問題ありませんでした\")", "        return 0\n    print(\"問題ありませんでした\")", "読めなかったときの終了コード 2"),
    ("tools/check_dist.py", 'if not isinstance(rec, dict) or rec.get("state") not in common.STATES:', "if False:", "status.json の三状態"),
    ("tools/check_dist.py", "    if total > PAGE_MAX_BYTES:", "    if False:", "1ページ300KBの上限"),
    ("tools/check_dist.py", '            problems.append(f"{rel}: 読み込む先のファイルがありません: {url!r}")', "            pass", "読み込む先のファイルの有無"),
    ("tools/check_dist.py", "        if path.suffix.lower() in IMAGE_SUFFIXES and size > IMAGE_MAX_BYTES:", "        if False:", "大きな画像の上限"),
    ("tools/check_dist.py", "    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:", "    if False:", "status.json の鮮度の閾値"),
    # ---- fetch.py（取り込み）----
    ("tools/fetch.py", "        if count < floor:", "        if False:", "件数の急減を unreadable に倒す"),
    ("tools/fetch.py", '            common.write_json(data_dir / f"{sid}.json", {**prev, "source": source_block(src)}, compact=True)  # 前回分を保つ', "            pass", "取れないとき前回分を保つ"),
    ("tools/fetch.py", "        return common.STATE_UNREADABLE, None, str(e)", "        return common.STATE_EMPTY, [], None", "取れないことを「0件」にしない"),
    ("tools/fetch.py", 'return common.STATE_UNREADABLE, None, f"取り込み中に想定外の失敗（{type(e).__name__}）", None', "raise", "想定外の失敗で全体を止めない"),
    ("tools/fetch.py", '    if not url.startswith("https://"):\n        raise FetchError', '    if False:\n        raise FetchError', "https 以外へ取りに行かない"),
    ("tools/fetch.py", '        raise FetchError(f"転送は辿りません（HTTP {code}）。', '        return None or FetchError(f"転送は辿りません（HTTP {code}）。', "転送を辿らない"),
    ("tools/fetch.py", "    if len(body) > max_bytes:", "    if False:", "大きさの上限"),
    ("tools/fetch.py", "            return json.loads(text, parse_constant=_reject_constant)", "            return json.loads(text)", "NaN・Infinity を取り込まない"),
    ("tools/fetch.py", "        ensure_encodable(items)\n", "", "書き出せない文字を取り込まない"),
    ("tools/fetch.py", "    if missing:", "    if False:", "CSV の見出しの列（エラーページを0件にしない）"),
    ("tools/fetch.py", "    if any(None in row for row in rows):", "    if False:", "CSV の列の多い行"),
    ("tools/fetch.py", "    if any(None in row.values() for row in rows):", "    if False:", "CSV の列の少ない行（途中で切れた）"),
    ("tools/fetch.py", "        if lacking:", "        if False:", "required_keys"),
    ("tools/fetch.py", '    if source["min_items"] and count < source["min_items"]:', "    if False:", "件数の下限"),
    ("tools/fetch.py", '    if report_time and source["max_report_age_hours"] and now - report_time > timedelta(hours=source["max_report_age_hours"]):', "    if False:", "元が黙って止まったことの検出"),
    ("tools/fetch.py", '        return f"safe-site-fetcher/1 (+{site_url}; contact: {contact_url})"', '        return f"safe-site-fetcher/1 (+{site_url}; contact: {contact_url}; owner@example.com)"', "User-Agent に個人情報を入れない（連絡先あり）"),
    ("tools/fetch.py", '    return f"safe-site-fetcher/1 (+{site_url})"', '    return f"safe-site-fetcher/1 (+{site_url}; owner@example.com)"', "User-Agent に個人情報を入れない"),
    ("tools/fetch.py", '    if contact_url.startswith("https://"):', "    if True:", "User-Agent に mailto を入れない"),
    ("tools/fetch.py", "    if report_time and prev_time and report_time < common.parse_iso_z(prev_time):", "    if False:", "発表時刻の後戻りを unreadable に倒す"),
    ("tools/fetch.py", "        if late():", "        if False:", "全体の締切を過ぎたら取りに行かない"),
    ("tools/fetch.py", '.replace("\\n", "%0A")', "", "Actions の命令の差し込み防止"),
    ("tools/fetch.py", "    if XML_DTD_RE.search(body):", "    if False:", "DOCTYPE・ENTITY を含む XML を読まない"),
    ("tools/common.py", '        if (hb_rec is None or hb_rec.get("state") == STATE_UNREADABLE) and rec.get("state") != STATE_UNREADABLE:', "        if False:", "見張り役が読めないとき、頼る取得先も確かめられないにする"),
    ("tools/build.py", "    common.apply_heartbeats(sources, state)  # サイト専用の取り込みが書いた記録にも効かせる", "    pass", "build でも見張り役を効かせる（サイト専用の取り込みの記録）"),
    # ---- common.py・build.py ----
    ("tools/common.py", '        if hb == s["id"] or hb not in by_id:', "        if False:", "heartbeat はほかの取得先の id だけ"),
    ("tools/common.py", '        if by_id[hb]["heartbeat"] is not None:', "        if False:", "見張りの連鎖を作らない"),
    ("tools/common.py", '"url": src["url"].replace("{code}", code)', '"url": src["url"]', "each の {code} の展開"),
    ("tools/common.py", "    if production:\n        leftovers = find_placeholders(cfg)", "    if False:\n        leftovers = find_placeholders(cfg)", "公開用の設定に未設定を残さない"),
    ("tools/common.py", 'SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")', 'SOURCE_ID_RE = re.compile(r".*")', "取得先の id でパスの外へ出させない"),
    ("tools/common.py", '    if url.startswith("https://") or (allow_mailto and url.startswith("mailto:")):', "    if True:", "設定の URL は https だけ"),
    ("tools/common.py", '        if src["format"] == "csv" and src["columns"] is None:', "        if False:", "CSV には columns を必須にする"),
    ("tools/build.py", """f'<th scope="row">{esc(src["name"])}</th>'""", """f'<th scope="row">{src["name"]}</th>'""", "外から来た文字列のエスケープ"),
    ("tools/build.py", "            rec = dict(never)", '            rec = {**never, "state": common.STATE_EMPTY}', "一度も取れていない取得先を「0件」にしない"),
    ("tools/build.py", "    if out != (root / \"dist\").resolve() and tmp not in out.parents:", "    if False:", "消してよい場所だけを作り直す"),
    ("tools/build.py", '    if out.is_symlink() or (root / "dist").is_symlink():', "    if False:", "シンボリックリンクの先を消さない"),
    ("tools/build.py", '            if not isinstance(value, str) or not CSP_VALUE_RE.match(value) or ";" in value or "," in value:', "            if False:", "headers.json の CSP の値で行や指令を割らせない"),
    ("tools/build.py", "        if for_meta and name in META_UNSUPPORTED:", "        if False:", "<meta> の CSP から frame-ancestors を除く"),
    # ---- 第5版: サイト専用の呼び口・設定・データの公開・Cookie・試験のネット遮断 ----
    ("tools/common.py", '    if unknown:\n        raise ConfigError(f"site/config.json に知らない項目', '    if False:\n        raise ConfigError(f"site/config.json に知らない項目', "config.json の知らない項目を止める"),
    ("tools/common.py", '    problems = site_hooks.check_config(cfg["site"], production=production)', "    problems = []", "site の設定を site_hooks で検める"),
    ("tools/common.py", "        if outside:", "        if False:", "取得先の URL の前置き（source_url_prefixes）"),
    ("tools/common.py", '            if not prefix.endswith("/"):', "            if False:", "前置きは / で終わる"),
    ("tools/build.py", '    if cfg["publish_data"]:', "    if True:", "publish_data が false なら dist/data を作らない"),
    ("tools/build.py", "        if not isinstance(rel, str) or not EXTRA_PAGE_RE.match(rel) or rel in GENERATED_PAGES or (out / rel).exists():", "        if False:", "site_hooks のページの名前の検査"),
    ("tools/check_dist.py", '    if cfg is not None and not cfg["publish_data"] and (dist / "data").exists():', "    if False:", "publish_data が false なのに dist/data がある"),
    ("tools/check_dist.py", "    problems += hook_problems\n    unreadable += hook_unreadable\n", "    pass\n", "check_dist から site_hooks を呼ぶ"),
    ("tools/check_repo.py", "    return problems + hook_problems, unreadable + hook_unreadable", "    return problems, unreadable", "check_repo から site_hooks を呼ぶ"),
    ("tools/check_live.py", '        if "set-cookie" in headers:', "        if False:", "Cookie を返す応答の検出"),
    ("tools/check_live.py", "    problems += hook_problems\n    unreadable += hook_unreadable\n", "    pass\n", "check_live から site_hooks を呼ぶ"),
    ("tools/fetch.py", '            payload = {"fetched_at": stamp, "items": items, "source": source_block(src)}', '            payload = {"fetched_at": stamp, "items": items}', "公開する JSON に出典を付ける"),
    ("tests/helpers.py", "socket.socket.connect = _no_network  # type: ignore[method-assign]\nsocket.socket.connect_ex = _no_network  # type: ignore[method-assign]\nsocket.create_connection = _no_network  # type: ignore[assignment]\n", "", "試験のネットの遮断"),
    # ---- check_live.py（公開後）----
    ("tools/check_live.py", "    if now - generated > max_age:", "    if False:", "定期実行が止まったことの検出"),
    ("tools/check_live.py", "        elif status != 404:", "        elif False:", "何でも 200 を返す設定の検出"),
    ("tools/check_live.py", '        if state != "active":', "        if False:", "止められた workflow の検出（60日無活動）"),
    ("tools/check_live.py", "            elif hashlib.sha256(body).hexdigest() != expected[rel]:", "            elif False:", "公開中のファイルと今作った物の照合"),
    ("tools/check_live.py", '    if final.scheme != "https" or final.netloc.lower() != base.netloc.lower():', "    if False:", "別のホストへの転送の検出"),
    # ---- check_repo.py（設定ファイル）----
    ("tools/check_repo.py",
     """PINNED_USES_RE = re.compile(r"^\\s*(-\\s+)?uses: ([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}) # v\\d+(\\.\\d+){0,2}\\s*$")""",
     """PINNED_USES_RE = re.compile(r"^\\s*(-\\s+)?uses:\\s*(\\S+)(.*)$")""",
     "uses の決まった形（SHA 固定・版の注記）"),
    ("tools/check_repo.py", "        if expr not in ALLOWED_EXPRESSIONS:", "        if False:", "許していない式 ${{ }} の検出"),
    ("tools/check_repo.py", '        if not (in_deploy_job and code.strip() == f"CLOUDFLARE_API_TOKEN: {SECRET_EXPRESSION}"):', "        if False:", "秘密は deploy ジョブの決まった1行だけ"),
    ("tools/check_repo.py", '            if "${{" in content:', "            if False:", "run: の中の ${{ }}（差し込み）"),
    ("tools/check_repo.py", "    if not TOP_PERMISSIONS_RE.search(code_text):", "    if False:", "一番上の permissions: {}"),
    ("tools/check_repo.py", "    if FLOW_RE.search(flow_text):", "    if False:", "フロー形式の禁止（書き方の揺れ）"),
    ("tools/check_repo.py", '            if code.strip() == "persist-credentials: false":', '            if "persist-credentials: false" in code_text:', "checkout の手順の中の persist-credentials: false"),
    ("tools/check_repo.py", '    (re.compile(r":\\s*write\\b", re.I), ', '    (re.compile(r"(?!x)x", re.I), ', "書き込みの権限の検出"),
    ("tools/check_repo.py", '    if cfg.get("preview_urls") is not False:', "    if False:", "プレビュー URL を作らない"),
    ("tools/check_repo.py", "    unknown = set(cfg) - WRANGLER_KEYS", "    unknown = set()", "wrangler.jsonc の項目の制限（build 等）"),
    ("tools/check_repo.py", "    unknown = set(pkg) - PACKAGE_KEYS", "    unknown = set()", "package.json の項目の制限（scripts・packageManager）"),
    ("tools/check_repo.py", "        if not isinstance(ver, str) or not EXACT_VERSION_RE.match(ver):", "        if False:", "npm の版の固定"),
    ("tools/check_repo.py", "        elif not resolved.startswith(REGISTRY):", "        elif False:", "lockfile の取得元は npm の公式だけ"),
    ("tools/check_repo.py", "        if path.exists() or path.is_symlink():", "        if False:", ".claude・.github/actions・.npmrc・wrangler.json 等を置かせない"),
    ("tools/check_repo.py", "            if tracked:", "            if False:", "生成物を git に入れさせない"),
    ("tools/check_repo.py", "        if len(days) < len(ecosystems) or any(d < COOLDOWN_MIN_DAYS for d in days):", "        if len(days) < len(ecosystems):", "Dependabot の cooldown の日数の下限"),
    # ---- repo_settings（GitHub の設定を入れる・確かめる。gh のアカウントが管理者でなければ何もしない）----
    ("repo_settings/check.sh", 'if [ "$ADMIN" != "true" ]; then', 'if false; then', "check.sh は管理者でないアカウントでは何も確かめない"),
    ("repo_settings/check.sh", "--jq '.permissions.admin' 2>/dev/null) || ADMIN=\"\"", "--jq '.permissions.admin' 2>/dev/null) || ADMIN=\"true\"", "check.sh は管理者か読めないときも止まる"),
    ("repo_settings/apply.sh", 'if [ "$ADMIN" != "true" ]; then', 'if false; then', "apply.sh は管理者でないアカウントでは何も入れない"),
    ("repo_settings/apply.sh", "--jq '.permissions.admin' 2>/dev/null) || ADMIN=\"\"", "--jq '.permissions.admin' 2>/dev/null) || ADMIN=\"true\"", "apply.sh は管理者か読めないときも止まる"),
    ("repo_settings/check.sh", 'echo "違う    ${name}（今: ${got} / 期待: ${expected}）"', 'echo "違う    $name（今: $got / 期待: $expected）"', "macOS の sh で変数名を読み違えない書き方（${name}）"),
    # ---- 第1号の関所（tools/site_hooks.py・計画書 5.2 の PR1）----
    ("tools/site_hooks.py", "    if hits:", "    if False:", "G-名前: 禁止語の判定"),
    ("tools/site_hooks.py", '    text = text.replace("非公式", "")', "    text = text", "G-名前: 「非公式」を取り除いてから判定する（既定の名前を落とさない）"),
    ("tools/site_hooks.py", '        problems += name_problems(top.title or "", "index.html の <title>")', "        pass", "G-名前: トップの <title> も対象"),
    ("tools/site_hooks.py", "        if isinstance(site_name, str) and not site_name.endswith(UNOFFICIAL_SUFFIX):", "        if False:", "G-名前: サイト名は「（非公式）」で終わる"),
    ("tools/site_hooks.py", "        elif not DELAY_MIN <= delay <= DELAY_MAX:", "        elif False:", "設定キー: warning_delay_minutes の範囲"),
    ("tools/site_hooks.py", "    unknown = sorted(set(site) - set(SITE_KEYS))", "    unknown = []", "設定キー: site のほかのキーを止める"),
    ("tools/site_hooks.py", "        if worst > DAILY_BUDGET_BYTES:", "        if False:", "G-取得量: 予算との比較"),
    ("tools/site_hooks.py", "                problems.append(f\"G-取得量: custom の取得先 {src['id']} の最悪値を計算できません（{e}）。0 と数えずに止めます\")", "                pass", "G-取得量: custom の取得先を0と数えない"),
    ("tools/site_hooks.py", "    if enabled is False:", "    if False:", "G-準備中: 旗が false の間の検査"),
    ("tools/site_hooks.py", "        if top is not None and PREPARING_TEXT not in top_text:", "        if False:", "G-準備中: トップに「準備中です」"),
    ("tools/site_hooks.py", '    if any(line.lower().startswith("allow:") for line in lines):', "    if False:", "G-準備中: robots.txt に Allow を置かない"),
    ("tools/site_hooks.py", "        if top is not None and PREPARING_TEXT in top_text:", "        if False:", "G-準備中: 旗が true ならトップに「準備中です」を残さない"),
    # ---- 第1号の二段の取り込み（tools/fetch_jma_warning.py・計画書 5.2 の G-二段・PR2）と G-取得量の custom の加算 ----
    ("tools/site_hooks.py", "    per_run += sum(custom_worst.values())", "    pass", "G-取得量: custom の取得先の最悪値を足す"),
    ("tools/fetch_jma_warning.py", "    if not isinstance(href, str) or not href.startswith(LINK_PREFIX):", "    if not isinstance(href, str):", "G-二段: 前置きの外のリンクを辿らない"),
    ("tools/fetch_jma_warning.py", "    if failed:", "    if False:", "G-二段: 1本でも読めなければ warn 全体を unreadable"),
    ("tools/fetch_jma_warning.py", "    wanted = wanted[:MAX_LINKS_PER_RUN]", "    wanted = wanted", "G-二段: 1回に辿る数の上限"),
    ("tools/fetch_jma_warning.py", "        if LIMIT_TO_PREPARING_OFFICES and office not in PREPARING_OFFICES:", "        if False:", "G-二段: 準備中は2官署の電文だけを辿る"),
    ("tools/fetch_jma_warning.py", '        backfill = min(times) > common.parse_iso_z(prev["fetched_at"])', "        backfill = False", "G-二段: 取りこぼしの疑いで長期フィードを読む"),
    ("tools/fetch_jma_warning.py", "lambda: clock() - started > DEADLINE_SECONDS", "lambda: False", "G-二段: 締切を過ぎたら取りに行かない"),
    ("tools/fetch_jma_warning.py", "        key = (office, kind)", '        key = (office, "")', "G-二段: 官署×電文の種類ごとに保持する"),
    ("tools/fetch_jma_warning.py", '        report_time = common.iso_z(common.parse_report_time(feed["updated"][0]["#text"]))', '        report_time = feed["updated"][0]["#text"]', "G-二段: data/warn.json の report_time は UTC の Z の形"),
]


def run_tests(root: Path) -> int:
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "unittest", "discover", "-s", "tests", "-q"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return -1  # 止まらない版も「赤」に数える
    return proc.returncode


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(ROOT, base, ignore=COPY_IGNORE)
        if run_tests(base) != 0:
            print("壊す前の版で試験が通りません。先に試験を直してください", file=sys.stderr)
            return 2
        for i, (rel, old, new, what) in enumerate(MUTANTS, 1):
            label = f"M{i:02d} {what}"
            work = Path(tmp) / f"m{i:02d}"
            shutil.copytree(base, work)
            path = work / rel
            text = path.read_text(encoding="utf-8")
            count = text.count(old)
            if count != 1:
                failures.append(f"{label}: 壊す場所が {count} か所あります（1か所であるべき）。型が古い")
                shutil.rmtree(work)
                continue
            path.write_text(text.replace(old, new), encoding="utf-8")
            if run_tests(work) == 0:
                failures.append(f"{label}: 壊しても試験が通りました（生き残り）")
            else:
                print(f"赤になりました: {label}")
            shutil.rmtree(work)
    for failure in failures:
        print(f"検算の失敗: {failure}")
    if failures:
        return 1
    print(f"検算: {len(MUTANTS)} 件すべて赤になりました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
