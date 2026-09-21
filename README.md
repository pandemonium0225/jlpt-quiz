# 日文測驗自動產生器

從 Notion 的「日文文章」與「日文文法」頁面自動生成日文筆記複習測驗，
用 GitHub Actions 每天重建、部署到 GitHub Pages。網站維持靜態託管；AI 語意審題使用 OpenAI API，按用量計費。

---

## 一、建立 Notion integration

1. 開 <https://www.notion.so/my-integrations> → **New integration**
2. 名稱隨意（例如 `jlpt-quiz`），Type 選 **Internal**，關聯到你的 workspace
3. Capabilities 只要勾 **Read content** 就夠
4. 複製 **Internal Integration Secret**（`ntn_` 或 `secret_` 開頭）

## 二、把頁面分享給 integration

到 Notion 打開「**日文文章**」頁面 → 右上角 `⋯` → **連線／Connections** →
選剛才建立的 integration。子頁面會自動繼承權限，所以只要做這一次。

> 「日文文法」是「日文文章」的子頁面，會一起繼承。若你日後把它移走，
> 要另外對它做一次同樣的動作。

## 三、建立 GitHub repo

1. GitHub → **New repository**，名稱自取，可設 Private（Pages 對 Private repo 也能用）
2. 把本資料夾的所有檔案上傳（保留目錄結構）：

```
scripts/build_quiz.py
site/index.html
.github/workflows/deploy.yml
README.md
```

## 四、設定 Secret 與 Variables

Repo → **Settings → Secrets and variables → Actions**

**Secrets 分頁** → New repository secret：

| Name | Value |
|---|---|
| `NOTION_TOKEN` | 第一步複製的 integration secret |
| `OPENAI_API_KEY` | 用於題庫審核的 OpenAI API key，建議使用獨立專案 |

**Variables 分頁** → New repository variable：

| Name | Value |
|---|---|
| `ARTICLES_PAGE_ID` | `feec6a8276b64996a983402ca73738c4` |
| `GRAMMAR_PAGE_ID` | `3ce0db2643d081c69f7fdb6e2e41f2b9` |

## 五、開啟 GitHub Pages

Repo → **Settings → Pages** → Source 選 **GitHub Actions**（不要選 Deploy from a branch）。

## 六、第一次執行

Repo → **Actions** → 左側選 `Build and deploy quiz` → **Run workflow**。

跑完後網址會是 `https://<你的帳號>.github.io/<repo 名稱>/`，
在 Actions 的 deploy 步驟裡也會直接顯示連結。

之後每天台灣時間早上 7 點會自動重跑一次；想立刻更新就再按一次 Run workflow。

---

## 題目是怎麼生成的

| 題型 | 來源 | 規則 |
|---|---|---|
| 語彙 | 日文文章及巢狀子頁面 | 手動標粗體／上色的詞或片語 → 從完整原句挖空，選項依表面詞形與長度分組 |
| 文法 | 日文文法及其子頁面的例句 | 優先採例句標記，否則從文法標題推導 → 唯一命中才挖空；選項需有同類文法依據 |
| 接續 | 接續方式表格 | 顯示表格中的具體例子 → 選出該例對應的接續形式，包含所有適合出題的列 |

每次重建都重新讀取 Notion，完整取代題庫；新增、修改與刪除筆記會在下次成功重建後反映，沒有寫死的題目或舊題追加清單。段落、清單、toggle、callout 中的巢狀文字也會讀取；目前不解析 Notion 資料庫的列頁面。

題目先依筆記原句及規則生成候選，再由 AI 檢查題意與全部選項。AI 會移除同樣成立或無法確認錯誤的誘答，可靠誘答不足三個就不發布該題。原句與答案不由 AI 改寫。這仍是筆記複習，AI 審核無法保證完全沒有語意歧義。API 呼叫使用 Python 標準函式庫。

幾個會影響生成結果的細節：

- 時間戳（含 `[0:43]`、`（1:04）` 及時間區間）、數字標記、詞條模板與中文說明不會當成語彙題。
- 缺少標點的語彙與文法例句會先自動整理：利用空白、換行、明確句尾與接續語補上句界、句號／問號與必要逗號，再出題。不改詞彙或活用，保護引號、讀音與標記答案；無法細分的片段保留上下文，不因缺標點就淘汰。這是規則式整理，無法保證還原說話者所有停頓。
- 補標點的題目會在解答標示「補標點後的例句」，並可展開查看 Notion 原始段落。只整理產生的題庫，不回寫 Notion；括號不成對、挖空後沒有語境等情況仍會略過。
- 詞與片語不設固定長度上限；多次出現、整句都被挖掉、同題幹卻有不同答案的題目會略過。
- 文法頁面以 `【文法點】` 標題、`接續方式`、`例句`／`例文` 及緊接例句的 📝 引言辨識；解析區的其他表格不會誤當接續表。
- 至少有 3 個不重複的合適誘答才出題。讀音或半形／全形括號的差異不算不同選項；選項不足時不硬湊。
- 常見可互換的 `に／へ`、`と／や`、`から／より`、`が／の` 不互當錯誤選項；規則式解析仍無法判斷所有語意歧義。
- 翻譯、原句與 Notion 來源連結在作答後顯示。日文讀音用 ruby 注音，填空使用獨立的 `［　］`，原句不再包上重複的引號與句號。
- 接續表出的是「用法辨識」題：給例句，問其中的助詞表示什麼（場所、時刻、起点…）。誘答優先用同一個助詞的其他用法，那才是真的容易混淆的；不足才向同類助詞借，並排除與正解語意重疊、或文法點標題已經標明的用法。
- 題目在作答時就標出所屬的文法點，否則同一個例子可以對應多個描述，答案不唯一。
- 接續欄寫的是活用形而非語意的列改出「活用」題：從例句挖掉動詞，選正確的形（`よく［　］言葉` → 使う／使って／使い）。誘答是同一個動詞的其他活用形，但排除同一則筆記也接受的形。
- 活用題在作答時不顯示文法點，因為標題本身就是答案（【動詞辞書形＋名詞】）。
- 活用題的誘答排除兩種語意歧義：辞書形與ない形不同組（「よく使う言葉」與「よく使わない言葉」都對），接「ように」時普通形彼此不同組（「忘れないように」是為了不忘、「忘れたように」是裝作忘了，都對）。活用題不顯示文法點，學習者沒有線索排除，所以誘答不足的題目直接不出。
- 切詞與五段／一段的判斷需要 `fugashi` + `unidic-lite`（見 `requirements.txt`）。沒安裝時只會少掉需要切詞的幾題，其餘題型照常生成。

網頁每次載入會從各題型輪流隨機抽取最多 10 題，同一原句一輪只出一次，選項也會重排。按「換一組題目」只會從已同步的題庫重抽，不會直接呼叫 Notion。

一次只顯示一題。選好答案立刻判定：正解那一列標朱色 ○，選錯的標墨色 ×，解說隨即展開，再按「下一題」。鍵盤 `1`–`4` 選答，`Enter` 或 `→` 往下一題。頁面最上方的答案欄同時是進度與成績，十題答完後可以點 × 回看答錯的題目。

## 更新與本機驗證

維持每天台灣時間 07:00 自動重建（GitHub 排程可能延遲）。需要提前同步時，到 Actions → `Build and deploy quiz` → `Run workflow`。推送到 `main` 也會觸發；瀏覽器不持有 Notion token。

本機將憑證放在已忽略的 `.env`（格式參考 `.env.example`），執行：

```powershell
python -m unittest discover -s tests -v
node --test tests/test_ui.cjs
python scripts/build_quiz.py --ai-review
python -m http.server 8000 --bind 127.0.0.1 --directory site
```

開啟 <http://127.0.0.1:8000>。前兩項測試不需 Notion 連線，CI 也會先跑測試再生成題庫。若讀取或出題失敗，不會覆寫本機既有題庫，CI 也不會部署空資料。

## 自動審題、更新報告與費用

每天與手動更新共用同一流程：讀取筆記 → 規則生成候選 → AI 校驗及審題 → 比較已發布題庫 → 報告 → 部署。
AI 尚未審完時，Actions 可以成功結束，但 deploy 會略過，網站維持原版；報告狀態為 `pending`。
API 錯誤、損壞的基準、模型未通過校驗或整份題庫被排除時，流程失敗且不部署。只有 `ready=true` 才會發布。

預設模型是 **GPT-5.6 Sol**，`medium` reasoning，每題最多 6,000 輸出 tokens（含推理）。Terra 在實際校驗中出現基礎接續誤判及判定與理由矛盾，因此改用 Sol 驗證品質。
目前測試期間，Actions 使用 `--ai-no-run-limit` **停用單次金額上限**。經使用者同意，`--ai-initial-month-budget 10` 將首輪全庫審查的 UTC 月累計暫時提高至 **US$10**；所有候選都有結果才發布。
首輪成功審完後，完成標記與快取一起保存，後續執行自動恢復預設月額 **US$5**，即使提示或模型改版也不重新取得首輪加額。累計用量不歸零；若已超過 US$5，本月仍可沿用快取，但新增付費審查等下個 UTC 月續跑。中斷、待審或失敗不提前收回首輪加額。OpenAI 平台額度需另外手動調整，程式不會修改平台設定。
測試結束後，移除 workflow 中的 `--ai-no-run-limit` 即恢復預設單次 US$1、分批續審。本機預設仍為單次 US$1，可用同一參數停用。
每個候選的全部選項一次審查，只留下模型判定可明確排除的誘答；API 連線／讀取逾時設定為 180 秒，不串接多家投票、不自動升級模型、不自動重試失敗請求。
最多同時審三題，先預留所有在途請求的費用，再依結果結算；同一可見題目只送出一次。發生錯誤時停止送出新請求，已送出的結果仍會保存。Actions 日誌每十題顯示進度與估算用量。
六個校驗案例涵蓋已知多解、錯誤接續、正確活用、語彙多解及題意不清；未通過就停止發布。校驗也計費且使用相同快取。

AI 盲審只看作答時可見的題幹、指示、選項及用法題的文法點；不傳入隱藏的原句、翻譯、解說或標準答案身分。
模型、提示、輸出結構或可見題目內容變動時重審；只改選項順序或作答後才顯示的資料不重審。
快取同時保存通過及被排除的判定，因此壞題不會每天付費重審。變更審題政策時需更新 `scripts/ai_review.py` 的提示或 `POLICY_VERSION`。

按 2026-09-21 的[官方價格](https://developers.openai.com/api/docs/pricing)（每百萬輸入 US$4、輸出 US$20；Sol 促銷價至少到 2026-11-21），
若每題包含提示共用 1,500 輸入與 1,000 輸出 tokens，188 題約 US$4.89，另加校驗案例；變更 10 題約 US$0.26。
這是估算，實際取決於推理及輸出長度。程式先按輸入 bytes 加餘量及最大輸出預留費用，再依回傳用量調整；逾時或用量缺失時保留預留額。
價格變動需同步更新程式中的價格表。可用 `--ai-run-budget`、`--ai-month-budget` 調整本機預算；0 代表只用現有快取。

**另請在 OpenAI 專用專案的 Limits → Spend 設每月 US$5，並啟用 Enforce a hard limit。**
本機與 CI 不共享帳本；artifact 遺失、過期、人工取消或極端中斷可能使程式端紀錄不完整，因此程式估算不能取代平台限制。
平台硬上限的執行也可能略有延遲，詳見[官方 spend limits 文件](https://developers.openai.com/api/docs/guides/spend-limits)。

Actions 的 Summary 顯示新增、消失、修改、未變的題數、選項前後差異、未出題原因及 AI 用量。
完整資料在 `quiz-review-<attempt>` artifact（保存 30 天）：

- `report.md`／`report.json`：摘要與全部診斷，附來源連結。事件數不等於淘汰題數，同一素材可能涉及多種題型。
- `materials.json`：已解析的筆記素材，可離線重播；不含 API 金鑰，但包含筆記內容。

`quiz-baseline-<attempt>` 只在成功部署後保存題庫，供下次比較；AI 快取和用量由 `quiz-ai-state-<attempt>` 保存，待審或失敗的執行也會保留（皆保存 90 天）。
找不到基準時會明確提示，不把整份題庫誤報為新增。來源區塊及考點用於追蹤題目；重建 Notion 區塊可能呈現為新增／消失。
更新流程不會回寫 Notion，也不會寄送通知或訊息。

下載素材後可離線檢查生成與報告（不加 `--ai-review` 就不呼叫付費 API；輸出另存以免取代已審題庫）：

```sh
python scripts/build_quiz.py --from-snapshot materials.json --output .build/replay/quiz.json --previous site/quiz.json --report-dir .build/replay/report
node --test tests/*.cjs
```

要連同 AI 續審，加入 `--ai-review --ai-state .build/ai-state/cache.json`。金鑰只放本機 `.env` 或 GitHub Actions secret，不能放進素材或題庫。

## 排錯

- **Actions 紅字 `Notion API 404`**：頁面沒分享給 integration，回第二步
- **頁面顯示「題庫是空的」**：同上，或是 page ID 填錯
- **Actions 綠燈但網站是舊的**：先看 Summary 是否為 `pending`、deploy 是否略過；若是則等下次續審。已成功部署才嘗試強制重新整理（Ctrl/Cmd + Shift + R）。
- **想改每輪題數**：編輯 `site/index.html` 最上方的 `PER_ROUND`
- **想改更新頻率**：編輯 `.github/workflows/deploy.yml` 的 `cron`（時間是 UTC，台灣時間要減 8 小時）
