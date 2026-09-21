import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import ai_review as a
from scripts import build_quiz as b
from scripts.quiz_report import Diagnostics


def question():
    # 題庫既有的 を／から 多解案例，加上足夠誘答，驗證可修復而不整題刪掉。
    return {"kind": "grammar", "label": "文法", "instruction": "依 Notion 筆記的原句，選出填入［　］的內容。",
            "stem": "電車［　］降ります。", "answer": "を", "pool": ["から", "に", "が", "と"],
            "original": "電車を降ります。", "source": "格助詞　を", "source_url": ""}


def verdict(q, valid=None, uncertain=(), clear=True):
    valid = {q["answer"]} if valid is None else set(valid)
    return {"question_clear": clear, "explanation": "審核測試案例",
            "options": [{"index": i, "verdict": "valid" if p in valid else "uncertain" if p in uncertain else "invalid",
                         "reason": "一般合理解讀成立" if p in valid else "接續不成立"}
                        for i, p in enumerate(a.review_input(q)["options"])]}


def response(result, status="completed"):
    return {"status": status, "usage": {"input_tokens": 100, "output_tokens": 100},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}


class ReviewTests(unittest.TestCase):
    def test_alternative_answer_is_removed_and_cache_reused(self):
        q = question()
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def transport(body):
                calls.append(body)
                return response(verdict(q, valid={"を", "から"}))
            path = Path(directory) / "cache.json"
            reviewer = a.Reviewer(path, transport=transport)
            output = reviewer.filter_questions([q], Diagnostics())
            self.assertEqual(output[0]["pool"], ["に", "が", "と"])
            self.assertEqual(q["pool"], ["から", "に", "が", "と"])
            replay = a.Reviewer(path, transport=lambda _: self.fail("相同題目不得重複付費"))
            replay.filter_questions([dict(q, pool=list(reversed(q["pool"])))], Diagnostics())
            self.assertEqual(replay.stats["cache_hits"], 1)
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0]["store"])
            self.assertEqual(calls[0]["max_output_tokens"], a.MAX_OUTPUT_TOKENS)

    def test_changed_pool_or_prompt_requires_new_review(self):
        q = question()
        changed = dict(q, pool=q["pool"] + ["へ"])
        self.assertNotEqual(a.cache_key(q), a.cache_key(changed))
        before = a.cache_key(q)
        with patch.object(a, "PROMPT", a.PROMPT + " 新規則"):
            self.assertNotEqual(a.cache_key(q), before)

    def test_uncertain_options_and_wrong_answers_cannot_pass(self):
        q = question()
        q["pool"] = ["から", "に", "が"]
        for result in [verdict(q, uncertain={"から"}), verdict(q, valid={"から"}), verdict(q, clear=False)]:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory:
                reviewer = a.Reviewer(Path(directory) / "cache.json", transport=lambda _: response(result))
                self.assertEqual(reviewer.filter_questions([q], Diagnostics()), [])
                self.assertEqual(reviewer.stats["rejected"], 1)
                self.assertEqual(reviewer.stats["pending"], 0)

    def test_hidden_grammar_title_is_not_visible_context(self):
        self.assertEqual(a.review_input(question())["visible_context"], "")
        self.assertEqual(a.review_input(dict(question(), label="用法"))["visible_context"], "格助詞　を")

    def test_zero_budget_never_calls_api_and_reports_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            reviewer = a.Reviewer(Path(directory) / "cache.json", run_budget=0,
                                  transport=lambda _: self.fail("不得超支"))
            reviewer.filter_questions([question()], Diagnostics())
            self.assertEqual(reviewer.stats["pending"], 1)
            self.assertEqual(reviewer.stats["calls"], 0)

    def test_monthly_budget_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            first = a.Reviewer(path, transport=lambda _: response(verdict(question())))
            first.filter_questions([question()], Diagnostics())
            spent = first.stats["month_usd"]
            second = a.Reviewer(path, month_budget=spent, transport=lambda _: self.fail("月額已達上限"))
            second.filter_questions([dict(question(), stem="バス［　］降ります。")], Diagnostics())
            self.assertEqual(second.stats["pending"], 1)
            self.assertEqual(second.stats["month_usd"], spent)

    def test_api_failure_reserves_budget_and_stops_remaining_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def fail(body):
                calls.append(body)
                raise RuntimeError("連線失敗")
            path = Path(directory) / "cache.json"
            reviewer = a.Reviewer(path, transport=fail)
            reviewer.filter_questions([question(), dict(question(), stem="別題")], Diagnostics())
            self.assertEqual(len(calls), 1)
            self.assertEqual(reviewer.stats["pending"], 2)
            self.assertGreater(reviewer.stats["run_usd"], 0)
            self.assertFalse(json.loads(path.read_text())["cache"])

    def test_incomplete_or_duplicate_option_results_never_cached(self):
        q = question()
        duplicate = verdict(q)
        duplicate["options"][1]["index"] = duplicate["options"][0]["index"]
        for data in [response(verdict(q), status="incomplete"), response(duplicate),
                     {"status": "completed", "output": []}]:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                reviewer = a.Reviewer(Path(directory) / "cache.json", transport=lambda _: data)
                self.assertEqual(reviewer.filter_questions([q], Diagnostics()), [])
                self.assertTrue(reviewer.stats["error"])
                self.assertFalse(reviewer.state["cache"])

    def test_budget_must_be_finite_and_state_cannot_be_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            for budget in (-1, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    a.Reviewer(path, run_budget=budget)
            path.write_text('{"state_version": 0}')
            with self.assertRaises(ValueError):
                a.Reviewer(path)

    def test_model_calibration_checks_good_and_ambiguous_examples(self):
        def transport(body):
            incoming = json.loads(body["input"][1]["content"])
            _name, q, expected, clear = next(case for case in a.calibration_cases() if case[1]["stem"] == incoming["stem"])
            result = verdict(q, valid={p for p, v in expected.items() if v == "valid"}, clear=clear is not False)
            return response(result)
        with tempfile.TemporaryDirectory() as directory:
            reviewer = a.Reviewer(Path(directory) / "cache.json", transport=transport)
            self.assertTrue(reviewer.calibrate())
            self.assertEqual(reviewer.stats["calibration_passed"], 5)
            self.assertEqual(reviewer.stats["calls"], 5)
        with tempfile.TemporaryDirectory() as directory:
            # 模型只認原句、錯把「から」排除，不能通過校驗。
            bad = a.Reviewer(Path(directory) / "cache.json", transport=lambda _: response(verdict(a.calibration_cases()[0][1])))
            with self.assertRaisesRegex(ValueError, "起點替代"):
                bad.calibrate()

    def test_successful_review_publishes_only_filtered_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, out = root / "materials.json", root / "quiz.json"
            snapshot.write_text(json.dumps({"snapshot_version": 1, "grammar": [], "vocab": []}))
            q = dict(question(), id="id", revision="old")
            reviewer = a.Reviewer(root / "cache.json", transport=lambda _: response(verdict(q, valid={"を", "から"})))
            reviewer.stats["calibration_passed"] = 5
            with patch.object(b, "build_questions", return_value=[q]), patch.object(b, "Reviewer", return_value=reviewer), \
                 patch.object(reviewer, "calibrate", return_value=True), patch.dict(b.os.environ, {"OPENAI_API_KEY": "test"}):
                b.main(["--from-snapshot", str(snapshot), "--output", str(out), "--report-dir", str(root / "report"), "--ai-review"])
            published = json.loads(out.read_text())["questions"][0]
            self.assertNotIn("から", published["pool"])
            self.assertNotEqual(published["revision"], "old")
            self.assertEqual(published["ai_review"]["model"], a.MODEL)

    def test_unfinished_calibration_blocks_even_cached_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, out, output = root / "materials.json", root / "quiz.json", root / "outputs"
            snapshot.write_text(json.dumps({"snapshot_version": 1, "grammar": [], "vocab": []}))
            q = question()
            reviewer = a.Reviewer(root / "cache.json", transport=lambda _: response(verdict(q)))
            reviewer.review(q)
            with patch.object(b, "build_questions", return_value=[q]), patch.object(b, "Reviewer", return_value=reviewer), \
                 patch.object(reviewer, "calibrate", return_value=False), patch.dict(b.os.environ, {"OPENAI_API_KEY": "test", "GITHUB_OUTPUT": str(output)}):
                b.main(["--from-snapshot", str(snapshot), "--output", str(out), "--report-dir", str(root / "report"), "--ai-review"])
            self.assertFalse(out.exists())
            self.assertNotIn("ready=true", output.read_text())

    def test_pending_review_keeps_bank_and_workflow_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out, snapshot, output = root / "quiz.json", root / "materials.json", root / "outputs"
            q = question()
            old = {"schema_version": 2, "questions": [q]}
            out.write_text(json.dumps(old))
            original = out.read_bytes()
            snapshot.write_text(json.dumps({"snapshot_version": 1, "grammar": [], "vocab": []}))
            with patch.object(b, "build_questions", return_value=[q]), patch.dict(b.os.environ, {"OPENAI_API_KEY": "test", "GITHUB_OUTPUT": str(output)}):
                b.main(["--from-snapshot", str(snapshot), "--output", str(out), "--report-dir", str(root / "report"),
                        "--ai-review", "--ai-run-budget", "0", "--ai-state", str(root / "cache.json")])
            self.assertEqual(out.read_bytes(), original)
            self.assertEqual(output.read_text(), "ready=false\n")
            self.assertEqual(json.loads((root / "report/report.json").read_text())["status"], "pending")
