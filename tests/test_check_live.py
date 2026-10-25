from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import helpers

import build  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import check_dist  # noqa: E402
import check_live  # noqa: E402

NOW = helpers.NOW
LIVE = helpers.LIVE


class FakeSite:
    """偽の公開サイト。dist/ を配り、_headers の /* を全応答に付ける（Cloudflare の代わり）。"""

    def __init__(self, dist: Path, *, drop_header: str | None = None, missing_status: int = 404, unreachable: bool = False, redirect_to: str | None = None):
        rules = check_dist.parse_headers_file((dist / "_headers").read_text(encoding="utf-8"))
        self.headers = {k: v for k, v in rules["/*"].items() if k != drop_header}
        self.dist = dist
        self.missing_status = missing_status
        self.unreachable = unreachable
        self.redirect_to = redirect_to

    def __call__(self, url: str):
        if self.unreachable:
            raise check_live.Unreadable(f"{url} につながりませんでした（試験）")
        assert url.startswith(LIVE), url
        if self.redirect_to:  # 全ページを別のサイトへ飛ばす _redirects が効いている、の偽物
            return 200, {}, b"<html>evil</html>", self.redirect_to + url[len(LIVE):]
        path = self.dist / (url[len(LIVE):] or "index.html")
        if path.is_file():
            return 200, dict(self.headers), path.read_bytes(), url
        return self.missing_status, dict(self.headers), b"not found", url


class CheckLiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = helpers.make_root(self.tmp, sources=[helpers.source()])
        self.out = self.tmp / "dist"
        build.build(self.root, self.out, production=True, now=NOW)

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, site: FakeSite, *, now=NOW, max_source_age=None):
        return check_live.check_live(LIVE, get=site, now=now, max_age=timedelta(minutes=180), max_source_age=max_source_age)

    def test_healthy_site(self):
        problems, unreadable = self.check(FakeSite(self.out))
        self.assertEqual((problems, unreadable), ([], []))

    def test_cookie_is_a_problem(self):
        site = FakeSite(self.out)
        site.headers["set-cookie"] = "__cf_bm=x; path=/"
        problems, _ = self.check(site)
        self.assertTrue(any("Cookie を返しています" in p for p in problems), problems)

    def test_site_hooks_are_called(self):
        with mock.patch.object(check_live.site_hooks, "check_live", return_value=(["サイト専用の違反"], [])):
            problems, _ = self.check(FakeSite(self.out))
        self.assertIn("サイト専用の違反", problems)

    def test_missing_csp_header(self):
        problems, _ = self.check(FakeSite(self.out, drop_header="content-security-policy"))
        self.assertTrue(any("Content-Security-Policy がありません" in p for p in problems), problems)

    def test_stale_status_means_the_schedule_stopped(self):
        problems, _ = self.check(FakeSite(self.out), now=NOW + timedelta(hours=4))
        self.assertTrue(any("古いまま" in p for p in problems), problems)

    def test_soft_404(self):
        problems, _ = self.check(FakeSite(self.out, missing_status=200))
        self.assertTrue(any("404 であるべき" in p for p in problems), problems)

    def test_unreachable_is_not_reported_as_healthy(self):
        problems, unreadable = self.check(FakeSite(self.out, unreachable=True))
        self.assertEqual(problems, [])
        self.assertTrue(unreadable)

    def test_banner_missing_on_the_live_top_page(self):
        index = self.out / "index.html"
        index.write_text(index.read_text(encoding="utf-8").replace(" data-unofficial-banner", ""), encoding="utf-8")
        problems, _ = self.check(FakeSite(self.out))
        self.assertTrue(any("帯" in p for p in problems), problems)

    def test_source_age_is_checked_only_when_asked(self):
        problems, _ = self.check(FakeSite(self.out))
        self.assertEqual(problems, [])
        problems, _ = self.check(FakeSite(self.out), max_source_age=timedelta(hours=12))
        self.assertTrue(any("一度も取得できていません" in p for p in problems), problems)

    def test_workflow_stopped_for_inactivity_is_reported(self):
        url = "https://api.github.com/repos/test-owner/test-repo/actions/workflows?per_page=100"

        def api(state: str):
            body = json.dumps({"total_count": 2, "workflows": [
                {"path": ".github/workflows/ci.yml", "state": "active"},
                {"path": ".github/workflows/deploy.yml", "state": state},
            ]}).encode()
            return lambda u: (200, {}, body, u) if u == url else (404, {}, b"", u)

        self.assertEqual(check_live.check_workflows("test-owner/test-repo", get=api("active")), ([], []))
        problems, unreadable = check_live.check_workflows("test-owner/test-repo", get=api("disabled_inactivity"))
        self.assertEqual(unreadable, [])
        self.assertTrue(any("disabled_inactivity" in p for p in problems), problems)

    def test_workflow_state_that_cannot_be_read_is_not_healthy(self):
        problems, unreadable = check_live.check_workflows("test-owner/test-repo", get=lambda u: (403, {}, b"rate limited", u))
        self.assertEqual(problems, [])
        self.assertTrue(unreadable)

    def test_live_files_match_the_built_dist(self):
        waits = []
        problems, unreadable = check_live.check_same_as_dist(LIVE, self.out, get=FakeSite(self.out), sleep=waits.append)
        self.assertEqual((problems, unreadable, waits), ([], [], []))

    def test_file_changed_on_the_cdn_is_reported_after_retries(self):
        served = self.tmp / "served"
        shutil.copytree(self.out, served)
        index = served / "index.html"
        index.write_text(index.read_text(encoding="utf-8").replace("</body>", '<script src="https://beacon.example/x.js"></script></body>'), encoding="utf-8")
        waits = []
        problems, _ = check_live.check_same_as_dist(LIVE, self.out, get=FakeSite(served), sleep=waits.append, retries=2, wait_seconds=1)
        self.assertEqual(problems, ["公開中の index.html が今作った物と一致しません（HTTP 200・中身が違う）"])
        self.assertEqual(waits, [1, 1])

    def test_old_version_that_catches_up_is_accepted(self):
        served = self.tmp / "served"
        shutil.copytree(self.out, served)
        (served / "status.json").write_text("{}", encoding="utf-8")  # 古い版を返す拠点が残っていた

        def catch_up(_seconds):
            shutil.copyfile(self.out / "status.json", served / "status.json")

        problems, unreadable = check_live.check_same_as_dist(LIVE, self.out, get=FakeSite(served), sleep=catch_up)
        self.assertEqual((problems, unreadable), ([], []))

    def test_files_not_served_are_not_compared(self):
        self.assertNotIn("_headers", check_live.dist_hashes(self.out))
        self.assertIn("index.html", check_live.dist_hashes(self.out))

    def test_site_redirected_to_another_host_is_a_problem(self):
        problems, _ = self.check(FakeSite(self.out, redirect_to="https://evil.example/"))
        self.assertTrue(any("別の場所へ転送されています" in p for p in problems), problems)
        problems, _ = check_live.check_same_as_dist(LIVE, self.out, get=FakeSite(self.out, redirect_to="https://evil.example/"), sleep=lambda s: None, retries=0)
        self.assertTrue(problems)

    def test_old_last_success_is_reported(self):
        status = json.loads((self.out / "status.json").read_text(encoding="utf-8"))
        status["sources"]["s1"]["last_success_at"] = "2026-10-21T00:00:00Z"
        (self.out / "status.json").write_text(json.dumps(status), encoding="utf-8")
        problems, _ = self.check(FakeSite(self.out), max_source_age=timedelta(hours=12))
        self.assertTrue(any("最後に取得できたのは" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
