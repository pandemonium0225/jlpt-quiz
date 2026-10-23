# 題庫業務流程與程式碼導讀

本文件整理本專案從 Notion 筆記、候選題生成、AI Batch 審核，到發布題庫與瀏覽器作答的 18 項流程。

- 對照版本：`96f060c`，2026-10-20。
- 預設審核模式：GPT-6.1 Sol、medium reasoning、Batch API。
- 標示為「流程示意」的程式片段省略部分參數、欄位與錯誤處理，用於導讀，不是可直接取代原檔的完整程式。
- 標示為「歷史實例」的題目與判定來自本機保存的素材與舊模型快取；不代表新模型已完成本輪審核，也不代表目前線上題庫狀態。
- 範例中的價格、預算與排程是上述版本的程式設定，日後以實際程式碼為準。

題目網站：<https://pandemonium0225.github.io/jlpt-quiz/>。

## 目錄

1. [執行環境](#1-執行環境)
2. [主程式入口與整體順序](#2-主程式入口與整體順序)
3. [設定與狀態檔](#3-設定與狀態檔)
4. [讀取與解析 Notion](#4-讀取與解析-notion)
5. [規則生成候選題](#5-規則生成候選題)
6. [題目的四種識別](#6-題目的四種識別)
7. [沿用審核快取與誘答池](#7-沿用審核快取與誘答池)
8. [建立 AI 盲審輸入](#8-建立-ai-盲審輸入)
9. [六項模型校驗](#9-六項模型校驗)
10. [新題排隊與預算預留](#10-新題排隊與預算預留)
11. [上傳與建立 Batch](#11-上傳與建立-batch)
12. [接收結果與結算費用](#12-接收結果與結算費用)
13. [篩選題目與誘答](#13-篩選題目與誘答)
14. [固定每批題庫快照](#14-固定每批題庫快照)
15. [版本比較與寫出題庫](#15-版本比較與寫出題庫)
16. [Actions 保存進度與部署](#16-actions-保存進度與部署)
17. [瀏覽器抽題與作答](#17-瀏覽器抽題與作答)
18. [本機操作與重現](#18-本機操作與重現)

## 1. 執行環境

業務跨越三個執行環境：

```text
本機／GitHub Actions
    執行 Python
    讀取 Notion、產生題目、提交或接收 Batch、寫出題庫

OpenAI
    在背景執行已提交的 Batch
    保存批次狀態與審核結果

使用者瀏覽器
    讀取已發布的 quiz.json
    抽題、顯示選項、判分、顯示解答
```

| 檔案 | 負責的業務 |
|---|---|
| [scripts/build_quiz.py](../scripts/build_quiz.py) | 主流程、Notion 解析、候選題生成、寫出題庫 |
| [scripts/batch_review.py](../scripts/batch_review.py) | Batch 排隊、提交、查詢、接收、費用結算 |
| [scripts/ai_review.py](../scripts/ai_review.py) | 共用審題規則、校驗案例、快取、題目篩選，也保留舊即時模式 |
| [scripts/quiz_report.py](../scripts/quiz_report.py) | 題庫版本比較、診斷與報告 |
| [scripts/find_workflow_state.cjs](../scripts/find_workflow_state.cjs) | Actions 找回上次題庫與最新審題進度 |
| [.github/workflows/deploy.yml](../.github/workflows/deploy.yml) | 排程、測試、保存進度、部署 |
| [site/app.js](../site/app.js) | 網站抽題、作答、判分與回看 |

本機 Python 是「執行一次、處理一次、然後結束」的程式，不會因為終端機開著就每天執行。每天台灣時間早上 7 點的自動執行由 GitHub Actions 排程負責；GitHub 排程可能延遲。

## 2. 主程式入口與整體順序

來源：[build_quiz.main()](../scripts/build_quiz.py#L1094)。

```bash
python scripts/build_quiz.py --ai-review
```

`--ai-review` 啟用 AI 審題，目前預設選擇 Batch。沒有這個參數時，只進行規則生成與報告，不呼叫 AI 審題。

以下是 **AI Batch 路徑的流程示意**，不是完整的原始碼：

```python
def main(args):
    previous = read_bank(args.previous or args.output)

    reviewer = BatchReviewer(
        state_path=args.ai_state,
        run_budget=args.ai_run_budget,
        month_budget=args.ai_month_budget,
    )

    frozen = reviewer.state.get("candidate_snapshot")

    if frozen:
        # 上一批尚未處理完：完成當時的題庫。
        materials = frozen["materials"]
        questions = frozen["questions"]
    else:
        # 實際程式也能用 --from-snapshot 讀取離線素材。
        grammar = collect_grammar(GRAMMAR_ID)
        vocab = collect_vocab(ARTICLES_ID, skip_ids=[GRAMMAR_ID])
        materials = {"grammar": grammar, "vocab": vocab}
        questions = build_questions(vocab, grammar)

    # 先接收舊 Batch，再檢查六個模型校驗案例。
    calibrated = reviewer.calibrate()

    if not calibrated:
        reviewer.run_budget = 0
    elif not frozen:
        reviewer.stage_snapshot({
            "materials": materials,
            "questions": questions,
            # 實際還包含 events 與 generated_at。
        })

    accepted_questions = reviewer.filter_questions(questions, diagnostics)
    reviewer.flush()

    if reviewer.stats["error"]:
        raise ValueError(reviewer.stats["error"])

    if reviewer.stats["pending"] > 0 or not calibrated:
        write_pending_report()
        return

    if not accepted_questions:
        raise ValueError("沒有可發布的題目")

    reviewer.complete_initial_review()
    write_success_report()
    write_bank(args.output, payload_with(accepted_questions))
    reviewer.finish_snapshot()
    mark_ready_true_if_running_in_actions()
```

`write_pending_report()`、`write_success_report()`、`payload_with()` 與 `mark_ready_true_if_running_in_actions()` 是此處對數行原始碼的描述性代稱；原檔沒有這些同名函式。

發布條件是：

```text
全部候選題都有判定
AND 六項模型校驗通過
AND 沒有審核錯誤
AND 至少有一道合格題目
```

「都有判定」包含通過與排除，不要求每道候選題都通過。Batch 完成也不等於題庫立刻可發布，還要通過以上檢查。

## 3. 設定與狀態檔

來源：[load_dotenv()](../scripts/build_quiz.py#L45)、[Reviewer.__init__()](../scripts/ai_review.py#L163)。

本機可透過 `.env` 提供設定；Actions 使用 Secrets 與 Variables：

```text
NOTION_TOKEN
ARTICLES_PAGE_ID
GRAMMAR_PAGE_ID
OPENAI_API_KEY
```

`load_dotenv()` 使用 `os.environ.setdefault()`，已存在的環境變數優先，不會被 `.env` 覆蓋。憑證不寫進題庫、報告或版本控制。

預設檔案位置：

```text
site/quiz.json
    本機題庫輸出

.build/ai-state/cache.json
    AI 判定、月費帳本、Batch 工作、待完成的題庫快照

.build/review/materials.json
    已解析的 Notion 素材

.build/review/report.json
    完整更新報告

.build/review/report.md
    人可以閱讀的摘要
```

`cache.json` 的主要欄位如下，這裡僅展示結構：

```python
state = {
    "state_version": 1,
    "cache": {},       # 審題請求雜湊 → verdict、model、policy
    "months": {},      # UTC 月份 → 已結算用量與未完成預留
    "pools": {},       # 題目識別 → 先前審過的誘答池
    # 以下欄位按執行進度出現：
    # initial_review_completed_at
    # candidate_snapshot
    # batch
    # last_batch
}
```

關閉終端機後，下次仍可透過這份檔案續跑。狀態檔損壞時停止，避免把用量當成零而重複付費。

## 4. 讀取與解析 Notion

來源：[api_get() 與 children()](../scripts/build_quiz.py#L128)、[collect_vocab()](../scripts/build_quiz.py#L319)、[collect_grammar()](../scripts/build_quiz.py#L402)。

兩個來源入口不同，文法頁位於文章父頁之下：

```text
日文文章（ARTICLES_PAGE_ID）
├── Podcast 文章 A
├── Podcast 文章 B
└── 日文文法（GRAMMAR_PAGE_ID）
    ├── 【格助詞 を】
    └── 【格助詞 に】
```

讀文章時跳過文法頁，避免將文法內容再次當作文章語彙解析：

```python
grammar = collect_grammar(GRAMMAR_ID, diagnostics=diagnostics)
vocab = collect_vocab(
    ARTICLES_ID,
    skip_ids=[GRAMMAR_ID],
    diagnostics=diagnostics,
)
```

取得區塊時使用 Notion Blocks API：

```text
GET /v1/blocks/{block_id}/children?page_size=100
```

`children()` 的分頁順序是：

```python
out, cursor = [], None

while True:
    path = "/blocks/%s/children?page_size=100" % block_id.replace("-", "")
    if cursor:
        path += "&start_cursor=" + cursor

    data = api_get(path)
    out.extend(data.get("results", []))

    if not data.get("has_more"):
        break

    cursor = data.get("next_cursor")
```

Notion 的部分暫時錯誤有有限次重試；這與付費 Batch 建立逾時時禁止盲目重送，是兩種不同處理。

### 文章解析實例

程式找出手動標粗體或上色的片段，過濾時間戳、數字、模板與過短的片段，再擷取原句與緊接的翻譯。

以下是現有測試形式的最小輸入，已由 `marked_terms()` 離線實跑：

```python
rich_text = [
    {"plain_text": "[0:43] ", "annotations": {"bold": True}},
    {"plain_text": "子猫", "annotations": {"bold": True}},
]

marked_terms(rich_text)
# ["子猫"]
```

文章資料處理順序：

```text
遞迴讀取文章與文字區塊
→ 抽出手動標記詞
→ 移除時間戳、必要時依規則補標點
→ 找到完整且唯一包含該詞的例句
→ 尋找緊接的 📝 翻譯
→ 保存來源連結與區塊識別
```

例如只有詞條和中文說明、挖空後沒有日文語境的片段，會留下診斷原因並略過。程式不回寫 Notion。

### 文法解析實例

程式以 `【文法點】` 標題，以及「接續方式」「例句／例文」等區段辨識內容，不是把每個表格都當成接續表。

實際保存的素材節錄：

```python
{
    "title": "格助詞　を",
    "rules": [
        [
            "名詞（めいし）＋を＋他動詞（たどうし）",
            "コーヒーを飲（の）む",
        ],
        [
            "名詞（めいし）（起点・きてん）＋を＋移動動詞（いどうどうし）",
            "電車（でんしゃ）を降（お）りる",
        ],
    ],
    "examples": [
        ["コーヒーを飲（の）みます。", "喝咖啡。"],
        ["電車（でんしゃ）を降（お）ります。", "下電車。"],
    ],
    "targets": {
        "コーヒーを飲（の）みます。": [],
        "電車（でんしゃ）を降（お）ります。": [],
    },
}
```

`targets` 是例句的手動標記考點。沒有標記時，後續嘗試從文法標題推導。

作為資料量參考，2026-10-20 14:00 的本機素材快照有 23 個文法點、83 個文法例句、80 列接續表，以及 359 筆文章標記詞素材。這些數字是歷史快照，單位也不等同於最終題數。

## 5. 規則生成候選題

來源：[build_questions()](../scripts/build_quiz.py#L844)、[blank_sentence()](../scripts/build_quiz.py#L737)。

題目先由規則生成，再交給 AI 審核：

| 顯示名稱 | `kind` | 生成方式 |
|---|---|---|
| 語彙 | `vocab` | 文章標記詞挖空，同類詞當誘答 |
| 文法 | `grammar` | 例句的文法考點挖空 |
| 用法 | `connect` | 根據接續表，詢問助詞在例子裡的用途 |
| 活用 | `connect` | 根據變形配對或動詞切詞產生活用題 |

語彙誘答會比較表面詞形與長度，優先選同篇文章；文法誘答需要有同類文法依據。需要動詞切詞的活用題使用 `fugashi` 與 `unidic-lite`，缺少套件時會略過這類題目。

### 挖空與讀音

實際呼叫：

```python
blank_sentence("コーヒーを飲（の）みます。", ["を"])
# ("コーヒー［　］飲（の）みます。", "を")

blank_sentence("申請（しんせい）をします。", ["申請"])
# ("［　］をします。", "申請（しんせい）")
```

第二個例子會連讀音一起挖掉，避免題幹留下答案的讀音提示。

咖啡題的候選資料節錄如下：

```python
question = {
    "kind": "grammar",
    "label": "文法",
    "instruction": "依 Notion 筆記的原句，選出填入［　］的內容。",
    "stem": "コーヒー［　］飲（の）みます。",
    "answer": "を",
    "pool": ["に", "で", "が", "へ", "と", "から", "より", "の"],
    "source": "格助詞　を",
    "original": "コーヒーを飲（の）みます。",
    "translation": "喝咖啡。",
    "basis": "title_derived",
    "id": "2c7dd2c358049b1a",
    # 另有來源連結、來源區塊識別、版本與說明欄位。
}
```

`pool` 是誘答候選池，此時還沒保證每個誘答都可靠。每題最多先帶 8 個誘答送審，截斷前的完整候選放在 `pool_candidates`，供舊誘答池沿用檢查使用。

生成階段就會略過以下情況：

```text
考點出現不只一次，無法唯一挖空
挖空後沒有語境
題幹括號不完整
不重複的合適誘答不足三個
同一題幹對應不同正解
重複的題目
```

常見可互換助詞與筆記已接受的活用形式，也有規則預先排除，但仍需 AI 檢查其他語意歧義。

## 6. 題目的四種識別

來源：[候選題識別生成](../scripts/build_quiz.py#L1057)、[revision()](../scripts/quiz_report.py#L64)、[Batch cache_key()](../scripts/batch_review.py#L34)。

| 識別 | 用途 |
|---|---|
| `origin_key` | 追蹤來源 Notion 區塊與考點，供新舊題配對 |
| `id` | 根據題型、正規化題幹與答案產生題目識別 |
| `revision` | 偵測題目內容、選項、原句、翻譯或解說等是否改變 |
| AI `cache_key` | 判斷審題請求能否沿用既有判定 |

```text
只修改中文翻譯
    → revision 改變
    → AI 可見內容不變，cache_key 可以維持相同

只重排選項順序
    → revision 與 cache_key 都不會因順序而改變

修改題幹或送審選項內容
    → cache_key 改變，需要新的判定
```

`revision` 會把誘答池排序後比較。AI 快取識別則包含政策版本、模型、提示、輸出 schema 與實際請求內容，因此「內容修改」與「是否需要付費重審」是不同判斷。

## 7. 沿用審核快取與誘答池

來源：[BatchReviewer._cached()](../scripts/batch_review.py#L129)、[Reviewer._with_reviewed_pool()](../scripts/ai_review.py#L229)。

快取查詢的流程示意：

```python
cached = state["cache"].get(batch_cache_key(question))
if cached:
    return cached

old = state["cache"].get(legacy_cache_key(question))
if (
    old
    and old.get("model") == "gpt-5.6-sol"
    and old.get("policy") == POLICY_VERSION
):
    return old

return None
```

本次 GPT-5.6 Sol → GPT-6.1 Sol 遷移明確允許沿用同政策、同可見內容的舊 Sol 判定；題目仍保留真正審核模型名稱。新模型的六項校驗不適用這項沿用。

舊誘答池的判斷示意：

```python
old_pool = state["pools"].get(pool_key(question))
available = set(question.get("pool_candidates", question["pool"]))

if old_pool and set(old_pool) <= available:
    # 實際程式還會檢查是否有必要替換。
    question = dict(question, pool=list(old_pool))
```

例如：

```python
# 原本審過：
old_pool = ["から", "に", "が", "と"]

# 新詞擠進前幾名：
new_pool = ["へ", "から", "に", "が"]

# 但完整候選裡仍保有舊選項：
pool_candidates = ["へ", "から", "に", "が", "と"]
```

這時可以繼續使用舊池並命中快取，省下一次整題重審。若舊選項已被刪除、修改或規則排除，就不能沿用原池。

被排除的題目也保存判定，避免每天為同一份壞題付費。相應代價是：誘答不足而被排除的舊題，不會僅因新詞加入而自動重新嘗試。

## 8. 建立 AI 盲審輸入

來源：[review_input()](../scripts/ai_review.py#L81)、[Batch request_body()](../scripts/batch_review.py#L27)。

目前共用的可見輸入轉換為：

```python
def review_input(question):
    return {
        "type": question["label"],
        "instruction": question["instruction"],
        "stem": question["stem"],
        "visible_context": (
            question["source"] if question["label"] == "用法" else ""
        ),
        "options": sorted(set([question["answer"]] + question["pool"])),
    }
```

咖啡題的實際轉換結果：

```json
{
  "type": "文法",
  "instruction": "依 Notion 筆記的原句，選出填入［　］的內容。",
  "stem": "コーヒー［　］飲（の）みます。",
  "visible_context": "",
  "options": ["から", "が", "で", "と", "に", "の", "へ", "より", "を"]
}
```

AI 看得到所有選項，但沒有正解身分、原句、中文翻譯或作答後解說。用法題會提供文法點，因為前端作答時也會顯示該資訊。

審題請求的結構節錄如下，schema 細節省略：

```python
body = {
    "model": "gpt-6.1-sol",
    "store": False,
    "reasoning": {"effort": "medium"},
    "max_output_tokens": 6000,
    "input": [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": json.dumps(visible_question)},
    ],
    "text": {
        "format": {
            "type": "json_schema",
            "name": "japanese_quiz_review",
            "strict": True,
            "schema": SCHEMA,
        }
    },
}
```

每個選項必須回傳以下其中一個判定及簡短理由：

```text
valid      有一般合理的日文解讀
invalid    可指出具體接續錯誤或與明示語境矛盾
uncertain  無法明確排除，不能充當可靠錯誤選項
```

AI 的工作是檢查題意與全部選項；原句與正解由筆記及程式規則決定，不由 AI 改寫。

## 9. 六項模型校驗

來源：[calibration_cases()](../scripts/ai_review.py#L54)、[BatchReviewer.calibrate()](../scripts/batch_review.py#L189)。

校驗涵蓋起點替代、肯定否定皆成立、比況時態、正確て形、變形指示不足與語彙多解。

```python
def calibrate():
    collect()  # 先接收前一個 Batch 的結果。
    complete = True

    for case in calibration_cases():
        verdict = review(case.question)

        if verdict is None:
            complete = False
            continue

        if not matches_expected_verdict(case, verdict):
            raise ValueError("模型未通過校驗")

    return complete
```

此處 `case.question` 和 `matches_expected_verdict()` 是導讀用代稱；原程式使用 tuple 與直接比較。

例如其中一題的可見內容及預期限制：

```python
question = "電車［　］降ります。"
options = ["を", "から", "に", "が"]

expected = {
    "を": "valid",
    "から": "valid",
}
```

模型若把「から」誤判為必然錯誤，就不能通過這項校驗。預期答案不傳給模型。

新模型校驗也走 Batch，必須取得自己的六項結果。校驗未齊時，主程式會設定：

```python
reviewer.run_budget = 0
```

這會阻止本輪再將新候選題排入付費審核；先前已排好的校驗請求仍可由 `flush()` 提交。校驗未過時不發布，也不自動改回較貴模型或即時 API。

## 10. 新題排隊與預算預留

來源：[BatchReviewer._prepare_review()](../scripts/batch_review.py#L152)。

以下是流程示意：

```python
cached = lookup_cache(question)
if cached:
    return cached

if an_existing_batch_is_already_submitted():
    return None

if this_question_is_already_queued():
    return None

reserve = calculate_reservation(question)
if run_budget_is_insufficient() or month_budget_is_insufficient():
    return None

state["batch"]["requests"][key] = {
    "body": request_body(question),
    "reserve": reserve,
    "month": current_utc_month,
    "policy": POLICY_VERSION,
    "prices": [INPUT_PRICE, CACHED_PRICE, WRITE_PRICE, OUTPUT_PRICE],
}

state["months"][current_utc_month] += reserve
save_state()
return None
```

上面的查詢與判斷函式是描述性代稱。實際程式會先驗證輸入長度，並以請求雜湊去重。

此時 `None` 表示尚未取得判定，不代表題目被判錯。Batch 路徑的 `_prepare_review()` 只排隊，不會逐題呼叫即時 Responses API。

以咖啡題為例，當它沒有可沿用快取、需要重新送審時，本機算得：

```python
request_bytes = 2862
max_output_tokens = 6000

reserve = (
    (2862 + 2048) * 1.25
    + 6000 * 5.0
) / 1_000_000

# US$0.0361375
```

這是程式按輸入 bytes 加餘量、最貴輸入寫入價與最大輸出長度計算的保守預留，不是實際扣款。費用與請求在送出前一起保存；等待中不重複預留同一請求。

`ai_review.py` 中仍有舊即時模式的最多三個並行請求限制；Batch 則將預算容許的待審請求編成一批，不是每批限制三題。

## 11. 上傳與建立 Batch

來源：[BatchReviewer.flush()](../scripts/batch_review.py#L209)、[BatchAPI](../scripts/batch_review.py#L39)。

`flush()` 才負責將已排隊的請求送出。流程節錄：

```python
rows = [
    {
        "custom_id": key,
        "method": "POST",
        "url": "/v1/responses",
        "body": request["body"],
    }
    for key, request in state["batch"]["requests"].items()
]

file_id = api.upload(rows)
batch = api.create(file_id, state["batch"]["submission"])

state["batch"]["id"] = batch["id"]
state["batch"]["status"] = batch["status"]
save_state()
```

程式目前使用 Python 標準函式庫 `urllib` 呼叫 API，在記憶體中組出 JSONL 內容後以 multipart 上傳，沒有先寫出本機 `requests.jsonl`，也不需要安裝 OpenAI SDK。

```text
POST /v1/files
    上傳請求檔，purpose=batch

POST /v1/batches
    指定 input_file_id、/v1/responses、24h 處理窗口

GET /v1/batches/{batch_id}
    後續查詢狀態

GET /v1/files/{file_id}/content
    下載結果
```

本地與遠端狀態大致流轉如下：

```text
prepared → uploaded → submitting
                          ↓
                    validating
                          ↓
                    in_progress
                          ↓
                    finalizing
                          ↓
                     completed
```

前三個狀態用來保存本地提交進度。實際程式在上傳後保存 `file_id`，在建立批次前先保存 `submitting` 與唯一提交識別。

建立請求若逾時，下次利用 metadata 中的 `jlpt_submission` 查找既有批次。查不到就停止並保留預留額，避免盲目建立第二個可能重複計費的批次。

## 12. 接收結果與結算費用

來源：[BatchReviewer.collect()](../scripts/batch_review.py#L229)。

以下是流程示意：

```python
batch = api.retrieve(saved_batch_id)

if batch["status"] not in TERMINAL:
    save_current_status()
    return

rows = download_output_and_error_files(batch)

for row in rows:
    request = saved_requests[row["custom_id"]]
    settle_original_reservation(request, row)
    validate_response(row)
    save_successful_verdict(row)

atomic_save_state()
```

Batch 結果不依賴排列順序，使用 `custom_id` 配對；未知或重複的識別會讓流程停止。

成功判定與結算會一併寫入狀態檔，避免下次重新結算。跨月取回的結果，仍調整原提交月份的帳本。

假設一個回覆的用量是：

```python
input_tokens = 2000
cached_tokens = 1000
cache_write_tokens = 0
output_tokens = 1000
```

按此版本程式的 Batch 價格表結算：

```python
actual = (
    1000 * 1.0     # 一般輸入
    + 1000 * 0.05  # 快取讀取
    + 0 * 1.25     # 快取寫入
    + 1000 * 5.0   # 輸出
) / 1_000_000

# US$0.00605

month_total += actual - original_reserve
```

這組 token 數是計費示例，不是咖啡題的新模型實測用量。回覆缺少用量時會保留預留額。

部分請求失敗、回覆不完整或結構不合法時，已完成的成功判定仍保存，但工作標為 `blocked`，等待檢查後處理；不無限自動重試花費。`last_batch` 保存最近一次結算摘要。

## 13. 篩選題目與誘答

來源：[Reviewer.filter_questions()](../scripts/ai_review.py#L312)。`BatchReviewer` 繼承這份共用篩選邏輯，覆寫取得判定的方式。

實際判斷條件可濃縮成：

```python
safe_pool = [
    option
    for option in question["pool"]
    if decisions[option]["verdict"] == "invalid"
]

reject = (
    not verdict["question_clear"]
    or decisions[question["answer"]]["verdict"] != "valid"
    or len(safe_pool) < 3
)
```

只有被明確判為錯誤的選項，才能留下當誘答。`valid` 與 `uncertain` 都不適合作為唯一正解題的可靠錯誤選項。

### 歷史實例 A：咖啡題通過，但縮小誘答池

以本機保存的 GPT-5.6 Sol 歷史判定離線執行現行篩選邏輯：

```python
# 規則產生的原始誘答池：
["に", "で", "が", "へ", "と", "から", "より", "の"]

# 舊判定中，同樣可成立而被移除的誘答：
["で", "から", "より"]

# 篩選後留下：
["に", "が", "へ", "と", "の"]
```

這裡是在解釋程式如何使用已保存的判定，不把歷史模型的理由當作新的日文語法定論。

輸出節錄：

```python
{
    "id": "2c7dd2c358049b1a",
    "revision": "e1af68b46bb031e9",
    "stem": "コーヒー［　］飲（の）みます。",
    "answer": "を",
    "pool": ["に", "が", "へ", "と", "の"],
    "ai_review": {
        "model": "gpt-5.6-sol",
        "policy": "ja-quiz-review-v3",
    },
}
```

實際審核模型仍標為 GPT-5.6 Sol，沒有因切換預設模型就改標。

### 歷史實例 B：多解題被排除

另一道實際素材：

```python
stem = "はい、それでは、［　］。"
answer = "さっそく"
```

舊判定認為多個其他選項也能形成合理發話，因此剩不下三個明確錯誤誘答，整題被排除。

這兩題合併離線實跑的統計：

```python
{
    "calls": 0,
    "cache_hits": 2,
    "legacy_cache_hits": 2,
    "accepted": 1,
    "rejected": 1,
    "trimmed_options": 3,
    "run_usd": 0.0,
}
```

這表示這次沿用以前取得的判定，沒有發出新的付費請求；不代表歷史審核從未收費。

## 14. 固定每批題庫快照

來源：[stage_snapshot() 與 finish_snapshot()](../scripts/batch_review.py#L113)、[main() 的 frozen 分支](../scripts/build_quiz.py#L1140)。

校驗通過後，程式保存當批素材與候選題：

```python
state["candidate_snapshot"] = {
    "materials": materials,
    "questions": questions,
    "events": list(diagnostics.events),
    "generated_at": payload["generated_at"],
}
```

例如今天有 10 題新題正在審，明天又新增 5 題筆記，後續執行順序是：

```text
完成今天這份包含 10 題新題的題庫快照
→ 審完並寫出這批題庫
→ 清除 candidate_snapshot
→ 後續執行再讀取最新 Notion，處理新加入的 5 題
```

否則每天有新題，就可能永遠存在待審題而無法發布。這裡的題數是假設例子，並假設有足夠預算。

有快照時，程式優先讀快照，不重新取最新 Notion；等待中的筆記修改與刪除也會延到後續批次反映。`generated_at` 保留當批來源時間。

## 15. 版本比較與寫出題庫

來源：[compare_banks()](../scripts/quiz_report.py#L69)、[發布判斷](../scripts/build_quiz.py#L1192)、[write_bank()](../scripts/build_quiz.py#L1079)。

新舊題依序嘗試以下識別配對；只有無歧義的唯一配對才採用：

```python
origin_key
id
(kind, label, stem)
```

比較後產生：

```python
{
    "added": [],
    "removed": [],
    "changed": [],
    "unchanged": 123,
}
```

此處 `123` 是結構示意。咖啡題縮小誘答池後，離線實跑結果為：

```python
{
    "added": 0,
    "removed": 0,
    "changed": 1,
    "changed_fields": [["pool"]],
}
```

```text
id 保持：2c7dd2c358049b1a

revision：
    05b54e66a7b50e58
    → e1af68b46bb031e9
```

題幹與正解不變，因此題目識別保持，內容版本則因誘答池修改而改變。

發布前會分成三種結果：

| 報告狀態 | 觸發條件 | 題庫處理 |
|---|---|---|
| `pending` | 尚有待審題，或校驗未全部完成，且沒有審核錯誤 | 保存進度與報告，保留原題庫 |
| `failed` | API／校驗／資料錯誤，或沒有任何合格題目 | 輸出失敗報告並停止 |
| `success` | 校驗完成、沒有待審或錯誤，且至少有一題合格 | 寫出新的題庫 |

待審時不做完整差異比較，避免把尚未審完的題目誤報為消失。診斷事件數也不等於淘汰題數，同一素材可能有多個事件。

輸出題庫的結構是：

```python
payload = {
    "schema_version": 2,
    "generated_at": source_time,
    "stats": stats,
    "questions": accepted_questions,
}
```

寫出前會更新題目 `revision`、移除內部用的 `pool_candidates`。單份 JSON 採同目錄暫存檔加 `os.replace()` 寫入，避免一般寫入中斷留下半份檔案。題庫寫出後才清除候選快照。

## 16. Actions 保存進度與部署

來源：[deploy.yml](../.github/workflows/deploy.yml)、[find_workflow_state.cjs](../scripts/find_workflow_state.cjs)。

本機執行成功代表本機的輸出檔更新；發布至 GitHub Pages 是另一個 Actions 工作。

```text
排程／手動執行／推送 main
→ checkout、安裝相依
→ Python 與 JavaScript 測試
→ 找回已發布基準與最新 AI 進度
→ 執行 build_quiz.py
→ 保存報告與 AI 進度
→ ready=true 時上傳候選題庫與 Pages 檔案
→ 部署 Pages
→ 部署成功後保存新基準
```

Actions 模式開始時先寫 `ready=false`，主程式完整成功後才追加 `ready=true`。`pending` 可以讓建置正常結束，但會略過部署，因此 Actions 綠勾不等於網站已更新。

| Artifact | 內容 | 保存時間 |
|---|---|---:|
| `quiz-ai-state-*` | 快取、用量、Batch、題庫快照 | 90 天 |
| `quiz-review-*` | 更新報告與解析素材 | 30 天 |
| `quiz-candidate-*` | 本次準備部署的題庫 | 7 天 |
| `quiz-baseline-*` | 成功部署的題庫，供下次比較 | 90 天 |

下次執行分開選取兩份狀態：

```text
舊題庫基準：最近成功部署的版本
AI 進度：最近保存的進度，包含失敗或待審執行
```

並行群組設定不會為新執行取消正在跑的執行，讓已送出的請求有機會保存結果與費用。

目前查詢 Batch 的時機只有每日台灣時間 7 點排程、手動 Run workflow，或推送 `main` 觸發的執行。沒有常駐輪詢或 Batch 完成即時通知。例如上午 8 點完成，若未手動執行，通常要到隔天早上的排程才會發現。

## 17. 瀏覽器抽題與作答

來源：[site/app.js](../site/app.js#L224)。

瀏覽器載入已發布的靜態題庫：

```javascript
fetch('quiz.json?v=' + Date.now())
  .then(response => response.json())
  .then(data => {
    BANK = data.questions.filter(validQuestion);
    startRound();
  });
```

原程式還會檢查 HTTP 狀態、更新題庫資訊，並顯示空題庫或讀取失敗訊息。

每輪最多 10 題：

```javascript
const PER_ROUND = 10;
```

抽題規則是：

```text
打散各題型
→ 從 vocab、grammar、connect 三類輪流取題
→ 同一原句一輪只出一次
→ 每題從合格誘答池挑三個
→ 正解與誘答一起打散
```

`connect` 同時包含用法與活用，抽題並非按全題庫各類數量比例進行。

咖啡題的一次示意抽取：

```javascript
const q = {
  answer: 'を',
  pool: ['に', 'が', 'へ', 'と', 'の']
};

// 假設這次隨機挑到三個誘答，再打散：
const opts = ['へ', 'を', 'の', 'に'];
const ans = opts.indexOf(q.answer);
// ans === 1；索引從 0 開始。
```

作答時記錄選項索引：

```javascript
picks[cursor] = selectedIndex;
const correct = picks[cursor] === current[cursor].ans;
```

接著顯示正解、原句、中文翻譯、解說與來源連結。漢字讀音轉為 ruby 顯示；筆記文字先做 HTML 跳脫。

判分由 JavaScript 比對索引。按「換一組題目」只從已載入的題庫重抽，不會重新讀取 Notion 或呼叫 OpenAI。作答與成績保留在當頁的記憶體中。

## 18. 本機操作與重現

以下命令在專案根目錄執行。假設已有 `.venv` 並安裝專案相依；憑證放在被忽略的 `.env` 或環境變數中。

### 啟用 AI Batch 更新

```bash
.venv/bin/python scripts/build_quiz.py \
  --ai-review \
  --ai-mode batch \
  --ai-state .build/ai-state/cache.json \
  --output site/quiz.json
```

下次執行相同指令會讀取原狀態並續跑。本機預設單次 US$1、每個 UTC 月 US$5。

Actions 使用 `--ai-no-run-limit`，停用單次上限，但保留月額；此版本對 2026 年 10 月採已核准的 US$7，11 月起恢復 US$5。本機與 CI 不會自動共享同一份狀態檔與帳本。

### 只重播素材，檢查規則生成

先取得一份 `materials.json`，例如從 `quiz-review-*` artifact 下載。檔名可以透過參數改成實際路徑。

```bash
.venv/bin/python scripts/build_quiz.py \
  --from-snapshot materials.json \
  --output .build/replay/quiz.json \
  --report-dir .build/replay/report
```

沒有 `--ai-review` 時不呼叫 AI，輸出是規則生成的候選題庫。獨立輸出至 `.build/replay/`，方便與已審題庫區分。

### 檢查既有測試

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.cjs
```

測試包含 Notion 解析、出題規則、審題快取、預算、Batch 跨次續跑、錯誤保存、題庫快照及前端抽題，使用本地資料與模擬 API 驗證。

### 預覽本機題目網站

```bash
.venv/bin/python -m http.server 8000 \
  --bind 127.0.0.1 \
  --directory site
```

開啟 <http://127.0.0.1:8000>，讀取的是本機 `site/quiz.json`；與 GitHub Pages 已發布的檔案可能不同。

相關入口：[專案 README](../README.md)、[GitHub Actions](https://github.com/pandemonium0225/jlpt-quiz/actions/workflows/deploy.yml)。
