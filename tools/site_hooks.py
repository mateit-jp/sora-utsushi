"""サイト専用の呼び口（雛形では何もしない）。サイト固有の設定の検査・追加のページ・関所はここに書く。

汎用の道具（common.load_config・build.py・check_dist.py・check_repo.py・check_live.py）は、ここの関数を**必ず**呼ぶ。
汎用の道具そのものにサイト固有の規則（そのサイトの禁止語・欄の形など）を書かない（次のサイトに流用できなくなるため）。

掟:
- ここに書く規則も関所。足したら tests/ に試験を、tests/mutation_check.py に「壊すと赤になる型」を足す（掟10）。
- このファイルを消したり、関数の名前を変えたりすると、汎用の道具が import の時点で落ちる（黙って飛ばさない）。
- 「読めなかった」を「問題なし」にしない。検査に要る物が読めなければ、2つ目の並び（読めなかったもの）に入れる。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable


def check_config(site: dict, *, production: bool) -> list[str]:
    """site/config.json の "site" の下の設定を検める。問題の文の並びを返す。

    雛形には規則が無いので、"site" に何か書いたのに規則が無ければ、それ自体を問題にする（検めない設定を通さない）。
    """
    if site:
        return [f"site の設定 {sorted(site)} を検める規則が tools/site_hooks.py の check_config にありません"]
    return []


def pages(*, root: Path, cfg: dict, sources: list[dict], state: dict, now: datetime) -> dict[str, str]:
    """build.py が作る追加のページ。{dist の中のパス（例 "area/130000.html"）: ページの本文の断片（<h1> を含む）}。

    断片は base.html に入れて書き出される（帯・CSP・作った時刻は build.py が付ける）。外から来た文字列は common.esc() を通す。
    """
    return {}


def check_dist(*, dist: Path, data_dir: Path, cfg: dict | None, production: bool, now: datetime) -> tuple[list[str], list[str]]:
    """公開前の、サイト専用の検査。(違反, 読めなかったもの) を返す。data_dir は build の作業場の data/。"""
    return [], []


def check_repo(*, root: Path, production: bool) -> tuple[list[str], list[str]]:
    """リポジトリの、サイト専用の検査。(違反, 読めなかったもの) を返す。"""
    return [], []


def check_live(*, base_url: str, get: Callable, now: datetime) -> tuple[list[str], list[str]]:
    """公開中のサイトの、サイト専用の検査。get は check_live.http_get と同じ形（状態・ヘッダ・本文・転送後の URL）。"""
    return [], []
