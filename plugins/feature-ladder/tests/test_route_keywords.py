"""route.py keyword pre-fill and CLI: plurals and derived forms count, look-alike
words do not, and bad arguments give a message instead of a traceback.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import subprocess
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
ROUTER = PLUGIN / "skills" / "complexity-router" / "scripts"
sys.path.insert(0, str(ROUTER))

import route  # noqa: E402


class KeywordHit(unittest.TestCase):
    def test_plurals_set_the_flags(self):
        task = route.describe("rotate user passwords, store session tokens and check permissions on the new endpoints")
        self.assertTrue(task.get("security"))
        self.assertTrue(task.get("public_api"))

    def test_derived_forms_set_the_flags(self):
        task = route.describe("add authentication and authorization, run database migrations in worker threads")
        for flag in ("security", "persistence", "concurrency"):
            self.assertTrue(task.get(flag), flag)
        self.assertTrue(route.describe("encrypt the payload before upload").get("security"))
        self.assertTrue(route.describe("the rows were deleted from the table").get("irreversible"))

    def test_look_alike_words_do_not_match(self):
        for text, flag in (("credit the author of the stable release", "security"),
                           ("credit the author of the stable release", "persistence"),
                           ("unblock the clock widget", "concurrency"),
                           ("a productive session of sketching", "irreversible")):
            self.assertFalse(route.describe(text).get(flag), (text, flag))

    def test_prefix_stems_match(self):
        self.assertTrue(route.keyword_hit("make the webhook handler idempotent", ["idempot"]))
        self.assertTrue(route.keyword_hit("serializing the save file", ["serializ"]))

    def test_multi_word_keywords_still_match(self):
        self.assertTrue(route.keyword_hit("it makes two api calls", ["api call"]))
        self.assertIsNone(route.keyword_hit("nothing relevant here", ["api call"]))


class Docs(unittest.TestCase):
    def test_docstring_matches_the_scoring_table(self):
        self.assertEqual(route.POINTS["irreversible"](True), 2)
        self.assertNotIn("+3", route.__doc__)
        self.assertNotIn("intake policy", route.__doc__)


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROUTER / "route.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_bad_arguments_give_a_message_not_a_traceback(self):
        for args in (["--file"], ["{not json"], ["--file", "does-not-exist.json"]):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertNotIn("Traceback", r.stderr, args)
            self.assertIn("route", r.stderr, args)

    def test_a_json_task_still_routes(self):
        r = self.run_cli('{"files": 1, "loc": 20}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"agent": "mechanic"', r.stdout)


if __name__ == "__main__":
    unittest.main()
