#!/usr/bin/env python3
"""site/ と data/ から dist/ を作る（標準ライブラリだけで動く）。

使い方:
  python3 -I tools/build.py --out dist               # 手元・CI の確認用（未設定の値が残っていても作る）
  python3 -I tools/build.py --out dist --production  # 公開用（未設定の値が1つでも残っていれば止まる）

掟:
- 外から来た文字列（data/ の中身・設定の値）は必ず common.esc() を通してから埋め込む。
- site/pages/*.html は人が PR で入れる断片。<script> の本文・<style>・style 属性・on* 属性を書かない
  （CSP で動かないうえ、check_dist.py が落とす）。
- _headers と各ページの <meta> の CSP は site/headers.json の1か所から作る（食い違いを作らない）。
- データの描き方は三状態（ok / empty / unreadable）。unreadable を「0件」と描かない。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import site_hooks  # noqa: E402
from common import esc  # noqa: E402

EXTRA_PAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*\.html$")

# <meta> の CSP では効かない指令（ヘッダでだけ送る）
META_UNSUPPORTED = ("frame-ancestors", "report-uri", "report-to", "sandbox")
# build.py が作るページ（site/pages/ に同じ名前を置かない）
GENERATED_PAGES = ("status.html", "sources.html")

STATE_TEXT = {
    common.STATE_OK: "取得できました",
    common.STATE_EMPTY: "取得できました（元のデータが0件でした）",
    common.STATE_UNREADABLE: "取得できませんでした",
}


class BuildError(Exception):
    pass


def csp_text(csp: dict, *, for_meta: bool = False) -> str:
    parts = []
    for name, values in csp.items():
        if for_meta and name in META_UNSUPPORTED:
            continue
        parts.append(" ".join([name, *values]) if values else name)
    return "; ".join(parts)


def headers_file_text(spec: dict) -> str:
    """Cloudflare の _headers の書式（パスの行＋2文字下げた「名前: 値」）。"""
    lines = ["/*", f"  Content-Security-Policy: {csp_text(spec['csp'])}"]
    for name, value in spec["headers"].items():
        lines.append(f"  {name}: {value}")
    return "\n".join(lines) + "\n"


HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
CSP_NAME_RE = re.compile(r"^[a-z-]+$")
CSP_VALUE_RE = re.compile(r"^[\x21-\x7e]+$")  # 空白・制御文字・ASCII 以外を入れない（_headers の行を割らせない）


def load_headers_spec(root: Path) -> dict:
    """site/headers.json を読み、_headers の行を割ったり別の指令を差し込んだりできる値を拒む。"""
    spec = common.load_json(root / "site" / "headers.json")
    if not isinstance(spec, dict) or not isinstance(spec.get("csp"), dict) or not isinstance(spec.get("headers"), dict):
        raise BuildError('site/headers.json は {"csp": {...}, "headers": {...}} の形にしてください')
    for name, values in spec["csp"].items():
        if not CSP_NAME_RE.match(name) or not isinstance(values, list):
            raise BuildError(f"site/headers.json の CSP の指令 {name!r} の形が違います")
        for value in values:
            if not isinstance(value, str) or not CSP_VALUE_RE.match(value) or ";" in value or "," in value:
                raise BuildError(f"site/headers.json の CSP の {name} の値 {value!r} に使えない文字があります")
    for name, value in spec["headers"].items():
        if not HEADER_NAME_RE.match(name) or name.lower() == "content-security-policy":
            raise BuildError(f"site/headers.json のヘッダ名 {name!r} は使えません")
        if not isinstance(value, str) or not value or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value):
            raise BuildError(f"site/headers.json の {name} の値に制御文字か ASCII 以外の文字があります")
    return spec


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_h1 = False
        self.title: str | None = None
        self.buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "h1" and self.title is None:
            self.in_h1 = True

    def handle_endtag(self, tag):
        if tag == "h1" and self.in_h1:
            self.in_h1 = False
            self.title = "".join(self.buf).strip()

    def handle_data(self, data):
        if self.in_h1:
            self.buf.append(data)


def page_title(fragment: str, name: str) -> str:
    parser = _TitleParser()
    parser.feed(fragment)
    parser.close()
    if not parser.title:
        raise BuildError(f"site/pages/{name} に <h1> がありません（ページの題に使います）")
    return parser.title


def render(base: Template, *, cfg: dict, spec: dict, title: str, content: str, now: datetime) -> str:
    links = "・".join(
        f'<a href="{esc(link["url"])}" rel="noopener noreferrer">{esc(link["label"])}</a>'
        for link in cfg["official_links"]
    )
    full_title = cfg["site_name"] if title == cfg["site_name"] else f"{title} | {cfg['site_name']}"
    return base.substitute(
        lang=esc(cfg["language"]),
        meta_csp=esc(csp_text(spec["csp"], for_meta=True)),
        title=esc(full_title),
        unofficial_notice=esc(cfg["unofficial_notice"]),
        official_links=links,
        content=content,
        generated_at_iso=common.iso_z(now),
        generated_at_text=esc(common.jst_text(now)),
        contact_url=esc(cfg["contact_url"]),
    )


def load_state(root: Path, sources: list[dict]) -> dict:
    """data/state.json を読む。無い・欠けている取得先は unreadable に倒す（「0件」にしない）。"""
    never = {
        "state": common.STATE_UNREADABLE,
        "checked_at": None,
        "last_success_at": None,
        "items": None,
        "error": "まだ一度も取得していません",
        "showing_previous": False,
    }
    path = root / "data" / "state.json"
    records: dict = {}
    if path.exists():
        try:
            loaded = common.load_json(path)
            records = loaded["sources"] if isinstance(loaded, dict) and isinstance(loaded.get("sources"), dict) else {}
        except (OSError, ValueError) as e:
            raise BuildError(f"data/state.json を読めませんでした: {e}") from None
    state = {}
    for src in sources:
        rec = records.get(src["id"])
        if not isinstance(rec, dict) or rec.get("state") not in common.STATES:
            rec = dict(never)
        state[src["id"]] = rec
    common.apply_heartbeats(sources, state)  # サイト専用の取り込みが書いた記録にも効かせる
    return state


def _when(text: object) -> str:
    if not isinstance(text, str):
        return "—"
    try:
        return common.jst_text(common.parse_iso_z(text))
    except ValueError:
        return "（時刻を読めませんでした）"


def status_fragment(sources: list[dict], state: dict) -> str:
    head = "<h1>データの取得状況</h1>"
    if not sources:
        return head + "\n<p>取得先はまだ設定されていません。</p>"
    rows = []
    for src in sources:
        rec = state[src["id"]]
        notes = []
        if rec["state"] == common.STATE_UNREADABLE:
            if rec.get("showing_previous"):
                notes.append(f"前回（{_when(rec.get('last_success_at'))}）に取得した内容を表示しています")
            else:
                notes.append("表示できる内容がありません（これまでに取得できていません）")
            if rec.get("error"):
                notes.append(f"理由: {rec['error']}")
        elif rec.get("items") is not None:
            notes.append(f"{rec['items']} 件")
        if rec.get("report_time"):
            notes.append(f"元の発表時刻 {_when(rec['report_time'])}")
        rows.append(
            "<tr>"
            f'<th scope="row">{esc(src["name"])}</th>'
            f'<td class="state-{esc(rec["state"])}">{esc(STATE_TEXT[rec["state"]])}</td>'
            f"<td>{esc(_when(rec.get('checked_at')))}</td>"
            f"<td>{esc(' / '.join(notes))}</td>"
            "</tr>"
        )
    return (
        head
        + "\n<table>\n<thead><tr><th>取得先</th><th>状態</th><th>確かめた時刻</th><th>補足</th></tr></thead>\n<tbody>\n"
        + "\n".join(rows)
        + "\n</tbody>\n</table>"
    )


def sources_fragment(sources: list[dict]) -> str:
    head = "<h1>出典と利用条件</h1>"
    if not sources:
        return head + "\n<p>外部のデータは使っていません。</p>"
    items = []
    for src in sources:
        items.append(
            "<li>"
            f'<a href="{esc(src["url"])}" rel="noopener noreferrer">{esc(src["name"])}</a>'
            f'（利用条件: <a href="{esc(src["license_url"])}" rel="noopener noreferrer">{esc(src["license"])}</a>）'
            f"<br>{esc(src['attribution'])}"
            "</li>"
        )
    return head + "\n<ul>\n" + "\n".join(items) + "\n</ul>"


def reset_out_dir(out: Path, root: Path) -> None:
    """消してよい場所（<root>/dist か一時フォルダの中）だけを作り直す。

    ★シンボリックリンクは辿らない（dist が別の場所へのリンクだと、リンク先を丸ごと消してしまう）。
    """
    if out.is_symlink() or (root / "dist").is_symlink():
        raise BuildError(f"出力先がシンボリックリンクです。リンク先を消さないよう止めます: {out}")
    out = out.resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if out != (root / "dist").resolve() and tmp not in out.parents:
        raise BuildError(f"出力先は {root / 'dist'} か一時フォルダの中だけにしてください: {out}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)


def build(root: Path, out: Path, *, production: bool, now: datetime) -> None:
    try:
        cfg = common.load_config(root, production=production)
        sources = common.load_sources(root, production=production)
    except common.ConfigError as e:
        raise BuildError(str(e)) from None
    spec = load_headers_spec(root)
    base = Template((root / "site" / "base.html").read_text(encoding="utf-8"))
    pages_dir = root / "site" / "pages"
    pages = sorted(pages_dir.glob("*.html"))
    for page in pages:
        if page.name in GENERATED_PAGES:
            raise BuildError(f"site/pages/{page.name} は build.py が作るページと同じ名前です")

    reset_out_dir(out, root)
    shutil.copytree(root / "site" / "static", out, dirs_exist_ok=True)
    (out / "_headers").write_text(headers_file_text(spec), encoding="utf-8")

    for page in pages:
        fragment = page.read_text(encoding="utf-8").strip()
        html_text = render(base, cfg=cfg, spec=spec, title=page_title(fragment, page.name), content=fragment, now=now)
        (out / page.name).write_text(html_text, encoding="utf-8")

    state = load_state(root, sources)
    generated = {
        "status.html": status_fragment(sources, state),
        "sources.html": sources_fragment(sources),
    }
    for name, fragment in generated.items():
        html_text = render(base, cfg=cfg, spec=spec, title=page_title(fragment, name), content=fragment, now=now)
        (out / name).write_text(html_text, encoding="utf-8")

    extra = site_hooks.pages(root=root, cfg=cfg, sources=sources, state=state, now=now)
    for rel, fragment in sorted(extra.items()):
        if not isinstance(rel, str) or not EXTRA_PAGE_RE.match(rel) or rel in GENERATED_PAGES or (out / rel).exists():
            raise BuildError(f"site_hooks.pages の {rel!r} は書き出せない名前です（英小文字・数字・-・_ と / の .html だけ・既にあるページと重ねない）")
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(base, cfg=cfg, spec=spec, title=page_title(fragment, rel), content=fragment, now=now), encoding="utf-8")

    if cfg["publish_data"]:
        for src in sources:
            data_file = root / "data" / f"{src['id']}.json"
            if data_file.exists():
                (out / "data").mkdir(exist_ok=True)
                shutil.copyfile(data_file, out / "data" / data_file.name)

    common.write_json(
        out / "status.json",
        {
            "schema": 1,
            "generated_at": common.iso_z(now),
            "max_status_age_minutes": cfg["max_status_age_minutes"],
            "sources": state,
        },
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="site/ と data/ から dist/ を作る")
    ap.add_argument("--out", default="dist", help="出力先（既定: dist）")
    ap.add_argument("--production", action="store_true", help="公開用（未設定の値が残っていれば止まる）")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if not out.is_absolute():
        out = common.ROOT / out
    try:
        build(common.ROOT, out, production=args.production, now=common.utc_now())
    except BuildError as e:
        print(f"生成を止めました: {e}", file=sys.stderr)
        return 1
    print(f"生成しました: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
