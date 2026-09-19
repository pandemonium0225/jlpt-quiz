#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
從 Notion 抓「日文文章」與「日文文法」的內容，自動生成 JLPT 風格題庫。

產出：site/quiz.json

題型：
  vocab   語彙穴埋め   — 來源：文章頁面中使用者手動標記（粗體／背景色）的詞
  grammar 文法辨識     — 來源：日文文法頁面的例句
  connect 接續方式     — 來源：日文文法頁面的接續方式表格

環境變數（也可寫在專案根目錄的 .env，已被 .gitignore 排除；環境變數優先）：
  NOTION_TOKEN      Notion internal integration token
  ARTICLES_PAGE_ID  「日文文章」父頁面 ID
  GRAMMAR_PAGE_ID   「日文文法」頁面 ID
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


def load_dotenv(path):
    """讀 KEY=VALUE 格式的 .env；已存在的環境變數不會被覆蓋。"""
    try:
        f = open(path, encoding="utf-8-sig")
    except FileNotFoundError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
ARTICLES_ID = os.environ.get("ARTICLES_PAGE_ID", "").strip()
GRAMMAR_ID = os.environ.get("GRAMMAR_PAGE_ID", "").strip()

OUT = os.path.join(os.path.dirname(__file__), "..", "site", "quiz.json")

# 時間戳記等不該被當成生詞的標記
NOISE = re.compile(r"^[\s\[\]\(\)（）0-9:：.。、,，\-–—~〜<>→]*$")
DIGIT = re.compile(r"[0-9０-９]")  # 金額、年齡、數量等含數字的標記不適合當語彙題
SENT_SPLIT = re.compile(r"(?<=[。！？\?\!])")
TIMESTAMP = re.compile(r"^\s*\[?\s*\d{1,2}:\d{2}\s*\]?\s*")
KANA_ONLY = re.compile(r"^[ぁ-んァ-ヴー]+$")

MIN_TERM = 2    # 少於此長度視為助詞或活用詞尾
MAX_TERM = 24   # 超過此長度視為整句標記


# --------------------------------------------------------------------------
# Notion API
# --------------------------------------------------------------------------
def api_get(path):
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            body = e.read().decode("utf-8", "replace")[:400]
            raise SystemExit("Notion API %s on %s\n%s" % (e.code, path, body))
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    raise SystemExit("Notion API unreachable: " + path)


def children(block_id):
    """取得某個 block / page 的所有子 block（自動分頁）。"""
    out, cursor = [], None
    while True:
        path = "/blocks/%s/children?page_size=100" % block_id.replace("-", "")
        if cursor:
            path += "&start_cursor=" + cursor
        data = api_get(path)
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            return out
        cursor = data.get("next_cursor")


# --------------------------------------------------------------------------
# rich text 處理
# --------------------------------------------------------------------------
def rt_plain(rich):
    return "".join(t.get("plain_text", "") for t in rich)


def block_rich(b):
    body = b.get(b.get("type"), {})
    return body.get("rich_text", []) or []


def block_text(b):
    return rt_plain(block_rich(b))


def is_marked(token):
    """使用者手動標記：粗體或非預設背景／文字色。"""
    a = token.get("annotations", {})
    return bool(a.get("bold")) or a.get("color", "default") != "default"


def marked_terms(rich):
    """抽出連續的標記片段，過濾掉時間戳之類的雜訊。"""
    terms, buf = [], ""
    for t in rich:
        if is_marked(t):
            buf += t.get("plain_text", "")
        else:
            if buf:
                terms.append(buf)
                buf = ""
    if buf:
        terms.append(buf)

    clean = []
    for term in terms:
        term = term.strip()
        # 先判斷原字串：「00:00:00 –> 00:00:35」若先切掉開頭時間戳，剩下的殘片會漏網
        if NOISE.match(term):
            continue
        term = TIMESTAMP.sub("", term).strip()
        if not term or NOISE.match(term) or DIGIT.search(term):
            continue
        if len(term) < MIN_TERM or len(term) > MAX_TERM:
            # 太短（助詞、活用詞尾）或整句被標起來，都不適合做克漏字
            continue
        if len(term) <= 3 and KANA_ONLY.match(term):
            # 純假名的短片段多半是文法碎片（きゃ／のって／ずつ），不是詞彙
            continue
        clean.append(term)
    return clean


def strip_ts(text):
    return TIMESTAMP.sub("", text).strip()


def sentence_with(text, term):
    """回傳包含 term 的那一句；找不到就回傳整段。"""
    text = strip_ts(text)
    for s in SENT_SPLIT.split(text):
        if term in s and len(s.strip()) > len(term):
            return s.strip()
    return text


# --------------------------------------------------------------------------
# 解析：日文文章（父頁面下的所有子頁面）
# --------------------------------------------------------------------------
def collect_vocab(parent_id, skip_ids):
    """回傳 [{term, sentence, hint, source}]"""
    items = []
    skip = {s.replace("-", "") for s in skip_ids}

    for blk in children(parent_id):
        if blk.get("type") != "child_page":
            continue
        pid = blk["id"]
        if pid.replace("-", "") in skip:
            continue
        title = blk["child_page"]["title"]
        print("  讀取文章：%s" % title, file=sys.stderr)

        blocks = children(pid)
        for i, b in enumerate(blocks):
            if b.get("type") != "paragraph":
                continue
            rich = block_rich(b)
            terms = marked_terms(rich)
            if not terms:
                continue
            para = block_text(b)

            # 找緊接在後面的 📝 翻譯
            hint = ""
            for nxt in blocks[i + 1:i + 3]:
                if nxt.get("type") == "quote":
                    q = block_text(nxt).strip()
                    if q.startswith("📝"):
                        hint = q.lstrip("📝").strip()
                        break

            for term in terms:
                sent = sentence_with(para, term)
                if term not in sent:
                    continue
                # 整行都被標記（標題、目錄）挖空後沒有語境，無法作答
                if not sent.replace(term, "", 1).strip(" 　。、！？?!「」『』"):
                    continue
                items.append({
                    "term": term,
                    "sentence": sent,
                    "hint": hint,
                    "source": title,
                })
    return items


# --------------------------------------------------------------------------
# 解析：日文文法
# --------------------------------------------------------------------------
FURIGANA = re.compile(r"（[ぁ-んァ-ヴー・\s]+）")


def clean_title(name):
    """去掉注音括號，並只留「：」之前的名稱（之後是說明文字）。"""
    name = FURIGANA.sub("", name)
    head = re.split(r"[：:]", name, maxsplit=1)[0].strip()
    return head or name.strip()


def collect_grammar(page_id):
    """回傳 [{title, rules:[(接続, 例)], examples:[(日文, 中文)]}]"""
    entries, cur, section = [], None, ""

    for b in children(page_id):
        t = b.get("type")

        is_heading = bool(t) and t.startswith("heading_")

        # 文法點標題以「【」開頭；實際頁面是 heading_2，不依賴層級
        if is_heading and block_text(b).strip().startswith("【"):
            name = clean_title(re.sub(r"^【|】$", "", block_text(b).strip()))
            if not name:
                continue
            cur = {"title": name, "rules": [], "examples": []}
            entries.append(cur)
            section = ""
            continue

        if cur is None:
            continue

        if is_heading:
            head = block_text(b).strip()
            if "接続" in head or "接續" in head:
                section = "rules"
            elif "例句" in head or "例文" in head:
                section = "examples"
            elif "解析" in head:
                section = "analysis"
            continue

        if t == "table":
            rows = [r for r in children(b["id"]) if r.get("type") == "table_row"]
            header = b["table"].get("has_column_header")
            for idx, r in enumerate(rows):
                if header and idx == 0:
                    continue
                cells = r["table_row"]["cells"]
                if len(cells) < 2:
                    continue
                left = rt_plain(cells[0]).strip()
                right = rt_plain(cells[1]).strip()
                if left and right:
                    cur["rules"].append([left, right])
            continue

        if section == "examples":
            if t == "paragraph":
                jp = block_text(b).strip()
                if jp:
                    cur["examples"].append([jp, ""])
            elif t == "quote" and cur["examples"]:
                zh = block_text(b).strip().lstrip("📝").strip()
                if zh and not cur["examples"][-1][1]:
                    cur["examples"][-1][1] = zh

    return [e for e in entries if e["rules"] or e["examples"]]


# --------------------------------------------------------------------------
# 出題
# --------------------------------------------------------------------------
def build_questions(vocab, grammar):
    qs = []

    # --- 語彙穴埋め ---
    seen = set()
    all_terms = []
    for v in vocab:
        if v["term"] not in seen:
            seen.add(v["term"])
            all_terms.append(v["term"])

    for v in vocab:
        pool = [t for t in all_terms if t != v["term"]]
        # 誘答選字數相近的詞，避免長度差異直接洩漏答案
        near = [t for t in pool if abs(len(t) - len(v["term"])) <= 2]
        distractors = near if len(near) >= 3 else pool
        if len(distractors) < 3:
            continue
        stem = v["sentence"].replace(v["term"], "（　）", 1)
        qs.append({
            "kind": "vocab",
            "label": "語彙",
            "stem": stem,
            "hint": v["hint"],
            "answer": v["term"],
            "pool": distractors,
            "note": "出自〈%s〉，原句為「%s」。" % (v["source"], v["sentence"]),
        })

    # --- 文法辨識 ---
    titles = [g["title"] for g in grammar]
    for g in grammar:
        others = [t for t in titles if t != g["title"]]
        if len(others) < 3:
            continue
        for jp, zh in g["examples"][:2]:
            qs.append({
                "kind": "grammar",
                "label": "文法",
                "stem": "次（つぎ）の文（ぶん）で使（つか）われている文法（ぶんぽう）はどれですか。<br><span class=\"quoted\">%s</span>" % jp,
                "hint": zh,
                "answer": g["title"],
                "pool": others,
                "note": "這句示範的是「%s」。" % g["title"],
            })

    # --- 接續方式 ---
    for g in grammar:
        # 誘答只取「其他文法點」的接續；同一文法點的其他接續也是正解，不能當誘答
        own = {r for r, _ in g["rules"]}
        others = []
        for h in grammar:
            if h is g:
                continue
            others += [r for r, _ in h["rules"] if r not in own and r not in others]

        for rule, ex in g["rules"][:2]:
            if len(others) < 3:
                continue
            qs.append({
                "kind": "connect",
                "label": "接續",
                "stem": "「%s」の接続（せつぞく）として正（ただ）しいものはどれですか。" % g["title"],
                "hint": "例：%s" % ex,
                "answer": rule,
                "pool": others,
                "note": "「%s」的接續為「%s」，例：%s" % (g["title"], rule, ex),
            })

    return qs


# --------------------------------------------------------------------------
def main():
    missing = [k for k, v in [("NOTION_TOKEN", TOKEN),
                              ("ARTICLES_PAGE_ID", ARTICLES_ID),
                              ("GRAMMAR_PAGE_ID", GRAMMAR_ID)] if not v]
    if missing:
        raise SystemExit("缺少環境變數：" + ", ".join(missing))

    print("讀取日文文法…", file=sys.stderr)
    grammar = collect_grammar(GRAMMAR_ID)
    print("  取得 %d 則文法" % len(grammar), file=sys.stderr)

    print("讀取日文文章…", file=sys.stderr)
    vocab = collect_vocab(ARTICLES_ID, skip_ids=[GRAMMAR_ID])
    print("  取得 %d 個標記詞" % len(vocab), file=sys.stderr)

    questions = build_questions(vocab, grammar)
    print("產生 %d 題" % len(questions), file=sys.stderr)

    if not questions:
        raise SystemExit("沒有產生任何題目，請確認 integration 已被加入這些頁面。")

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600)),
        "stats": {
            "grammar_points": len(grammar),
            "marked_terms": len(vocab),
            "questions": len(questions),
        },
        "questions": questions,
    }

    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("已寫入 %s" % OUT, file=sys.stderr)


if __name__ == "__main__":
    main()
