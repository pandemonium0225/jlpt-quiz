"""出題診斷及版本比較；僅用標準函式庫，不讀取憑證或連線。"""

from collections import Counter, defaultdict
import hashlib
import html
import json
from pathlib import Path
import re


REASONS = {
    "mark_noise": "標記是時間戳、數字或模板",
    "mark_fragment": "標記過短、括號不完整或含多句",
    "short_kana": "短假名片段，無法確認是詞彙",
    "sentence_missing": "找不到完整且唯一包含標記的例句",
    "context_missing": "挖空後缺少日文語境",
    "rule_incomplete": "接續表缺少接續描述或例子",
    "empty_grammar": "文法點沒有可讀取的接續表或例句",
    "unknown_word_form": "無法辨識詞形",
    "blank_unavailable": "挖空位置不唯一或無法完整挖空",
    "grammar_target_missing": "無法從標記或標題取得考點",
    "short_key_unsupported": "短助詞缺少接續表佐證",
    "insufficient_options": "排除重複及歧義後，不足三個誘答",
    "invalid_stem": "題幹括號不完整",
    "answer_conflict": "同一題幹對應不同答案",
    "duplicate": "與另一題重複，合併保留一題",
    "usage_unavailable": "可辨識且不重複的用法不足兩種",
    "usage_conflict": "同一例子對應不同用法",
    "inflection_pairs_missing": "可辨識的變形配對不足兩組",
    "inflection_invalid": "無法辨識變形配對的語幹",
    "conjugation_unavailable": "接續形式不支援，或動詞位置不唯一",
    "tokenizer_unavailable": "未載入斷詞器，略過需切詞的活用題",
    "known_alternative": "排除筆記已佐證的起點用法替代答案",
    "ai_pending": "AI 尚未審核，保留原題庫等待續跑",
    "ai_rejected": "AI 判定語境不明、答案有疑慮或可靠誘答不足",
    "ai_options_removed": "AI 移除可成立或無法排除的誘答",
}


class Diagnostics:
    def __init__(self):
        self.events = []

    def record(self, reason, kind, source="", source_url="", text="", target="", details=""):
        self.events.append(dict(reason=reason, kind=kind, source=source,
                                source_url=source_url, text=text, target=target, details=details))


def diagnose(diagnostics, reason, kind, **context):
    if diagnostics is not None:
        diagnostics.record(reason, kind, **context)


# 選項順序不影響前端抽題，但注音、解說、答案等實際內容的變更需要呈現。
CONTENT_FIELDS = ("kind", "label", "instruction", "stem", "answer", "pool", "original",
                  "translation", "note", "source", "source_url", "source_text", "punctuation_restored")


def content(question):
    return {key: sorted(set(question.get(key, []))) if key == "pool" else question.get(key, "")
            for key in CONTENT_FIELDS}


def revision(question):
    data = json.dumps(content(question), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def compare_banks(previous, current):
    """先依來源區塊配對；舊版沒有來源識別時，僅做無歧義的內容配對。"""
    if previous is None:
        return {"available": False, "added": [], "removed": [], "changed": [], "unchanged": 0}
    old, new = previous["questions"], current["questions"]
    left, right, pairs = set(range(len(old))), set(range(len(new))), []
    # 同區塊的答案、題幹修改仍屬同一題；不可用模糊字串比對硬配。
    for key in (lambda q: q.get("origin_key"), lambda q: q.get("id"),
                lambda q: (q.get("kind"), q.get("label"), q.get("stem"))):
        oi, ni = defaultdict(list), defaultdict(list)
        for i in sorted(left):
            if key(old[i]):
                oi[key(old[i])].append(i)
        for i in sorted(right):
            if key(new[i]):
                ni[key(new[i])].append(i)
        for value in oi.keys() & ni.keys():
            if len(oi[value]) == len(ni[value]) == 1:
                a, b = oi[value][0], ni[value][0]
                pairs.append((a, b))
                left.remove(a)
                right.remove(b)
    changed, unchanged = [], 0
    for a, b in sorted(pairs):
        before, after = content(old[a]), content(new[b])
        fields = [k for k in CONTENT_FIELDS if before[k] != after[k]]
        if fields:
            changed.append({"before": old[a], "after": new[b], "fields": fields})
        else:
            unchanged += 1
    return {"available": True, "added": [new[i] for i in sorted(right)],
            "removed": [old[i] for i in sorted(left)], "changed": changed, "unchanged": unchanged}


def read_bank(path):
    """不存在代表首次建立；存在但損壞不能假裝沒有舊題庫。"""
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema_version") != 2
            or not isinstance(data.get("questions"), list)
            or any(not isinstance(q, dict) or not isinstance(q.get("stem"), str)
                   or not isinstance(q.get("answer"), str)
                   or not isinstance(q.get("pool"), list)
                   or not all(isinstance(p, str) for p in q["pool"]) for q in data["questions"])):
        raise ValueError("比較基準不是有效的 schema_version=2 題庫：" + str(path))
    return data


def make_report(previous, current, diagnostics, grammar, baseline, status="success", error=""):
    counts = Counter(e["reason"] for e in diagnostics.events)
    sources = Counter(q["source"] for q in current["questions"] if q["kind"] != "vocab")
    return {"report_version": 1, "status": status, "error": error,
            "generated_at": current.get("generated_at", ""), "baseline": baseline,
            "stats": current["stats"], "diff": compare_banks(previous, current),
            "reason_counts": dict(sorted(counts.items())), "events": diagnostics.events,
            "grammar_coverage": [{"source": g["title"], "source_url": g.get("source_url", ""),
                                  "questions": sources[g["title"]]} for g in grammar]}


def safe_text(text):
    # 筆記不可變成摘要中的 HTML、Markdown 連結、表格或 @mention。
    value = html.escape(str(text)).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>@~-])", r"\\\1", value)


def source_link(item):
    label = safe_text(item.get("source", "") or "筆記")
    url = item.get("source_url", "")
    if re.fullmatch(r"https://www\.notion\.so/[a-f0-9]{32}(#[a-f0-9]{32})?", url):
        return f"[{label}]({url})"
    return label


def render_markdown(report, limit=30):
    stats, diff = report["stats"], report["diff"]
    lines = ["## 題庫更新報告", "",
             f"生成狀態：{safe_text(report['status'])}。本摘要描述生成結果；是否已發布請看 deploy 工作。", "",
             f"題目 {stats['questions']} 題；標記詞素材 {stats['marked_terms']} 筆；文法 {stats['grammar_points']} 則。", "",
             "比較基準：" + safe_text(report["baseline"].get("description", "")), ""]
    if report["error"]:
        lines += ["錯誤：" + safe_text(report["error"]), ""]
    if diff["available"]:
        lines += [f"新增 {len(diff['added'])}、消失 {len(diff['removed'])}、修改 {len(diff['changed'])}、未變 {diff['unchanged']}。", ""]
        for label, items in (("修改的題目", diff["changed"]), ("新增的題目", diff["added"]),
                             ("消失的題目", diff["removed"])):
            if not items:
                continue
            lines += ["### " + label, ""]
            for item in items[:limit]:
                q = item.get("after", item)
                lines.append(f"- {source_link(q)}：{safe_text(q['stem'])}（答案：{safe_text(q['answer'])}）")
                if "fields" in item:
                    for field in item["fields"]:
                        before, after = item["before"].get(field, ""), q.get(field, "")
                        lines.append(f"  - {field}：{safe_text(before)} → {safe_text(after)}")
            if len(items) > limit:
                lines.append(f"- 另有 {len(items) - limit} 筆，請下載完整 JSON 報告。")
            lines.append("")
    elif report["status"] == "success":
        lines += ["本次沒有可比較的基準；不將全部題目誤報為新增。成功部署後供下次比較。", ""]
    else:
        lines += ["本次未完成可發布的題庫，保留現有網站；不把尚未完成的結果列為題目消失。", ""]
    ai = report.get("ai", {})
    if ai.get("enabled") and "calls" in ai:
        run_limit = ("單次不限額" if ai["run_budget_usd"] is None
                     else "上限 US$%.2f" % ai["run_budget_usd"])
        lines += ["### AI 語意審核", "",
                  f"模型 {safe_text(ai['model'])}；呼叫 {ai['calls']} 次，沿用快取 {ai['cache_hits']} 題。",
                  f"已知案例校驗 {ai['calibration_passed']} / {ai['calibration_total']}；未全數通過不發布。",
                  f"通過 {ai['accepted']}、排除 {ai['rejected']}、待審 {ai['pending']}；移除歧義誘答 {ai['trimmed_options']} 個。",
                  f"本次估計 US${ai['run_usd']:.4f}（{run_limit}）；本月 UTC 累計 US${ai['month_usd']:.4f} / {ai['month_budget_usd']:.2f}。",
                  "金額按程式內價格表估算，失敗請求保守預留；實際帳單以 API 服務為準。", ""]
    elif not ai.get("enabled"):
        lines += ["AI 語意審核未啟用；本次只驗證規則生成及差異報告。", ""]
    lines += ["### 未出題及品質檢查", "", "以下是檢查事件數；同一素材可能涉及不同題型，不等於淘汰題數。", ""]
    for reason, count in report["reason_counts"].items():
        lines.append(f"- {REASONS.get(reason, reason)}：{count}")
    # 雜訊時間戳優先級較低，摘要先放需要人判斷的事件；JSON 保留全部。
    events = sorted(report["events"], key=lambda e: e["reason"] in {"mark_noise", "short_kana", "duplicate"})
    if events:
        lines += ["", "<details><summary>查看診斷範例與筆記連結</summary>", ""]
        for e in events[:limit]:
            lines.append(f"- {source_link(e)}：{REASONS.get(e['reason'], e['reason'])}；"
                         f"{safe_text(e['target'] or e['text'])} {safe_text(e['details'])}")
        lines += ["", "</details>", ""]
    uncovered = [g for g in report["grammar_coverage"] if not g["questions"]]
    if uncovered:
        lines += ["### 尚未產出題目的文法點", ""]
        lines += [f"- {source_link(g)}" for g in uncovered[:limit]]
        lines.append("")
    lines += ["完整資料請下載本次 quiz-review artifact。已知規則無法保證排除所有語意多解；未變不代表已人工審核。", ""]
    # Actions 摘要有大小上限；完整 JSON 不截斷。
    result = "\n".join(lines)
    if len(result.encode("utf-8")) > 700_000:
        result = result.encode("utf-8")[:700_000].decode("utf-8", errors="ignore") + "\n\n摘要過長，請下載完整報告。\n"
    return result


def write_report(directory, report):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "report.md").write_text(render_markdown(report), encoding="utf-8")
