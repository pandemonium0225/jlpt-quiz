import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import ai_review as legacy
from scripts import batch_review as b
from scripts import build_quiz
from scripts.quiz_report import Diagnostics


def question(stem="水を［　］ください。"):
    return {"label": "活用", "kind": "connect", "instruction": "選擇答案", "stem": stem,
            "answer": "飲んで", "pool": ["飲む", "飲んだ", "飲みます"], "source": "", "source_url": ""}


def result(q, valid=None, clear=True):
    return {"question_clear": clear, "explanation": "測試",
            "options": [{"index": i, "verdict": "valid" if p in (valid or {q["answer"]}) else "invalid",
                         "reason": "測試"} for i, p in enumerate(legacy.review_input(q)["options"])]}


class FakeAPI:
    def __init__(self):
        self.uploads, self.creates = [], []
        self.status = "in_progress"
        self.rows = []
        self.lose_create_response = False
        self.found = True
        self.read_error = False

    def upload(self, rows):
        self.uploads.append(rows)
        return "file-test"

    def create(self, file_id, submission):
        self.creates.append((file_id, submission))
        if self.lose_create_response:
            raise RuntimeError("建立逾時")
        return {"id": "batch-test", "status": self.status}

    def recover(self, submission):
        return {"id": "batch-test", "status": self.status} if self.found else None

    def retrieve(self, batch_id):
        return {"id": batch_id, "status": self.status, "output_file_id": "file-output"}

    def content(self, file_id):
        if self.read_error:
            raise RuntimeError("下載失敗")
        return self.rows

    def finish(self, overrides=None):
        self.status = "completed"
        self.rows = []
        for row in self.uploads[-1]:
            visible = json.loads(row["body"]["input"][1]["content"])
            q = question(visible["stem"])
            q.update(label=visible["type"], instruction=visible["instruction"],
                     answer=visible["options"][0], pool=visible["options"][1:])
            verdict = result(q)
            for _, case, expected, clear in legacy.calibration_cases():
                if case["stem"] == q["stem"]:
                    verdict = result(case, valid={p for p, v in expected.items() if v == "valid"},
                                     clear=clear is not False)
            if overrides:
                verdict = overrides(q, verdict)
            self.rows.append({
                "custom_id": row["custom_id"], "error": None,
                "response": {"status_code": 200, "body": {
                    "status": "completed",
                    "usage": {"input_tokens": 1000, "output_tokens": 100,
                              "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 200}},
                    "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": json.dumps(verdict)}]}]}}})
        self.rows.reverse()  # Batch 不保證結果順序。


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.api = FakeAPI()

    def reviewer(self, **kwargs):
        return b.BatchReviewer(self.path, api=self.api, **kwargs)

    def test_calibration_queues_all_cases_and_resumes_without_duplicate_submission(self):
        first = self.reviewer()
        self.assertFalse(first.calibrate())
        first.flush()
        self.assertEqual(len(self.api.uploads[0]), 6)
        self.assertTrue(all(row["body"]["model"] == b.MODEL for row in self.api.uploads[0]))
        self.assertTrue(all("service_tier" not in row["body"] for row in self.api.uploads[0]))
        reserved = first.stats["month_usd"]
        second = self.reviewer()
        self.assertFalse(second.calibrate())
        second.flush()
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(second.stats["month_usd"], reserved)
        self.api.finish()
        third = self.reviewer()
        self.assertTrue(third.calibrate())
        self.assertEqual(third.stats["collected"], 6)
        self.assertLess(third.stats["month_usd"], reserved)
        self.assertNotIn("batch", third.state)
        spent = third.stats["month_usd"]
        fourth = self.reviewer()
        self.assertTrue(fourth.calibrate())
        self.assertEqual(fourth.stats["month_usd"], spent)
        self.assertEqual(len(self.api.creates), 1)

    def test_candidates_are_queued_together_and_deduplicated(self):
        reviewer = self.reviewer()
        qs = [question("題目" + str(i)) for i in range(4)]
        reviewer.filter_questions(qs + [qs[0]], Diagnostics())
        reviewer.flush()
        self.assertEqual(len(self.api.uploads[0]), 4)
        self.assertEqual(reviewer.stats["pending"], 5)

    def test_old_sol_cache_and_pools_survive_but_do_not_calibrate_luna(self):
        q = question()
        old = legacy.Reviewer(self.path)
        old.state["cache"][legacy.cache_key(q)] = {
            "verdict": result(q), "model": legacy.MODEL, "policy": legacy.POLICY_VERSION}
        old.state["pools"][legacy.pool_key(q)] = q["pool"]
        for _, case, _, _ in legacy.calibration_cases():
            old.state["cache"][legacy.cache_key(case)] = {
                "verdict": result(case), "model": legacy.MODEL, "policy": legacy.POLICY_VERSION}
        legacy.save_state(self.path, old.state)
        reviewer = self.reviewer()
        crowded = dict(q, pool=["新詞"] + q["pool"][:2], pool_candidates=["新詞"] + q["pool"])
        accepted = reviewer.filter_questions([crowded], Diagnostics())
        self.assertEqual(accepted[0]["ai_review"]["model"], legacy.MODEL)
        self.assertEqual(reviewer.stats["reused_pools"], 1)
        self.assertEqual(reviewer.stats["legacy_cache_hits"], 1)
        self.assertFalse(reviewer.calibrate())
        reviewer.flush()
        self.assertEqual(len(self.api.uploads[0]), 6)
        # 題幹或政策變動不能誤用 Sol 快取。
        self.assertIsNone(reviewer._cached(dict(q, stem="修改題幹")))
        with patch.object(legacy, "POLICY_VERSION", "new-policy"):
            self.assertIsNone(reviewer._cached(q))

    def test_budget_zero_does_not_create_a_batch(self):
        reviewer = self.reviewer(run_budget=0)
        self.assertFalse(reviewer.calibrate())
        reviewer.filter_questions([question()], Diagnostics())
        reviewer.flush()
        self.assertFalse(self.api.uploads)
        self.assertNotIn("batch", reviewer.state)
        self.assertEqual(reviewer.stats["month_usd"], 0)

    def test_prepared_batch_and_budget_survive_restart_before_upload(self):
        reviewer = self.reviewer()
        reviewer.review(question())
        reserved = reviewer.stats["month_usd"]
        resumed = self.reviewer()
        resumed.review(question())
        resumed.flush()
        self.assertEqual(len(self.api.uploads[0]), 1)
        self.assertEqual(resumed.stats["month_usd"], reserved)

    def test_create_timeout_recovers_existing_batch_without_resubmitting(self):
        reviewer = self.reviewer()
        reviewer.review(question())
        self.api.lose_create_response = True
        with self.assertRaisesRegex(RuntimeError, "逾時"):
            reviewer.flush()
        self.assertEqual(json.loads(self.path.read_text())["batch"]["status"], "submitting")
        resumed = self.reviewer()
        resumed.collect()
        resumed.flush()
        self.assertEqual(resumed.state["batch"]["id"], "batch-test")
        self.assertEqual(len(self.api.creates), 1)

    def test_unknown_submission_blocks_without_spending_again(self):
        reviewer = self.reviewer()
        reviewer.review(question())
        self.api.lose_create_response = True
        with self.assertRaises(RuntimeError):
            reviewer.flush()
        self.api.found = False
        resumed = self.reviewer()
        with self.assertRaisesRegex(ValueError, "不自動重送"):
            resumed.collect()
        resumed.flush()
        self.assertEqual(len(self.api.creates), 1)

    def test_settlement_accounts_for_cache_discounts_and_original_month(self):
        reviewer = self.reviewer()
        reviewer.month = "2026-09"
        reviewer.review(question())
        reviewer.flush()
        self.api.finish()
        resumed = self.reviewer()
        resumed.collect()
        expected = (200 * b.INPUT_PRICE + 600 * b.CACHED_PRICE
                    + 200 * b.WRITE_PRICE + 100 * b.OUTPUT_PRICE) / 1_000_000
        self.assertAlmostEqual(resumed.state["months"]["2026-09"], expected)
        self.assertAlmostEqual(resumed.stats["settled_usd"], expected)
        self.assertEqual(resumed.stats["reserved_usd"], 0)

    def test_partial_failure_saves_good_results_and_blocks_repeated_payment(self):
        reviewer = self.reviewer()
        reviewer.filter_questions([question("一"), question("二")], Diagnostics())
        reviewer.flush()
        self.api.finish()
        self.api.rows[0].update(response=None, error={"code": "batch_expired"})
        self.api.status = "expired"
        resumed = self.reviewer()
        with self.assertRaisesRegex(ValueError, "部分失敗"):
            resumed.collect()
        self.assertEqual(len(resumed.state["cache"]), 1)
        self.assertEqual(resumed.state["batch"]["status"], "blocked")
        spent = resumed.stats["month_usd"]
        again = self.reviewer()
        with self.assertRaisesRegex(ValueError, "失敗項目"):
            again.collect()
        self.assertEqual(again.stats["month_usd"], spent)
        self.assertEqual(len(self.api.creates), 1)

    def test_missing_or_duplicate_results_cannot_publish(self):
        for corruption in ("missing", "duplicate", "incomplete", "bad_indices"):
            with self.subTest(corruption=corruption):
                path = self.path.with_name(corruption + ".json")
                api = FakeAPI()
                reviewer = b.BatchReviewer(path, api=api)
                reviewer.review(question())
                reviewer.flush()
                api.finish()
                if corruption == "missing":
                    api.rows = []
                elif corruption == "duplicate":
                    api.rows *= 2
                elif corruption == "incomplete":
                    api.rows[0]["response"]["body"]["status"] = "incomplete"
                else:
                    api.rows[0]["response"]["body"]["output"][0]["content"][0]["text"] = json.dumps(result(
                        dict(question(), pool=[])))
                resumed = b.BatchReviewer(path, api=api)
                with self.assertRaises(ValueError):
                    resumed.collect()
                self.assertFalse(resumed.state["cache"])

    def test_download_failure_can_resume_without_double_settlement(self):
        reviewer = self.reviewer()
        reviewer.review(question())
        reviewer.flush()
        self.api.finish()
        self.api.read_error = True
        reserved = reviewer.stats["month_usd"]
        with self.assertRaises(RuntimeError):
            self.reviewer().collect()
        self.assertEqual(self.reviewer().stats["month_usd"], reserved)
        self.api.read_error = False
        done = self.reviewer()
        done.collect()
        self.assertLess(done.stats["month_usd"], reserved)
        self.assertNotIn("batch", done.state)

    def test_failed_calibration_does_not_submit_candidates(self):
        reviewer = self.reviewer()
        reviewer.calibrate()
        reviewer.flush()
        self.api.finish(lambda q, verdict: dict(verdict, question_clear=True)
                        if q["stem"] == "静かだ → ［　］" else verdict)
        with self.assertRaisesRegex(ValueError, "變形指示不足"):
            self.reviewer().calibrate()
        self.assertEqual(len(self.api.creates), 1)

    def test_end_to_end_default_batch_keeps_site_until_calibrated_and_reviewed(self):
        root = Path(self.directory.name)
        snapshot, out, output = root / "materials.json", root / "quiz.json", root / "outputs"
        q = dict(question(), id="test", revision="old")
        old = {"schema_version": 2, "questions": [q]}
        out.write_text(json.dumps(old))
        snapshot.write_text(json.dumps({"snapshot_version": 1, "grammar": [], "vocab": []}))
        args = ["--from-snapshot", str(snapshot), "--output", str(out), "--report-dir", str(root / "report"),
                "--ai-review", "--ai-state", str(self.path)]
        def run():
            output.write_text("")
            with patch.object(build_quiz, "build_questions", return_value=[copy.deepcopy(q)]), \
                 patch.object(b, "BatchAPI", return_value=self.api), \
                 patch.dict(build_quiz.os.environ, {"OPENAI_API_KEY": "test", "GITHUB_OUTPUT": str(output)}):
                build_quiz.main(args)
            return json.loads((root / "report/report.json").read_text())
        self.assertEqual(run()["status"], "pending")
        self.assertEqual(json.loads(out.read_text()), old)
        self.assertEqual(len(self.api.uploads[0]), 6)  # 校驗尚未過，不送候選題。
        self.api.finish()
        self.assertEqual(run()["status"], "pending")
        self.assertEqual(len(self.api.uploads[1]), 1)
        # 筆記每天新增題目時，已完成批次仍須發布，不能被下一批的新題拖住。
        q["stem"] = "新筆記的新題目"
        self.api.finish()
        self.assertEqual(run()["status"], "success")
        self.assertIn("ready=true", output.read_text())
        published = json.loads(out.read_text())["questions"][0]
        self.assertEqual(published["ai_review"]["model"], b.MODEL)
        self.assertNotEqual(published["stem"], q["stem"])
        self.assertNotIn("candidate_snapshot", json.loads(self.path.read_text()))
        self.assertEqual(len(self.api.creates), 2)
        self.assertIn("Batch 狀態", (root / "report/report.md").read_text())
