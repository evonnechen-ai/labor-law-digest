# 勞動法令日報：每天自動推播 LINE 卡片（免費版）

每天早上 07:20 左右，自動抓取勞動部新聞稿與勞動相關新聞，由 AI 整理成重點與「人資影響」，再以 LINE 卡片推播給你。每則都可以點開原文。

| 用途 | 服務 | 費用 |
|---|---|---|
| 每天自動執行 | GitHub Actions | 免費（私人專案每月 2,000 分鐘，本程式每次約 1 分鐘） |
| AI 摘要 | Google Gemini API 免費額度 | 免費（每天只呼叫 1 次） |
| LINE 推播 | LINE 官方帳號「輕用量」方案 | 免費（每月 200 則，每天 1 則約用 31 則） |

整體設定約需 30–40 分鐘，全程不需要信用卡。

---

## 步驟 1：建立 LINE 官方帳號與 Messaging API

1. 前往 LINE Developers（https://developers.line.biz/console/），用你的 LINE 帳號登入。
2. 點「Create a new provider」，名稱隨意填（例如：我的人資工具）。
3. 在 Provider 裡選「Create a Messaging API channel」，依指示建立 LINE 官方帳號。
   - 帳號名稱例如「勞動法令小幫手」，方案選預設的免費「輕用量」即可。
4. 進入建立好的 Channel：
   - **Basic settings** 分頁最下方的「Your user ID」（U 開頭的一串字）→ 這是 `LINE_USER_ID`。
   - **Messaging API** 分頁最下方的「Channel access token (long-lived)」→ 按「Issue」→ 這是 `LINE_CHANNEL_ACCESS_TOKEN`。
5. 在 **Messaging API** 分頁掃描 QR code，**用自己的 LINE 加這個官方帳號為好友**（沒加好友就收不到推播）。
6. 建議到 LINE Official Account Manager 關閉「自動回應訊息」和「加入好友的歡迎訊息」，避免干擾。

## 步驟 2：申請 Gemini API 金鑰

1. 前往 Google AI Studio（https://aistudio.google.com/apikey），用 Google 帳號登入。
2. 點「Create API key」→ 複製金鑰 → 這是 `GEMINI_API_KEY`。
3. 不需要綁定付款方式，維持免費方案即可。

> 注意：免費方案的資料可能被 Google 用於改進產品。本程式只送出公開新聞標題，不涉及客戶或公司資料。

## 步驟 3：把程式放上 GitHub

1. 註冊／登入 GitHub（https://github.com），右上角「+」→「New repository」。
2. 名稱例如 `labor-law-digest`，選 **Private（私人）**，按「Create repository」。
3. 在新專案頁面點「uploading an existing file」，把這包檔案**全部**拖進去上傳（包含 `.github/workflows/daily.yml` 資料夾），按「Commit changes」。
   - 若拖曳時 `.github` 資料夾沒有被上傳（Mac 預設隱藏），可改用「Add file → Create new file」，檔名輸入 `.github/workflows/daily.yml`，再貼上檔案內容。

## 步驟 4：設定金鑰（Secrets）

在 GitHub 專案頁面：**Settings → Secrets and variables → Actions → New repository secret**，新增三筆：

| Name | Secret |
|---|---|
| `GEMINI_API_KEY` | 步驟 2 的金鑰 |
| `LINE_CHANNEL_ACCESS_TOKEN` | 步驟 1 的 Channel access token |
| `LINE_USER_ID` | 步驟 1 的 Your user ID |

金鑰放在 Secrets 裡是加密的，不會出現在程式碼中。

## 步驟 5：手動測試一次

1. 到專案的 **Actions** 分頁（第一次需按「I understand my workflows, go ahead and enable them」）。
2. 左側點「勞動法令日報」→ 右側「Run workflow」→ 綠色「Run workflow」。
3. 約 1 分鐘後，LINE 應該會收到卡片。若失敗，點進該次執行紀錄看紅色錯誤訊息（見下方疑難排解）。

完成後，每天早上會自動推播，不需要再做任何事。

---

## 常用調整

| 想改什麼 | 改哪裡 |
|---|---|
| 推播時間 | `.github/workflows/daily.yml` 的 `cron`。格式是 UTC 時間，台灣時間減 8 小時。例：早上 8:10 → `"10 0 * * *"`；只在平日 → `"20 23 * * 0-4"` |
| 追蹤關鍵字 | `main.py` 最上方的 `KEYWORD_QUERIES` |
| 每天最多幾則 | 在 workflow 的 `env:` 加 `MAX_ITEMS: "8"`（建議不超過 8，以免卡片太長） |
| 摘要風格 | `main.py` 的 `PROMPT` 文字 |
| 分享給同事 | 請同事加官方帳號好友，取得他們的 User ID 後改用 multicast（需修改程式，每人每天各算 1 則，留意每月 200 則上限） |

## 疑難排解

| 錯誤訊息 | 原因與處理 |
|---|---|
| LINE 推播失敗 401 | Channel access token 錯誤或過期，重新 Issue 後更新 Secret |
| LINE 推播失敗 400 / "to" invalid | `LINE_USER_ID` 填錯，或尚未加官方帳號好友 |
| Gemini 摘要失敗 | 會自動改推「標題清單」不中斷；若持續失敗，檢查金鑰，或把 `GEMINI_MODEL` 改為 AI Studio 上列出的其他免費 Flash 模型 |
| 抓到 0 則 | 當天確實沒新聞，或來源網站暫時無法連線，會推一則「今日沒有相關新聞」 |
| 某天沒收到推播 | GitHub 排程偶爾延遲或略過；若專案設為公開（Public），60 天沒有任何更新時 GitHub 會自動暫停排程，到 Actions 頁面重新啟用即可（建議維持 Private） |

## 提醒

AI 摘要可能有誤或遺漏。在提供客戶正式建議前，請點原文並以勞動部、全國法規資料庫公告為準。
