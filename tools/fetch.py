#!/usr/bin/env python3
"""外部データを取りに行き、三状態で data/ に残す（型A: Actions の定期実行から呼ぶ）。

使い方:
  python3 -I tools/fetch.py --previous-from-live   # 前回分は公開中のサイト（site/config.json の site_url）から読む
  python3 -I tools/fetch.py                        # 前回分は手元の data/ だけ

掟:
- 三状態: ok（1件以上）/ empty（元が0件）/ unreadable（取れない・形が変・急減・発表時刻の後戻り・時間切れ）。
  ★unreadable のときは前回分（公開中のサイトの data/<id>.json）を保つ。空で上書きしない。
- 急減（前回の件数から max_drop_ratio を超えて減った）は unreadable に倒す（形式の変化を疑う）。
  本当に減ったのなら、人が sources.json の max_drop_ratio を見直して PR で通す。
- report_time_path があれば、発表時刻が前回より古い答え（古い写しを掴んだ等）を unreadable に倒す。
- 同時に取りに行くのは max_workers 本まで。全体の締切（deadline）を過ぎた取得先には行かず unreadable にする
  （元のサイトが重い日＝出番の日に、ジョブが終わらず何も公開できなくなるのを防ぐ）。
- User-Agent に個人情報（メール・氏名）を入れない。サイトの URL と、https の連絡先だけ。
- 取りに行くのは sources.json に書いた URL だけ。データの中の URL を辿らない。
- このジョブに秘密（API トークン）を渡さない（外のデータを処理するジョブだから）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

PREVIOUS_SIZE_FACTOR = 4  # 前回分（整形済みの JSON）は元より大きいことがある
PREVIOUS_TIMEOUT_SECONDS = 10  # 前回分は自分のサイト（CDN）から読むので短く待つ
DEFAULT_MAX_WORKERS = 4
DEFAULT_DEADLINE_SECONDS = 600  # deploy の build ジョブの timeout-minutes（15分）より短く


class FetchError(Exception):
    """取れなかった。理由は人が読める文で持つ（そのまま取得状況のページに出る）。"""


Fetcher = Callable[..., bytes]


def user_agent(site_url: str, contact_url: str) -> str:
    """サイトの URL（＝名前）と、https の連絡先だけを入れる。mailto（メールアドレス）は入れない。"""
    if contact_url.startswith("https://"):
        return f"safe-site-fetcher/1 (+{site_url}; contact: {contact_url})"
    return f"safe-site-fetcher/1 (+{site_url})"


def check_fetch_url(url: str) -> None:
    if not url.startswith("https://"):
        raise FetchError(f"https 以外の URL には取りに行きません: {url}")


def read_limited(stream, max_bytes: int) -> bytes:
    body = stream.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise FetchError(f"大きすぎます（{max_bytes} バイトを超えました）")
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """転送は辿らない（http への格下げも、別のホストへの付け替えも、要求を出す前に断る）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FetchError(f"転送は辿りません（HTTP {code}）。sources.json の url を転送先の https の URL に直してください")


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url: str, *, timeout: float, max_bytes: int, user_agent: str) -> bytes:
    """本物の取得。https だけ・転送は辿らない・大きさの上限つき。"""
    check_fetch_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise FetchError(f"HTTP {resp.status}")
            return read_limited(resp, max_bytes)
    except FetchError:
        raise
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"接続できませんでした（{type(e).__name__}）") from None
    except Exception as e:  # noqa: BLE001  壊れた URL・応答（http.client の例外など）も「取れなかった」に倒す
        raise FetchError(f"取得中に想定外の失敗（{type(e).__name__}）") from None


def _reject_constant(name: str) -> object:
    raise FetchError(f"JSON に {name} があります（JSON の決まりの外。ブラウザで読めない）")


def dig(obj: object, path: list, what: str) -> object:
    """JSON の中を、鍵（文字列）と添字（整数）の並びで辿る。無ければ FetchError。"""
    for key in path:
        if isinstance(key, str) and isinstance(obj, dict) and key in obj:
            obj = obj[key]
        elif isinstance(key, int) and isinstance(obj, list) and key < len(obj):
            obj = obj[key]
        else:
            raise FetchError(f"{what} の {key!r} が見つかりません")
    return obj


XML_DTD_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def xml_local(tag: str) -> str:
    """{名前空間}名前 から名前だけを取る。"""
    return tag.rsplit("}", 1)[-1]


def xml_to_dict(elem: ET.Element) -> dict:
    """XML の要素を dict にする。属性は "@名前"、文字は "#text"、子は名前ごとに必ず並び（1つでも並び）。"""
    out: dict = {}
    for key, value in elem.attrib.items():
        out["@" + xml_local(key)] = value
    text = (elem.text or "").strip()
    if text:
        out["#text"] = text
    for child in elem:
        if not isinstance(child.tag, str):
            continue  # 処理命令など
        out.setdefault(xml_local(child.tag), []).append(xml_to_dict(child))
    return out


def parse_xml(body: bytes) -> dict:
    """XML を安全に読む。DTD・実体の宣言を含む物は読まない（実体の展開で膨らませる攻撃を入口で断つ）。"""
    if XML_DTD_RE.search(body):
        raise FetchError("DOCTYPE・ENTITY を含む XML は読みません")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FetchError(f"XML として読めませんでした（{e}）") from None
    return xml_to_dict(root)


def parse_document(body: bytes, source: dict) -> object:
    if source["format"] == "xml":
        return parse_xml(body)
    try:
        text = body.decode(source["encoding"])
    except (UnicodeDecodeError, LookupError):
        raise FetchError(f"{source['encoding']} として読めませんでした") from None
    if source["format"] == "json":
        try:
            return json.loads(text, parse_constant=_reject_constant)
        except json.JSONDecodeError as e:
            raise FetchError(f"JSON として読めませんでした（{e.lineno}行目）") from None
        except (ValueError, RecursionError) as e:
            raise FetchError(f"JSON として読めませんでした（{type(e).__name__}）") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        rows = list(reader)
    except csv.Error as e:
        raise FetchError(f"CSV として読めませんでした（{e}）") from None
    if not reader.fieldnames:
        raise FetchError("CSV の見出し行がありません")
    missing = [col for col in source["columns"] if col not in reader.fieldnames]
    if missing:
        raise FetchError(f"CSV の見出しに {missing} がありません（エラーページや形式の変化を疑う）")
    if any(None in row for row in rows):
        raise FetchError("見出しより列の多い行があります")
    if any(None in row.values() for row in rows):
        raise FetchError("見出しより列の少ない行があります（途中で切れた疑い）")
    return rows


def select_items(doc: object, source: dict) -> list[dict]:
    target = dig(doc, source["items_path"], "items_path")
    if source["shape"] == "object":
        if not isinstance(target, dict):
            raise FetchError("オブジェクト（1件の表）が見つかりません")
        return [target]
    if not isinstance(target, list):
        raise FetchError("並び（配列）が見つかりません")
    if not all(isinstance(item, dict) for item in target):
        raise FetchError("要素が表の1行（オブジェクト）の形ではありません")
    if source["required_keys"]:
        lacking = [i for i, item in enumerate(target) if any(k not in item for k in source["required_keys"])]
        if lacking:
            raise FetchError(f"{len(lacking)} 件の行に required_keys {source['required_keys']} がそろっていません")
    return target


def ensure_encodable(obj: object) -> None:
    """書き出せない文字（対になっていないサロゲート等）があれば、取れなかったことにする（後で全体が落ちないように）。"""
    try:
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError, RecursionError) as e:
        raise FetchError(f"書き出せない値があります（{type(e).__name__}）") from None


def read_report_time(doc: object, source: dict) -> datetime | None:
    if source["report_time_path"] is None:
        return None
    value = dig(doc, source["report_time_path"], "発表時刻（report_time_path）")
    try:
        return common.parse_report_time(value)
    except ValueError as e:
        raise FetchError(f"発表時刻を読めませんでした（{e}）") from None


def decide(source: dict, prev: dict | None, fetcher: Fetcher, ua: str, now: datetime) -> tuple[str, list | None, str | None, str | None]:
    """取りに行き、三状態のどれかを決める。戻り値は (状態, 新しい行の並び or None, 理由 or None, 発表時刻 or None)。

    ★1つの取得先で何が起きても、例外で全体を止めない（ほかの取得先の更新まで止まるため）。
    """
    try:
        body = fetcher(source["url"], timeout=source["timeout_seconds"], max_bytes=source["max_bytes"], user_agent=ua)
        doc = parse_document(body, source)
        items = select_items(doc, source)
        report_time = read_report_time(doc, source)
        ensure_encodable(items)
    except FetchError as e:
        return common.STATE_UNREADABLE, None, str(e), None
    except Exception as e:  # noqa: BLE001  想定外も「読めなかった」に倒す（「無い」にしない）
        return common.STATE_UNREADABLE, None, f"取り込み中に想定外の失敗（{type(e).__name__}）", None
    if report_time and source["max_report_age_hours"] and now - report_time > timedelta(hours=source["max_report_age_hours"]):
        return (
            common.STATE_UNREADABLE,
            None,
            f"元の発表時刻が {common.iso_z(report_time)} のままです（{source['max_report_age_hours']} 時間を超えて古い。元が止まっている疑い）",
            None,
        )
    stamp = common.iso_z(report_time) if report_time else None
    prev_time = prev.get("report_time") if prev else None
    if report_time and prev_time and report_time < common.parse_iso_z(prev_time):
        return (
            common.STATE_UNREADABLE,
            None,
            f"発表時刻が前回より古い答えでした（前回 {prev_time} → 今回 {stamp}）。古い写しを掴んだ疑いがあるので前回分を保ちます",
            None,
        )
    count = len(items)
    prev_count = len(prev["items"]) if prev else None
    if prev_count:
        floor = prev_count * (1 - source["max_drop_ratio"])
        if count < floor:
            return (
                common.STATE_UNREADABLE,
                None,
                f"件数の急減（前回 {prev_count} 件 → 今回 {count} 件）。形式の変化を疑い、前回分を保ちます",
                None,
            )
    if source["min_items"] and count < source["min_items"]:
        return common.STATE_UNREADABLE, None, f"件数が {count} 件しかありません（下限 {source['min_items']} 件）", None
    if count == 0:
        return common.STATE_EMPTY, [], None, stamp
    return common.STATE_OK, items, None, stamp


def valid_payload(obj: object) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return False
    if not all(isinstance(item, dict) for item in obj["items"]):
        return False
    try:
        common.parse_iso_z(obj.get("fetched_at"))
        if obj.get("report_time") is not None:
            common.parse_iso_z(obj["report_time"])
    except (TypeError, ValueError):
        return False
    return True


def source_block(src: dict) -> dict:
    """公開する data/<id>.json に付ける出典（JSON の写しにも出典と利用条件が付くように）。"""
    return {key: src[key] for key in ("name", "url", "license", "license_url", "attribution")}


def load_previous_one(src: dict, base_url: str | None, data_dir: Path, fetcher: Fetcher, ua: str) -> tuple[dict | None, str | None]:
    """前回分: 公開中のサイトの data/<id>.json。読めなければ手元の data/<id>.json。どちらも無ければ None。"""
    sid = src["id"]
    note = None
    if base_url:
        try:
            body = fetcher(
                f"{base_url}data/{sid}.json",
                timeout=min(src["timeout_seconds"], PREVIOUS_TIMEOUT_SECONDS),
                max_bytes=src["max_bytes"] * PREVIOUS_SIZE_FACTOR,
                user_agent=ua,
            )
            obj = json.loads(body.decode("utf-8"))
            if valid_payload(obj):
                return obj, None
            note = f"{sid}: 公開中の前回分の形が変です"
        except FetchError as e:
            note = f"{sid}: 公開中の前回分を読めませんでした（{e}）"
        except (UnicodeDecodeError, json.JSONDecodeError):
            note = f"{sid}: 公開中の前回分が JSON として読めませんでした"
    local = data_dir / f"{sid}.json"
    if local.exists():
        try:
            obj = common.load_json(local)
        except (OSError, ValueError):
            obj = None
        if valid_payload(obj):
            return obj, note
    return None, note


def run(
    root: Path,
    *,
    fetcher: Fetcher,
    previous_base_url: str | None,
    now: datetime,
    max_workers: int = DEFAULT_MAX_WORKERS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    cfg = common.load_config(root, production=False)
    sources = [s for s in common.load_sources(root, production=False) if s["format"] != "custom"]
    ua = user_agent(cfg["site_url"], cfg["contact_url"])
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    started = clock()

    def late() -> bool:
        return clock() - started > deadline_seconds

    def previous_task(src: dict) -> tuple[dict | None, str | None]:
        # 前回分は締切に関係なく必ず読みに行く（時間切れの取得先でも、前回分を出し続けるため）
        return load_previous_one(src, previous_base_url, data_dir, fetcher, ua)

    def fetch_task(pair: tuple[dict, dict | None]) -> tuple[str, list | None, str | None, str | None]:
        src, prev = pair
        if late():
            return common.STATE_UNREADABLE, None, f"時間切れ（全体の締切 {deadline_seconds:g} 秒を過ぎたので取りに行きませんでした）", None
        return decide(src, prev, fetcher, ua, now)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        previous = list(pool.map(previous_task, sources))
        results = list(pool.map(fetch_task, [(src, prev) for src, (prev, _) in zip(sources, previous)]))

    stamp = common.iso_z(now)
    records = {}
    for src, (prev, _), (state, items, error, report_time) in zip(sources, previous, results):
        sid = src["id"]
        if items is not None:
            payload = {"fetched_at": stamp, "items": items, "source": source_block(src)}
            if report_time:
                payload["report_time"] = report_time
            common.write_json(data_dir / f"{sid}.json", payload, compact=True)
            records[sid] = {
                "state": state,
                "checked_at": stamp,
                "last_success_at": stamp,
                "items": len(items),
                "report_time": report_time,
                "error": None,
                "showing_previous": False,
            }
        elif prev is not None:
            common.write_json(data_dir / f"{sid}.json", {**prev, "source": source_block(src)}, compact=True)  # 前回分を保つ
            records[sid] = {
                "state": state,
                "checked_at": stamp,
                "last_success_at": prev["fetched_at"],
                "items": len(prev["items"]),
                "report_time": prev.get("report_time"),
                "error": error,
                "showing_previous": True,
            }
        else:
            records[sid] = {
                "state": state,
                "checked_at": stamp,
                "last_success_at": None,
                "items": None,
                "report_time": None,
                "error": error,
                "showing_previous": False,
            }
    common.apply_heartbeats(sources, records)
    notes = [note for _, note in previous if note]
    common.write_json(data_dir / "state.json", {"schema": 1, "checked_at": stamp, "previous_notes": notes, "sources": records})
    return records


def merge_records(root: Path, new_records: dict) -> None:
    """サイト専用の取り込み（tools/fetch_<名前>.py）が、自分の取得先の記録を data/state.json に足すための口。

    記録の形は run() と同じ（state・checked_at・last_success_at・items・report_time・error・showing_previous）。
    fetch.py の後に走らせる。state.json が無ければ作る。
    """
    path = root / "data" / "state.json"
    state = common.load_json(path) if path.exists() else {"schema": 1, "sources": {}}
    for sid, rec in new_records.items():
        if rec.get("state") not in common.STATES:
            raise ValueError(f"{sid} の state が三状態のどれでもありません")
        state["sources"][sid] = rec
    common.write_json(path, state)


def one_line(text: str) -> str:
    """ログに出す外由来の文字列から改行と制御文字を除く（行頭の :: で Actions の命令を作らせない）。"""
    return "".join(ch if ch.isprintable() else " " for ch in text)


def gha_escape(text: str) -> str:
    """Actions のワークフローコマンドの値の決まったエスケープ（%・CR・LF）。"""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="外部データを三状態で取り込む")
    ap.add_argument("--previous-from-live", action="store_true", help="前回分を公開中のサイト（config の site_url）から読む")
    ap.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="同時に取りに行く数（既定 4）")
    ap.add_argument("--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS, help="全体の締切（既定 600 秒）")
    args = ap.parse_args(argv)
    if not 1 <= args.max_workers <= 8:
        print("--max-workers は 1〜8 にしてください（元のサイトへの負荷）", file=sys.stderr)
        return 1
    try:
        base = common.load_config(common.ROOT, production=False)["site_url"] if args.previous_from_live else None
        records = run(
            common.ROOT,
            fetcher=http_get,
            previous_base_url=base,
            now=common.utc_now(),
            max_workers=args.max_workers,
            deadline_seconds=args.deadline_seconds,
        )
    except common.ConfigError as e:
        print(f"取り込みを止めました: {e}", file=sys.stderr)
        return 1
    for sid, rec in records.items():
        line = f"{sid}: {rec['state']}"
        if rec["error"]:
            line += f"（{one_line(rec['error'])}）"
        print(line)
        if rec["state"] == common.STATE_UNREADABLE:
            # Actions の画面に黄色の注記として出す（公開は前回分を保って続ける）
            message = gha_escape(f"{sid}: {rec['error']}")
            print(f"::warning title=取得できませんでした::{message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
