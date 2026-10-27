"""サイト専用の呼び口（第1号「そらの写し（非公式）」）。サイト固有の設定の検査・追加のページ・関所はここに書く。

汎用の道具（common.load_config・build.py・check_dist.py・check_repo.py・check_live.py）は、ここの関数を**必ず**呼ぶ。
汎用の道具そのものにサイト固有の規則（そのサイトの禁止語・欄の形など）を書かない（次のサイトに流用できなくなるため）。

掟:
- ここに書く規則も関所。足したら tests/ に試験を、tests/mutation_check.py に「壊すと赤になる型」を足す（掟10）。
- このファイルを消したり、関数の名前を変えたりすると、汎用の道具が import の時点で落ちる（黙って飛ばさない）。
- 「読めなかった」を「問題なし」にしない。検査に要る物が読めなければ、2つ目の並び（読めなかったもの）に入れる。

第1号の規則（計画書 5.2。PR1 で足したもの）:
- G-名前（計画書 2.1）: サイト名・URL のホスト名・Worker 名・トップの <h1> と <title>・全ページの名前の <meta> に、
  気象庁や天気サービスの名前・「公式」などの語を入れない。サイト名は「（非公式）」で終わる。
- 設定キー（計画書 4.6）: config の "site" は weather_sections_enabled（真偽値）と warning_delay_minutes（null か 30〜89 の整数）だけ。
- G-取得量（計画書 7.3）: 1日の取得量の最悪値が予算（2,000,000,000 バイト）以下。custom の取得先は、取り込みの道具の
  定数（フィード2本と電文の上限）から最悪値を足す。定数を読めなければ違反（読めなかったを通さない。PR2）。
- G-準備中（計画書 5.2）: 天気の欄の旗が false の間は、天気の欄・区域ページ・データの公開を出さず、トップに「準備中です」、
  robots.txt は Disallow: /。旗が true ならトップに「準備中です」を残さない。
- 写しの関所（計画書 9.5・PR3）: data-copy の要素の文字（または data-copy-attr の属性）が、build の作業場の data/<id>.json の
  data-path の値と1文字も違わない。辿れない・文字列でない・子要素がある は違反。照合に要る JSON が無い・読めなければ
  「検査できませんでした」（「問題なし」にしない）。
- 禁止句の関所（計画書 9.6・PR4）: data-copy の要素の外の文字（全 HTML）と、公開する JS の文字列に、「警報なし」「安全です」
  「リアルタイム」などの言い方を置かない（NFKC と空白の除去をしてから判定）。警報欄の中だけは「発表なし」「0件」も置かない。
  data-copy の要素の中（気象庁の原文）は対象外。
"""
from __future__ import annotations

import ast
import json
import re
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

# ---- G-名前（計画書 2.1） ----
# 判定の前に NFKC で正規化し、空白を除き、「非公式」を全部取り除く（末尾の「（非公式）」を「公式」に当てないため）。
# 店主が語を足すときは第12章 #4 で受け、同じ PR で検算を足す。
FORBIDDEN_NAME_WORDS = (
    "気象庁", "jma", "kishou", "tenki", "天気.jp", "ウェザー", "weathernews", "weathermap", "yahoo", "ヤフー",
    "日本気象協会", "jwa", "nhk", "公式", "official", "本家", "臨時版",
)
UNOFFICIAL_SUFFIX = "（非公式）"
NAME_META = (("name", "application-name"), ("property", "og:site_name"), ("property", "og:title"))
MANIFEST_NAMES = ("manifest.json", "manifest.webmanifest", "site.webmanifest")

# ---- 設定キー（計画書 4.6） ----
SITE_KEYS = ("weather_sections_enabled", "warning_delay_minutes")
DELAY_MIN, DELAY_MAX = 30, 89  # 90 以上はページ全体の閾値（90分）を超え、欄ごとの帯が意味を持たない

# ---- G-取得量（計画書 7.3） ----
RUNS_PER_DAY = 48  # 30分ごと
DAILY_BUDGET_BYTES = 2_000_000_000  # 1日10GB（IP 遮断の線）の20%
# custom の取得先 → その取り込みの道具。1回の最悪値は フィード＋長期フィード＋1回に辿る数×電文1本 の上限（計画書 7.3）
CUSTOM_FETCHERS = {"warn": "tools/fetch_jma_warning.py"}
CUSTOM_WORST_CONSTANTS = ("FEED_MAX_BYTES", "BACKFILL_FEED_MAX_BYTES", "MAX_LINKS_PER_RUN", "MAX_BYTES_PER_LINK")

# ---- G-準備中 ----
PREPARING_TEXT = "準備中です"
WEATHER_SECTION_ATTR = "data-weather-section"


def _normalized_for_names(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    text = text.replace("非公式", "")
    return text.casefold()


FORBIDDEN_NAME_KEYS = tuple(_normalized_for_names(w) for w in FORBIDDEN_NAME_WORDS)


def name_problems(value: object, where: str) -> list[str]:
    """G-名前の判定。名前に使えない語があれば、その語を挙げた問題の文を返す。"""
    if not isinstance(value, str):
        return [f"{where}: 名前が文字列ではありません"]
    normalized = _normalized_for_names(value)
    hits = [word for word, key in zip(FORBIDDEN_NAME_WORDS, FORBIDDEN_NAME_KEYS) if key in normalized]
    if hits:
        return [f"{where}: 名前に使えない語が入っています: {hits}（{value!r}。計画書 2.1）"]
    return []


def check_config(site: dict, *, production: bool) -> list[str]:
    """site/config.json の "site" の下の設定を検める。問題の文の並びを返す。

    知らない項目は、検める規則が無いので止める（検めない設定を通さない）。
    """
    problems = []
    unknown = sorted(set(site) - set(SITE_KEYS))
    if unknown:
        problems.append(f"site の設定 {unknown} を検める規則が tools/site_hooks.py の check_config にありません")
    missing = [key for key in SITE_KEYS if key not in site]
    if missing:
        problems.append(f"site の設定に {missing} がありません（計画書 4.6）")
    flag = site.get("weather_sections_enabled")
    if "weather_sections_enabled" in site and not isinstance(flag, bool):
        problems.append(f"site.weather_sections_enabled は true か false にしてください（今: {flag!r}）")
    delay = site.get("warning_delay_minutes")
    if delay is not None:
        if isinstance(delay, bool) or not isinstance(delay, int):
            problems.append(f"site.warning_delay_minutes は null か整数にしてください（今: {delay!r}）")
        elif not DELAY_MIN <= delay <= DELAY_MAX:
            problems.append(f"site.warning_delay_minutes は {DELAY_MIN}〜{DELAY_MAX} にしてください（今: {delay}。計画書 4.6）")
    return problems


def pages(*, root: Path, cfg: dict, sources: list[dict], state: dict, now: datetime) -> dict[str, str]:
    """build.py が作る追加のページ。{dist の中のパス（例 "area/130000.html"）: ページの本文の断片（<h1> を含む）}。

    断片は base.html に入れて書き出される（帯・CSP・作った時刻は build.py が付ける）。外から来た文字列は common.esc() を通す。
    天気の欄の旗が false の間（準備中）は、区域ページを作らない。旗が true なら区域ページと一覧（tools/render_weather.py）。
    """
    if cfg["site"].get("weather_sections_enabled") is not True:
        return {}
    import render_weather  # 第1号の描き方（旗が true のときだけ読む）

    return render_weather.pages(root, sources, state, now, delay=cfg["site"].get("warning_delay_minutes"))


# ---- 禁止句の関所（計画書 9.6） ----
FORBIDDEN_PHRASE_RE = re.compile(
    r"(特別警報|警報|注意報)(等)?(は|が|の)?(なし|無し|ありません|発表されていません|出ていません|解除されています|平常|安全|問題ありません)"
)
FORBIDDEN_WORDS = ("安全です", "必ず最新", "常に最新", "リアルタイム", "遅れはありません", "遅れません")
WARN_ONLY_WORDS = ("発表なし", "0件")  # 警報欄の中だけ（取得状況のページは empty を「0件」と書くので全体には掛けない）
JS_STRING_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')


def _phrase_key(text: str) -> str:
    """判定のための正規化（NFKC・全角と半角の空白と改行の除去）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def phrase_hits(text: str, *, in_warn: bool = False) -> list[str]:
    key = _phrase_key(text)
    hits = [m.group(0) for m in FORBIDDEN_PHRASE_RE.finditer(key)]
    hits += [word for word in FORBIDDEN_WORDS if word in key]
    if in_warn:
        hits += [word for word in WARN_ONLY_WORDS if word in key]
    return hits


class _PhraseScan(HTMLParser):
    """data-copy の要素の外の文字（全体と、警報欄の中）を集める。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.outside: list[str] = []
        self.warn: list[str] = []
        self._stack: list[tuple[str, bool, bool]] = []  # (タグ, data-copy か, 警報欄か)

    def _inside(self, index: int) -> bool:
        return any(entry[index] for entry in self._stack)

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        a = dict(attrs)
        self._stack.append((tag, "data-copy" in a, a.get("data-weather-section") == "warn"))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if not self._inside(1):
            self.outside.append(data)
            if self._inside(2):
                self.warn.append(data)


def phrase_problems(dist: Path) -> tuple[list[str], list[str]]:
    """禁止句の関所。全 HTML の data-copy の外の文字と、公開する JS の文字列を見る。"""
    problems: list[str] = []
    unreadable: list[str] = []
    for path in sorted(dist.rglob("*.html")):
        rel = path.relative_to(dist).as_posix()
        scan = _PhraseScan()
        try:
            scan.feed(path.read_text(encoding="utf-8"))
            scan.close()
        except (OSError, UnicodeDecodeError) as e:
            unreadable.append(f"{rel}（{type(e).__name__}。禁止句の関所）")
            continue
        found = phrase_hits("".join(scan.outside)) + [h for h in phrase_hits("".join(scan.warn), in_warn=True) if h in WARN_ONLY_WORDS]
        if found:
            problems.append(f"禁止句の関所: {rel}: 使えない言い方があります: {sorted(set(found))}（計画書 9.6）")
    for path in sorted(dist.rglob("*.js")):
        rel = path.relative_to(dist).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            unreadable.append(f"{rel}（{type(e).__name__}。禁止句の関所）")
            continue
        literals = "\n".join(a or b for a, b in JS_STRING_RE.findall(text))
        found = phrase_hits(literals)
        if found:
            problems.append(f"禁止句の関所: {rel}: 文字列に使えない言い方があります: {sorted(set(found))}（計画書 9.6）")
    return problems, unreadable


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _CopyScan(HTMLParser):
    """data-copy の要素ごとに、取得先・場所・照合する属性・文字・子要素の有無を集める（写しの関所）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict] = []
        self._stack: list[tuple[str, dict | None]] = []

    def _open(self, tag: str, attrs: list, *, void: bool) -> None:
        for _, rec in self._stack:
            if rec is not None:
                rec["child"] = True
        a = {k: v for k, v in attrs}
        rec = None
        if "data-copy" in a:
            attr = a.get("data-copy-attr")
            rec = {"sid": a.get("data-copy") or "", "path": a.get("data-path"), "attr": attr,
                   "attr_value": a.get(attr) if attr else None, "text": [], "child": False}
            self.records.append(rec)
        if not void:
            self._stack.append((tag, rec))

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, void=tag in VOID_TAGS)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, void=True)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        for _, rec in self._stack:
            if rec is not None:
                rec["text"].append(data)


def _dig(obj: object, path: object) -> object:
    if not isinstance(path, list):
        raise ValueError("data-path が並びではありません")
    for key in path:
        if isinstance(key, str) and isinstance(obj, dict) and key in obj:
            obj = obj[key]
        elif isinstance(key, int) and not isinstance(key, bool) and isinstance(obj, list) and 0 <= key < len(obj):
            obj = obj[key]
        else:
            raise ValueError(f"{key!r} が見つかりません")
    return obj


def copy_problems(dist: Path, data_dir: Path) -> tuple[list[str], list[str]]:
    """写しの関所。dist の全 HTML の data-copy の要素を、取り込んだ JSON の値と照合する。"""
    import common

    records: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    for path in sorted(dist.rglob("*.html")):
        rel = path.relative_to(dist).as_posix()
        scan = _CopyScan()
        try:
            scan.feed(path.read_text(encoding="utf-8"))
            scan.close()
        except (OSError, UnicodeDecodeError) as e:
            unreadable.append(f"{rel}（{type(e).__name__}。写しの関所）")
            continue
        records += [(rel, rec) for rec in scan.records]
    if not records:
        return [], unreadable  # 天気の欄が無い（準備中）なら、照合する物が無い
    # 照合に使う JSON: build の作業場の data/（フォルダがあれば）。無ければ dist/data/（build が写した物）
    if data_dir.is_dir():
        base = data_dir
    elif (dist / "data").is_dir():
        base = dist / "data"
    else:
        return [], unreadable + [f"写しの関所: {data_dir} も dist/data/ も無いので、data-copy の要素 {len(records)} 個を照合できませんでした"]
    problems: list[str] = []
    payloads: dict[str, object] = {}
    for rel, rec in records:
        sid = rec["sid"]
        where = f"{rel} の data-copy=\"{sid}\" data-path={rec['path']}"
        if not common.SOURCE_ID_RE.match(sid):
            problems.append(f"写しの関所: {where}: 取得先の id の形が違います")
            continue
        if sid not in payloads:
            try:
                payloads[sid] = json.loads((base / f"{sid}.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as e:
                payloads[sid] = None
                unreadable.append(f"写しの関所: {sid}.json を読めませんでした（{type(e).__name__}）")
        if payloads[sid] is None:
            continue
        try:
            expected = _dig(payloads[sid], json.loads(rec["path"] or ""))
        except ValueError as e:
            problems.append(f"写しの関所: {where}: 元の JSON を辿れません（{e}）")
            continue
        if not isinstance(expected, str):
            problems.append(f"写しの関所: {where}: 元の値が文字列ではありません")
            continue
        if rec["attr"]:
            actual = rec["attr_value"]
        else:
            if rec["child"]:
                problems.append(f"写しの関所: {where}: data-copy の要素の中に子要素があります")
                continue
            actual = "".join(rec["text"])
        if actual != expected:
            problems.append(f"写しの関所: {where}: 元の文字列と違います（元 {expected!r} ／ ページ {actual!r}）")
    return problems, unreadable


class _PageScan(HTMLParser):
    """名前の対象（<h1>・<title>・名前の <meta>）と、天気の欄の要素と、<main> の中の文字を集める。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.h1: str | None = None
        self.title: str | None = None
        self.metas: list[tuple[str, str]] = []
        self.weather_sections = 0
        self.main_text: list[str] = []
        self._in: str | None = None
        self._buf: list[str] = []
        self._main_depth = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if WEATHER_SECTION_ATTR in a:
            self.weather_sections += 1
        if tag == "main":
            self._main_depth += 1
        if tag == "meta":
            for attr, value in NAME_META:
                if a.get(attr, "").lower() == value:
                    self.metas.append((value, a.get("content", "")))
        if tag in ("h1", "title") and self._in is None and getattr(self, tag) is None:
            self._in = tag
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "main" and self._main_depth:
            self._main_depth -= 1
        if tag == self._in:
            setattr(self, tag, "".join(self._buf).strip())
            self._in = None

    def handle_data(self, data):
        if self._in is not None:
            self._buf.append(data)
        if self._main_depth:
            self.main_text.append(data)


def _scan(path: Path) -> _PageScan:
    scan = _PageScan()
    scan.feed(path.read_text(encoding="utf-8"))
    scan.close()
    return scan


def _robots_problems(dist: Path) -> tuple[list[str], list[str]]:
    path = dist / "robots.txt"
    if not path.is_file():
        return ["G-準備中: robots.txt がありません（準備中は Disallow: /）"], []
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeDecodeError) as e:
        return [], [f"robots.txt（{type(e).__name__}）"]
    problems = []
    if "Disallow: /" not in lines:
        problems.append("G-準備中: robots.txt に Disallow: / がありません（準備中の名前とページを検索に載せない）")
    if any(line.lower().startswith("allow:") for line in lines):
        problems.append("G-準備中: robots.txt に Allow: の行があります（準備中は Disallow: / だけ）")
    return problems, []


def check_dist(*, dist: Path, data_dir: Path, cfg: dict | None, production: bool, now: datetime) -> tuple[list[str], list[str]]:
    """公開前の、サイト専用の検査。(違反, 読めなかったもの) を返す。data_dir は build の作業場の data/。"""
    if cfg is None:
        return [], ["site/config.json を受け取れなかったので、G-名前と G-準備中を検査できませんでした（check_dist に cfg を渡す）"]
    problems: list[str] = []
    unreadable: list[str] = []
    scans: dict[str, _PageScan] = {}
    failed: set[str] = set()
    for path in sorted(dist.rglob("*.html")):
        rel = path.relative_to(dist).as_posix()
        try:
            scans[rel] = _scan(path)
        except (OSError, UnicodeDecodeError) as e:
            failed.add(rel)
            unreadable.append(f"{rel}（{type(e).__name__}。G-名前・G-準備中）")

    # G-名前: トップの <h1> と <title>、全ページの名前の <meta>、manifest の名前
    top = scans.get("index.html")
    if top is None:
        if "index.html" not in failed:
            problems.append("G-名前: index.html がありません")
    else:
        problems += name_problems(top.h1 or "", "index.html の <h1>")
        problems += name_problems(top.title or "", "index.html の <title>")
    for rel, scan in scans.items():
        for meta, content in scan.metas:
            problems += name_problems(content, f"{rel} の <meta> {meta}")
    for name in MANIFEST_NAMES:
        path = dist / name
        if not path.is_file():
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            unreadable.append(f"{name}（{type(e).__name__}。G-名前）")
            continue
        for key in ("name", "short_name"):
            if isinstance(manifest, dict) and key in manifest:
                problems += name_problems(manifest[key], f"{name} の {key}")

    # G-準備中
    enabled = cfg["site"].get("weather_sections_enabled")
    top_text = "".join(top.main_text) if top is not None else ""
    if enabled is False:
        sections = sorted(rel for rel, scan in scans.items() if scan.weather_sections)
        if sections:
            problems.append(f"G-準備中: 天気の欄の旗が false なのに {WEATHER_SECTION_ATTR} の要素があります: {sections}")
        if (dist / "area").exists():
            problems.append("G-準備中: 天気の欄の旗が false なのに dist/area/ があります")
        if cfg.get("publish_data") is not False:
            problems.append("G-準備中: 天気の欄の旗が false の間は publish_data を false にしてください")
        if top is not None and PREPARING_TEXT not in top_text:
            problems.append(f"G-準備中: 天気の欄の旗が false なのに、トップに「{PREPARING_TEXT}」がありません")
        robots_problems, robots_unreadable = _robots_problems(dist)
        problems += robots_problems
        unreadable += robots_unreadable
    elif enabled is True:
        if top is not None and PREPARING_TEXT in top_text:
            problems.append(f"G-準備中: 天気の欄の旗が true なのに、トップに「{PREPARING_TEXT}」が残っています")
    else:
        unreadable.append("site.weather_sections_enabled を読めませんでした（G-準備中）")

    # 写しの関所
    copy_found, copy_unreadable = copy_problems(dist, data_dir)
    problems += copy_found
    unreadable += copy_unreadable

    # 禁止句の関所
    phrase_found, phrase_unreadable = phrase_problems(dist)
    problems += phrase_found
    unreadable += phrase_unreadable
    return problems, unreadable


def _read_json(root: Path, rel: str, unreadable: list[str]) -> object:
    try:
        return json.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        unreadable.append(f"{rel}（{type(e).__name__}。G-名前）")
        return None


def custom_worst_bytes(root: Path, src: dict) -> int:
    """custom の取得先の1回の最悪値を、取り込みの道具の定数から計算する（道具を import せず、定数の行だけを読む）。

    定数が読めなければ ValueError（G-取得量は「読めなかった」を通さない）。
    """
    rel = CUSTOM_FETCHERS.get(src["id"])
    if rel is None:
        raise ValueError("最悪値を計算する規則がありません")
    try:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as e:
        raise ValueError(f"{rel} を読めませんでした（{type(e).__name__}）") from None
    values: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Constant) and type(value.value) is int:
                values[node.targets[0].id] = value.value
    missing = [name for name in CUSTOM_WORST_CONSTANTS if name not in values]
    if missing:
        raise ValueError(f"{rel} に整数の定数 {missing} がありません")
    return values["FEED_MAX_BYTES"] + values["BACKFILL_FEED_MAX_BYTES"] + values["MAX_LINKS_PER_RUN"] * values["MAX_BYTES_PER_LINK"]


def daily_worst_bytes(sources: list[dict], custom_worst: dict[str, int]) -> int:
    """G-取得量の式（計画書 7.3）: 48 ×［custom でない取得先の max_bytes の和（each は展開済み）＋ custom の最悪値の和］。"""
    per_run = sum(src["max_bytes"] for src in sources if src["format"] != "custom")
    per_run += sum(custom_worst.values())
    return RUNS_PER_DAY * per_run


def check_repo(*, root: Path, production: bool) -> tuple[list[str], list[str]]:
    """リポジトリの、サイト専用の検査。(違反, 読めなかったもの) を返す。"""
    import check_repo as repo_tools  # 汎用の道具（JSONC の注釈を除く）。循環しないよう呼ばれた時に読む
    import common

    problems: list[str] = []
    unreadable: list[str] = []

    # G-名前: config のサイト名と URL のホスト名、wrangler.jsonc の Worker 名
    cfg = _read_json(root, "site/config.json", unreadable)
    if isinstance(cfg, dict):
        site_name = cfg.get("site_name")
        problems += name_problems(site_name, "site/config.json の site_name")
        if isinstance(site_name, str) and not site_name.endswith(UNOFFICIAL_SUFFIX):
            problems.append(f"site/config.json の site_name は「{UNOFFICIAL_SUFFIX}」で終わる形にしてください（計画書 2.1）")
        host = urlsplit(cfg.get("site_url") if isinstance(cfg.get("site_url"), str) else "").hostname
        problems += name_problems(host or "", "site/config.json の site_url のホスト名")
    elif cfg is not None:
        unreadable.append("site/config.json の一番外がオブジェクトではありません（G-名前）")
    try:
        wrangler = json.loads(repo_tools.strip_jsonc((root / "wrangler.jsonc").read_text(encoding="utf-8")))
        problems += name_problems(wrangler.get("name"), "wrangler.jsonc の name")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError) as e:
        unreadable.append(f"wrangler.jsonc（{type(e).__name__}。G-名前）")

    # G-取得量
    try:
        sources = common.load_sources(root, production=False)
    except common.ConfigError as e:
        unreadable.append(f"site/sources.json（G-取得量: {e}）")
    else:
        custom_worst: dict[str, int] = {}
        for src in sources:
            if src["format"] != "custom":
                continue
            try:
                custom_worst[src["id"]] = custom_worst_bytes(root, src)
            except ValueError as e:
                problems.append(f"G-取得量: custom の取得先 {src['id']} の最悪値を計算できません（{e}）。0 と数えずに止めます")
        worst = daily_worst_bytes(sources, custom_worst)
        if worst > DAILY_BUDGET_BYTES:
            problems.append(f"G-取得量: 1日の最悪値 {worst:,} バイトが予算 {DAILY_BUDGET_BYTES:,} バイトを超えます（計画書 7.3）")
    return problems, unreadable


def check_live(*, base_url: str, get: Callable, now: datetime) -> tuple[list[str], list[str]]:
    """公開中のサイトの、サイト専用の検査。get は check_live.http_get と同じ形（状態・ヘッダ・本文・転送後の URL）。"""
    return [], []
