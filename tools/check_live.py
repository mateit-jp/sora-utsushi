#!/usr/bin/env python3
"""公開中のサイトを外から確かめる（デプロイ直後の verify ジョブと、外の見張りの両方から使う）。

使い方:
  python3 -I tools/check_live.py                       # site/config.json の site_url を見る（デプロイ直後の verify）
  python3 -I tools/check_live.py https://<公開URL>/ --repo OWNER/REPO --max-source-age-minutes 720   # 外の見張り

見るもの:
- トップと status.html の応答ヘッダが check_dist.py の最低方針を満たすか（CSP ほか）
- トップの HTML に「非公式」の帯があるか（check_dist.py と同じ検査）
- 無いはずのページが 404 を返すか（何でも 200 を返す設定になっていないか）
- status.json の generated_at が古すぎないか（定期実行が黙って止まっていないか）
- （--max-source-age-minutes を付けたときだけ）各取得先の最後の成功が古すぎないか
- （--repo を付けたときだけ）GitHub の workflow が止められていないか（60日無活動の自動停止）
- （--expect-dist を付けたときだけ）公開中のファイルが、今作った dist と1バイトも違わないか（全件）
終了コード: 0=問題なし / 1=違反あり / 2=読めなかった（つながらない等。「問題なし」とは言わない）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_dist  # noqa: E402
import common  # noqa: E402
import site_hooks  # noqa: E402

MAX_BODY = 5_000_000
USER_AGENT = "safe-site-check-live/1"
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
NOT_SERVED = {"_headers", "_redirects"}  # Cloudflare が読むだけで、ファイルとしては配らない
VERIFY_RETRIES = 3
VERIFY_WAIT_SECONDS = 20  # 公開の直後は、古い版を返す拠点が残ることがある

# get(url) -> (HTTP の状態コード, 小文字の名前にした応答ヘッダ, 本文, 転送を辿った後の URL)
Getter = Callable[[str], "tuple[int, dict[str, str], bytes, str]"]


class Unreadable(Exception):
    pass


def http_get(url: str) -> tuple[int, dict[str, str], bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read(MAX_BODY), resp.geturl()
    except urllib.error.HTTPError as e:
        headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, headers, e.read(MAX_BODY) if e.fp else b"", url
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Unreadable(f"{url} につながりませんでした（{type(e).__name__}）") from None
    except Exception as e:  # noqa: BLE001  壊れた応答も「確かめられなかった」に倒す
        raise Unreadable(f"{url} を読めませんでした（{type(e).__name__}）") from None


def moved_away(base_url: str, final_url: str) -> str | None:
    """転送の行き先が、同じホストの https でなければ理由を返す（全ページを別のサイトへ飛ばす設定を見つける）。"""
    base, final = urlsplit(base_url), urlsplit(final_url)
    if final.scheme != "https" or final.netloc.lower() != base.netloc.lower():
        return f"別の場所へ転送されています: {final_url}"
    return None


def check_live(
    base_url: str,
    *,
    get: Getter,
    now: datetime,
    max_age: timedelta,
    max_source_age: timedelta | None,
) -> tuple[list[str], list[str]]:
    """戻り値は (違反, 読めなかったもの)。"""
    problems: list[str] = []
    unreadable: list[str] = []

    pages = {}
    for path in ("", "status.html"):
        url = base_url + path
        try:
            status, headers, body, final = get(url)
        except Unreadable as e:
            unreadable.append(str(e))
            continue
        away = moved_away(base_url, final)
        if away:
            problems.append(f"{url}: {away}")
            continue
        if status != 200:
            problems.append(f"{url}: HTTP {status}")
            continue
        if "set-cookie" in headers:
            problems.append(f"{url}: Cookie を返しています（Set-Cookie。Cookie を使わない方針・about の文と食い違う）")
        problems += check_dist.check_header_set(headers, url)
        pages[url] = (headers, body)

    top = pages.get(base_url)
    if top is not None:
        headers, body = top
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{base_url}: UTF-8 の HTML ではありません")
        else:
            problems += check_dist.check_html_text(text, base_url, headers.get("content-security-policy"))

    missing = f"{base_url}no-such-page-{secrets.token_hex(8)}.html"
    try:
        status, _, _, final = get(missing)
        if moved_away(base_url, final):
            problems.append(f"無いはずのページが {final} へ転送されました（404 であるべき）")
        elif status != 404:
            problems.append(f"無いはずのページが HTTP {status} を返しました（404 であるべき）: {missing}")
    except Unreadable as e:
        unreadable.append(str(e))

    try:
        status, _, body, final = get(base_url + "status.json")
    except Unreadable as e:
        unreadable.append(str(e))
        return problems, unreadable
    if moved_away(base_url, final):
        problems.append(f"status.json: {moved_away(base_url, final)}")
        return problems, unreadable
    if status != 200:
        problems.append(f"status.json: HTTP {status}")
        return problems, unreadable
    try:
        obj = json.loads(body.decode("utf-8"))
        generated = common.parse_iso_z(obj["generated_at"])
        sources = obj["sources"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
        problems.append(f"status.json の形が変です（{type(e).__name__}）")
        return problems, unreadable
    if now - generated > max_age:
        problems.append(
            f"status.json が古いままです（生成 {common.jst_text(generated)}）。定期実行が止まっていないか、"
            "Actions の画面と「60日無活動で定期実行が止まる」規則を確かめてください"
        )
    hook_problems, hook_unreadable = site_hooks.check_live(base_url=base_url, get=get, now=now)
    problems += hook_problems
    unreadable += hook_unreadable
    if max_source_age is not None and isinstance(sources, dict):
        for sid, rec in sources.items():
            last = rec.get("last_success_at") if isinstance(rec, dict) else None
            try:
                last_dt = common.parse_iso_z(last) if last else None
            except (TypeError, ValueError):
                last_dt = None
            if last_dt is None:
                problems.append(f"取得先 {sid}: 一度も取得できていません")
            elif now - last_dt > max_source_age:
                problems.append(f"取得先 {sid}: 最後に取得できたのは {common.jst_text(last_dt)} です")
    return problems, unreadable


def dist_hashes(dist: Path) -> dict[str, str]:
    """今作った dist の、配るファイルごとの sha256。"""
    hashes = {}
    for path in sorted(dist.rglob("*")):
        if path.is_file() and path.name not in NOT_SERVED:
            hashes[path.relative_to(dist).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def check_same_as_dist(
    base_url: str,
    dist: Path,
    *,
    get: Getter,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = VERIFY_RETRIES,
    wait_seconds: float = VERIFY_WAIT_SECONDS,
) -> tuple[list[str], list[str]]:
    """公開中のファイルが、今作った物と1バイトも違わないかを全件で照合する。

    CDN 側での書き換え（解析タグの自動挿入・メールアドレスの難読化など）や、検査を通っていない版の公開を見つける。
    一致しない物は、少し待って読み直す（公開の直後は古い版を返す拠点が残ることがある）。
    """
    expected = dist_hashes(dist)
    if not expected:
        return [], [f"{dist} に照合するファイルがありません"]
    pending = sorted(expected)
    mismatched: dict[str, str] = {}
    unreadable: dict[str, str] = {}
    for attempt in range(retries + 1):
        mismatched, unreadable = {}, {}
        for rel in pending:
            try:
                status, _, body, final = get(base_url + quote(rel))
            except Unreadable as e:
                unreadable[rel] = str(e)
                continue
            if moved_away(base_url, final):
                mismatched[rel] = moved_away(base_url, final)
            elif hashlib.sha256(body).hexdigest() != expected[rel]:
                mismatched[rel] = f"HTTP {status}・中身が違う"
        pending = sorted(set(mismatched) | set(unreadable))
        if not pending:
            return [], []
        if attempt < retries:
            sleep(wait_seconds)
    problems = [f"公開中の {rel} が今作った物と一致しません（{why}）" for rel, why in sorted(mismatched.items())]
    return problems, [f"{rel}: {why}" for rel, why in sorted(unreadable.items())]


def check_workflows(repo: str, *, get: Getter) -> tuple[list[str], list[str]]:
    """GitHub の公開 API で、workflow が止められていないかを見る（60日無活動で disabled_inactivity になる）。"""
    url = f"https://api.github.com/repos/{repo}/actions/workflows?per_page=100"
    try:
        status, _, body, _ = get(url)
    except Unreadable as e:
        return [], [str(e)]
    if status != 200:
        return [], [f"{url}: HTTP {status}（workflow の状態を読めませんでした。止まっていないとは言えません）"]
    try:
        workflows = json.loads(body.decode("utf-8"))["workflows"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
        return [], [f"workflow の一覧の形が変です（{type(e).__name__}）"]
    if not isinstance(workflows, list) or not workflows:
        return [], ["workflow が1つも見えません（読めなかったものとして扱います）"]
    problems = []
    for wf in workflows:
        state = wf.get("state") if isinstance(wf, dict) else None
        if state != "active":
            path = wf.get("path") if isinstance(wf, dict) else "?"
            problems.append(
                f"workflow {path} が止められています（state={state}）。60日無活動で止まったのなら、"
                "Actions の画面でその workflow を開き Enable workflow を押してください"
            )
    return problems, []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="公開中のサイトを外から確かめる")
    ap.add_argument("base_url", nargs="?", default=None, help="公開中のサイトの URL（既定: site/config.json の site_url）")
    ap.add_argument("--max-age-minutes", type=int, default=None, help="status.json の許す古さ（既定: config の値）")
    ap.add_argument("--max-source-age-minutes", type=int, default=None, help="各取得先の最後の成功の許す古さ（既定: 見ない）")
    ap.add_argument("--repo", default=None, help="OWNER/REPO を渡すと、workflow が止められていないかも見る（外の見張り用）")
    ap.add_argument("--expect-dist", default=None, help="今作った dist のフォルダ。公開中のファイルと全件で照合する（verify ジョブ用）")
    args = ap.parse_args(argv)
    if args.repo is not None and not REPO_RE.match(args.repo):
        print("--repo は OWNER/REPO の形にしてください", file=sys.stderr)
        return 2
    base, max_age_minutes = args.base_url, args.max_age_minutes
    if base is None or max_age_minutes is None:
        try:
            cfg = common.load_config(common.ROOT, production=False)
        except common.ConfigError as e:
            print(f"設定を読めませんでした: {e}", file=sys.stderr)
            return 2
        base = base or cfg["site_url"]
        max_age_minutes = max_age_minutes or cfg["max_status_age_minutes"]
    if not base.startswith("https://") or not base.endswith("/"):
        print("URL は https:// で始まり / で終わる形にしてください", file=sys.stderr)
        return 2
    problems, unreadable = check_live(
        base,
        get=http_get,
        now=common.utc_now(),
        max_age=timedelta(minutes=max_age_minutes),
        max_source_age=timedelta(minutes=args.max_source_age_minutes) if args.max_source_age_minutes else None,
    )
    if args.repo is not None:
        more_problems, more_unreadable = check_workflows(args.repo, get=http_get)
        problems += more_problems
        unreadable += more_unreadable
    if args.expect_dist is not None:
        more_problems, more_unreadable = check_same_as_dist(base, Path(args.expect_dist), get=http_get)
        problems += more_problems
        unreadable += more_unreadable
    for p in problems:
        print(f"違反: {p}")
    for u in unreadable:
        print(f"確かめられませんでした: {u}")
    if problems:
        return 1
    if unreadable:
        return 2
    print("問題ありませんでした")
    return 0


if __name__ == "__main__":
    sys.exit(main())
