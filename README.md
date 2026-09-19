# 日文測驗自動產生器

從 Notion 的「日文文章」與「日文文法」頁面自動生成 JLPT 風格測驗，
用 GitHub Actions 每天重建、部署到 GitHub Pages。全程免費。

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
| 語彙 | 日文文章各篇子頁面 | 你**手動標粗體或上背景色**的詞 → 從原句挖空，其他標記詞當誘答 |
| 文法 | 日文文法 | 每則的例句 → 問這句用的是哪個文法點 |
| 接續 | 日文文法 | 接續方式表格 → 問某文法點的正確接續形式 |

所以維持現在的筆記格式就好，新增的內容隔天自動變成新題目。

幾個會影響生成結果的細節：

- 時間戳（`[0:43]`）雖然是粗體，但會被自動排除
- 標記超過 24 個字的整句不會拿來出題，只挑詞彙級別的標記
- 文法頁面靠 `【文法點】` 標題、`接續方式` 表格、`例句` 段落與 📝 引言來辨識區塊
- 每個題型至少要有 4 個同類項目才出得了題（3 個誘答）

網頁每次載入會從題庫隨機抽 10 題，按「換一組題目」可以重抽，不需要重新部署。

## 排錯

- **Actions 紅字 `Notion API 404`**：頁面沒分享給 integration，回第二步
- **頁面顯示「題庫是空的」**：同上，或是 page ID 填錯
- **Actions 綠燈但網站是舊的**：Pages 有快取，強制重新整理（Ctrl/Cmd + Shift + R）
- **想改每輪題數**：編輯 `site/index.html` 最上方的 `PER_ROUND`
- **想改更新頻率**：編輯 `.github/workflows/deploy.yml` 的 `cron`（時間是 UTC，台灣時間要減 8 小時）
