from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import helpers

import common  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import fetch  # noqa: E402

NOW = helpers.NOW
LIVE = helpers.LIVE
URL = helpers.DATA_URL
PREV_URL = LIVE + "data/s1.json"


def items(n: int) -> list[dict]:
    return [{"no": i} for i in range(n)]


def payload(n: int, fetched_at: str = "2026-10-23T05:00:00Z") -> bytes:
    return json.dumps({"fetched_at": fetched_at, "items": items(n)}).encode("utf-8")


class FetchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_fetch(self, *, routes: dict, sources: list | None = None, previous: bool = True) -> tuple[dict, Path, helpers.FakeWeb]:
        root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), sources=sources or [helpers.source()])
        web = helpers.FakeWeb(routes)
        records = fetch.run(root, fetcher=web, previous_base_url=LIVE if previous else None, now=NOW)
        return records, root, web

    def data_file(self, root: Path) -> dict:
        return json.loads((root / "data" / "s1.json").read_text(encoding="utf-8"))

    def test_ok_writes_data_and_state(self):
        records, root, _ = self.run_fetch(routes={URL: json.dumps(items(2)).encode()})
        rec = records["s1"]
        self.assertEqual((rec["state"], rec["items"], rec["showing_previous"]), ("ok", 2, False))
        data = self.data_file(root)
        self.assertEqual((data["fetched_at"], data["items"]), ("2026-10-23T06:00:00Z", items(2)))
        self.assertEqual(data["source"]["attribution"], "試験の出典表示")  # 公開する JSON の写しにも出典
        state = json.loads((root / "data" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["sources"]["s1"]["state"], "ok")

    def test_empty_when_the_source_really_has_zero(self):
        records, _, _ = self.run_fetch(routes={URL: b"[]"})
        self.assertEqual((records["s1"]["state"], records["s1"]["items"]), ("empty", 0))

    def test_unreadable_keeps_previous_from_live_site(self):
        records, root, _ = self.run_fetch(routes={URL: fetch.FetchError("HTTP 503"), PREV_URL: payload(1)})
        rec = records["s1"]
        self.assertEqual(rec["state"], "unreadable")
        self.assertTrue(rec["showing_previous"])
        self.assertEqual(rec["last_success_at"], "2026-10-23T05:00:00Z")
        self.assertEqual(rec["items"], 1)
        self.assertEqual(rec["error"], "HTTP 503")
        self.assertEqual(self.data_file(root)["items"], items(1))

    def test_unreadable_without_previous_is_not_zero(self):
        records, root, _ = self.run_fetch(routes={URL: fetch.FetchError("HTTP 503")})
        rec = records["s1"]
        self.assertEqual((rec["state"], rec["items"], rec["showing_previous"]), ("unreadable", None, False))
        self.assertFalse((root / "data" / "s1.json").exists())

    def test_local_previous_is_used_when_live_site_is_unreachable(self):
        root = helpers.make_root(self.tmp, sources=[helpers.source()])
        (root / "data").mkdir()
        (root / "data" / "s1.json").write_bytes(payload(2, "2026-10-23T04:00:00Z"))
        web = helpers.FakeWeb({URL: fetch.FetchError("HTTP 503"), PREV_URL: fetch.FetchError("接続できませんでした")})
        rec = fetch.run(root, fetcher=web, previous_base_url=LIVE, now=NOW)["s1"]
        self.assertEqual((rec["state"], rec["items"], rec["last_success_at"]), ("unreadable", 2, "2026-10-23T04:00:00Z"))

    def test_sudden_drop_is_unreadable_and_keeps_previous(self):
        records, root, _ = self.run_fetch(routes={URL: json.dumps(items(3)).encode(), PREV_URL: payload(10)})
        rec = records["s1"]
        self.assertEqual(rec["state"], "unreadable")
        self.assertIn("急減", rec["error"])
        self.assertEqual(self.data_file(root)["items"], items(10))

    def test_drop_down_to_the_floor_is_still_ok(self):
        records, _, _ = self.run_fetch(routes={URL: json.dumps(items(5)).encode(), PREV_URL: payload(10)})
        self.assertEqual(records["s1"]["state"], "ok")

    def test_drop_to_zero_is_unreadable_unless_ratio_is_one(self):
        records, _, _ = self.run_fetch(routes={URL: b"[]", PREV_URL: payload(4)})
        self.assertEqual(records["s1"]["state"], "unreadable")
        records, _, _ = self.run_fetch(routes={URL: b"[]", PREV_URL: payload(4)}, sources=[helpers.source(max_drop_ratio=1.0)])
        self.assertEqual(records["s1"]["state"], "empty")

    def test_csv_in_cp932(self):
        csv_url = "https://data.example.com/s1.csv"
        body = "名前,数\nあ,1\nい,2\n".encode("cp932")
        records, root, _ = self.run_fetch(routes={csv_url: body}, sources=[helpers.source(url=csv_url, format="csv", encoding="cp932", columns=["名前", "数"])])
        self.assertEqual(records["s1"]["state"], "ok")
        self.assertEqual(self.data_file(root)["items"][0], {"名前": "あ", "数": "1"})

    def test_csv_with_extra_columns_is_unreadable(self):
        csv_url = "https://data.example.com/s1.csv"
        records, _, _ = self.run_fetch(routes={csv_url: b"a,b\n1,2,3\n"}, sources=[helpers.source(url=csv_url, format="csv", columns=["a", "b"])])
        self.assertEqual(records["s1"]["state"], "unreadable")

    def test_items_path(self):
        src = helpers.source(items_path=["data", "list"])
        records, _, _ = self.run_fetch(routes={URL: b'{"data": {"list": [{"x": 1}]}}'}, sources=[src])
        self.assertEqual(records["s1"]["state"], "ok")
        records, _, _ = self.run_fetch(routes={URL: b'{"data": {}}'}, sources=[src], previous=False)
        self.assertEqual(records["s1"]["state"], "unreadable")

    def test_broken_json_is_unreadable(self):
        records, _, _ = self.run_fetch(routes={URL: b"{not json"})
        self.assertEqual(records["s1"]["state"], "unreadable")
        self.assertIn("JSON", records["s1"]["error"])

    def test_wrong_encoding_is_unreadable(self):
        records, _, _ = self.run_fetch(routes={URL: "[{\"a\": \"あ\"}]".encode("cp932")})
        self.assertEqual(records["s1"]["state"], "unreadable")

    def test_too_large_is_unreadable(self):
        records, _, _ = self.run_fetch(routes={URL: b"[" + b" " * 20 + b"]"}, sources=[helpers.source(max_bytes=10)])
        self.assertEqual(records["s1"]["state"], "unreadable")
        with self.assertRaises(fetch.FetchError):
            fetch.read_limited(io.BytesIO(b"x" * 11), 10)
        self.assertEqual(fetch.read_limited(io.BytesIO(b"x" * 10), 10), b"x" * 10)

    def test_only_https_is_fetched(self):
        with self.assertRaises(fetch.FetchError):
            fetch.check_fetch_url("http://data.example.com/s1.json")
        root = helpers.make_root(self.tmp, sources=[helpers.source(url="http://data.example.com/s1.json")])
        with self.assertRaises(common.ConfigError):
            common.load_sources(root, production=False)

    def test_source_id_cannot_leave_the_data_dir(self):
        for bad in ("../evil", "a/b", "A", ".hidden", ""):
            root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), sources=[helpers.source(id=bad)])
            with self.assertRaises(common.ConfigError):
                common.load_sources(root, production=False)

    def test_previous_with_a_strange_shape_is_ignored(self):
        records, _, _ = self.run_fetch(routes={URL: fetch.FetchError("HTTP 503"), PREV_URL: b'{"items": "x"}'})
        self.assertEqual((records["s1"]["showing_previous"], records["s1"]["items"]), (False, None))

    def test_user_agent_has_no_personal_information(self):
        _, _, web = self.run_fetch(routes={URL: b"[]"})
        self.assertTrue(web.calls)
        for call in web.calls:
            ua = call["user_agent"]
            self.assertTrue(ua.isascii())
            self.assertNotIn("@", ua)
            self.assertIn(LIVE, ua)

    def test_user_agent_never_carries_a_mailto_contact(self):
        ua = fetch.user_agent(LIVE, "mailto:owner@example.com")
        self.assertNotIn("@", ua)
        self.assertNotIn("mailto", ua)
        self.assertIn("contact: https://c.example.com/", fetch.user_agent(LIVE, "https://c.example.com/"))

    # ---- 発表時刻（report_time_path）----
    def overview(self, when: str) -> bytes:
        return json.dumps({"publishingOffice": "気象台", "reportDatetime": when, "text": "晴れ"}).encode()

    def overview_source(self, **over) -> dict:
        return helpers.source(shape="object", report_time_path=["reportDatetime"], **over)

    def test_newer_report_is_ok_and_keeps_the_report_time(self):
        prev = json.dumps({"fetched_at": "2026-10-23T02:00:00Z", "report_time": "2026-10-23T02:00:00Z", "items": [{"text": "前"}]}).encode()
        records, root, _ = self.run_fetch(routes={URL: self.overview("2026-10-23T11:00:00+09:00"), PREV_URL: prev}, sources=[self.overview_source()])
        rec = records["s1"]
        self.assertEqual((rec["state"], rec["report_time"]), ("ok", "2026-10-23T02:00:00Z"))
        self.assertEqual(self.data_file(root)["report_time"], "2026-10-23T02:00:00Z")
        records, _, _ = self.run_fetch(routes={URL: self.overview("2026-10-23T17:00:00+09:00"), PREV_URL: prev}, sources=[self.overview_source()])
        self.assertEqual((records["s1"]["state"], records["s1"]["report_time"]), ("ok", "2026-10-23T08:00:00Z"))

    def test_report_time_going_back_is_unreadable_and_keeps_previous(self):
        prev = json.dumps({"fetched_at": "2026-10-23T02:00:00Z", "report_time": "2026-10-23T02:00:00Z", "items": [{"text": "前"}]}).encode()
        records, root, _ = self.run_fetch(routes={URL: self.overview("2026-10-23T05:00:00+09:00"), PREV_URL: prev}, sources=[self.overview_source()])
        rec = records["s1"]
        self.assertEqual((rec["state"], rec["showing_previous"], rec["report_time"]), ("unreadable", True, "2026-10-23T02:00:00Z"))
        self.assertIn("前回より古い", rec["error"])
        self.assertEqual(self.data_file(root)["items"], [{"text": "前"}])

    def test_report_time_missing_or_without_timezone_is_unreadable(self):
        for body in (json.dumps({"text": "晴れ"}).encode(), self.overview("2026-10-23T11:00:00")):
            records, _, _ = self.run_fetch(routes={URL: body}, sources=[self.overview_source()], previous=False)
            self.assertEqual(records["s1"]["state"], "unreadable")
            self.assertIn("発表時刻", records["s1"]["error"])

    def test_index_path_and_object_shape(self):
        body = json.dumps([{"reportDatetime": "2026-10-23T11:00:00+09:00", "timeSeries": [{"a": 1}, {"a": 2}]}, {"x": 1}]).encode()
        src = helpers.source(items_path=[0, "timeSeries"], report_time_path=[0, "reportDatetime"])
        records, _, _ = self.run_fetch(routes={URL: body}, sources=[src], previous=False)
        self.assertEqual((records["s1"]["state"], records["s1"]["items"]), ("ok", 2))
        records, _, _ = self.run_fetch(routes={URL: body}, sources=[helpers.source(items_path=[5])], previous=False)
        self.assertEqual(records["s1"]["state"], "unreadable")

    # ---- each（{code} の展開）----
    def test_each_expands_one_entry_into_many_sources(self):
        src = helpers.source(id="fc", url="https://data.example.com/fc/{code}.json", each={"code": ["130000", "270000"]})
        routes = {"https://data.example.com/fc/130000.json": b"[{}]", "https://data.example.com/fc/270000.json": b"[]"}
        records, root, web = self.run_fetch(routes=routes, sources=[src], previous=False)
        self.assertEqual({k: v["state"] for k, v in records.items()}, {"fc-130000": "ok", "fc-270000": "empty"})
        self.assertEqual(sorted(c["url"] for c in web.calls), sorted(routes))
        names = [s["name"] for s in common.load_sources(root, production=False)]
        self.assertEqual(names, ["試験の取得先（130000）", "試験の取得先（270000）"])

    def test_each_rules(self):
        bad = [
            helpers.source(url="https://data.example.com/{code}.json"),  # each が無いのに {code}
            helpers.source(each={"code": ["130000"]}),  # each があるのに url に {code} が無い
            helpers.source(url="https://data.example.com/{code}.json", each={"code": ["../x"]}),
            helpers.source(url="https://data.example.com/{code}.json", each={"code": ["1", "1"]}),
            helpers.source(url="https://data.example.com/{code}.json", each={"area": ["1"]}),
            helpers.source(format="csv", items_path=["x"]),
        ]
        for src in bad:
            root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), sources=[src])
            with self.assertRaises(common.ConfigError, msg=str(src)):
                common.load_sources(root, production=False)

    # ---- 全体の締切 ----
    def test_deadline_marks_the_rest_unreadable_but_keeps_previous(self):
        ticks = iter([0.0] + [10_000.0] * 50)  # 始めた直後に締切を過ぎたことにする
        root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), sources=[helpers.source()])
        web = helpers.FakeWeb({URL: json.dumps(items(2)).encode(), PREV_URL: payload(3)})
        records = fetch.run(root, fetcher=web, previous_base_url=LIVE, now=NOW, deadline_seconds=600, clock=lambda: next(ticks))
        rec = records["s1"]
        self.assertEqual((rec["state"], rec["showing_previous"], rec["items"]), ("unreadable", True, 3))
        self.assertIn("時間切れ", rec["error"])
        self.assertNotIn(URL, [c["url"] for c in web.calls])

    # ---- 第3版: 審査役の指摘（変なデータで全体が止まらない・偽の「取れた」を出さない）----
    def test_strange_json_is_unreadable_and_does_not_stop_the_run(self):
        bodies = {
            "huge-int": b'[{"a": 1' + b"0" * 5000 + b"}]",
            "lone-surrogate": b'[{"a": "\\ud800"}]',
            "nan": b'[{"a": NaN}]',
            "deep": b"[" * 100_000,
        }
        for label, body in bodies.items():
            records, _, _ = self.run_fetch(routes={URL: body}, previous=False)
            self.assertEqual(records["s1"]["state"], "unreadable", label)

    def test_unexpected_exception_in_one_source_does_not_stop_the_others(self):
        other = helpers.source(id="s2", url="https://data.example.com/s2.json")
        records, _, _ = self.run_fetch(routes={URL: RuntimeError("壊れた"), "https://data.example.com/s2.json": b"[{}]"}, sources=[helpers.source(), other], previous=False)
        self.assertEqual((records["s1"]["state"], records["s2"]["state"]), ("unreadable", "ok"))
        self.assertIn("想定外", records["s1"]["error"])

    def test_csv_error_page_is_not_zero(self):
        csv_url = "https://data.example.com/s1.csv"
        page = "<!doctype html><html><body>メンテナンス中</body></html>\n".encode()
        records, _, _ = self.run_fetch(routes={csv_url: page}, sources=[helpers.source(url=csv_url, format="csv", columns=["名前"])], previous=False)
        self.assertEqual(records["s1"]["state"], "unreadable")
        self.assertIn("見出し", records["s1"]["error"])

    def test_csv_cut_in_the_middle(self):
        csv_url = "https://data.example.com/s1.csv"
        records, _, _ = self.run_fetch(routes={csv_url: b"a,b\n1,2\n3\n"}, sources=[helpers.source(url=csv_url, format="csv", columns=["a", "b"])], previous=False)
        self.assertEqual(records["s1"]["state"], "unreadable")

    def test_required_keys(self):
        src = helpers.source(required_keys=["code", "name"])
        records, _, _ = self.run_fetch(routes={URL: b'[{"code": 1, "name": "x"}, {"code": 2}]'}, sources=[src], previous=False)
        self.assertEqual(records["s1"]["state"], "unreadable")

    def test_min_items_catches_a_slow_decline(self):
        src = helpers.source(min_items=10)
        records, _, _ = self.run_fetch(routes={URL: json.dumps(items(9)).encode(), PREV_URL: payload(12)}, sources=[src])
        self.assertEqual(records["s1"]["state"], "unreadable")
        self.assertIn("下限", records["s1"]["error"])

    def test_source_that_silently_stopped_is_unreadable(self):
        src = self.overview_source(max_report_age_hours=48)
        records, _, _ = self.run_fetch(routes={URL: self.overview("2026-05-28T10:16:00+09:00")}, sources=[src], previous=False)
        self.assertEqual(records["s1"]["state"], "unreadable")
        self.assertIn("元が止まっている疑い", records["s1"]["error"])
        records, _, _ = self.run_fetch(routes={URL: self.overview("2026-10-23T11:00:00+09:00")}, sources=[src], previous=False)
        self.assertEqual(records["s1"]["state"], "ok")

    def test_redirects_are_refused_before_following(self):
        with self.assertRaises(fetch.FetchError):
            fetch._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil.example/")

    def test_new_config_rules(self):
        bad = [
            helpers.source(format="csv"),  # CSV なのに columns が無い
            helpers.source(max_report_age_hours=24),  # report_time_path が無い
            helpers.source(min_items=0),
            helpers.source(required_keys=[]),
        ]
        for src in bad:
            root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), sources=[src])
            with self.assertRaises(common.ConfigError, msg=str(src)):
                common.load_sources(root, production=False)

    # ---- 第4版: XML と見張り役（heartbeat）----
    FEED_URL = "https://feed.example.com/extra.xml"

    def feed(self, updated: str, *, doctype: bool = False) -> bytes:
        head = '<?xml version="1.0" encoding="UTF-8"?>\n'
        if doctype:
            head += '<!DOCTYPE feed [<!ENTITY a "aaaaaaaaaa">]>\n'
        return (head + f"""<feed xmlns="http://www.w3.org/2005/Atom" lang="ja">
  <title>試験のフィード</title>
  <updated>{updated}</updated>
  <entry>
    <title>試験の電文</title>
    <id>https://feed.example.com/data/1.xml</id>
    <updated>{updated}</updated>
    <link type="application/xml" href="https://feed.example.com/data/1.xml"/>
    <content type="text">【試験】見出しの文</content>
  </entry>
</feed>
""").encode("utf-8")

    def feed_source(self, **over) -> dict:
        src = helpers.source(id="hb", url=self.FEED_URL, format="xml", items_path=["entry"], report_time_path=["updated", 0, "#text"], max_report_age_hours=1)
        src.update(over)
        return src

    def test_xml_feed_is_read_into_items(self):
        records, root, _ = self.run_fetch(routes={self.FEED_URL: self.feed("2026-10-23T14:50:00+09:00")}, sources=[self.feed_source()], previous=False)
        rec = records["hb"]
        self.assertEqual((rec["state"], rec["items"], rec["report_time"]), ("ok", 1, "2026-10-23T05:50:00Z"))
        item = json.loads((root / "data" / "hb.json").read_text(encoding="utf-8"))["items"][0]
        self.assertEqual(item["link"][0]["@href"], "https://feed.example.com/data/1.xml")
        self.assertEqual(item["content"][0]["#text"], "【試験】見出しの文")

    def test_xml_with_doctype_or_broken_is_unreadable(self):
        for body, needle in ((self.feed("2026-10-23T14:50:00+09:00", doctype=True), "DOCTYPE"), (b"<feed><entry></feed>", "XML として読めません")):
            records, _, _ = self.run_fetch(routes={self.FEED_URL: body}, sources=[self.feed_source()], previous=False)
            self.assertEqual(records["hb"]["state"], "unreadable")
            self.assertIn(needle, records["hb"]["error"])

    def test_stale_feed_is_unreadable(self):
        records, _, _ = self.run_fetch(routes={self.FEED_URL: self.feed("2026-10-23T12:00:00+09:00")}, sources=[self.feed_source()], previous=False)
        self.assertEqual(records["hb"]["state"], "unreadable")

    def test_heartbeat_makes_dependents_unconfirmed(self):
        warn = helpers.source(id="w", heartbeat="hb")
        routes = {URL: b'[{"code": "03"}]'}
        records, root, _ = self.run_fetch(routes={**routes, self.FEED_URL: self.feed("2026-10-23T12:00:00+09:00")}, sources=[self.feed_source(), warn], previous=False)
        rec = records["w"]
        self.assertEqual((rec["state"], rec["showing_previous"]), ("unreadable", True))
        self.assertIn("見張り役（hb）", rec["error"])
        self.assertTrue((root / "data" / "w.json").exists())  # データは消さない（前回の内容として出す）
        records, _, _ = self.run_fetch(routes={**routes, self.FEED_URL: self.feed("2026-10-23T14:50:00+09:00")}, sources=[self.feed_source(), warn], previous=False)
        self.assertEqual(records["w"]["state"], "ok")

    def test_custom_sources_are_left_to_the_site_specific_fetcher(self):
        custom = helpers.source(id="warn", format="custom", heartbeat="hb")
        records, root, web = self.run_fetch(routes={self.FEED_URL: self.feed("2026-10-23T14:50:00+09:00")}, sources=[self.feed_source(), custom], previous=False)
        self.assertNotIn("warn", records)
        self.assertNotIn(URL, [c["url"] for c in web.calls])
        fetch.merge_records(root, {"warn": {"state": "ok", "checked_at": "2026-10-23T06:00:00Z", "last_success_at": "2026-10-23T06:00:00Z", "items": 3, "report_time": None, "error": None, "showing_previous": False}})
        state = json.loads((root / "data" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(set(state["sources"]), {"hb", "warn"})
        with self.assertRaises(ValueError):
            fetch.merge_records(root, {"warn": {"state": "none"}})

    def test_heartbeat_rules(self):
        cases = [
            [helpers.source(id="w", heartbeat="nothing")],
            [helpers.source(id="w", heartbeat="w")],
            [self.feed_source(heartbeat="x"), helpers.source(id="x", url="https://data.example.com/x.json"), helpers.source(id="w", heartbeat="hb")],
            [helpers.source(columns=["a"])],  # columns は CSV だけ
        ]
        for sources in cases:
            root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), sources=sources)
            with self.assertRaises(common.ConfigError, msg=str(sources)):
                common.load_sources(root, production=False)

    def test_previous_kept_also_carries_the_source(self):
        _, root, _ = self.run_fetch(routes={URL: fetch.FetchError("HTTP 503"), PREV_URL: payload(1)})
        self.assertEqual(self.data_file(root)["source"]["license"], "CC BY 4.0")

    def test_network_is_blocked_in_tests(self):
        with self.assertRaises(helpers.NetworkInTests):  # 「unreadable」に飲み込まれず、試験が落ちる
            fetch.http_get("https://127.0.0.1:9/x.json", timeout=1, max_bytes=10, user_agent="x")  # 遮断を壊しても外へは出ない宛先

    def test_source_url_prefixes(self):
        root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), sources=[helpers.source()], config={"source_url_prefixes": ["https://data.example.com/"]})
        self.assertEqual([s["id"] for s in common.load_sources(root, production=False)], ["s1"])
        root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), sources=[helpers.source()], config={"source_url_prefixes": ["https://other.example.com/"]})
        with self.assertRaises(common.ConfigError):
            common.load_sources(root, production=False)
        root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)), config={"source_url_prefixes": ["https://data.example.com"]})
        with self.assertRaises(common.ConfigError):
            common.load_config(root, production=False)  # / で終わらない前置き（data.example.com.evil を通す）

    def test_log_lines_cannot_start_new_workflow_commands(self):
        self.assertEqual(fetch.one_line("a\nb\rc"), "a b c")
        escaped = fetch.gha_escape("x\n::add-mask::y 100%")
        self.assertNotIn("\n", escaped)
        self.assertEqual(escaped, "x%0A::add-mask::y 100%25")


if __name__ == "__main__":
    unittest.main()
