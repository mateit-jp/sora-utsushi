#!/usr/bin/env python3
"""dist/ を公開してよいかを検査する（build.py とは独立した関所）。

build.py の作り方を信用せず、出来上がったファイルだけを見る。最低限の方針（REQUIRED_* / CSP_*）は
site/headers.json ではなくこのファイルに持つ。headers.json を緩めても、ここで止まる。
方針を変えるときは、このファイルと tests/ と tests/mutation_check.py を同じ PR で直す。

★この関所が止めるのは「うっかり」。関所そのものを書き換える PR は止められない（承認0の ruleset では
  PR が tools/ も書き換えられる）。tools/・.github/・site/headers.json・site/static/banner.css・freshness.js を
  変える PR は、店主が差分を見てから merge する。

使い方:
  python3 -I tools/check_dist.py dist               # 形の検査
  python3 -I tools/check_dist.py dist --production  # ＋未設定の値・status.json の鮮度
終了コード: 0=問題なし / 1=違反あり（本文に列挙）/ 2=検査できなかった（読めないファイル等）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import site_hooks  # noqa: E402

# ---- 応答ヘッダの最低方針（dist/_headers と、公開後の check_live.py の両方で使う） ----
REQUIRED_HEADERS = {
    "x-content-type-options": {"nosniff"},
    "x-frame-options": {"DENY"},
    "referrer-policy": {"no-referrer", "same-origin", "strict-origin-when-cross-origin"},
    "cross-origin-opener-policy": {"same-origin"},
}
HSTS_MIN_MAX_AGE = 31_536_000  # 1年
SECURITY_HEADER_NAMES = set(REQUIRED_HEADERS) | {"content-security-policy", "strict-transport-security"}
# _headers に書いてよいヘッダ（これ以外は落とす。Refresh で外へ飛ばす・Set-Cookie・Report-To 等を書かせない）
ALLOWED_HEADER_NAMES = SECURITY_HEADER_NAMES | {"permissions-policy", "cross-origin-resource-policy", "cache-control"}
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
# Cloudflare の _headers の決まり（2026-10 の公式文書）: 規則は100まで・1行2,000字まで（超えた行は黙って捨てられる）
HEADERS_MAX_RULES = 100
HEADERS_MAX_LINE = 2000

# CSP: 必ず入っていて、値の集合が完全に一致しなければならない指令
CSP_EXACT = {
    "default-src": {"'none'"},
    "base-uri": {"'none'"},
    "form-action": {"'none'"},
    "frame-ancestors": {"'none'"},
    "require-trusted-types-for": {"'script'"},
}
# CSP: 書いてよい指令と、値に許す語。これ以外の語（ホスト名・https:・'unsafe-inline' 等）は落とす
CSP_ALLOWED = {
    "default-src": {"'none'"},
    "script-src": {"'self'"},
    "style-src": {"'self'"},
    "img-src": {"'self'"},
    "font-src": {"'self'"},
    "connect-src": {"'self'"},
    "manifest-src": {"'self'"},
    "media-src": {"'self'"},
    "object-src": {"'none'"},
    "frame-src": {"'none'"},
    "child-src": {"'none'"},
    "worker-src": {"'none'"},
    "base-uri": {"'none'"},
    "form-action": {"'none'"},
    "frame-ancestors": {"'none'"},
    "require-trusted-types-for": {"'script'"},
    "trusted-types": {"'none'"},
    "upgrade-insecure-requests": set(),
}
# <meta> の CSP では効かない指令
META_UNSUPPORTED = {"frame-ancestors", "report-uri", "report-to", "sandbox"}

# ---- ページの中身の方針 ----
BANNER_ATTR = "data-unofficial-banner"
STALE_ATTR = "data-stale-banner"  # 帯の中で、更新が止まったときだけ出す欄（hidden を許す唯一の要素）
BANNER_WORD = "非公式"
BANNER_TAGS = {"header", "div", "section", "aside"}
HIDING_ATTRS = {"hidden", "popover", "inert", "aria-hidden"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr", "param"}
# 入力を受けない・他所を埋め込まない・CSP の外へ出る道を作らない・パーサの解釈がずれる物を置かない
FORBIDDEN_TAGS = {
    "form", "textarea", "select", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "style",
    "portal", "fencedframe", "svg", "math", "template", "noscript",
}
FORBIDDEN_INPUT_TYPES = {"password", "file", "email", "tel"}
FORBIDDEN_ATTRS = {"style", "action", "formaction", "ping", "srcdoc"}
RESOURCE_ATTRS = {"src", "poster", "data", "background", "manifest"}
SRCSET_ATTRS = {"srcset", "imagesrcset"}
LINK_NAV_RELS = {"canonical", "alternate"}
# ブラウザと html.parser で読み方がずれる書き方（コメント・CDATA の中に <script> を隠せる）は、文字列のまま禁止する
HTML_FORBIDDEN_SEQUENCES = ("<!--", "<![", "--!>")
# 生の文字列で数えた開始タグの数と、html.parser が見た数が違えば「読み方がずれた」とみなす
COUNTED_TAGS = ("script", "style", "meta", "link", "iframe", "object", "embed", "base", "form", "svg", "math", "template", "noscript")
# 素の JS で使わないもの（Trusted Types でも止まるが、ここで先に落とす）。注記の中の語にも当たる（それでよい）
JS_FORBIDDEN = [
    (re.compile(r"\binnerHTML\b|\bouterHTML\b|\binsertAdjacentHTML\b"), "HTML を文字列で差し込んでいます（textContent と createElement を使う）"),
    (re.compile(r"\bdocument\s*\.\s*write"), "document.write を使っています"),
    (re.compile(r"\beval\s*\(|\bnew\s+Function\b"), "文字列をコードとして実行しています"),
    (re.compile(r"\b(localStorage|sessionStorage|indexedDB)\b|\bdocument\s*\.\s*cookie\b"), "閲覧者の端末に保存しています（Cookie なしの方針）"),
    (re.compile(r"\bsendBeacon\b"), "閲覧の記録を送っています"),
    (re.compile(r"\blocation\b|\bwindow\s*\.\s*open\b"), "ページを別の場所へ移そうとしています"),
    (re.compile(r"unofficial|data-unofficial-banner"), "「非公式」の帯を JS から触っています"),
]
SVG_FORBIDDEN = re.compile(
    r"<\s*([\w.-]+:)?(script|foreignObject)\b|\son[a-z]+\s*=|javascript:|<!|(href|src)\s*=\s*[\"']\s*(https?:)?//",
    re.IGNORECASE,
)
# 帯の見た目と「更新が止まっています」の仕組みは、中身を固定する（変えるなら店主の承認で、ここの値を変える）
PINNED_FILES = {
    "banner.css": "073949cbe5d98fe2ae7048c8e10db0b27b121c5dc933c10b1eb063eeceeda775",
    "freshness.js": "0d8662818bb7ffba202bf406446bc2b80fe223b85bac30740625920ad39aec9d",
}
# ほかの CSS に書いてはいけないもの（帯を隠す・外から読む・帯を狙う）
CSS_FORBIDDEN = [
    (re.compile(r"@import", re.I), "@import は使いません"),
    (re.compile(r"display\s*:\s*none|visibility\s*:\s*(hidden|collapse)|content-visibility|opacity\s*:\s*0*(\.0+)?\s*[;}!]", re.I),
     "見えなくする指定は使いません（帯を隠せるため）"),
    (re.compile(r"unofficial|stale|banner|first-child|first-of-type|nth-|:has\(|\bheader\b", re.I), "帯を狙える選び方は使いません（帯の見た目は banner.css だけ）"),
]
CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")\s]*)", re.I)

# ---- ファイルの方針（Cloudflare の静的アセットの上限は 2026-10 時点の公式文書の値） ----
ALLOWED_SUFFIXES = {".html", ".css", ".js", ".json", ".txt", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".webmanifest", ".woff2"}
ALLOWED_NAMES = {"_headers"}  # _redirects は置かない（全ページを別のサイトへ飛ばせる）
TEXT_SUFFIXES = {".html", ".css", ".js", ".json", ".txt", ".svg", ".webmanifest"}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 20_000
# 文字中心で軽く作る（報告書の Must）。1ページ＝HTML と、それが読み込む同じサイトの CSS・JS・画像の合計
PAGE_MAX_BYTES = 300 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"}
IMAGE_MAX_BYTES = 200 * 1024
STATUS_MAX_AGE = timedelta(minutes=60)  # build 直後の検査なので、生成から1時間以内のはず


class HeadersFileError(Exception):
    pass


def _squash(url: str) -> str:
    """ブラウザと同じく、URL の中の空白・制御文字を無いものとして扱う（java\\tscript: 対策）。"""
    return "".join(ch for ch in url if ord(ch) > 0x20 and ord(ch) != 0x7F)


def is_same_origin(url: str) -> bool:
    v = _squash(url)
    if not v or v.startswith("//") or "\\" in v:
        return False
    parts = urlsplit(v)
    return parts.scheme == "" and parts.netloc == ""


def nav_problem(url: str) -> str | None:
    """<a href> に許すのは、同じサイトの中・https://・mailto: だけ。"""
    if is_same_origin(url):
        return None
    v = _squash(url).lower()
    if v.startswith("https://") or v.startswith("mailto:"):
        return None
    return f"許していないリンク先です: {url!r}"


def parse_csp(text: str) -> tuple[dict[str, list[str]], list[str]]:
    directives: dict[str, list[str]] = {}
    problems: list[str] = []
    for part in text.split(";"):
        tokens = part.split()
        if not tokens:
            continue
        name = tokens[0].lower()
        if name in directives:
            problems.append(f"CSP の {name} が2回あります（後の方はブラウザに無視されます）")
            continue
        directives[name] = tokens[1:]
    return directives, problems


def check_csp(text: str, *, header: bool) -> list[str]:
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in text):
        return ["CSP に制御文字か ASCII 以外の文字があります"]
    directives, problems = parse_csp(text)
    for name, need in CSP_EXACT.items():
        if not header and name in META_UNSUPPORTED:
            continue
        if name not in directives:
            problems.append(f"CSP に {name} がありません")
        elif set(directives[name]) != need:
            problems.append(f"CSP の {name} は {sorted(need)} だけにしてください（今: {directives[name]}）")
    for name, values in directives.items():
        if name not in CSP_ALLOWED:
            problems.append(f"CSP の {name} は許していません")
            continue
        if not header and name in META_UNSUPPORTED:
            problems.append(f"<meta> の CSP に {name} は書けません（ヘッダでだけ効く）")
        bad = [v for v in values if v not in CSP_ALLOWED[name]]
        if bad:
            problems.append(f"CSP の {name} に許していない値があります: {bad}")
    return problems


def hsts_max_age(value: str) -> int | None:
    """Strict-Transport-Security の max-age を、指令を区切って読む（x-max-age= などに当てない）。"""
    for part in value.split(";"):
        key, sep, val = part.strip().partition("=")
        if sep and key.strip().lower() == "max-age" and val.strip().isdigit():
            return int(val.strip())
    return None


def check_header_set(headers: dict[str, str], where: str) -> list[str]:
    """応答ヘッダ（名前は小文字）が最低方針を満たすか。"""
    problems = []
    for name, allowed in REQUIRED_HEADERS.items():
        value = headers.get(name)
        if value is None:
            problems.append(f"{where}: {name} がありません")
        elif value.strip() not in allowed:
            problems.append(f"{where}: {name} の値 {value!r} は許していません（許す値: {sorted(allowed)}）")
    max_age = hsts_max_age(headers.get("strict-transport-security") or "")
    if max_age is None or max_age < HSTS_MIN_MAX_AGE:
        problems.append(f"{where}: Strict-Transport-Security の max-age が {HSTS_MIN_MAX_AGE} 未満か、ありません")
    csp = headers.get("content-security-policy")
    if csp is None:
        problems.append(f"{where}: Content-Security-Policy がありません")
    else:
        problems += [f"{where}: {p}" for p in check_csp(csp, header=True)]
    return problems


def parse_headers_file(text: str) -> dict[str, dict[str, str]]:
    """Cloudflare と同じ読み方で _headers を読む（改行は \\n だけ・前後の空白を除いた行頭が / ならパスの行）。

    Cloudflare と読み方がずれると、関所が見た物と本番に付く物が食い違う（垂直タブや字下げの違いで別の行に化ける）。
    だから、ずれうる文字（制御文字・ASCII 以外）と、黙って捨てられる長い行は、ここで違反にする。
    """
    rules: dict[str, dict[str, str]] = {}
    current = None
    for lineno, raw in enumerate(text.split("\n"), 1):
        if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) > 0x7E for ch in raw):
            raise HeadersFileError(f"{lineno}行目: 制御文字か ASCII 以外の文字があります（Cloudflare と読み方がずれる）")
        if len(raw) > HEADERS_MAX_LINE:
            raise HeadersFileError(f"{lineno}行目: {len(raw)} 字あります（{HEADERS_MAX_LINE} 字を超える行は Cloudflare に捨てられる）")
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("/") or line.startswith("https://"):
            current = line
            if current in rules:
                raise HeadersFileError(f"{lineno}行目: 同じパス {current} が2回あります")
            rules[current] = {}
            if len(rules) > HEADERS_MAX_RULES:
                raise HeadersFileError(f"規則が {HEADERS_MAX_RULES} を超えています")
            continue
        if current is None or not raw[:1].isspace():
            raise HeadersFileError(f"{lineno}行目: パスの行でも、字下げしたヘッダの行でもありません")
        if line.startswith("!"):
            raise HeadersFileError(f"{lineno}行目: ヘッダを外す指定（!）は使いません")
        name, sep, value = line.partition(":")
        key = name.strip().lower()
        if not sep or not HEADER_NAME_RE.match(name.strip()):
            raise HeadersFileError(f"{lineno}行目: 「名前: 値」の形ではありません")
        if key in rules[current]:
            raise HeadersFileError(f"{lineno}行目: {current} に {key} が2回あります")
        rules[current][key] = value.strip()
    return rules


def check_headers_file(text: str) -> tuple[list[str], str | None]:
    """dist/_headers を検査し、(問題, 全ページに付く CSP) を返す。"""
    try:
        rules = parse_headers_file(text)
    except HeadersFileError as e:
        return [f"_headers: {e}"], None
    if "/*" not in rules:
        return ["_headers: 全ページに効く /* の規則がありません"], None
    problems = check_header_set(rules["/*"], "_headers の /*")
    for path, headers in rules.items():
        for key in headers:
            if key not in ALLOWED_HEADER_NAMES:
                problems.append(f"_headers の {path}: {key} は書いてよいヘッダではありません")
            elif key in SECURITY_HEADER_NAMES and path != "/*":
                problems.append(f"_headers の {path}: 安全のためのヘッダ {key} は /* でだけ書いてください（上書きしない）")
    return problems, rules["/*"].get("content-security-policy")


class PageScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.lang: str | None = None
        self.meta_csp: list[str] = []
        self.banners = 0
        self.banner_text: list[str] = []
        self.banner_open: list[tuple[str, bool]] | None = None  # 帯の中で開いているタグと「隠れているか」（帯の外では None）
        self.banner_closed = False
        self.resources: list[str] = []  # このページが読み込む同じサイトのファイル（重さと有無を見る）
        self.seen_tags: dict[str, int] = {}
        self.body_seen = False
        self._in_head = False
        self._first_checked = False
        self._resource_before_csp = False

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag: str, attrs: list, closed: bool) -> None:
        where = f"{self.getpos()[0]}行目 <{tag}>"
        self.seen_tags[tag] = self.seen_tags.get(tag, 0) + 1
        a: dict[str, str] = {}
        for name, value in attrs:
            if name in a:
                self.problems.append(f"{where}: 同じ属性 {name} が2つあります")
            a[name] = "" if value is None else value
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "head":
            self._in_head = True
        if tag in ("html", "body") and HIDING_ATTRS & set(a):
            self.problems.append(f"{where}: ページ全体を隠す属性 {sorted(HIDING_ATTRS & set(a))} は使えません")
        if self.body_seen and not self._first_checked:
            self._first_checked = True
            if BANNER_ATTR not in a:
                self.problems.append(f"{where}: <body> の最初の要素が「非公式」の帯ではありません")
        if tag == "body":
            self.body_seen = True
            self._in_head = False
        if tag in FORBIDDEN_TAGS:
            self.problems.append(f"{where}: <{tag}> は使えません")
        if tag == "input" and a.get("type", "text").lower() in FORBIDDEN_INPUT_TYPES:
            self.problems.append(f"{where}: 個人情報を入力させる欄は作りません")
        for name, value in a.items():
            if name.startswith("on"):
                self.problems.append(f"{where}: イベント属性 {name} は使えません（CSP で止まる）")
            if name in FORBIDDEN_ATTRS:
                self.problems.append(f"{where}: 属性 {name} は使えません")
            if name in RESOURCE_ATTRS:
                if is_same_origin(value):
                    self.resources.append(value)
                else:
                    self.problems.append(f"{where}: 外部から読み込もうとしています: {name}={value!r}")
            if name in SRCSET_ATTRS:
                for candidate in value.split(","):
                    first = candidate.strip().split(" ")[0]
                    if first and not is_same_origin(first):
                        self.problems.append(f"{where}: 外部から読み込もうとしています: {name}={value!r}")
                    elif first:
                        self.resources.append(first)
        if tag in ("script", "link", "style") and not self.meta_csp:
            self._resource_before_csp = True
        if tag == "script" and "src" not in a:
            self.problems.append(f"{where}: 本文つきの <script> は使えません（CSP で止まる）")
        if tag == "link":
            rels = set(a.get("rel", "").lower().split())
            href = a.get("href", "")
            if rels and rels <= LINK_NAV_RELS:
                problem = nav_problem(href)
                if problem:
                    self.problems.append(f"{where}: {problem}")
            elif not is_same_origin(href):
                self.problems.append(f"{where}: 外部から読み込もうとしています: href={href!r}")
            else:
                self.resources.append(href)
        if tag in ("a", "area") and "href" in a:
            problem = nav_problem(a["href"])
            if problem:
                self.problems.append(f"{where}: {problem}")
            elif not is_same_origin(a["href"]) and _squash(a["href"]).lower().startswith("https://"):
                rels = set(a.get("rel", "").lower().split())
                if not {"noopener", "noreferrer"} <= rels:
                    self.problems.append(f"{where}: 外へのリンクに rel=\"noopener noreferrer\" がありません")
        if tag == "meta":
            equiv = a.get("http-equiv", "").lower()
            if equiv == "content-security-policy":
                if not self._in_head:
                    self.problems.append(f"{where}: <meta> の CSP は <head> の中に置いてください（外では効かない）")
                self.meta_csp.append(a.get("content", ""))
            elif equiv:
                self.problems.append(f"{where}: http-equiv={equiv} は使えません")
        # 帯の中の追跡（hidden などで隠れた所の文字は「非公式」に数えない）
        if self.banner_open is not None:
            hiding = HIDING_ATTRS & set(a)
            allowed_hidden = STALE_ATTR in a and hiding <= {"hidden"}
            if hiding and not allowed_hidden:
                self.problems.append(f"{where}: 帯の中で {sorted(hiding)} は使えません（帯を隠せるため）")
            if tag not in VOID_TAGS and not closed:
                parent_hidden = self.banner_open[-1][1] if self.banner_open else False
                self.banner_open.append((tag, parent_hidden or bool(hiding)))
        if BANNER_ATTR in a:
            self.banners += 1
            if self.banner_open is not None:
                self.problems.append(f"{where}: 帯の中に帯があります")
            elif tag not in BANNER_TAGS or closed:
                self.problems.append(f"{where}: 帯は {sorted(BANNER_TAGS)} のどれかにしてください")
            else:
                if HIDING_ATTRS & set(a):
                    self.problems.append(f"{where}: 帯に {sorted(HIDING_ATTRS & set(a))} は使えません")
                self.banner_open = []

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        if self.banner_open is None:
            return
        if self.banner_open:
            if self.banner_open[-1][0] == tag:
                self.banner_open.pop()
        else:
            self.banner_open = None
            self.banner_closed = True

    def handle_data(self, data):
        if self.banner_open is not None and not (self.banner_open and self.banner_open[-1][1]):
            self.banner_text.append(data)


def raw_html_problems(text: str, scan: PageScan) -> list[str]:
    """html.parser とブラウザで読み方がずれる書き方を、文字列のまま見つける。"""
    problems = []
    lowered = text.lower()
    for seq in HTML_FORBIDDEN_SEQUENCES:
        if seq in lowered:
            problems.append(f"{seq} は使えません（コメント等の読み方がブラウザとずれ、中に <script> を隠せる）")
    for tag in COUNTED_TAGS:
        raw = len(re.findall(rf"<{tag}\b", lowered))
        if raw != scan.seen_tags.get(tag, 0):
            problems.append(f"<{tag}> の数が、文字列では {raw} 個・検査器の読みでは {scan.seen_tags.get(tag, 0)} 個（読み方がずれている）")
    if re.search(r"</script[^>]", lowered):
        problems.append("</script の後ろに余計な文字があります（読み方がずれる）")
    return problems


def check_html_text(text: str, where: str, header_csp: str | None) -> list[str]:
    scan = PageScan()
    try:
        scan.feed(text)
        scan.close()
    except Exception as e:  # noqa: BLE001  壊れた HTML は「問題あり」に倒す
        return [f"{where}: HTML として読めませんでした（{type(e).__name__}）"]
    problems = [f"{where}: {p}" for p in scan.problems]
    problems += [f"{where}: {p}" for p in raw_html_problems(text, scan)]
    if not scan.body_seen:
        problems.append(f"{where}: <body> がありません（帯が先頭にあるかを確かめられない）")
    if scan.banners != 1:
        problems.append(f"{where}: 「非公式」の帯（{BANNER_ATTR}）が {scan.banners} 個あります（1個だけにする）")
    elif not scan.banner_closed:
        problems.append(f"{where}: 帯が閉じていません")
    if BANNER_WORD not in "".join(scan.banner_text):
        problems.append(f"{where}: 帯に「{BANNER_WORD}」の文字がありません（隠れた所の文字は数えない）")
    if not (scan.lang or "").lower().startswith("ja"):
        problems.append(f'{where}: <html lang="ja"> がありません')
    if scan._resource_before_csp:
        problems.append(f"{where}: <meta> の CSP より前に読み込みがあります（CSP は後ろにしか効かない）")
    if len(scan.meta_csp) != 1:
        problems.append(f"{where}: <meta> の CSP が {len(scan.meta_csp)} 個あります（1個にする）")
    else:
        problems += [f"{where}: {p}" for p in check_csp(scan.meta_csp[0], header=False)]
        if header_csp is not None:
            expected, _ = parse_csp(header_csp)
            expected = {k: v for k, v in expected.items() if k not in META_UNSUPPORTED}
            got, _ = parse_csp(scan.meta_csp[0])
            if {k: sorted(v) for k, v in got.items()} != {k: sorted(v) for k, v in expected.items()}:
                problems.append(f"{where}: <meta> の CSP がヘッダの CSP と食い違っています")
    return problems


def resolve_ref(dist: Path, page: Path, url: str) -> Path | None:
    """ページが参照する同じサイトの URL を dist の中のファイルに直す。dist の外を指すなら None。"""
    v = unquote(_squash(url).split("#", 1)[0].split("?", 1)[0])
    if not v:
        return None
    target = dist / v.lstrip("/") if v.startswith("/") else page.parent / v
    try:
        resolved = target.resolve()
        resolved.relative_to(dist.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def page_weight_problems(dist: Path, page: Path, text: str) -> list[str]:
    """1ページの重さ（HTML＋読み込む CSS・JS・画像）と、読み込む先のファイルの有無。"""
    rel = page.relative_to(dist).as_posix()
    scan = PageScan()
    try:
        scan.feed(text)
        scan.close()
    except Exception:  # noqa: BLE001  壊れた HTML は check_html_text が別に落とす
        return []
    problems = []
    files = {}
    for url in scan.resources:
        target = resolve_ref(dist, page, url)
        if target is None or not target.is_file():
            problems.append(f"{rel}: 読み込む先のファイルがありません: {url!r}")
        else:
            files[target] = target.stat().st_size
    total = len(text.encode("utf-8")) + sum(files.values())
    if total > PAGE_MAX_BYTES:
        problems.append(f"{rel}: 1ページの重さが {total} バイトあります（上限 {PAGE_MAX_BYTES}。文字中心で軽く作る）")
    banner_css = dist / "banner.css"
    if banner_css.resolve() not in files:
        problems.append(f"{rel}: /banner.css を読み込んでいません（帯の見た目は banner.css で決める）")
    return problems


def css_problems(rel: str, text: str) -> list[str]:
    problems = []
    if Path(rel).name in PINNED_FILES:
        return problems  # 中身を固定しているので、ここでは見ない
    for pattern, why in CSS_FORBIDDEN:
        if pattern.search(text):
            problems.append(f"{rel}: {why}")
    for url in CSS_URL_RE.findall(text):
        if url and not is_same_origin(url):
            problems.append(f"{rel}: CSS から外部を読もうとしています: url({url})")
    return problems


def check_status(path: Path, now: datetime) -> list[str]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        generated = common.parse_iso_z(obj["generated_at"])
        sources = obj["sources"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"status.json を読めませんでした（{type(e).__name__}）"]
    problems = []
    if not now - STATUS_MAX_AGE <= generated <= now + timedelta(minutes=5):
        problems.append(f"status.json の generated_at が今と合いません: {obj['generated_at']}")
    limit = obj.get("max_status_age_minutes")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        problems.append("status.json に max_status_age_minutes（更新が止まったと言うまでの分）がありません")
    if not isinstance(sources, dict):
        return problems + ["status.json の sources がオブジェクトではありません"]
    for sid, rec in sources.items():
        if not isinstance(rec, dict) or rec.get("state") not in common.STATES:
            problems.append(f"status.json の {sid} の state が三状態のどれでもありません")
    return problems


def check_dist(
    dist: Path,
    *,
    production: bool,
    now: datetime,
    data_dir: Path | None = None,
    cfg: dict | None = None,
) -> tuple[list[str], list[str]]:
    """戻り値は (違反, 読めなかったもの)。cfg は site/config.json（コマンドから使うときは必ず渡す）。"""
    problems: list[str] = []
    unreadable: list[str] = []
    if not dist.is_dir() or dist.is_symlink():
        return [], [f"{dist} がありません（またはシンボリックリンクです）"]
    if cfg is not None and not cfg["publish_data"] and (dist / "data").exists():
        problems.append("publish_data が false なのに dist/data/ があります（取り込んだデータを公開しない設定）")
    hook_problems, hook_unreadable = site_hooks.check_dist(
        dist=dist, data_dir=data_dir if data_dir is not None else common.ROOT / "data", cfg=cfg, production=production, now=now
    )
    problems += hook_problems
    unreadable += hook_unreadable
    files = sorted(p for p in dist.rglob("*") if p.is_file() or p.is_symlink())
    if len(files) > MAX_FILES:
        problems.append(f"ファイルが {len(files)} 個あります（上限 {MAX_FILES}）")

    header_csp = None
    headers_path = dist / "_headers"
    if not headers_path.is_file():
        problems.append("_headers がありません（安全のためのヘッダが付かない）")
    else:
        try:
            header_problems, header_csp = check_headers_file(headers_path.read_bytes().decode("utf-8"))
            problems += header_problems
        except (OSError, UnicodeDecodeError) as e:
            unreadable.append(f"_headers（{type(e).__name__}）")

    for name, digest in PINNED_FILES.items():
        path = dist / name
        if not path.is_file():
            problems.append(f"{name} がありません（帯と鮮度の表示の仕組み）")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name} の中身が固定した物と違います（変えるなら店主の承認で check_dist.py の PINNED_FILES を直す）")

    html_count = 0
    for path in files:
        rel = path.relative_to(dist).as_posix()
        if path.is_symlink():
            problems.append(f"{rel}: シンボリックリンクは公開しません")
            continue
        if any(part.startswith(".") for part in path.relative_to(dist).parts):
            problems.append(f"{rel}: 隠しファイルは公開しません")
            continue
        if path.name not in ALLOWED_NAMES and path.suffix.lower() not in ALLOWED_SUFFIXES:
            problems.append(f"{rel}: 公開してよい種類のファイルではありません")
            continue
        try:
            size = path.stat().st_size
        except OSError as e:
            unreadable.append(f"{rel}（{type(e).__name__}）")
            continue
        if size > MAX_FILE_BYTES:
            problems.append(f"{rel}: 大きすぎます（{size} バイト）")
        if path.suffix.lower() in IMAGE_SUFFIXES and size > IMAGE_MAX_BYTES:
            problems.append(f"{rel}: 大きな画像は置きません（{size} バイト。上限 {IMAGE_MAX_BYTES}）")
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in ALLOWED_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            unreadable.append(f"{rel}（{type(e).__name__}）")
            continue
        if production:
            marks = [m for m in common.PLACEHOLDER_MARKS if m in text]
            if marks:
                problems.append(f"{rel}: 未設定の印が残っています: {marks}")
        suffix = path.suffix.lower()
        if suffix == ".html":
            html_count += 1
            problems += check_html_text(text, rel, header_csp)
            problems += page_weight_problems(dist, path, text)
        elif suffix == ".js" and path.name not in PINNED_FILES:
            for pattern, why in JS_FORBIDDEN:
                if pattern.search(text):
                    problems.append(f"{rel}: {why}")
        elif suffix == ".css":
            problems += css_problems(rel, text)
        elif suffix == ".svg" and SVG_FORBIDDEN.search(text):
            problems.append(f"{rel}: SVG の中にスクリプト・イベント属性・外部への参照・<! があります")
    if html_count == 0:
        problems.append("HTML のページが1つもありません")

    status_path = dist / "status.json"
    if not status_path.is_file():
        problems.append("status.json がありません（外から鮮度を確かめられない）")
    elif production:
        problems += check_status(status_path, now)
    return problems, unreadable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="dist/ を公開してよいかを検査する")
    ap.add_argument("dist", help="検査するフォルダ（例: dist）")
    ap.add_argument("--production", action="store_true", help="未設定の値と status.json の鮮度も見る")
    ap.add_argument("--data-dir", default=None, help="build の作業場の data/（既定: リポジトリの data/）。サイト専用の検査が読む")
    args = ap.parse_args(argv)
    try:
        cfg = common.load_config(common.ROOT, production=args.production)
    except common.ConfigError as e:
        print(f"違反: 設定を読めませんでした: {e}")
        return 1
    data_dir = Path(args.data_dir) if args.data_dir else common.ROOT / "data"
    problems, unreadable = check_dist(Path(args.dist), production=args.production, now=common.utc_now(), data_dir=data_dir, cfg=cfg)
    for p in problems:
        print(f"違反: {p}")
    for u in unreadable:
        print(f"検査できませんでした: {u}")
    if problems:
        print(f"公開できません（違反 {len(problems)} 件・検査できなかったもの {len(unreadable)} 件）", file=sys.stderr)
        return 1
    if unreadable:
        print(f"公開できません（検査できなかったもの {len(unreadable)} 件。「問題なし」とは言えません）", file=sys.stderr)
        return 2
    print("問題ありませんでした")
    return 0


if __name__ == "__main__":
    sys.exit(main())
