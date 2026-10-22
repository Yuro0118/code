---
name: recording-to-notes
description: 把錄音或影片（mp3、m4a、wav、mp4、mov 等）用 Groq Whisper API 轉成 SRT 字幕與逐字稿，再整理成 Obsidian 筆記。使用者提供音檔路徑、說「轉逐字稿」「轉字幕」「幫我把錄音寫成筆記」「整理這場會議／課程錄音」時使用。
---

# 錄音 → 字幕 → Obsidian 筆記

流程分三步：轉錄（腳本）→ 了解筆記庫慣例 → 寫筆記。全程用台灣繁體中文。

## 1. 事前檢查

- 確認使用者給的檔案路徑存在。路徑有空白或中文時要加引號。
- 確認 `GROQ_API_KEY` 已設定：`test -n "$GROQ_API_KEY" && echo ok`。沒有的話，請使用者到 https://console.groq.com/keys 申請（免費、不用綁信用卡），並把金鑰加進 `~/.claude/settings.json`：
  ```json
  { "env": { "GROQ_API_KEY": "gsk_..." } }
  ```
  然後重開 Claude Code。不要把金鑰寫進筆記庫或任何會被 git 追蹤的檔案。
- 確認有 `ffmpeg`（`which ffmpeg`）。沒有的話請使用者安裝（macOS：`brew install ffmpeg`；Windows：`winget install ffmpeg`）。

## 2. 決定輸出位置（先看筆記庫的慣例）

如果目前目錄或其上層有 `.obsidian/`，就是 Obsidian 筆記庫：

1. 讀筆記庫根目錄的 `CLAUDE.md`（若有），裡面的規則優先於本文件的預設。
2. 讀 `.obsidian/app.json`：
   - `attachmentFolderPath` → 字幕與逐字稿放這裡（`./` 開頭代表與筆記同目錄）。
   - `newFileFolderPath` → 新筆記的預設資料夾。
3. 看 `.obsidian/templates.json` 或 Templater 設定找範本資料夾；有「會議」「錄音」「課程」之類的範本就照著用。
4. 用 `ls -t` 找同類型的 2～3 篇近期筆記看一下，模仿它們的 frontmatter 欄位、標籤寫法、標題格式、檔名規則（例如 `2026-10-22 週會.md`）。
5. 還是無法判斷筆記該放哪個資料夾時，問使用者一次，之後照同樣方式處理。

不在筆記庫中的話，輸出到音檔所在的目錄即可。

## 3. 轉錄

```bash
python3 "<此 skill 的目錄>/scripts/transcribe.py" "<音檔路徑>" --out-dir "<附件資料夾>" --name "<輸出檔名>"
```

- `<此 skill 的目錄>` 是載入本 skill 時顯示的 Base directory。
- `--name` 用與筆記相同的檔名（不含副檔名），這樣字幕和筆記容易對應。
- 使用者有提到人名、公司、產品、專有名詞時，用 `--prompt "名詞1、名詞2"` 傳入，可以大幅減少錯字。
- 內容不是中文就加 `--language en`（或 `ja` 等）；語言混雜時用 `--language auto`。
- 對準確度要求高時加 `--model whisper-large-v3`（較慢，但仍在免費額度內）。
- 錄音超過約 1.5 小時要在背景執行（Bash 的 `run_in_background`），因為可能碰到 Groq 每小時 2 小時音訊的上限，腳本會自動等待重試。
- 中途因額度失敗的話，之後重跑同一個指令就會沿用已完成的段落。

腳本完成後，stdout 最後一行是 JSON，內含 `srt`、`transcript`（附時間戳的 `.transcript.md`）、`duration`。stderr 是進度訊息。

## 4. 寫筆記

讀 `transcript` 檔案（不要讀 SRT，比較長），然後寫筆記。筆記庫有範本或既有格式就照著做；沒有的話用以下預設：

```markdown
---
date: YYYY-MM-DD
tags:
  - 錄音筆記
duration: "HH:MM:SS"
source: "[[<輸出檔名>.transcript]]"
---

# <依內容下的標題>

## 摘要
3～5 句話說明這段錄音在講什麼、結論是什麼。

## 重點
- 條列主要觀點，每點一行，必要時附時間戳 `[12:34]`

## 決議與待辦
- [ ] 待辦事項（負責人、期限有提到才寫）

## 詳細筆記
### <主題一> `[00:00]`
依主題分段整理，不是逐句改寫逐字稿。

## 逐字稿
![[<輸出檔名>.transcript]]
```

撰寫原則：

- **只寫錄音裡有的內容**，不要補充錄音沒講的資訊或推測。
- Whisper 會把同音字、專有名詞聽錯：依上下文能確定的就直接改正；不確定的保留原字並在後面加 `（?）`。
- Whisper 不會分辨說話者。除非內容明顯（例如自我介紹後的發言），不要硬標「A 說」「B 說」。
- 沒有待辦或決議的章節就整個刪掉，不要留空標題。
- 課程或演講類型的錄音，把「決議與待辦」換成「延伸問題」或「名詞解釋」等更合適的章節。
- 時間戳格式沿用逐字稿的 `[mm:ss]` 或 `[hh:mm:ss]`，方便回頭對照錄音。
- 使用者若同時希望把音檔放進筆記庫，可在逐字稿區塊上方加 `![[音檔檔名]]` 讓 Obsidian 直接播放；但音檔很大，預設不複製，先問使用者。

## 5. 回報

完成後簡短告訴使用者：筆記路徑、字幕與逐字稿路徑、錄音長度，以及有標 `（?）` 的不確定詞（如果有的話），請他們確認。
