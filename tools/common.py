"""tools/ の共通部品（標準ライブラリだけ）。

ここに置くのは「どの道具でも同じでなければならないもの」だけ:
- esc(): HTML に埋め込む文字列は必ずこれを通す
- 時刻は UTC の ISO 8601（末尾 Z）で持ち、表示だけ日本時間にする
- 三状態の名前（STATE_OK / STATE_EMPTY / STATE_UNREADABLE）
- site/config.json と site/sources.json の読み込みと検査
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9), "JST")

# 三状態。「読めなかった」を「無い」と描かないために、名前を分けて持つ。
STATE_OK = "ok"                  # 取れた（1件以上）
STATE_EMPTY = "empty"            # 取れた。元のデータが0件だった
STATE_UNREADABLE = "unreadable"  # 取れなかった・形が変だった・急減した（前回分を保つ）
STATES = (STATE_OK, STATE_EMPTY, STATE_UNREADABLE)

# 公開用（--production）で残っていてはいけない印
PLACEHOLDER_MARKS = ("example.invalid", "未設定")

# 取得先の id はファイル名になる（data/<id>.json）。パスの外へ出られない形に限る。
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SOURCE_FORMATS = ("json", "csv", "xml", "custom")  # custom: fetch.py は取りに行かず、サイト専用の取り込み（tools/fetch_<名前>.py）が埋める
SOURCE_ENCODINGS = ("utf-8", "utf-8-sig", "cp932", "shift_jis", "euc-jp")
SOURCE_SHAPES = ("list", "object")  # object は1件の並びとして持つ（概況のように一番外がオブジェクトのデータ）
EACH_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")  # each で {code} に入れる値（id にも入る）
SOURCE_DEFAULTS = {
    "timeout_seconds": 20,
    "max_bytes": 2_000_000,
    "max_drop_ratio": 0.5,
    "encoding": "utf-8-sig",
    "items_path": [],
    "shape": "list",
    "report_time_path": None,  # 発表時刻のある場所。あれば「前回より古い発表」を unreadable に倒す
    "each": None,  # {"code": [...]} で url の {code} を展開し、1つの書き方から複数の取得先を作る
    "columns": None,  # CSV なら必須: 見出しに必ずある列（エラーページの HTML を CSV と読んで「0件」にしないため）
    "required_keys": None,  # JSON の各行に必ずある鍵（形の変化を「取れた」にしないため）
    "min_items": None,  # これより少なければ unreadable（直前比の急減を少しずつ繰り返す減り方への見張り）
    "max_report_age_hours": None,  # 発表時刻がこれより古ければ unreadable（元が黙って止まったときの見張り。report_time_path が要る）
    "heartbeat": None,  # 見張り役の取得先の id。見張り役が unreadable なら、この取得先も「新しさを確かめられない」にする
}
SOURCE_REQUIRED = ("id", "name", "url", "format", "license", "license_url", "attribution")

CONFIG_REQUIRED = {
    "site_name": str,
    "site_url": str,
    "language": str,
    "unofficial_notice": str,
    "official_links": list,
    "contact_url": str,
    "max_status_age_minutes": int,
}
# 書かなくてよい項目と既定値。これ以外の項目は打ち間違いとみなして止める
CONFIG_OPTIONAL = {
    "site": {},  # サイト専用の設定。検めるのは tools/site_hooks.py の check_config
    "publish_data": True,  # false なら dist/data/ を作らない（前回分を公開中のサイトから読めなくなる）
    "source_url_prefixes": None,  # 取りに行ってよい URL の前置き（https）。書けば sources.json の全 url に効く
}


class ConfigError(Exception):
    """設定ファイルが読めない・形が違う（人が直すまで先へ進まない）。"""


def esc(value: object) -> str:
    """HTML の本文・属性値に埋め込む前に必ず通す（引用符もエスケープする）。"""
    return html.escape(str(value), quote=True)


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj: object, *, compact: bool = False) -> None:
    """途中で落ちても半端なファイルを残さないよう、一時ファイルに書いてから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_z(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def jst_text(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%Y年%m月%d日 %H:%M（日本時間）")


def parse_report_time(value: object) -> datetime:
    """発表時刻（ISO 8601。+09:00 などの時差つき、または末尾 Z）を UTC にする。時差の無いものは読めないとする。"""
    if not isinstance(value, str):
        raise ValueError("発表時刻が文字列ではありません")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("発表時刻に時差がありません")
    return dt.astimezone(timezone.utc)


def find_placeholders(obj: object, where: str = "") -> list[str]:
    """設定の中に残った「未設定」の印を、場所つきで全部返す。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            found += find_placeholders(value, f"{where}.{key}" if where else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found += find_placeholders(value, f"{where}[{i}]")
    elif isinstance(obj, str) and any(mark in obj for mark in PLACEHOLDER_MARKS):
        found.append(where)
    return found


def check_url(url: object, what: str, *, allow_mailto: bool = False) -> str:
    """https:// で始まる URL（または許すときだけ mailto:）だけを通す。"""
    if not isinstance(url, str) or not url:
        raise ConfigError(f"{what} がありません")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        raise ConfigError(f"{what} に空白や制御文字が入っています: {url!r}")
    if url.startswith("https://") or (allow_mailto and url.startswith("mailto:")):
        return url
    raise ConfigError(f"{what} は https:// で始めてください: {url!r}")


def load_config(root: Path, *, production: bool) -> dict:
    path = root / "site" / "config.json"
    try:
        cfg = load_json(path)
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path} を読めませんでした: {e}") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} の一番外側がオブジェクトではありません")
    unknown = set(cfg) - set(CONFIG_REQUIRED) - set(CONFIG_OPTIONAL)
    if unknown:
        raise ConfigError(f"site/config.json に知らない項目があります: {sorted(unknown)}（サイト専用の設定は \"site\" の下に書く）")
    cfg = {**CONFIG_OPTIONAL, **cfg}
    for key, typ in CONFIG_REQUIRED.items():
        if not isinstance(cfg.get(key), typ):
            raise ConfigError(f"site/config.json の {key} がありません（または型が違います）")
    if not isinstance(cfg["site"], dict):
        raise ConfigError("site/config.json の site はオブジェクトにしてください")
    if not isinstance(cfg["publish_data"], bool):
        raise ConfigError("site/config.json の publish_data は true か false にしてください")
    prefixes = cfg["source_url_prefixes"]
    if prefixes is not None:
        if not isinstance(prefixes, list) or not prefixes:
            raise ConfigError("site/config.json の source_url_prefixes は空でない並びにしてください")
        for i, prefix in enumerate(prefixes):
            check_url(prefix, f"source_url_prefixes[{i}]")
            if not prefix.endswith("/"):
                raise ConfigError(f"source_url_prefixes[{i}] は / で終わる形にしてください（前置きの途中で切れる名前を通さない）")
    site_url = check_url(cfg["site_url"], "site_url")
    if not site_url.endswith("/") or not site_url.isascii():
        raise ConfigError("site_url は ASCII だけで書き、/ で終わる形にしてください")
    if not cfg["official_links"]:
        raise ConfigError("official_links が空です（公式の情報への導線は必須）")
    for i, link in enumerate(cfg["official_links"]):
        if not isinstance(link, dict) or not isinstance(link.get("label"), str) or not link["label"]:
            raise ConfigError(f"official_links[{i}] の label がありません")
        check_url(link.get("url"), f"official_links[{i}].url")
    check_url(cfg["contact_url"], "contact_url", allow_mailto=True)
    if cfg["max_status_age_minutes"] <= 0:
        raise ConfigError("max_status_age_minutes は1以上にしてください")
    if production:
        leftovers = find_placeholders(cfg)
        if leftovers:
            raise ConfigError("公開用なのに未設定の値が残っています: " + "、".join(leftovers))
    import site_hooks  # サイト専用の設定の検査（雛形では "site" が空でなければ止める）

    problems = site_hooks.check_config(cfg["site"], production=production)
    if problems:
        raise ConfigError("site の設定: " + "／".join(problems))
    return cfg


def is_json_path(keys: object) -> bool:
    """JSON の中の場所の書き方: 文字列（オブジェクトの鍵）か0以上の整数（配列の添字）の並び。"""
    return isinstance(keys, list) and all(
        isinstance(k, str) or (isinstance(k, int) and not isinstance(k, bool) and k >= 0) for k in keys
    )


def expand_each(src: dict, i: int) -> list[dict]:
    """each: {"code": [...]} があれば、url の {code} を値ごとに置き換えた取得先に展開する。"""
    each = src["each"]
    if each is None:
        if "{code}" in src["url"]:
            raise ConfigError(f"sources[{i}] の url に {{code}} があるのに each がありません")
        return [src]
    if not isinstance(each, dict) or set(each) != {"code"}:
        raise ConfigError(f'sources[{i}] の each は {{"code": [...]}} の形にしてください')
    codes = each["code"]
    if not isinstance(codes, list) or not codes or len(set(codes)) != len(codes):
        raise ConfigError(f"sources[{i}] の each.code は重複の無い、空でない並びにしてください")
    if src["url"].count("{code}") != 1:
        raise ConfigError(f"sources[{i}] の url には {{code}} をちょうど1つ書いてください")
    expanded = []
    for code in codes:
        if not isinstance(code, str) or not EACH_VALUE_RE.match(code):
            raise ConfigError(f"sources[{i}] の each.code の値 {code!r} は英小文字・数字・-・_ だけにしてください")
        expanded.append({**src, "id": f"{src['id']}-{code}", "name": f"{src['name']}（{code}）", "url": src["url"].replace("{code}", code), "each": None})
    return expanded


def apply_heartbeats(sources: list[dict], records: dict) -> None:
    """見張り役が unreadable なら、それに頼る取得先も「新しさを確かめられない」にする（データは前回の内容として出す）。

    fetch.py と build.py の両方で呼ぶ（サイト専用の取り込みが後から書いた記録にも効かせるため）。
    """
    for src in sources:
        hb = src.get("heartbeat")
        rec = records.get(src["id"])
        if not hb or rec is None:
            continue
        hb_rec = records.get(hb)
        if (hb_rec is None or hb_rec.get("state") == STATE_UNREADABLE) and rec.get("state") != STATE_UNREADABLE:
            rec["state"] = STATE_UNREADABLE
            rec["error"] = f"見張り役（{hb}）が読めない・古いので、この取得先が新しいかを確かめられません"
            rec["showing_previous"] = True


def load_sources(root: Path, *, production: bool) -> list[dict]:
    """site/sources.json を読み、既定値を補って返す。形が違えば ConfigError。"""
    path = root / "site" / "sources.json"
    try:
        raw = load_json(path)
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path} を読めませんでした: {e}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ConfigError('site/sources.json は {"sources": [...]} の形にしてください')
    seen: set[str] = set()
    sources: list[dict] = []
    for i, item in enumerate(raw["sources"]):
        if not isinstance(item, dict):
            raise ConfigError(f"sources[{i}] がオブジェクトではありません")
        for key in SOURCE_REQUIRED:
            if not isinstance(item.get(key), str) or not item[key]:
                raise ConfigError(f"sources[{i}] の {key} がありません")
        unknown = set(item) - set(SOURCE_REQUIRED) - set(SOURCE_DEFAULTS)
        if unknown:
            raise ConfigError(f"sources[{i}] に知らない項目があります: {sorted(unknown)}")
        src = {**SOURCE_DEFAULTS, **item}
        sid = src["id"]
        if not SOURCE_ID_RE.match(sid):
            raise ConfigError(f"sources[{i}] の id {sid!r} は英小文字・数字・-・_ だけにしてください")
        check_url(src["url"], f"sources[{i}].url")
        check_url(src["license_url"], f"sources[{i}].license_url")
        if src["format"] not in SOURCE_FORMATS:
            raise ConfigError(f"sources[{i}] の format は {SOURCE_FORMATS} のどれかにしてください")
        if src["encoding"] not in SOURCE_ENCODINGS:
            raise ConfigError(f"sources[{i}] の encoding は {SOURCE_ENCODINGS} のどれかにしてください")
        ratio = src["max_drop_ratio"]
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
            raise ConfigError(f"sources[{i}] の max_drop_ratio は 0〜1 にしてください")
        for key in ("timeout_seconds", "max_bytes"):
            value = src[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"sources[{i}] の {key} は1以上の整数にしてください")
        if not is_json_path(src["items_path"]):
            raise ConfigError(f"sources[{i}] の items_path は「文字列か0以上の整数」の並びにしてください")
        if src["report_time_path"] is not None and not (is_json_path(src["report_time_path"]) and src["report_time_path"]):
            raise ConfigError(f"sources[{i}] の report_time_path は空でない「文字列か0以上の整数」の並びにしてください")
        if src["shape"] not in SOURCE_SHAPES:
            raise ConfigError(f"sources[{i}] の shape は {SOURCE_SHAPES} のどれかにしてください")
        if src["format"] == "csv" and (src["items_path"] or src["report_time_path"] is not None or src["shape"] != "list"):
            raise ConfigError(f"sources[{i}] は CSV なので items_path・report_time_path・shape は使えません")
        for key in ("columns", "required_keys"):
            value = src[key]
            if value is not None and (not isinstance(value, list) or not value or not all(isinstance(k, str) and k for k in value)):
                raise ConfigError(f"sources[{i}] の {key} は空でない文字列の並びにしてください")
        if src["format"] == "csv" and src["columns"] is None:
            raise ConfigError(f"sources[{i}] は CSV なので columns（見出しに必ずある列）を書いてください")
        if src["format"] != "csv" and src["columns"] is not None:
            raise ConfigError(f"sources[{i}] の columns は CSV でだけ使えます")
        if src["heartbeat"] is not None and (not isinstance(src["heartbeat"], str) or not SOURCE_ID_RE.match(src["heartbeat"])):
            raise ConfigError(f"sources[{i}] の heartbeat は取得先の id にしてください")
        for key in ("min_items", "max_report_age_hours"):
            value = src[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ConfigError(f"sources[{i}] の {key} は1以上の整数にしてください")
        if src["max_report_age_hours"] is not None and src["report_time_path"] is None:
            raise ConfigError(f"sources[{i}] の max_report_age_hours には report_time_path が要ります")
        for expanded in expand_each(src, i):
            if not SOURCE_ID_RE.match(expanded["id"]):
                raise ConfigError(f"sources[{i}] の展開した id {expanded['id']!r} が長すぎるか、形が違います")
            if expanded["id"] in seen:
                raise ConfigError(f"sources の id {expanded['id']!r} が重複しています")
            seen.add(expanded["id"])
            sources.append(expanded)
    prefixes = load_config(root, production=False)["source_url_prefixes"]
    if prefixes is not None:
        outside = [s["id"] for s in sources if not any(s["url"].startswith(p) for p in prefixes)]
        if outside:
            raise ConfigError(f"取りに行ってよい URL の前置き（source_url_prefixes）の外の取得先があります: {outside}")
    by_id = {s["id"]: s for s in sources}
    for s in sources:
        hb = s["heartbeat"]
        if hb is None:
            continue
        if hb == s["id"] or hb not in by_id:
            raise ConfigError(f"取得先 {s['id']} の heartbeat {hb!r} が、ほかの取得先の id ではありません")
        if by_id[hb]["heartbeat"] is not None:
            raise ConfigError(f"見張り役 {hb} にさらに heartbeat を付けないでください（見張りの連鎖は作らない）")
    if production:
        leftovers = find_placeholders(sources)
        if leftovers:
            raise ConfigError("公開用なのに sources.json に未設定の値が残っています: " + "、".join(leftovers))
    return sources
