# CLAUDE.md

這份文件給 Claude Code 讀。開啟這個專案時請先看完，再開始動作。

---

## 這是什麼

一個把 Notion 筆記自動轉成日文測驗網站的專案。

使用者（Sean）在 Notion 用 Bite Size Japanese podcast 學日文（N3–N2 程度），
長期累積兩類筆記。這個專案把筆記抓下來、依規則生成 JLPT 風格的選擇題，
用 GitHub Actions 每天重建，部署到 GitHub Pages。

**現況：程式碼已寫好，但從未實跑過。** 沒有人驗證過 parser 是否真的對得上
Notion 的資料結構。第一件事就是驗證它。

## 資料來源

兩個 Notion 頁面，ID 已固定：

| 用途 | Page ID |
|---|---|
| 日文文章（父頁面，底下約 27 篇子頁面） | `feec6a8276b64996a983402ca73738c4` |
| 日文文法（上面那頁的子頁面之一） | `3ce0db2643d081c69f7fdb6e2e41f2b9` |

### 日文文章：逐字稿 + 使用者的手動標記

每篇是一集 podcast 的逐字稿整理，格式大致是「日文原句（漢字附平假名）」
後面接一個 `📝` 開頭的 quote block 放中文翻譯。

關鍵：**使用者會手動把生詞標成粗體或加背景色**。這些標記是語彙題的唯一來源。

已知的格式不一致，parser 必須容忍：
- 時間戳有時是行內粗體 `**[0:43]**`，有時是獨立的 heading `### 0:00`
- 行內粗體的時間戳會被誤判成標記詞，已用 `TIMESTAMP` regex 排除
- 使用者的標記有兩種性質，只有第一種適合出題：
  - 詞彙：ボーダーコリー、なまけて、しょうもない、中絶、申請、親しい
  - 文法碎片：「は」「きゃ」「のって」「方」「ずつ」
  - 目前用長度與純假名規則過濾（見 `marked_terms()`），已對上述實例驗證過

### 日文文法：結構化的文法筆記

每則的結構固定：

```
## 【文法點名稱】
#### 📅 日期
### 問題解析
（散文說明）
### 接續方式
（表格：接続 | 例）
### 例句
日文例句
> 📝 中文翻譯
```

對應到 Notion API 的 block type：`heading_1` 是文法點標題，
`heading_2` 是區塊標題，表格是 `table` + `table_row` 子 block。

目前約有 20 則文法點。

## 出題規則

| kind | 題型 | 來源 | 作法 |
|---|---|---|---|
| `vocab` | 語彙 | 文章的標記詞 | 從原句挖空，誘答取自其他標記詞（挑字數相近的） |
| `grammar` | 文法辨識 | 文法頁的例句 | 給例句，問用的是哪個文法點，誘答是其他文法點名稱 |
| `connect` | 接續 | 文法頁的接續表格 | 給文法點，問正確接續形式，誘答是其他文法點的接續 |

生成的是「題庫」不是「考卷」：`quiz.json` 存所有題目與誘答候選池，
前端每次載入隨機抽 10 題、隨機排列選項。使用者按「換一組題目」可重抽，
不需要重新部署。

## 檔案結構

```
scripts/build_quiz.py      抓 Notion → 產生 site/quiz.json（純 stdlib，無相依套件）
site/index.html            測驗 UI，fetch quiz.json
site/quiz.json             生成產物（尚不存在）
.github/workflows/deploy.yml   每天 UTC 23:00 重建並部署
README.md                  給使用者看的設定步驟
```

## 待辦：請依序完成，每步確認後再繼續

### 1. 環境檢查

確認 `gh auth status` 已登入。若未登入，告訴使用者要跑什麼，停下來等他。

### 2. 實跑驗證（最重要，不要跳過）

```bash
export ARTICLES_PAGE_ID=feec6a8276b64996a983402ca73738c4
export GRAMMAR_PAGE_ID=3ce0db2643d081c69f7fdb6e2e41f2b9
export NOTION_TOKEN='...'    # 停下來讓使用者自己輸入
python3 scripts/build_quiz.py
```

跑完打開 `site/quiz.json`，向使用者回報：

- 三種 kind 各幾題
- 各抽兩題印出完整內容（題幹、正解、誘答池前幾個）
- 有沒有明顯壞掉的題目：題幹被句子切割切壞、誘答一眼可排除、
  挖空後句子讀不通、標記詞含多餘的活用詞尾

**發現問題時先跟使用者討論再改規則，不要自行決定什麼叫「合理的題目」。**
判斷題目品質需要日文語感，使用者才是能判斷的人。

常見失敗與處理方向：
- API 回 404 → integration 沒被加入 Notion 頁面，請使用者到頁面右上 `⋯` → 連線 加入
- 文法題數為 0 → heading level 判斷錯了，Notion API 的 heading_1/2/3 與
  markdown 的 `#` 數量不一定對應，印出實際 block type 確認
- 語彙題數過少 → 過濾規則太嚴，或只有少數文章有標記（後者是正常的，
  使用者才剛開始標記，告訴他多標幾篇就會變好）

### 3. 建 repo 並部署

```bash
git init && git add -A && git commit -m "Notion JLPT quiz generator"
gh repo create jlpt-quiz --private --source=. --push

gh secret set NOTION_TOKEN          # 互動式輸入，不要寫在指令列
gh variable set ARTICLES_PAGE_ID --body feec6a8276b64996a983402ca73738c4
gh variable set GRAMMAR_PAGE_ID --body 3ce0db2643d081c69f7fdb6e2e41f2b9

gh api -X POST repos/{owner}/jlpt-quiz/pages -f build_type=workflow   # 409 = 已啟用，可忽略
gh workflow run "Build and deploy quiz"
gh run watch
```

完成後把網站網址給使用者。

## 規範

- **Token 絕對不進 commit、不進指令列參數。** 本機開發只能放在被 `.gitignore` 排除的
  `.env`（由使用者自己建立並貼上，Claude 不要讀取或列印這個檔案）；CI 與 `gh secret set` 維持互動式輸入。
- 建 `.gitignore` 排除 `__pycache__/`、`.env`、`*.token`
- `build_quiz.py` 維持零外部相依（只用 stdlib），這樣 CI 不需要裝套件、也不會有供應鏈風險
- 使用者用繁體中文溝通；程式碼註解也用繁體中文
- 使用者偏好 Python
- 「行」指 columns、「列」指 rows（台灣用法），討論資料結構時注意

## 未來可能的調整方向

使用者提過但尚未決定的：

- 錯題本：記錄答錯的題目，下次優先出（需要 localStorage 或後端）
- 依文章來源篩選題目（只練某一集）
- 文法題目前只問「這句用哪個文法」，可以再加「接續填空」之類更難的題型

這些都還沒討論細節，不要自己先做。
