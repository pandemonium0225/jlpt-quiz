#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
從 Notion 抓「日文文章」與「日文文法」的內容，自動生成原句複習題庫。

產出：site/quiz.json

題型：
  vocab   語彙穴埋め   — 來源：文章頁面中使用者手動標記（粗體／背景色）的詞
  grammar 文法填空     — 來源：日文文法頁面的例句
  connect 接續方式     — 來源：日文文法頁面的接續方式表格

環境變數（也可寫在專案根目錄的 .env，已被 .gitignore 排除；環境變數優先）：
  NOTION_TOKEN      Notion internal integration token
  ARTICLES_PAGE_ID  「日文文章」父頁面 ID
  GRAMMAR_PAGE_ID   「日文文法」頁面 ID
"""

import hashlib
import json
import os
import re
import sys
import tempfile
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
TIMESTAMP = re.compile(r"^\s*[\[（]?\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\]）]?(?:\s*[–—-]+>?\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\]）]?)?\s*")
KANA_ONLY = re.compile(r"^[ぁ-んァ-ヴー]+$")
FURIGANA = re.compile(r"(?<=[一-龯々〆ヶ])（[ぁ-んァ-ヴー・\s]+）")
BLANK = "［　］"
TEXT_BLOCKS = {"paragraph", "bulleted_list_item", "numbered_list_item", "callout", "toggle"}
PAIRS = {"「": "」", "『": "』", "（": "）", "【": "】", "［": "］"}


def normalize_text(text):
    """統一括號與日文問驚號；保留小數、原有語意和句末標點。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(str.maketrans({"(": "（", ")": "）", "?": "？", "!": "！"})).strip()


def surface(text):
    """比較用文字：讀音、空白差異不應變成不同選項。"""
    return re.sub(r"\s+", "", FURIGANA.sub("", normalize_text(text)))


def balanced(text):
    stack = []
    for ch in text:
        if ch in PAIRS:
            stack.append(PAIRS[ch])
        elif ch in PAIRS.values():
            if not stack or stack.pop() != ch:
                return False
    return not stack


def sentences(text):
    """只在引號／括號外斷句，避免把對話的後半截變成題幹。"""
    stack, start = [], 0
    for i, ch in enumerate(text):
        if ch in PAIRS:
            stack.append(PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        if not stack and ch in "。！？\n":
            # 連續的問驚號留在同一句。
            if i + 1 < len(text) and text[i + 1] in "。！？":
                continue
            if text[start:i + 1].strip():
                yield text[start:i + 1].strip()
            start = i + 1
    if text[start:].strip():
        yield text[start:].strip()


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
    return normalize_text("".join(t.get("plain_text", t.get("text", {}).get("content", "")) for t in rich))


def block_rich(b):
    body = b.get(b.get("type"), {})
    return body.get("rich_text", []) or []


def block_text(b):
    return rt_plain(block_rich(b))


def is_marked(token):
    """使用者手動標記：粗體或非預設背景／文字色。"""
    a = token.get("annotations", {})
    return bool(a.get("bold")) or a.get("color", "default") != "default"


def marked_terms(rich, grammar=False):
    """抽出連續的標記片段，過濾掉時間戳之類的雜訊。"""
    terms, buf = [], ""
    for t in rich:
        if is_marked(t):
            buf += t.get("plain_text", t.get("text", {}).get("content", ""))
        else:
            if buf:
                terms.append(buf)
                buf = ""
    if buf:
        terms.append(buf)

    clean = []
    for term in terms:
        term = normalize_text(term).strip(" 、。，,。！？：:；;")
        # 先判斷原字串：「00:00:00 –> 00:00:35」若先切掉開頭時間戳，剩下的殘片會漏網
        if NOISE.match(term):
            continue
        term = TIMESTAMP.sub("", term).strip()
        if not term or NOISE.match(term) or DIGIT.search(term) or re.search(r"[〜～＋→：:]", term):
            continue
        plain = surface(term)
        if (len(plain) < 2 and not grammar) or not balanced(term) or re.search(r"[。！？\n]", term):
            continue
        if not grammar and len(plain) <= 3 and KANA_ONLY.match(plain):
            # 純假名的短片段多半是文法碎片（きゃ／のって／ずつ），不是詞彙
            continue
        clean.append(term)
    return clean


def strip_ts(text):
    return "\n".join(TIMESTAMP.sub("", line).strip() for line in normalize_text(text).splitlines()).strip()


SPEECH_END = re.compile(
    r"(?:です|ます|ました|ません|でした|でしょう|だろう|だった|ください|"
    r"ている|でいる|てる|でる|なった|かった|ない)(?:よね|ね|よ|か|かな)?$"
)
QUESTION_END = re.compile(r"(?:です|ます|でした|ました|ません|でしょう)か$")
NEXT_SENTENCE = r"(?:そして|それから|なので|でも|一方|もう|やっぱ|そりゃ|それは|その時|私[はも]|今日[はも]|明日[はも])"


def restore_punctuation(text, protected=()):
    """補逐字稿標點，只插入標點／整理分隔空白，不改詞彙與活用。

    以空白、換行、明確句尾與接續語推定句界；不在讀音、引號或標記答案內動手。
    無法細分的片段保留整段，不再因缺少標點淘汰。這不是語意重寫。
    """
    text = strip_ts(text)
    if not text or not balanced(text):
        return text
    guarded = set()
    stack = []
    for i, ch in enumerate(text):
        if ch in PAIRS:
            stack.append(PAIRS[ch])
        if stack:
            guarded.add(i)
        if stack and ch == stack[-1]:
            stack.pop()
    for term in protected:
        for match in re.finditer(re.escape(term), text):
            # 標記內的空白不是斷句依據。
            guarded.update(range(match.start() + 1, match.end()))

    edits = []
    for match in re.finditer(r"\s+", text):
        start, end = match.span()
        if start in guarded or not start or end == len(text):
            continue
        left, right = text[:start], text[end:]
        plain = FURIGANA.sub("", left)
        # 表情符號不影響前面的口語句尾辨識，顯示時保留。
        tail = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\ufe0f]+$", "", plain)
        if left[-1] in "。！？、：；」』" or right[0] in "。！？、：；」』":
            continue
        if re.search(r"[A-Za-z0-9]$", left) and re.match(r"[A-Za-z0-9]", right):
            continue  # 例如 Bite Size、4.5 %，不破壞外文與數值。
        if not re.search(r"[ぁ-んァ-ヴ一-龯]", plain):
            continue
        # 句尾後還接引用／條件等成分時，句子尚未結束。
        continues = re.match(r"(?:という|と言|と思|って|ので|のに|なら|けど|けれど)", right)
        explicit_end = re.search(r"(?:です|ます|ました|ません|でした|でしょう|ください)(?:よね|ね|よ|か)?$|(?:よね|ね|かな)$", tail)
        if SPEECH_END.search(tail) and not continues and (explicit_end or re.match(NEXT_SENTENCE, right)):
            separator = "？" if QUESTION_END.search(tail) else "。"
        elif re.search(r"(?:なくって|なくて|けど|けれど|ので|から|して|って|だし|あるし|たし|ますし|ですし|たら|なら|について)$", tail):
            separator = "、"
        else:
            separator = ""  # 助詞前後或詞內的排版空白直接接回。
        edits.append((start, end, separator))

    # 沒有空白時，只在明確句尾後緊接新句開頭的情況補句界。
    for match in re.finditer(r"(?:です|ます|ました|ません|でした)(?:よね|ね|よ|か)?(?=" + NEXT_SENTENCE + r")", text):
        at = match.end()
        if at not in guarded:
            edits.append((at, at, "？" if QUESTION_END.search(match.group()) else "。"))

    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    # 句末補點；已有標點、刪節號或接續符號時不重複加點。
    if (re.search(r"[ぁ-んァ-ヴ]", FURIGANA.sub("", text))
            and not re.search(r"[。！？、：；…→／＋][」』）]*$", text)
            and not re.search(r"[をにへがはも]$", text)):
        text += "？" if QUESTION_END.search(FURIGANA.sub("", text)) else "。"
    return text


def sentence_with(text, term, *, punctuated=False):
    """先補標點，再擷取含標記詞且括號完整的句子。"""
    text = text if punctuated else restore_punctuation(text, protected=[term])
    for s in sentences(text):
        # 「場所に」等仍缺後半句；補標點不能代替補造原文沒有的述語。
        if re.search(r"[をにへがはも]$", s):
            continue
        if term in s and len(s) > len(term) and balanced(s):
            return s
    return ""


# --------------------------------------------------------------------------
# 解析：日文文章（父頁面下的所有子頁面）
# --------------------------------------------------------------------------
def collect_vocab(parent_id, skip_ids):
    """回傳 [{term, sentence, hint, source}]"""
    items = []
    skip = {s.replace("-", "") for s in skip_ids}

    visited = set()

    def visit(pid, title, page_id):
        key = pid.replace("-", "")
        if key in skip or key in visited:
            return
        visited.add(key)
        blocks = children(pid)
        for i, b in enumerate(blocks):
            kind = b.get("type")
            if kind == "child_page":
                child_title = b["child_page"]["title"]
                print("  讀取文章：%s" % child_title, file=sys.stderr)
                visit(b["id"], child_title, b["id"])
                continue
            if b.get("has_children") and kind not in {"table", "child_database", "link_to_page"}:
                visit(b["id"], title, page_id)
            if kind not in TEXT_BLOCKS:
                continue
            rich = block_rich(b)
            terms = marked_terms(rich)
            if not terms:
                continue
            para = block_text(b)
            prepared = restore_punctuation(para, protected=terms)
            raw = "".join(t.get("plain_text", t.get("text", {}).get("content", "")) for t in rich)

            # 找緊接在後面的 📝 翻譯
            hint = ""
            for nxt in blocks[i + 1:]:
                if not block_text(nxt) and nxt.get("type") == "paragraph":
                    continue
                if nxt.get("type") == "quote":
                    q = block_text(nxt).strip()
                    if q.startswith("📝"):
                        hint = q.lstrip("📝").strip()
                break

            for term in terms:
                sent = sentence_with(prepared, term, punctuated=True)
                if not sent or sent.count(term) != 1:
                    continue
                context = surface(sent.replace(term, "", 1))
                # 詞條＋中文解釋、補充說明標籤不是日文例句。
                if len(re.findall(r"[ぁ-んァ-ヴ]", context)) < 3 or "：" in context:
                    continue
                # 整行都被標記（標題、目錄）挖空後沒有語境，無法作答
                if not sent.replace(term, "", 1).strip(" 　。、！？?!「」『』"):
                    continue
                items.append({
                    "term": term,
                    "sentence": sent,
                    "source_text": raw if prepared != strip_ts(para) else "",
                    "punctuation_restored": prepared != strip_ts(para),
                    "hint": hint,
                    "source": title,
                    "source_url": "https://www.notion.so/" + page_id.replace("-", "") + "#" + b["id"].replace("-", ""),
                })
    visit(parent_id, "日文文章", parent_id)
    return items


# --------------------------------------------------------------------------
# 解析：日文文法
# --------------------------------------------------------------------------
def clean_title(name):
    """去掉注音括號，並只留「：」之前的名稱（之後是說明文字）。"""
    name = FURIGANA.sub("", name)
    head = re.split(r"[：:]", name, maxsplit=1)[0].strip()
    return head or name.strip()


def collect_grammar(page_id, _pages=None):
    """回傳 [{title, rules:[(接続, 例)], examples:[(日文, 中文)]}]"""
    pages = set() if _pages is None else _pages
    key = page_id.replace("-", "")
    if key in pages:
        return []
    pages.add(key)
    entries, cur, section = [], None, ""

    def walk(pid, seen):
        key = pid.replace("-", "")
        if key in seen:
            return
        seen.add(key)
        for block in children(pid):
            yield block
            if block.get("has_children") and block.get("type") not in {"table", "child_page", "child_database"}:
                yield from walk(block["id"], seen)

    for b in walk(page_id, set()):
        t = b.get("type")
        if t == "child_page":
            entries.extend(collect_grammar(b["id"], pages))
            continue

        is_heading = bool(t) and t.startswith("heading_")

        # 文法點標題以「【」開頭；實際頁面是 heading_2，不依賴層級
        if is_heading and block_text(b).strip().startswith("【"):
            match = re.match(r"^【([^】]+)】", block_text(b).strip())
            name = clean_title(match.group(1)) if match else ""
            if not name:
                continue
            cur = {"title": name, "rules": [], "examples": [], "targets": {}, "source_texts": {},
                   "source_url": "https://www.notion.so/" + page_id.replace("-", "") + "#" + b["id"].replace("-", "")}
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
            else:
                section = ""
            continue

        if t == "table" and section == "rules":
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
            if t in TEXT_BLOCKS:
                jp = block_text(b).strip()
                if jp and re.search(r"[ぁ-んァ-ヴ]", FURIGANA.sub("", jp)):
                    targets = marked_terms(block_rich(b), grammar=True)
                    prepared = restore_punctuation(jp, protected=targets)
                    cur["examples"].append([prepared, ""])
                    cur["targets"][prepared] = targets
                    if prepared != strip_ts(jp):
                        cur["source_texts"][prepared] = "".join(
                            token.get("plain_text", token.get("text", {}).get("content", ""))
                            for token in block_rich(b))
            elif t == "quote" and cur["examples"]:
                zh = block_text(b).strip().lstrip("📝").strip()
                if zh and not cur["examples"][-1][1]:
                    cur["examples"][-1][1] = zh

    return [e for e in entries if e["rules"] or e["examples"]]


# --------------------------------------------------------------------------
# 出題
# --------------------------------------------------------------------------
def derive_keys(title):
    """從文法點標題推出句中會出現的關鍵字，例如「格助詞　から（起点）」→ ['から']。"""
    t = re.sub(r"（[^）]*）", "", normalize_text(title))
    m = re.search(r"「〜?([^」]+)」", t)
    if m:
        return [m.group(1)]
    if "　" in t:
        t = t.split("　", 1)[1]
    keys = []
    for part in re.split(r"／|\s+vs\.?\s+", t):
        part = part.strip().lstrip("〜～")
        if "＋" in part:
            part = part.split("＋")[-1]
        if part and not re.search(r"名詞|動詞|形容詞|辞書形|普通形|語幹|数量詞", part):
            keys.append(part)
    return list(dict.fromkeys(keys))


def category(title):
    return title.split("　")[0] if "　" in title else None


def grammar_family(entry):
    """有明確詞類標題就採用；否則僅用接續表可辨識的共同前接形式。"""
    explicit = category(entry["title"])
    if explicit:
        return explicit
    prefixes = [surface(rule).split("＋")[0] for rule, _ in entry["rules"]]
    if prefixes and all(p.startswith(("動詞", "可能形")) for p in prefixes):
        return "動詞接續"
    # 無法分類不代表同一類，避免將副詞、數量詞、句尾形式混在一起。
    return entry["title"]


def blank_sentence(sent, keys):
    """在句中挖掉關鍵字；關鍵字出現次數不是剛好一次就放棄，避免挖錯位置。
    比對時先把注音遮掉，才不會誤中讀音裡的假名。"""
    sent = normalize_text(sent)
    if not balanced(sent) or BLANK in sent:
        return None
    # 建立去讀音後的索引，挖空時連同答案的讀音一起移除。
    readings = list(FURIGANA.finditer(sent))
    hidden = {i for m in readings for i in range(m.start(), m.end())}
    positions = [i for i in range(len(sent)) if i not in hidden]
    plain = "".join(sent[i] for i in positions)
    keys = list(dict.fromkeys(surface(k) for k in keys if surface(k)))
    hits = [(m.start(), k) for k in keys for m in re.finditer(re.escape(k), plain)]
    # 例如「なければよかった」與「よかった」重疊時，以較完整者為準。
    hits = [(i, k) for i, k in hits if not any(
        j <= i and i + len(k) <= j + len(other) and len(other) > len(k)
        for j, other in hits)]
    if len(hits) != 1:
        return None
    i, k = hits[0]
    start, end = positions[i], positions[i + len(k) - 1] + 1
    for reading in readings:
        if reading.start() == end:
            end = reading.end()
    stem = sent[:start] + BLANK + sent[end:]
    if not balanced(stem) or not surface(stem.replace(BLANK, "")).strip("。、！？「」『』"):
        return None
    return stem, sent[start:end]


def word_form(term):
    """保守的表面形式分組，未知詞形不任意混入名詞選項。"""
    term = surface(term)
    if re.fullmatch(r"([ぁ-んァ-ヴー]{2,})\1", term):
        return "擬聲擬態"
    for ending in ("ください", "ばいい", "つつ", "でしょう", "だろう", "しよう", "みよう"):
        if term.endswith(ending):
            return ending
    for ending in ("ません", "ました", "ます", "ている", "でいる", "ていた", "でいた",
                   "てる", "でる", "ない", "たい", "する", "した", "して", "くて", "な"):
        if term.endswith(ending):
            return ending
    if re.fullmatch(r"[一-龯々ァ-ヴーA-Za-z・]+", term):
        return "名詞"
    if term.endswith("い"):
        return "い"
    if term.endswith(("う", "く", "ぐ", "す", "つ", "ぬ", "ぶ", "む", "る")):
        return "辞書形"
    if term.endswith(("て", "で")):
        return "て形"
    return None


def unique_options(options, answer):
    seen, result = {surface(answer)}, []
    for option in options:
        key = surface(option)
        if key and key not in seen and balanced(option):
            seen.add(key)
            result.append(option)
    return result


def short_key_supported(jp, key, rules):
    """短助詞須有接續表例子的相鄰文字佐證，避免挖到詞內假名。"""
    text, key = surface(jp), surface(key)
    if len(key) > 1:
        return True
    i = text.find(key)
    left, right = text[max(0, i - 2):i], text[i + len(key):i + len(key) + 2]
    for _, example in rules:
        example = surface(example)
        for match in re.finditer(re.escape(key), example):
            j = match.start()
            if (len(left) == 2 and example[max(0, j - 2):j] == left
                    or len(right) == 2 and example[j + len(key):j + len(key) + 2] == right):
                return True
    return False


def build_questions(vocab, grammar):
    qs = []
    instruction = "依 Notion 筆記的原句，選出填入［　］的內容。"

    def add(kind, label, stem, answer, pool, source, original, translation="", note="", source_url="", source_text=""):
        pool = unique_options(pool, answer)
        if len(pool) < 3 or not balanced(stem):
            return
        qs.append({
            "kind": kind, "label": label, "instruction": instruction if kind != "connect" else
            "依 Notion 接續表，選出下列例子對應的接續形式。",
            "stem": stem, "hint": "", "answer": answer, "pool": pool[:8],
            "source": source, "source_url": source_url,
            "original": original, "translation": translation, "note": note,
            "punctuation_restored": bool(source_text), "source_text": source_text,
        })

    # 不以字數相近代替詞形相近，也不為湊四選一混入未知詞形。
    for v in vocab:
        hit = blank_sentence(v["sentence"], [v["term"]])
        form = word_form(v["term"])
        if not hit or not form:
            continue
        peers = [w for w in vocab if word_form(w["term"]) == form
                 and abs(len(surface(w["term"])) - len(surface(v["term"]))) <= max(2, len(surface(v["term"])) // 2)
                 and surface(w["term"]) not in surface(v["sentence"])]
        peers.sort(key=lambda w: (w["source"] != v["source"],
                                  abs(len(surface(w["term"])) - len(surface(v["term"])))))
        same_source = [w for w in peers if w["source"] == v["source"]]
        if len(unique_options([w["term"] for w in same_source], hit[1])) >= 3:
            peers = same_source
        add("vocab", "語彙", hit[0], hit[1], [w["term"] for w in peers],
            v["source"], v["sentence"], v.get("hint", ""),
            "答案依據筆記原句；其他表達是否可用，仍需依語境判斷。", v.get("source_url", ""),
            v.get("source_text", ""))

    # 優先讀取例句標記；未標記時才從標題推導，且需唯一且完整的命中。
    blanks = []
    for g in grammar:
        for jp, zh in g["examples"]:
            marked = g.get("targets", {}).get(jp, [])
            keys = marked or derive_keys(g["title"])
            hit = blank_sentence(jp, keys)
            if hit and (marked or short_key_supported(jp, hit[1], g["rules"])):
                blanks.append((g, hit[0], hit[1], jp, zh))

    # 挖空後句子完全相同、正解卻不同（と／や、から／より 的對照例句），空格有兩個正解，整組捨棄
    answers_by_stem = {}
    for _g, stem, ans, _jp, _zh in blanks:
        answers_by_stem.setdefault(surface(stem), set()).add(surface(ans))
    blanks = [b for b in blanks if len(answers_by_stem[surface(b[1])]) == 1]

    # 只有實際成功挖過空的關鍵字才可當誘答（避免「名詞」這類從標題誤推的字）
    proven = {}
    for g, _s, ans, _jp, _zh in blanks:
        proven.setdefault(id(g), set()).add(ans)

    for g, stem, ans, jp, zh in blanks:
        cat = grammar_family(g)
        # 同類別的誘答；無法分類的文法點不任意互為誘答。
        peers = [h for h in grammar if h is not g and id(h) in proven and grammar_family(h) == cat]
        pool = list(proven[id(g)] - {ans}) + [k for h in peers for k in sorted(proven[id(h)])]
        pool = list(dict.fromkeys(k for k in pool if k != ans))
        # 常見可互換助詞不可互當錯誤選項；其他歧義仍以原句複習的指示限定。
        alternatives = ({"に", "へ"}, {"と", "や"}, {"から", "より"}, {"が", "の"})
        excluded = set().union(*(group for group in alternatives if surface(ans) in group))
        pool = [k for k in pool if surface(k) not in excluded]
        add("grammar", "文法", stem, ans, pool, g["title"], jp, zh,
            "文法點：" + g["title"] + "\n答案依據筆記原句，不代表其他表達在所有語境下都錯誤。",
            g.get("source_url", ""), g.get("source_texts", {}).get(jp, ""))

    # --- 接續方式 ---
    for g in grammar:
        # 誘答只取「其他文法點」的接續；同一文法點的其他接續也是正解，不能當誘答
        own = {surface(r) for r, _ in g["rules"]}
        others = []
        for h in grammar:
            if h is g:
                continue
            others += [r for r, _ in h["rules"] if surface(r) not in own and r not in others]

        for rule, ex in g["rules"]:
            # 相同例子對應不同規則時，沒有唯一可核對的答案。
            matches = {surface(r) for h in grammar for r, example in h["rules"]
                       if surface(example) == surface(ex)}
            if len(matches) != 1 or not ex:
                continue
            ranked = sorted(others, key=lambda r: (surface(r).split("＋")[0] != surface(rule).split("＋")[0],
                                                   abs(len(surface(r)) - len(surface(rule)))))
            add("connect", "接續", ex, rule, ranked, g["title"], ex,
                note="筆記記載的接續：" + rule, source_url=g.get("source_url", ""))

    # 內容修改、刪除由每次全量重建反映；穩定 ID 不依賴日期或抽題順序。
    result, seen, answers = [], set(), {}
    for q in qs:
        key = (q["kind"], surface(q["stem"]))
        answers.setdefault(key, set()).add(surface(q["answer"]))
    for q in qs:
        key = (q["kind"], surface(q["stem"]))
        if key in seen:
            continue
        if len(answers[key]) > 1:
            continue
        seen.add(key)
        q["id"] = hashlib.sha256(("|".join(key) + "|" + surface(q["answer"])).encode("utf-8")).hexdigest()[:16]
        result.append(q)
    return result


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
        raise SystemExit("沒有可用題目；請確認 Notion 分享權限、筆記格式及同類選項數量。未覆寫現有題庫。")

    payload = {
        "schema_version": 2,
        "generated_at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600)),
        "stats": {
            "grammar_points": len(grammar),
            "marked_terms": len(vocab),
            "questions": len(questions),
            "by_kind": {kind: sum(q["kind"] == kind for q in questions)
                        for kind in ("vocab", "grammar", "connect")},
        },
        "questions": questions,
    }

    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    # 讀取或產生失敗不覆蓋舊檔；完整寫入後才替換，避免讀到半份 JSON。
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(OUT),
                                         suffix=".tmp", delete=False) as f:
            temporary = f.name
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(temporary, OUT)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    print("已寫入 %s" % OUT, file=sys.stderr)


if __name__ == "__main__":
    main()
