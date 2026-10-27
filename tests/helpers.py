"""試験の共通部品。本物のネットワークに触れない（偽の取得関数・偽のサイトだけを使う）。"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import socket  # noqa: E402


class NetworkInTests(BaseException):
    """試験がネットに出ようとした。BaseException にして、道具の「想定外も unreadable に倒す」except で飲み込ませない。"""


def _no_network(*args, **kwargs):
    raise NetworkInTests("試験がネットに出ようとしました（偽の取得関数・偽のサイトを使う）")


# ★試験はネットに出ない。helpers を import した時点で、接続を例外にする（各試験に任せない）
socket.socket.connect = _no_network  # type: ignore[method-assign]
socket.socket.connect_ex = _no_network  # type: ignore[method-assign]
socket.create_connection = _no_network  # type: ignore[assignment]

import fetch  # noqa: E402

NOW = datetime(2026, 10, 23, 6, 0, 0, tzinfo=timezone.utc)
LIVE = "https://test-site.example.com/"
DATA_URL = "https://data.example.com/s1.json"

TEST_CONFIG = {
    "site_name": "試験のサイト",
    "site_url": LIVE,
    "official_links": [{"label": "公式のお知らせ", "url": "https://official.example.com/news"}],
    "contact_url": "https://github.com/test-owner/test-repo/issues",
    # 汎用の道具の試験は、雛形と同じ条件で流す（取得先の前置きなし・データを公開・鮮度の閾値180分）。
    # サイト専用の設定は、天気の欄を出す段（旗 true）にしておく（準備中の関所は tests/test_site_hooks.py が見る）
    "max_status_age_minutes": 180,
    "site": {"weather_sections_enabled": True, "warning_delay_minutes": None},
    "publish_data": True,
    "source_url_prefixes": None,
}
TEST_INDEX = "<h1>試験のサイト</h1>\n<p>試験の本文です。</p>\n"


def load_cfg(root: Path) -> dict:
    """check_dist に渡す設定（コマンドの check_dist.py と同じく、site/config.json を読んで渡す）。"""
    import common

    return common.load_config(root, production=False)


def make_root(tmp: Path, *, sources: list | None = None, config: dict | None = None) -> Path:
    """雛形の site/ を一時フォルダへ写し、未設定の値を試験用の値で埋める。"""
    root = tmp / "root"
    shutil.copytree(ROOT / "site", root / "site")
    cfg = json.loads((root / "site" / "config.json").read_text(encoding="utf-8"))
    cfg.update(TEST_CONFIG)
    cfg.update(config or {})
    (root / "site" / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    (root / "site" / "sources.json").write_text(json.dumps({"sources": sources or []}, ensure_ascii=False), encoding="utf-8")
    (root / "site" / "pages" / "index.html").write_text(TEST_INDEX, encoding="utf-8")
    return root


def source(**over) -> dict:
    src = {
        "id": "s1",
        "name": "試験の取得先",
        "url": DATA_URL,
        "format": "json",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/deed.ja",
        "attribution": "試験の出典表示",
    }
    src.update(over)
    return src


def write_state(root: Path, records: dict) -> None:
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "state.json").write_text(json.dumps({"schema": 1, "sources": records}, ensure_ascii=False), encoding="utf-8")


class FakeWeb:
    """偽の取得関数。routes の URL → bytes か例外。載っていない URL は HTTP 404。"""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[dict] = []

    def __call__(self, url, *, timeout, max_bytes, user_agent):
        self.calls.append({"url": url, "user_agent": user_agent, "max_bytes": max_bytes, "timeout": timeout})
        route = self.routes.get(url)
        if route is None:
            raise fetch.FetchError("HTTP 404")
        if isinstance(route, Exception):
            raise route
        if len(route) > max_bytes:
            raise fetch.FetchError(f"大きすぎます（{max_bytes} バイトを超えました）")
        return route
