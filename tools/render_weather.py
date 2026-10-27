"""区域ページ（府県天気予報・天気概況の写し）を作る（第1号専用。site_hooks.pages() から呼ぶ。計画書 9.5）。

掟:
- 気象庁由来の文字列は、取り込んだ data/<id>.json の値を変えずに載せる（strip・全角空白の置換・NFKC をしない）。
  載せた要素には data-copy="<取得先 id>" と data-path（<id>.json の中の場所）を付け、check_dist の写しの関所が
  公開の前に1文字ずつ照合する。data-copy の要素の中には子要素を入れない。
- 外から来た文字列は common.esc() を通す。weatherCodes から絵や言葉を作らない。区域名を書き換えない。
- 三状態: 取れた（表）／取れなかったが前回分あり（「前回（…）」と理由を書いて前回の表）／前回分も無い
  （「この欄はまだ取得できていません」と理由）。取れなかったことを「0件」「無い」と書かない。
- 形が想定と違えば RenderError で、その欄だけ「形が想定と違ったため表示できませんでした」にする。ほかの例外は捕まえない。
- 欄ごとの「更新が止まっています」は freshness.js が出す（ここでは data-section-stale の属性を書くだけ）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import common
from common import esc

SECTION_MAX_AGE_MINUTES = 90  # 欄ごとの帯の閾値（ページ全体の閾値と同じ。計画書 9.5）
FORECAST_PAGE = "https://www.jma.go.jp/bosai/forecast/"
FORECAST_AREA_PAGE = "https://www.jma.go.jp/bosai/forecast/#area_type=offices&area_code={code}"  # 8.2 の手順5の形
SECTIONS = (("fc", "府県天気予報"), ("ov", "天気概況"))


class RenderError(Exception):
    """取り込んだデータの形が、描き方の想定と違う（その欄だけ描かない）。"""


def copy_attrs(sid: str, path: list) -> str:
    """写しの関所に照合させる属性（data-copy と data-path）。"""
    return f' data-copy="{esc(sid)}" data-path="{esc(json.dumps(path, ensure_ascii=False))}"'


def _at(obj: object, path: list) -> object:
    for key in path:
        if isinstance(key, str) and isinstance(obj, dict) and key in obj:
            obj = obj[key]
        elif isinstance(key, int) and isinstance(obj, list) and 0 <= key < len(obj):
            obj = obj[key]
        else:
            raise RenderError(f"{path} が見つかりません")
    return obj


def _text(payload: dict, path: list) -> str:
    value = _at(payload, path)
    if not isinstance(value, str):
        raise RenderError(f"{path} が文字列ではありません")
    return value


def _when(text: str) -> str:
    try:
        return common.jst_text(common.parse_report_time(text))
    except ValueError:
        raise RenderError(f"時刻 {text!r} を読めません") from None


def copied(tag: str, sid: str, payload: dict, path: list, extra: str = "") -> str:
    """気象庁由来の文字列を1つ、写しの印つきの要素にする。"""
    return f"<{tag}{extra}{copy_attrs(sid, path)}>{esc(_text(payload, path))}</{tag}>"


def times_line(sid: str, payload: dict, report_path: list) -> str:
    report = _text(payload, report_path)
    fetched = payload.get("fetched_at")
    if not isinstance(fetched, str):
        raise RenderError("取得時刻がありません")
    return (
        f'<p>気象庁の発表時刻: <time datetime="{esc(report)}">{esc(_when(report))}</time>'
        f'（原文: {copied("span", sid, payload, report_path)}）　'
        f'当サイトの取得時刻: <time datetime="{esc(fetched)}">{esc(_when(fetched))}</time></p>'
    )


def stale_marker(fetched_at: object) -> str:
    return (
        f'<p data-section-stale data-fetched-at="{esc(fetched_at if isinstance(fetched_at, str) else "")}"'
        f' data-max-age-minutes="{SECTION_MAX_AGE_MINUTES}" hidden></p>'
    )


def fc_body(sid: str, payload: dict) -> str:
    """府県天気予報（items[0].timeSeries の [0] 天気・風・波、[1] 降水確率、[2] 気温）。"""
    base = ["items", 0, "timeSeries"]
    series = _at(payload, base)
    if not isinstance(series, list) or len(series) < 3:
        raise RenderError("timeSeries が3つそろっていません")
    out = []
    # [0] 区域ごとの 時刻／天気／風／波（鍵の無い列は出さない。weatherCodes は出さない）
    times0 = _at(payload, base + [0, "timeDefines"])
    areas0 = _at(payload, base + [0, "areas"])
    if not isinstance(times0, list) or not isinstance(areas0, list):
        raise RenderError("timeSeries[0] の形が違います")
    columns = (("weathers", "天気"), ("winds", "風"), ("waves", "波"))
    for i, area in enumerate(areas0):
        if not isinstance(area, dict):
            raise RenderError("区域の形が違います")
        keys = [(key, label) for key, label in columns if key in area]
        if not keys or keys[0][0] != "weathers":
            raise RenderError("天気の列がありません")
        out.append(copied("h3", sid, payload, base + [0, "areas", i, "area", "name"]))
        head = "".join(f"<th>{label}</th>" for _, label in keys)
        rows = []
        for j, when in enumerate(times0):
            if not isinstance(when, str):
                raise RenderError("時刻が文字列ではありません")
            cells = "".join(copied("td", sid, payload, base + [0, "areas", i, key, j]) for key, _ in keys)
            rows.append(f'<tr><th scope="row"><time datetime="{esc(when)}">{esc(_when(when))}</time></th>{cells}</tr>')
        out.append(f"<table>\n<thead><tr><th>時刻</th>{head}</tr></thead>\n<tbody>\n" + "\n".join(rows) + "\n</tbody>\n</table>")
    # [1] 降水確率（%）・[2] 気温（℃）: 行は区域・地点、列は時刻
    for n, key, title in ((1, "pops", "降水確率（%）"), (2, "temps", "気温（℃）")):
        times = _at(payload, base + [n, "timeDefines"])
        areas = _at(payload, base + [n, "areas"])
        if not isinstance(times, list) or not isinstance(areas, list):
            raise RenderError(f"timeSeries[{n}] の形が違います")
        head = "".join(f'<th scope="col"><time datetime="{esc(t)}">{esc(_when(t))}</time></th>' for t in times if isinstance(t, str))
        rows = []
        for i, area in enumerate(areas):
            values = _at(payload, base + [n, "areas", i, key])
            if not isinstance(values, list) or len(values) != len(times):
                raise RenderError(f"{key} の数が時刻の数と違います")
            cells = "".join(copied("td", sid, payload, base + [n, "areas", i, key, j]) for j in range(len(values)))
            name_cell = copied("th", sid, payload, base + [n, "areas", i, "area", "name"], extra=' scope="row"')
            rows.append(f"<tr>{name_cell}{cells}</tr>")
        out.append(f"<h3>{title}</h3>\n<table>\n<thead><tr><th></th>{head}</tr></thead>\n<tbody>\n" + "\n".join(rows) + "\n</tbody>\n</table>")
    return "\n".join(out)


def ov_body(sid: str, payload: dict) -> str:
    """天気概況（items[0] の publishingOffice・targetArea・headlineText・text）。"""
    base = ["items", 0]
    out = [
        f"<p>発表: {copied('span', sid, payload, base + ['publishingOffice'])}　対象: {copied('span', sid, payload, base + ['targetArea'])}</p>"
    ]
    if _text(payload, base + ["headlineText"]):
        out.append(copied("p", sid, payload, base + ["headlineText"], extra=' class="jma-text"'))
    out.append(copied("p", sid, payload, base + ["text"], extra=' class="jma-text"'))
    return "\n".join(out)


def load_payload(data_dir: Path, sid: str) -> dict | None:
    path = data_dir / f"{sid}.json"
    if not path.is_file():
        return None
    try:
        obj = common.load_json(path)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def section(kind: str, title: str, code: str, state: dict, data_dir: Path) -> str:
    sid = f"{kind}-{code}"
    rec = state.get(sid) or {}
    payload = load_payload(data_dir, sid)
    head = f'<section data-weather-section="{esc(kind)}">\n<h2>{esc(title)}</h2>'
    source = f'<p>出典：気象庁ホームページ （<a href="{FORECAST_PAGE}" rel="noopener noreferrer">{FORECAST_PAGE}</a>）</p>'
    reason = rec.get("error") or "理由は記録されていません"
    if payload is None:
        return (
            f"{head}\n<p>この欄はまだ取得できていません（理由: {esc(reason)}）。気象庁のページで確かめてください。</p>\n"
            f"{stale_marker(None)}\n{source}\n</section>"
        )
    notes = []
    if rec.get("state") == common.STATE_UNREADABLE:
        notes.append(
            f"<p>最新の取得に失敗したか、新しさを確かめられませんでした（理由: {esc(reason)}）。"
            f"下は前回（{esc(_when_or_dash(payload.get('fetched_at')))}）に取得した内容です。気象庁のページで確かめてください。</p>"
        )
    try:
        if kind == "fc":
            body = times_line(sid, payload, ["items", 0, "reportDatetime"]) + "\n" + fc_body(sid, payload)
        else:
            body = times_line(sid, payload, ["items", 0, "reportDatetime"]) + "\n" + ov_body(sid, payload)
    except RenderError:
        body = "<p>この欄は形が想定と違ったため表示できませんでした（取得は成功）。気象庁のページで確かめてください。</p>"
    return "\n".join([head, *notes, body, stale_marker(payload.get("fetched_at")), source, "</section>"])


def _when_or_dash(text: object) -> str:
    if not isinstance(text, str):
        return "—"
    try:
        return common.jst_text(common.parse_iso_z(text))
    except ValueError:
        return "（時刻を読めませんでした）"


def area_page(code: str, name: str, state: dict, data_dir: Path) -> str:
    link = FORECAST_AREA_PAGE.format(code=code)
    parts = [
        f"<h1>{esc(name)}の天気予報（気象庁の発表の写し）</h1>",
        f'<p>気象庁の該当ページ: <a href="{esc(link)}" rel="noopener noreferrer">{esc(name)}の天気予報（気象庁）</a></p>',
    ]
    for kind, title in SECTIONS:
        parts.append(section(kind, title, code, state, data_dir))
    return "\n".join(parts)


def area_index(areas: list[tuple[str, str]]) -> str:
    items = "\n".join(f'<li><a href="/area/{esc(code)}.html">{esc(name)}</a></li>' for code, name in areas)
    return f"<h1>区域の一覧</h1>\n<ul>\n{items}\n</ul>"


def pages(root: Path, sources: list[dict], state: dict, now: datetime) -> dict[str, str]:
    """区域ページ（fc の each.code の区域）と一覧。区域名は site/areas.json の原文。"""
    names = {a["code"]: a["name"] for a in common.load_json(root / "site" / "areas.json")["offices"]}
    codes = [s["id"][len("fc-"):] for s in sources if s["id"].startswith("fc-")]
    data_dir = root / "data"
    out = {f"area/{code}.html": area_page(code, names[code], state, data_dir) for code in codes}
    out["area/index.html"] = area_index([(code, names[code]) for code in codes])
    return out
