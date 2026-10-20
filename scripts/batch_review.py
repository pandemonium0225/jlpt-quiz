"""跨排程續跑的 Batch 審題；保存預留、工作識別及結果，避免重複送審。"""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

if __package__:
    from . import ai_review as legacy
else:
    import ai_review as legacy


MODEL = "gpt-6-luna"
# 2026-10-20 官方 Batch 短上下文價格（USD / 百萬 tokens）。
# https://developers.openai.com/api/docs/models/gpt-6-luna
INPUT_PRICE = 0.05
CACHED_PRICE = 0.005
WRITE_PRICE = 0.0625
OUTPUT_PRICE = 0.25
TERMINAL = {"completed", "failed", "expired", "cancelled"}


def request_body(question):
    body = legacy.request_body(question)
    body["model"] = MODEL
    body.pop("service_tier")
    return body


def cache_key(question):
    data = json.dumps([legacy.POLICY_VERSION, request_body(question)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


class BatchAPI:
    def request(self, method, path, data=None, content_type="application/json", raw=False):
        token = legacy.os.environ.get("OPENAI_API_KEY", "").strip()
        if not token:
            raise ValueError("AI 審題已啟用，但未設定 OPENAI_API_KEY")
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
        request = urllib.request.Request(
            "https://api.openai.com/v1" + path, data=data, method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=legacy.REQUEST_TIMEOUT_SECONDS) as response:
                content = response.read()
            return content.decode("utf-8") if raw else json.loads(content)
        except urllib.error.HTTPError as error:
            raise RuntimeError("OpenAI Batch API HTTP %d；保留進度與原題庫" % error.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("OpenAI Batch API 連線失敗或逾時；保留進度，下次核對批次") from None

    def upload(self, rows):
        boundary = "jlpt-" + uuid.uuid4().hex
        content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        data = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"quiz.jsonl\"\r\n"
            f"Content-Type: application/jsonl\r\n\r\n{content}\r\n--{boundary}--\r\n"
        ).encode()
        return self.request("POST", "/files", data, "multipart/form-data; boundary=" + boundary)["id"]

    def create(self, file_id, submission):
        return self.request("POST", "/batches", {
            "input_file_id": file_id, "endpoint": "/v1/responses", "completion_window": "24h",
            "metadata": {"jlpt_submission": submission}})

    def retrieve(self, batch_id):
        return self.request("GET", "/batches/" + urllib.parse.quote(batch_id, safe=""))

    def content(self, file_id):
        text = self.request("GET", "/files/" + urllib.parse.quote(file_id, safe="") + "/content", raw=True)
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def recover(self, submission):
        """建立請求結果不明時只查詢，不重送可能已計費的批次。"""
        after = ""
        while True:
            page = self.request("GET", "/batches?limit=100" + (
                "&after=" + urllib.parse.quote(after, safe="") if after else ""))
            for batch in page["data"]:
                if (batch.get("metadata") or {}).get("jlpt_submission") == submission:
                    return batch
            if not page.get("has_more"):
                return None
            cursor = page.get("last_id")
            if not cursor or cursor == after:
                raise ValueError("Batch 清單分頁資料不完整；停止恢復")
            after = cursor


class BatchReviewer(legacy.Reviewer):
    deferred = True

    def __init__(self, *args, api=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.api = api or BatchAPI()
        self.calibrating = False
        self.stats.update(model=MODEL, mode="batch", batch_status="idle", batch_id="",
                          submitted=0, collected=0, reserved_usd=0.0, settled_usd=0.0,
                          legacy_cache_hits=0)
        self._batch_stats()

    def _save(self):
        legacy.save_state(self.path, self.state)
        self._batch_stats()

    def stage_snapshot(self, snapshot):
        self.state["candidate_snapshot"] = snapshot
        self._save()

    def finish_snapshot(self):
        self.state.pop("candidate_snapshot", None)
        self._save()

    def _batch_stats(self):
        job = self.state.get("batch")
        if job:
            if not isinstance(job, dict) or not isinstance(job.get("requests"), dict):
                raise ValueError("Batch 進度損壞；停止送審")
            self.stats.update(batch_status=job["status"], batch_id=job.get("id", ""))
        self.stats["month_usd"] = self.state["months"].get(self.month, 0.0)

    def _cached(self, question, allow_legacy=True):
        cached = self.state["cache"].get(cache_key(question))
        if cached:
            return cached
        # 明確的單次遷移政策：同提示／schema／可見題目下，沿用較貴 Sol 的既有判定。
        # 六項新模型校驗絕不沿用 Sol；未來改提示或政策也會自然失效。
        if allow_legacy:
            old = self.state["cache"].get(legacy.cache_key(question))
            if old and old.get("model") == legacy.MODEL and old.get("policy") == legacy.POLICY_VERSION:
                return old
        return None

    def review_metadata(self, question):
        cached = self._cached(question)
        return {"model": cached["model"], "policy": cached["policy"]}

    def _prepare_review(self, question, allow_request=True):
        cached = self._cached(question, allow_legacy=not self.calibrating)
        if cached:
            self.stats["cache_hits"] += 1
            if cached["model"] == legacy.MODEL:
                self.stats["legacy_cache_hits"] += 1
            return legacy.validate_verdict(cached["verdict"], question), None
        if not allow_request:
            return None, None
        job = self.state.get("batch")
        if job and job["status"] != "prepared":
            return None, None
        key = cache_key(question)
        if job and key in job["requests"]:
            return None, None
        body = request_body(question)
        size = len(json.dumps(body, ensure_ascii=False).encode())
        if size > legacy.MAX_REQUEST_BYTES:
            raise ValueError("題目與參考段落過長，超過單次審題輸入限制")
        # 預留最大輸出及最貴的輸入寫入價；在送出前一併保存預留與請求。
        reserve = ((size + 2048) * WRITE_PRICE + legacy.MAX_OUTPUT_TOKENS * OUTPUT_PRICE) / 1_000_000
        month_spend = self.state["months"].get(self.month, 0.0)
        if ((self.run_budget is not None and self.stats["reserved_usd"] + reserve > self.run_budget)
                or month_spend + reserve > self.month_budget):
            return None, None
        if job is None:
            job = {"status": "prepared", "submission": uuid.uuid4().hex, "requests": {}}
            self.state["batch"] = job
        job["requests"][key] = {"body": body, "reserve": reserve, "month": self.month,
                                "policy": legacy.POLICY_VERSION,
                                "prices": [INPUT_PRICE, CACHED_PRICE, WRITE_PRICE, OUTPUT_PRICE]}
        self.state["months"][self.month] = month_spend + reserve
        self.stats["reserved_usd"] += reserve
        self.stats["run_usd"] += reserve
        self._save()
        return None, None

    def calibrate(self):
        self.collect()
        self.calibrating = True
        complete = True
        try:
            for name, question, expected, clear in legacy.calibration_cases():
                verdict = self.review(question)
                if verdict is None:
                    complete = False
                    continue
                options = legacy.review_input(question)["options"]
                actual = {options[v["index"]]: v["verdict"] for v in verdict["options"]}
                if any(actual[p] != value for p, value in expected.items()) or (
                        clear is not None and verdict["question_clear"] != clear):
                    raise ValueError("AI 未通過已知案例「%s」；停止發布，不自動升級模型" % name)
                self.stats["calibration_passed"] += 1
        finally:
            self.calibrating = False
        return complete

    def flush(self):
        job = self.state.get("batch")
        if not job or job["status"] not in {"prepared", "uploaded"} or self.stats["error"]:
            return
        if job["status"] == "prepared":
            rows = [{"custom_id": key, "method": "POST", "url": "/v1/responses", "body": r["body"]}
                    for key, r in job["requests"].items()]
            job["file_id"] = self.api.upload(rows)
            job["status"] = "uploaded"
            self._save()
        # 保存意圖再送出；中斷後利用唯一 metadata 找回，不自動重複建立。
        job["status"] = "submitting"
        self._save()
        result = self.api.create(job["file_id"], job["submission"])
        job["id"] = result["id"]
        job["status"] = result["status"]
        self.stats["submitted"] = len(job["requests"])
        self.stats["calls"] = self.stats["submitted"]
        self._save()

    def collect(self):
        job = self.state.get("batch")
        if not job or job["status"] in {"prepared", "uploaded"}:
            return
        if job["status"] == "blocked":
            raise ValueError("上次 Batch 有失敗項目；已保存成功結果，需檢查後才可重新送審")
        if job["status"] == "submitting":
            recovered = self.api.recover(job["submission"])
            if not recovered:
                raise ValueError("Batch 建立結果不明且尚未查到；保留預留額，不自動重送，請核對 API 批次")
            job.update(id=recovered["id"], status=recovered["status"])
            self._save()
        batch = self.api.retrieve(job["id"])
        job["status"] = batch["status"]
        self._save()
        if batch["status"] not in TERMINAL:
            return
        rows = []
        for field in ("output_file_id", "error_file_id"):
            if batch.get(field):
                rows.extend(self.api.content(batch[field]))
        by_key = {}
        for row in rows:
            key = row.get("custom_id")
            if key not in job["requests"] or key in by_key:
                raise ValueError("Batch 結果含未知或重複的 custom_id；停止結算")
            by_key[key] = row
        errors = []
        # 先在記憶體完整結算，再原子保存所有快取與用量，重跑不會重複扣款。
        for key, request in job["requests"].items():
            row = by_key.get(key, {})
            response = row.get("response") or {}
            body = response.get("body") or {}
            usage = body.get("usage") or {}
            inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
            actual = request["reserve"]
            if type(inputs) is int and inputs >= 0 and type(outputs) is int and outputs >= 0:
                details = usage.get("input_tokens_details") or {}
                cached, written = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
                if not all(type(n) is int and n >= 0 for n in (cached, written)) or cached + written > inputs:
                    errors.append("Batch token 用量無效")
                else:
                    regular, read, write, output = request["prices"]
                    actual = ((inputs - cached - written) * regular + cached * read
                              + written * write + outputs * output) / 1_000_000
            elif batch["status"] == "failed" and not row:
                actual = 0.0  # 批次驗證失敗，尚未執行任何請求。
            elif (row.get("error") or {}).get("code") == "batch_expired":
                actual = 0.0  # 官方定義：未及執行，不收費；已完成項目仍結算。
            self.state["months"][request["month"]] = max(
                0.0, self.state["months"][request["month"]] + actual - request["reserve"])
            self.stats["settled_usd"] += actual
            try:
                if row.get("error") or response.get("status_code") != 200 or body.get("status") != "completed":
                    raise ValueError("Batch 含未完成或失敗的請求")
                texts = [part["text"] for item in body.get("output", []) if item.get("type") == "message"
                         for part in item.get("content", []) if part.get("type") == "output_text"]
                if len(texts) != 1:
                    raise ValueError("Batch 未回傳完整審核結果")
                visible = json.loads(request["body"]["input"][1]["content"])
                options = visible["options"]
                question = {"label": visible["type"], "instruction": visible["instruction"],
                            "stem": visible["stem"], "source": visible["visible_context"],
                            "answer": options[0], "pool": options[1:]}
                verdict = legacy.validate_verdict(json.loads(texts[0]), question)
                self.state["cache"][key] = {"verdict": verdict, "model": request["body"]["model"],
                                            "policy": request["policy"]}
                self.stats["collected"] += 1
            except (ValueError, KeyError, TypeError):
                errors.append("Batch 含無效或未完成的審核結果")
        # 保存批次摘要方便人工對帳；失敗工作保留鎖定狀態，不無限重試花錢。
        self.state["last_batch"] = {"id": job["id"], "status": batch["status"],
                                   "settled_usd": self.stats["settled_usd"],
                                   "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if errors:
            job["status"] = "blocked"
        else:
            del self.state["batch"]
        self._save()
        self.stats["batch_status"] = "blocked" if errors else "completed"
        self.stats["batch_id"] = job["id"]
        if errors:
            raise ValueError("Batch 部分失敗；成功結果與費用已保存，不自動重送，請檢查批次 " + job["id"])
