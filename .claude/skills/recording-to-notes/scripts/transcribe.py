#!/usr/bin/env python3
"""用 Groq Whisper API 把錄音／影片轉成字幕（SRT）與逐字稿。

只依賴 Python 標準函式庫與 ffmpeg；有安裝 opencc 時會自動轉成台灣繁體。

用法：
    python3 transcribe.py 錄音.m4a [--out-dir 目錄] [--language zh]

API Key 從環境變數 GROQ_API_KEY 讀取（或目前目錄下的 .env）。
完成後在 stdout 印出一行 JSON，列出產生的檔案路徑。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_BASE = os.environ.get("GROQ_BASE_URL", "https://api.groq.com").rstrip("/")
API_PATH = "/openai/v1/audio/transcriptions"

# 免費層單檔上限 25 MB；32 kbps 單聲道約 14 MB/小時，20 分鐘一段遠低於上限
DEFAULT_CHUNK_MINUTES = 20
# 在目標切點前後多少秒內找靜音處下刀，避免把一句話切成兩半
SILENCE_SEARCH_WINDOW = 60
MAX_RETRIES = 8

DEFAULT_PROMPT_ZH = "以下是台灣繁體中文的錄音內容，請使用繁體中文與全形標點符號。"

# Whisper 在靜音或音樂段落常見的幻覺字串
HALLUCINATION_PATTERNS = [
    "amara.org",
    "字幕由",
    "字幕提供",
    "字幕志願者",
    "字幕志愿者",
    "不吝點贊",
    "不吝点赞",
    "點贊訂閱",
    "点赞订阅",
    "明鏡與點點",
    "明镜与点点",
    "優優獨播",
    "优优独播",
    "yoyo television",
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(msg):
    log(f"錯誤：{msg}")
    sys.exit(1)


def load_api_key():
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    env_file = Path.cwd() / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:export\s+)?GROQ_API_KEY\s*=\s*['\"]?([^'\"\s]+)", line)
            if m:
                return m.group(1)
    fail(
        "找不到 GROQ_API_KEY。請到 https://console.groq.com/keys 建立金鑰，"
        "並設定環境變數 GROQ_API_KEY（或放在 ~/.claude/settings.json 的 env 中）。"
    )


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        fail(f"無法讀取音訊長度：{path}\n{r.stderr.strip()}")


def preprocess(src, dst):
    """轉成 16 kHz 單聲道 32 kbps mp3：語音辨識足夠，檔案又小。"""
    r = run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn",
             "-ac", "1", "-ar", "16000", "-b:a", "32k", str(dst)])
    if r.returncode != 0:
        fail(f"ffmpeg 轉檔失敗：\n{r.stderr.strip()}")


def detect_silences(path):
    """回傳 [(start, end), ...] 靜音區間。"""
    r = run(["ffmpeg", "-v", "info", "-i", str(path),
             "-af", "silencedetect=noise=-30dB:d=0.4", "-f", "null", "-"])
    silences = []
    start = None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    return silences


def plan_chunks(duration, chunk_sec, silences):
    """決定每段的 (start, end)，盡量切在靜音中間。"""
    cuts = [0.0]
    while duration - cuts[-1] > chunk_sec:
        target = cuts[-1] + chunk_sec
        best = None
        for s, e in silences:
            mid = (s + e) / 2
            if abs(mid - target) <= SILENCE_SEARCH_WINDOW and \
                    (best is None or abs(mid - target) < abs(best - target)):
                best = mid
        cuts.append(best if best is not None else target)
    cuts.append(duration)
    return list(zip(cuts[:-1], cuts[1:]))


def extract_chunk(src, start, end, dst):
    r = run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
             "-i", str(src), "-c", "copy", str(dst)])
    if r.returncode != 0:
        fail(f"ffmpeg 切割失敗：\n{r.stderr.strip()}")


def build_multipart(fields, file_field, file_path):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8")
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{Path(file_path).name}\"\r\nContent-Type: audio/mpeg\r\n\r\n".encode("utf-8")
    )
    parts.append(Path(file_path).read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe_chunk(api_key, chunk_path, args, prompt):
    fields = [
        ("model", args.model),
        ("response_format", "verbose_json"),
        ("temperature", "0"),
        ("timestamp_granularities[]", "segment"),
    ]
    if args.language != "auto":
        fields.append(("language", args.language))
    if prompt:
        fields.append(("prompt", prompt))
    body, content_type = build_multipart(fields, "file", chunk_path)

    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(
            API_BASE + API_PATH, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": content_type,
                "User-Agent": "recording-to-notes/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (401, 403):
                fail(f"Groq 拒絕金鑰（HTTP {e.code}）：{detail}")
            if e.code == 429 or e.code >= 500:
                wait = retry_after(e.headers, attempt)
                log(f"  HTTP {e.code}，{wait:.0f} 秒後重試（第 {attempt}/{MAX_RETRIES} 次）")
                time.sleep(wait)
                continue
            fail(f"Groq 回傳 HTTP {e.code}：{detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            wait = min(2 ** attempt, 60)
            log(f"  連線失敗（{e}），{wait} 秒後重試（第 {attempt}/{MAX_RETRIES} 次）")
            time.sleep(wait)
    fail("重試次數用盡。若是超過每小時／每日額度，稍後重跑同一指令即可從中斷處繼續。")


def retry_after(headers, attempt):
    value = headers.get("retry-after") if headers else None
    try:
        return max(1.0, float(value)) + 1
    except (TypeError, ValueError):
        return min(2 ** attempt * 5, 300)


def is_hallucination(seg):
    text = seg.get("text", "").strip().lower()
    if not text:
        return True
    if any(p in text for p in HALLUCINATION_PATTERNS):
        return True
    if seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -1.0:
        return True
    if seg.get("compression_ratio", 0) > 2.8:
        return True
    return False


def make_converter(language, disabled):
    if disabled or language not in ("zh", "auto"):
        return lambda s: s
    try:
        import opencc
    except ImportError:
        log("提示：未安裝 opencc，略過簡轉繁（pip install opencc 可啟用）")
        return lambda s: s
    for config in ("s2twp", "s2twp.json"):
        try:
            return opencc.OpenCC(config).convert
        except Exception:
            continue
    log("提示：opencc 載入 s2twp 失敗，略過簡轉繁")
    return lambda s: s


def fmt_srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_clock(t):
    t = int(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def write_srt(segments, path):
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{fmt_srt_time(seg['start'])} --> {fmt_srt_time(seg['end'])}",
                  seg["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_transcript(segments, path, title, duration, block_sec=30):
    """合併成約 30 秒一段、附時間戳的逐字稿，方便閱讀與寫筆記。"""
    lines = [f"# 逐字稿：{title}", "", f"長度：{fmt_clock(duration)}", ""]
    block, block_start, last_end = [], None, None
    for seg in segments:
        new_block = block and (seg["start"] - block_start >= block_sec
                               or seg["start"] - last_end > 3)
        if new_block:
            lines.append(f"[{fmt_clock(block_start)}] {join_text(block)}")
            block = []
        if not block:
            block_start = seg["start"]
        block.append(seg["text"])
        last_end = seg["end"]
    if block:
        lines.append(f"[{fmt_clock(block_start)}] {join_text(block)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def join_text(parts):
    out = ""
    for p in parts:
        # Whisper 中文常不加標點，句與句之間補空白避免黏在一起
        if out and not re.search(r"[，。！？；：、,.!?;:]$", out):
            out += " "
        out += p
    return out


def main():
    ap = argparse.ArgumentParser(description="用 Groq Whisper 把錄音轉成 SRT 字幕與逐字稿")
    ap.add_argument("input", help="錄音或影片檔（mp3、m4a、wav、mp4、mov…）")
    ap.add_argument("--out-dir", help="輸出目錄（預設與輸入檔同目錄）")
    ap.add_argument("--name", help="輸出檔名（不含副檔名，預設同輸入檔）")
    ap.add_argument("--language", default="zh", help="語言代碼，如 zh、en、ja；auto 為自動偵測")
    ap.add_argument("--model", default="whisper-large-v3-turbo",
                    help="whisper-large-v3-turbo（快）或 whisper-large-v3（較準）")
    ap.add_argument("--prompt", help="提示詞：可放專有名詞、人名，提高辨識正確率")
    ap.add_argument("--chunk-minutes", type=float, default=DEFAULT_CHUNK_MINUTES)
    ap.add_argument("--no-convert", action="store_true", help="不做簡轉繁")
    args = ap.parse_args()

    src = Path(args.input).expanduser().resolve()
    if not src.is_file():
        fail(f"找不到檔案：{src}")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            fail(f"需要 {tool}，請先安裝 ffmpeg（macOS: brew install ffmpeg）")
    api_key = load_api_key()

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.name or src.stem

    base_prompt = args.prompt or ""
    if args.language == "zh" and not args.prompt:
        base_prompt = DEFAULT_PROMPT_ZH
    elif args.language == "zh":
        base_prompt = f"{DEFAULT_PROMPT_ZH}{args.prompt}"

    # 快取：額度用完中斷時，重跑同一指令會沿用已完成的段落
    settings_key = hashlib.sha1(
        f"{src.stat().st_size}:{src.stat().st_mtime_ns}:{args.model}:{args.language}:"
        f"{base_prompt}:{args.chunk_minutes}".encode()
    ).hexdigest()[:10]
    cache_dir = Path(tempfile.gettempdir()) / "groq-transcribe-cache" / f"{stem}-{settings_key}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(src)
    log(f"音訊長度 {fmt_clock(duration)}，前處理中…")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        audio = tmp / "audio.mp3"
        preprocess(src, audio)

        chunk_sec = args.chunk_minutes * 60
        silences = detect_silences(audio) if duration > chunk_sec else []
        chunks = plan_chunks(duration, chunk_sec, silences)
        log(f"共 {len(chunks)} 段，開始轉錄（模型 {args.model}）")

        convert = make_converter(args.language, args.no_convert)
        segments, prev_tail = [], ""
        for i, (start, end) in enumerate(chunks, 1):
            cache_file = cache_dir / f"chunk_{i:03d}.json"
            if cache_file.is_file():
                result = json.loads(cache_file.read_text(encoding="utf-8"))
                log(f"[{i}/{len(chunks)}] {fmt_clock(start)}–{fmt_clock(end)} 使用快取")
            else:
                log(f"[{i}/{len(chunks)}] {fmt_clock(start)}–{fmt_clock(end)} 上傳中…")
                chunk_path = tmp / f"chunk_{i:03d}.mp3"
                extract_chunk(audio, start, end, chunk_path)
                # 把上一段結尾當作提示，讓斷點前後的用詞一致
                prompt = (base_prompt + prev_tail)[-400:]
                result = transcribe_chunk(api_key, chunk_path, args, prompt)
                cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

            kept = []
            for seg in result.get("segments", []):
                if is_hallucination(seg):
                    continue
                text = convert(seg["text"].strip())
                kept.append({
                    "start": round(start + seg["start"], 3),
                    "end": round(min(start + seg["end"], end), 3),
                    "text": text,
                })
            segments += kept
            if kept:
                prev_tail = join_text(s["text"] for s in kept)[-120:]
            log(f"    取得 {len(kept)} 句")

    if not segments:
        fail("沒有辨識出任何內容，請確認錄音是否有人聲。")

    srt_path = out_dir / f"{stem}.srt"
    txt_path = out_dir / f"{stem}.transcript.md"
    write_srt(segments, srt_path)
    write_transcript(segments, txt_path, stem, duration)
    shutil.rmtree(cache_dir, ignore_errors=True)

    log("完成")
    print(json.dumps({
        "srt": str(srt_path),
        "transcript": str(txt_path),
        "duration": fmt_clock(duration),
        "segment_count": len(segments),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
