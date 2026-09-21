import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_quiz as b
from scripts import quiz_report as r


def vocabulary():
    return [{"term": term, "sentence": sentence, "source": "記事", "hint": "翻譯",
             "source_url": "https://www.notion.so/" + "a" * 32 + "#" + str(i) * 32,
             "origin_key": str(i) + ":mark:0"}
            for i, (term, sentence) in enumerate([
                ("新聞", "毎朝、新聞を読みます。"), ("雑誌", "この雑誌を買いました。"),
                ("辞書", "知らない言葉を辞書で調べます。"), ("切手", "封筒に切手を貼ります。")])]


class ReportTests(unittest.TestCase):
    def test_new_vocabulary_changes_old_options(self):
        vocab = vocabulary()
        before = {"questions": b.build_questions(vocab, [])}
        added = {"term": "地図", "sentence": "地図を買いました。", "source": "記事", "origin_key": "4:mark:0"}
        after = {"questions": b.build_questions(vocab + [added], [])}
        diff = r.compare_banks(before, after)
        self.assertEqual(len(diff["added"]), 1)
        self.assertEqual(len(diff["changed"]), 4)
        self.assertTrue(all(item["fields"] == ["pool"] for item in diff["changed"]))

    def test_source_identity_matches_answer_and_stem_edits(self):
        original = b.build_questions(vocabulary(), [])[0]
        updated = dict(original, id="new-content-id", stem="今朝、［　］を読みました。", answer="雑誌")
        diff = r.compare_banks({"questions": [original]}, {"questions": [updated]})
        self.assertEqual(len(diff["changed"]), 1)
        self.assertEqual(diff["changed"][0]["fields"], ["stem", "answer"])
        self.assertFalse(diff["added"] or diff["removed"])

    def test_pool_order_and_new_tracking_fields_are_not_content_edits(self):
        q = b.build_questions(vocabulary(), [])[0]
        old = {k: v for k, v in q.items() if k not in {"origin_key", "revision", "basis"}}
        new = dict(q, pool=list(reversed(q["pool"])))
        self.assertEqual(r.revision(old), r.revision(new))
        self.assertEqual(r.compare_banks({"questions": [old]}, {"questions": [new]})["unchanged"], 1)

    def test_no_baseline_and_corrupt_baseline_are_distinct(self):
        self.assertFalse(r.compare_banks(None, {"questions": [1]})["available"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quiz.json"
            self.assertIsNone(r.read_bank(path))
            path.write_text('{"questions": []}')
            with self.assertRaises(ValueError):
                r.read_bank(path)

    def test_parser_and_generation_rejections_have_sources(self):
        d = r.Diagnostics()
        self.assertEqual(b.marked_terms([{"plain_text": "[0:43]", "annotations": {"bold": True}}],
                                       diagnostics=d, source="記事", source_url="url"), [])
        b.build_questions(vocabulary()[:3], [], d)
        self.assertEqual(d.events[0]["reason"], "mark_noise")
        self.assertEqual(d.events[0]["source_url"], "url")
        self.assertEqual([e["reason"] for e in d.events[1:]], ["insufficient_options"] * 3)
        self.assertTrue(all(e["source_url"] for e in d.events))

    def test_offline_replay_reports_edits_and_preserves_bank_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "materials.json"
            out = root / "quiz.json"
            report = root / "review"
            data = {"snapshot_version": 1, "vocab": vocabulary(), "grammar": [], "parse_events": []}
            snapshot.write_text(json.dumps(data))
            args = ["--from-snapshot", str(snapshot), "--output", str(out), "--report-dir", str(report)]
            with patch.object(b, "children", side_effect=AssertionError("離線不可連線")):
                b.main(args)
                b.main(args)
                self.assertEqual(json.loads((report / "report.json").read_text())["diff"]["unchanged"], 4)
                data["vocab"] = vocabulary()[:3]
                snapshot.write_text(json.dumps(data))
                prior = out.read_bytes()
                with self.assertRaises(SystemExit):
                    b.main(args)
                self.assertEqual(out.read_bytes(), prior)
                failed = json.loads((report / "report.json").read_text())
                self.assertEqual(failed["status"], "failed")
                self.assertFalse(failed["diff"]["available"])
                self.assertIn("不足三個誘答", (report / "report.md").read_text())

    def test_report_escapes_untrusted_notes(self):
        q = b.build_questions(vocabulary(), [])[0]
        q.update(stem="<script>alert(1)</script> [link](https://evil.test) @user |", source="<img>")
        report = r.make_report({"questions": []}, {"questions": [q], "stats": {
            "questions": 1, "marked_terms": 1, "grammar_points": 0}}, r.Diagnostics(), [], {"description": "test"})
        markdown = r.render_markdown(report)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[link](https://evil.test)", markdown)

    @unittest.skipUnless(b.TAGGER, "需斷詞器辨識普通形與禮貌形")
    def test_departure_guard_requires_matching_note_evidence(self):
        rules = [["名詞（起点）＋を＋移動動詞", "電車を降りる"],
                 ["名詞（対象）＋を＋他動詞", "コーヒーを飲む"]]
        self.assertEqual(b.departure_alternatives("電車［　］降ります。", "を", rules), {"から"})
        self.assertEqual(b.departure_alternatives("コーヒー［　］飲みます。", "を", rules), set())
        self.assertEqual(b.departure_alternatives("階段［　］降ります。", "を", rules), set())
        self.assertEqual(b.departure_alternatives("電車［　］降ります。", "を", []), set())
