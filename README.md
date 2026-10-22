# code

## recording-to-notes：錄音 → 字幕 → Obsidian 筆記

一個 Claude Code skill：丟一個錄音或影片給 Claude Code，它會用 Groq 免費的 Whisper API 轉成 SRT 字幕與逐字稿，再依你的 Obsidian 筆記庫格式整理成筆記。

### 安裝

1. 安裝 ffmpeg（macOS：`brew install ffmpeg`；Windows：`winget install ffmpeg`）。
2. 到 https://console.groq.com/keys 申請免費 API Key。
3. 把金鑰加進 `~/.claude/settings.json`（沒有這個檔案就建立一個）：
   ```json
   { "env": { "GROQ_API_KEY": "gsk_..." } }
   ```
4. 把 skill 複製到全域 skills 目錄，所有專案都能用（包括 Obsidian 筆記庫）：
   ```bash
   mkdir -p ~/.claude/skills
   cp -r .claude/skills/recording-to-notes ~/.claude/skills/
   ```
   只想在某個筆記庫使用的話，改成複製到該筆記庫的 `.claude/skills/`。
5. （選用）`pip install opencc`：Whisper 輸出簡體字時自動轉成台灣繁體。

### 使用

在筆記庫目錄開啟 Claude Code，然後說：

```
幫我把 ~/Downloads/週會.m4a 轉成筆記
```

或輸入 `/recording-to-notes ~/Downloads/週會.m4a`。

也可以只用轉錄腳本：

```bash
python3 ~/.claude/skills/recording-to-notes/scripts/transcribe.py 錄音.m4a --out-dir 輸出目錄
```

常用參數：`--prompt "人名、專有名詞"` 提高辨識率、`--language en|ja|auto`、`--model whisper-large-v3`（較準較慢）。

### 免費額度（Groq，以官網 Limits 頁面為準）

- 每小時約 2 小時音訊、每天約 8 小時音訊。
- 長錄音會自動在靜音處切段上傳；遇到額度限制會自動等待重試，中斷後重跑同一指令可接續。
- 注意：音檔會上傳到 Groq 的伺服器處理，機密錄音請自行評估。
