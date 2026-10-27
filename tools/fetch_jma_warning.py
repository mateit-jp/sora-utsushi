#!/usr/bin/env python3
"""気象警報・注意報の電文（防災情報XML の VPWW55〜61）を二段で取り込む（第1号専用。計画書 4.5・9.4）。

使い方（deploy.yml の build ジョブで fetch.py の直後に走らせる。計画書 8.5）:
  python3 -I tools/fetch_jma_warning.py --previous-from-live   # 前回分は公開中のサイトの /data/warn.json から読む
  python3 -I tools/fetch_jma_warning.py                        # 前回分は手元の data/warn.json だけ

決まり（CLAUDE.md 掟6b・計画書 4.5 の①〜⑧）:
- ⓪ 前回分（data/warn.json）は fetch.load_previous_one で読む。
- ① 毎回、高頻度フィード（sources.json の warn の url＝extra.xml）を読む。長期フィード（BACKFILL_FEED_URL）は、
  (a) 前回分が無い・読めない (b) 高頻度フィードの entry の updated の最古が前回の fetched_at より新しい
  （その間を取りこぼした疑い。entry の時刻が読めなければ当たるとみなす）のときだけ読む。同じ電文は URL で重ねる。
- ② 辿るのは、電文の種類が VPWW55〜61 で、リンクが LINK_PREFIX（https）で始まる entry だけ。
- ③ 準備中公開の間（LIMIT_TO_PREPARING_OFFICES が True）は、PREPARING_OFFICES の官署の電文だけを辿る。
  官署の見分けは entry のメタデータだけ（リンクのファイル名の末尾のコード。site/warn_offices.json の key）で行い、
  電文を開いてから絞ることはしない（絞る前に全官署の電文を取りに行かないため）。
- ④ 1回に辿る数・電文1本・フィードの大きさ・同時取得数・待ち時間・全体の締切に上限がある（下の定数）。
- ⑤ 取り込み済みの電文は URL で前回分から使い回す（電文は不変）。
- ⑥ 1本でも読めなければ（上限を超えて辿れなかった・締切を過ぎた を含む）warn 全体を unreadable にし、前回分を保つ
  （一部だけ抜けた表示は「無い」に読める）。
- ⑦ 取得は fetch.http_get（転送を辿らない・大きさの上限）、XML は fetch.parse_xml（DOCTYPE・ENTITY を読まない）。
- 官署×電文の種類ごとに最新の電文1件を残す。フィードから消えても、同じ官署・同じ種類のより新しい電文が来るまで持ち越す。
- data/warn.json の fetched_at と report_time は common.iso_z() の末尾 Z の形（fetch.load_previous_one が前回分として読める形）。
- 記録は fetch.merge_records() で data/state.json に足す。warn の heartbeat（jma-feed）は build.py が当てる。
- 電文の文字列は取り出したまま持つ（要約・言い換え・コード表からの名前づけをしない）。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import fetch  # noqa: E402

SOURCE_ID = "warn"
# ① 長期フィード（取りこぼしの拾い）
BACKFILL_FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra_l.xml"
# ② 辿ってよいリンクの前置き（2026-10-23・24 の実測で、フィードの電文のリンクはすべてこれで始まった）
LINK_PREFIX = "https://www.data.jma.go.jp/developer/xml/data/"
WARNING_TYPES = ("VPWW55", "VPWW56", "VPWW57", "VPWW58", "VPWW59", "VPWW60", "VPWW61")
# リンクのファイル名: <作成時刻14桁>_<版>_<電文の種類>_<官署のコード6桁>.xml（末尾のコードが site/warn_offices.json の key "file_code"）
LINK_NAME_RE = re.compile(r"^\d{14}_\d+_(VPWW\d{2})_(\d{6})\.xml$")
# ③ 準備中公開の間に辿る官署（東京都・大阪府。site/warn_offices.json の 130000・270000 の値と同じ）
PREPARING_OFFICES = ("130000", "270000")
LIMIT_TO_PREPARING_OFFICES = True
# ④ 上限（計画書 4.5 の④・7.3。値を変えたら PR の本文に G-取得量の式の値を書く）
MAX_LINKS_PER_RUN = 50
MAX_BYTES_PER_LINK = 200_000  # 電文の見本の最大 26,802 バイトの3倍（80,406）以上
FEED_MAX_BYTES = 937_299  # extra.xml の見本の最大 312,433 バイトの3倍（sources.json の jma-feed の max_bytes と同じ）
BACKFILL_FEED_MAX_BYTES = 11_258_559  # extra_l.xml の見本 3,752,853 バイトの3倍
MAX_WORKERS = 4
LINK_TIMEOUT_SECONDS = 10
DEADLINE_SECONDS = 180  # 前回分 10秒＋フィード2本 20秒＋電文 ceil(50/4)×10＝130秒 の合計 160秒が収まる


class WarningFetchError(fetch.FetchError):
    pass


def _text(node: object, *path: str) -> str:
    """xml_to_dict の形を、子の名前の並びで辿って文字を返す（各段の先頭）。無ければ WarningFetchError。"""
    for name in path:
        if not isinstance(node, dict) or not node.get(name):
            raise WarningFetchError(f"電文に {'/'.join(path)} がありません（形式の変化を疑う）")
        node = node[name][0]
    if not isinstance(node, dict):
        raise WarningFetchError(f"電文の {'/'.join(path)} の形が違います")
    return node.get("#text", "")


def _optional(node: dict, name: str) -> str | None:
    """子の要素の文字。要素が無ければ None（空の要素は ""）。"""
    if not node.get(name):
        return None
    child = node[name][0]
    return child.get("#text", "") if isinstance(child, dict) else None


def entry_time(entry: dict) -> datetime | None:
    try:
        return common.parse_report_time(entry["updated"][0]["#text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def entry_link(entry: dict) -> str | None:
    for link in entry.get("link", []):
        href = link.get("@href") if isinstance(link, dict) else None
        if isinstance(href, str):
            return href
    return None


def classify(href: str | None) -> tuple[str, str] | None:
    """② 辿ってよい電文のリンクなら (電文の種類, 官署のコード) を返す。前置きの外・種類の外は None。"""
    if not isinstance(href, str) or not href.startswith(LINK_PREFIX):
        return None
    m = LINK_NAME_RE.match(href.rsplit("/", 1)[-1])
    if not m or m.group(1) not in WARNING_TYPES:
        return None
    return m.group(1), m.group(2)


def extract_item(doc: dict, url: str, kind: str, office: str) -> dict:
    """電文から、名称・区域名・見出し文の文字列を取り出したまま持つ（取り出す要素は見本の10本すべてにあるもの）。"""
    head = doc.get("Head", [None])[0]
    body = doc.get("Body", [None])[0]
    if not isinstance(body, dict) or not body.get("Warning"):
        raise WarningFetchError("電文に Body/Warning がありません（形式の変化を疑う）")
    headline = head.get("Headline", [{}])[0] if isinstance(head, dict) else {}
    warnings = []
    for block in body["Warning"]:
        items = []
        for item in block.get("Item", []):
            items.append({
                # Kind には Name の無いもの（Status「発表警報・注意報はなし」だけ）がある。無い要素は None のまま持つ
                "kinds": [{"name": _optional(k, "Name"), "status": _optional(k, "Status")} for k in item.get("Kind", [])],
                "area": _text(item, "Area", "Name"),
            })
        warnings.append({"type": block.get("@type", ""), "items": items})
    return {
        "url": url,
        "office": office,
        "kind": kind,
        "report_datetime": _text(doc, "Head", "ReportDateTime"),
        "control_title": _text(doc, "Control", "Title"),
        "publishing_office": _text(doc, "Control", "PublishingOffice"),
        "head_title": _text(doc, "Head", "Title"),
        "headline": headline.get("Text", [{}])[0].get("#text", "") if isinstance(headline, dict) else "",
        "warnings": warnings,
    }


def read_feed(url: str, max_bytes: int, timeout: float, fetcher: fetch.Fetcher, ua: str) -> dict:
    body = fetcher(url, timeout=timeout, max_bytes=max_bytes, user_agent=ua)
    doc = fetch.parse_xml(body)
    if not isinstance(doc.get("entry", []), list):
        raise WarningFetchError("フィードの entry の形が違います")
    return doc


def run(
    root: Path,
    *,
    fetcher: fetch.Fetcher,
    previous_base_url: str | None,
    now: datetime,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """取り込んで data/warn.json と data/state.json の warn の記録を書く。戻り値は warn の記録。"""
    cfg = common.load_config(root, production=False)
    by_id = {s["id"]: s for s in common.load_sources(root, production=False)}
    src = by_id.get(SOURCE_ID)
    if src is None or src["format"] != "custom":
        raise common.ConfigError(f"sources.json に custom の取得先 {SOURCE_ID} がありません")
    ua = fetch.user_agent(cfg["site_url"], cfg["contact_url"])
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    started = clock()
    stamp = common.iso_z(now)

    prev, _note = fetch.load_previous_one(src, previous_base_url, data_dir, fetcher, ua)  # ⓪
    try:
        payload = _collect(src, prev, fetcher, ua, now, lambda: clock() - started > DEADLINE_SECONDS)
    except fetch.FetchError as e:
        error = str(e)
        payload = None
    except Exception as e:  # noqa: BLE001  想定外も「読めなかった」に倒す（「無い」にしない）
        error = f"取り込み中に想定外の失敗（{type(e).__name__}）"
        payload = None

    if payload is not None:
        fetch.ensure_encodable(payload["items"])
        common.write_json(data_dir / f"{SOURCE_ID}.json", payload, compact=True)
        record = {
            "state": common.STATE_OK if payload["items"] else common.STATE_EMPTY,
            "checked_at": stamp,
            "last_success_at": stamp,
            "items": len(payload["items"]),
            "report_time": payload["report_time"],
            "error": None,
            "showing_previous": False,
        }
    elif prev is not None:
        common.write_json(data_dir / f"{SOURCE_ID}.json", {**prev, "source": fetch.source_block(src)}, compact=True)  # 前回分を保つ
        record = {
            "state": common.STATE_UNREADABLE,
            "checked_at": stamp,
            "last_success_at": prev["fetched_at"],
            "items": len(prev["items"]),
            "report_time": prev.get("report_time"),
            "error": error,
            "showing_previous": True,
        }
    else:
        record = {
            "state": common.STATE_UNREADABLE,
            "checked_at": stamp,
            "last_success_at": None,
            "items": None,
            "report_time": None,
            "error": error,
            "showing_previous": False,
        }
    fetch.merge_records(root, {SOURCE_ID: record})
    return record


def _collect(src: dict, prev: dict | None, fetcher: fetch.Fetcher, ua: str, now: datetime, late: Callable[[], bool]) -> dict:
    # ① 高頻度フィード（正）
    feed = read_feed(src["url"], FEED_MAX_BYTES, src["timeout_seconds"], fetcher, ua)
    try:
        report_time = common.iso_z(common.parse_report_time(feed["updated"][0]["#text"]))
    except (KeyError, IndexError, TypeError, ValueError):
        raise WarningFetchError("高頻度フィードの updated を読めませんでした") from None
    entries = list(feed.get("entry", []))

    # ① 取りこぼしの疑いがあれば長期フィードも読む
    times = [entry_time(e) for e in entries]
    if prev is None:
        backfill = True
    elif not times or any(t is None for t in times):
        backfill = True
    else:
        backfill = min(times) > common.parse_iso_z(prev["fetched_at"])
    if backfill:
        if late():
            raise WarningFetchError(f"締切（{DEADLINE_SECONDS}秒）を過ぎました")
        entries += list(read_feed(BACKFILL_FEED_URL, BACKFILL_FEED_MAX_BYTES, src["timeout_seconds"], fetcher, ua).get("entry", []))

    # ②③ 辿ってよい電文を、官署×種類ごとに最新の1本へ絞る（同じ URL は重ねる）
    # ファイル名は作成時刻14桁で始まるので、同じ官署・種類の中では名前の大きい方が新しい
    newest: dict[tuple[str, str], str] = {}
    for entry in entries:
        href = entry_link(entry)
        found = classify(href)
        if found is None:
            continue
        kind, office = found
        if LIMIT_TO_PREPARING_OFFICES and office not in PREPARING_OFFICES:
            continue
        key = (office, kind)
        if key not in newest or href > newest[key]:
            newest[key] = href

    kept: dict[tuple[str, str], dict] = {}
    for item in (prev or {}).get("items", []):
        if isinstance(item, dict) and isinstance(item.get("office"), str) and isinstance(item.get("kind"), str):
            if LIMIT_TO_PREPARING_OFFICES and item["office"] not in PREPARING_OFFICES:
                continue
            kept[(item["office"], item["kind"])] = item
    by_url = {item.get("url"): item for item in kept.values()}

    # ⑤ 前回分にある URL は取りに行かない
    wanted = sorted(((key, href) for key, href in newest.items() if href not in by_url), key=lambda p: p[1], reverse=True)
    for key, href in newest.items():
        if href in by_url:
            kept[key] = by_url[href]
    # ④ 1回に辿る数の上限。超えた分を辿らないまま「取れた」にはしない（⑥）
    over = len(wanted) - MAX_LINKS_PER_RUN
    wanted = wanted[:MAX_LINKS_PER_RUN]

    def fetch_one(pair: tuple[tuple[str, str], str]) -> tuple[tuple[str, str], dict | Exception]:
        (office, kind), href = pair
        try:
            if late():
                raise WarningFetchError(f"締切（{DEADLINE_SECONDS}秒）を過ぎました")
            body = fetcher(href, timeout=LINK_TIMEOUT_SECONDS, max_bytes=MAX_BYTES_PER_LINK, user_agent=ua)
            return (office, kind), extract_item(fetch.parse_xml(body), href, kind, office)
        except fetch.FetchError as e:
            return (office, kind), e

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(fetch_one, wanted))
    failed = [item for _, item in results if isinstance(item, Exception)]
    if failed:
        raise failed[0]  # ⑥ 1本でも読めなければ warn 全体が unreadable
    if over > 0:
        raise WarningFetchError(f"新しい電文が1回の上限（{MAX_LINKS_PER_RUN}本）を {over} 本超えました。前回分を保ちます")
    for key, item in results:
        if not isinstance(item, Exception):
            kept[key] = item

    items = [kept[key] for key in sorted(kept)]
    return {"fetched_at": common.iso_z(now), "report_time": report_time, "items": items, "source": fetch.source_block(src)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="気象警報・注意報の電文を二段で取り込む（fetch.py の後に走らせる）")
    ap.add_argument("--previous-from-live", action="store_true", help="前回分を公開中のサイト（config の site_url）から読む")
    args = ap.parse_args(argv)
    try:
        base = common.load_config(common.ROOT, production=False)["site_url"] if args.previous_from_live else None
        record = run(common.ROOT, fetcher=fetch.http_get, previous_base_url=base, now=common.utc_now())
    except common.ConfigError as e:
        print(f"取り込みを止めました: {e}", file=sys.stderr)
        return 1
    line = f"{SOURCE_ID}: {record['state']}"
    if record["error"]:
        line += f"（{fetch.one_line(record['error'])}）"
    print(line)
    if record["state"] == common.STATE_UNREADABLE:
        print(f"::warning title=取得できませんでした::{fetch.gha_escape(SOURCE_ID + ': ' + str(record['error']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
