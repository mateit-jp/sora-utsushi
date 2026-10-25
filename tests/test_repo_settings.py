"""repo_settings/check.sh と apply.sh を、偽の gh で流す（本物の gh・ネットには触れない）。

確かめるのはシェルの組み立て（管理者でないアカウントでは何もせずに止まる・止まり方の終了コード）だけ。
jq の式が本物の GitHub の応答に合っているかは、ここでは確かめられない（初回に本物で流して確かめる。付録E E.10）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

ROOT = helpers.ROOT
REPO = "test-owner/test-repo"

# 偽の gh。呼ばれた (方法, パス) を記録し、spec のとおりに答える。答えの無い読み取りは 404 で失敗する
FAKE_GH = '''#!{python} -I
import json, os, sys
spec = json.load(open(os.environ["FAKE_GH_SPEC"], encoding="utf-8"))
args = sys.argv[1:]
method, path, jq = "GET", None, None
i = 1
while i < len(args):
    a = args[i]
    if a in ("--method", "-X"):
        method = args[i + 1]; i += 2; continue
    if a == "--jq":
        jq = args[i + 1]; i += 2; continue
    if a in ("-H", "--input", "-f", "-F"):
        i += 2; continue
    if a.startswith("-"):
        i += 1; continue
    if path is None:
        path = a
    i += 1
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([method, path]) + "\\n")
if args[:1] != ["api"]:
    sys.exit("偽の gh: api 以外が呼ばれました")
if spec["login"] is None:
    sys.exit(4)
if path == "user":
    print(spec["login"]); sys.exit(0)
if method != "GET":
    if spec["admin"]:
        sys.exit(0)
    print("gh: Not Found (HTTP 404)", file=sys.stderr); sys.exit(1)
if jq == ".permissions.admin":
    print("true" if spec["admin"] else "false"); sys.exit(0)
answer = spec["answers"].get(path)
if answer is None:
    print("gh: Not Found (HTTP 404)", file=sys.stderr); sys.exit(1)
print(answer)
'''


class RepoSettingsScriptsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "gh"
        fake.write_text(FAKE_GH.replace("{python}", sys.executable), encoding="utf-8")
        fake.chmod(0o755)
        self.log = self.tmp / "gh_calls.jsonl"
        self.spec = self.tmp / "spec.json"
        self.env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        # 偽の gh を PATH の先頭に置く（本物の gh があっても、そちらは呼ばれない）
        self.env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", FAKE_GH_SPEC=str(self.spec), FAKE_GH_LOG=str(self.log))

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, name: str, *, login: str | None, admin: bool, answers: dict | None = None):
        prefix = f"repos/{REPO}"
        spec = {"login": login, "admin": admin, "answers": {f"{prefix}/{k}": v for k, v in (answers or {}).items()}}
        self.spec.write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.run(["sh", str(ROOT / "repo_settings" / name), REPO], env=self.env, capture_output=True, text=True, errors="replace", timeout=60)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []
        return proc, calls

    def assertStoppedBeforeAnything(self, proc, calls):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(calls, [["GET", "user"], ["GET", f"repos/{REPO}"]])  # 確かめの2回だけ。設定は読みも書きもしない

    def test_check_stops_when_the_account_is_not_the_admin(self):
        proc, calls = self.run_script("check.sh", login="someone-else", admin=False)
        self.assertStoppedBeforeAnything(proc, calls)
        self.assertIn("someone-else", proc.stdout)
        self.assertIn("管理者だと確かめられませんでした", proc.stdout)
        self.assertNotIn("違う", proc.stdout)  # 読めなかったものを「違う」「無効」と言わない
        self.assertNotIn("OK", proc.stdout)

    def test_check_stops_when_gh_is_not_logged_in(self):
        proc, calls = self.run_script("check.sh", login=None, admin=False)
        self.assertStoppedBeforeAnything(proc, calls)
        self.assertIn("読めませんでした", proc.stdout)
        self.assertNotIn("OK", proc.stdout)

    def test_check_goes_on_for_the_admin(self):
        proc, calls = self.run_script("check.sh", login="test-owner", admin=True)
        self.assertIn(f"OK      gh のアカウント（test-owner）は {REPO} の管理者", proc.stdout)
        self.assertIn(["GET", f"repos/{REPO}/private-vulnerability-reporting"], calls)
        self.assertTrue(all(m == "GET" for m, _ in calls), calls)  # 読むだけ

    def test_check_reports_unreadable_items_as_unreadable(self):
        # 管理者でも、答えの無い項目（404）は「読めない」。終了コードは「違う」があれば1、無ければ2
        answers = {"vulnerability-alerts": "HTTP/2.0 204 No Content", "rulesets": "42"}
        proc, _ = self.run_script("check.sh", login="test-owner", admin=True, answers=answers)
        self.assertIn("読めない CodeQL", proc.stdout)
        self.assertNotIn("違う    CodeQL", proc.stdout)
        self.assertEqual(proc.returncode, 2, proc.stdout)

    def test_check_reports_a_different_setting(self):
        answers = {"code-scanning/default-setup": "not-configured", "vulnerability-alerts": "HTTP/2.0 204 No Content", "rulesets": "42"}
        proc, _ = self.run_script("check.sh", login="test-owner", admin=True, answers=answers)
        self.assertIn("違う    CodeQL（今: not-configured / 期待: configured）", proc.stdout)
        self.assertNotIn("unbound", proc.stderr)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

    def test_apply_stops_when_the_account_is_not_the_admin(self):
        proc, calls = self.run_script("apply.sh", login="someone-else", admin=False)
        self.assertStoppedBeforeAnything(proc, calls)
        self.assertIn("何も入れていません", proc.stderr)

    def test_apply_stops_when_gh_is_not_logged_in(self):
        proc, calls = self.run_script("apply.sh", login=None, admin=False)
        self.assertStoppedBeforeAnything(proc, calls)

    def test_apply_writes_settings_as_the_admin(self):
        answers = {"environments/production/deployment-branch-policies": "main", "rulesets": ""}
        proc, calls = self.run_script("apply.sh", login="test-owner", admin=True, answers=answers)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(["PATCH", f"repos/{REPO}"], calls)
        self.assertIn(["POST", f"repos/{REPO}/rulesets"], calls)


class ShellVariableNamesTest(unittest.TestCase):
    """macOS の sh（bash 3.2）は、変数名の直後の全角の文字の1バイト目まで変数名に読む（set -u で終了コード127）。

    Linux の sh（dash）では起きないので、CI でも見張れるように書き方そのものを見る。注記の行は見ない。
    """

    def test_no_variable_is_followed_directly_by_a_non_ascii_character(self):
        pattern = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])")
        scripts = sorted((ROOT / "repo_settings").glob("*.sh"))
        self.assertEqual([p.name for p in scripts], ["apply.sh", "check.sh"])
        found = []
        for path in scripts:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.lstrip().startswith("#"):
                    found += [f"{path.name}:{n}: {m.group(0)}" for m in pattern.finditer(line)]
        self.assertEqual(found, [], "変数は ${name} の形で囲んでください")


if __name__ == "__main__":
    unittest.main()
