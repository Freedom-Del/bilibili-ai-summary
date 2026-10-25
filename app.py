"""
B站视频 AI 总结工具
提取 B站视频字幕 → DeepSeek AI 总结 → 生成 Markdown 笔记
无字幕时自动降级：下载音频 → Whisper 语音识别
"""

import logging
import os
import re
import shutil
import ssl
import tempfile
import time
from datetime import datetime
from urllib.parse import quote

import requests
import urllib3
from flask import Flask, Response, jsonify, render_template, request
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

from db import init_db, save_summary, get_history, get_history_count, get_by_id, delete_history, update_title, update_sort_orders, update_short_title

init_db()

# ── 基础配置 ──────────────────────────────────────────────────

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# 全局关闭 SSL 验证（国内网络兼容）
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
os.environ.setdefault("HF_HUB_DISABLE_SSL_VERIFY", "1")

# FFmpeg 路径：优先用 FFMPEG_PATH 环境变量，否则从 PATH 自动查找
_FFMPEG_PATH = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
if _FFMPEG_PATH:
    app.logger.info("FFmpeg: %s", _FFMPEG_PATH)
else:
    app.logger.warning("FFmpeg 未找到，语音识别功能将不可用（请安装 FFmpeg 并加入 PATH，或设置 FFMPEG_PATH）")

# B站 API 请求头
BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
}

# API Key：优先使用用户输入的，否则从环境变量读取
DEFAULT_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# Whisper 模型懒加载
_whisper_model = None

# ── 工具函数 ──────────────────────────────────────────────────


def bili_get(url: str, timeout: int = 15) -> requests.Response:
    """B站 API GET 请求，自动重试 3 次，关闭 SSL 验证"""
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=BILI_HEADERS, timeout=timeout, verify=False)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_error = e
            if attempt < 2:
                wait = (attempt + 1) * 2
                app.logger.warning("请求失败，%ds 后重试 (%d/3): %s", wait, attempt + 1, e)
                time.sleep(wait)
    raise last_error


def extract_bvid(url: str) -> str | None:
    """从 B站链接中提取 BV 号，支持 b23.tv 短链"""
    m = re.search(r"(BV[a-zA-Z0-9]{10})", url)
    if m:
        return m.group(1)
    if "b23.tv" in url or "bili2233.cn" in url:
        try:
            resp = requests.head(url, headers=BILI_HEADERS, allow_redirects=True, timeout=10, verify=False)
            m = re.search(r"(BV[a-zA-Z0-9]{10})", resp.url)
            if m:
                return m.group(1)
        except Exception:
            pass
    return None


def fmt_time(seconds: float) -> str:
    """秒数 → [HH:]MM:SS 时间戳"""
    h, m, s = int(seconds // 3600), int((seconds % 3600) // 60), int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ── B站字幕获取 ──────────────────────────────────────────────


def fetch_bilibili_subtitle(bvid: str) -> dict:
    """从 B站 API 获取字幕；无字幕时返回 {"no_subtitle": True}"""
    # 1) 视频信息
    view = bili_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}").json()
    if view["code"] != 0:
        return {"error": f"B站 API 错误: {view.get('message', '未知错误')}"}

    info = view["data"]
    title, cid = info["title"], info["cid"]
    pages = info.get("pages", [])
    page_info = f"（共 {len(pages)} 集，默认 P1）" if len(pages) > 1 else ""

    # 2) 字幕列表
    player = bili_get(f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}").json()
    subtitles = player.get("data", {}).get("subtitle", {}).get("subtitles", [])

    if not subtitles:
        return {"title": title, "bvid": bvid, "cid": cid, "page_info": page_info, "no_subtitle": True}

    # 3) 选字幕：中文 AI > 中文 CC > 任意
    chosen = None
    for sub in subtitles:
        if sub.get("lan_doc", "").startswith("中文") and sub.get("subtitle_url"):
            chosen = sub
            break
    if not chosen:
        chosen = next((s for s in subtitles if s.get("subtitle_url")), None)
    if not chosen:
        return {"title": title, "bvid": bvid, "cid": cid, "page_info": page_info, "no_subtitle": True}

    sub_url = chosen["subtitle_url"]
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url

    # 4) 解析字幕
    body = bili_get(sub_url).json().get("body", [])
    lines = [f"[{fmt_time(it['from'])}] {it['content'].strip()}" for it in body]

    return {
        "title": title, "bvid": bvid, "cid": cid,
        "page_info": page_info,
        "text": "\n".join(lines),
        "language": chosen.get("lan_doc", "未知"),
        "method": "B站内置字幕",
    }


# ── Whisper 语音识别（降级）───────────────────────────────────


def _load_whisper():
    """懒加载 Whisper tiny 模型（存 E 盘，约 70MB）"""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    import whisper
    # 模型缓存目录：可用 WHISPER_MODEL_DIR 环境变量覆盖，默认存到用户缓存目录
    model_root = os.getenv(
        "WHISPER_MODEL_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "whisper"),
    )
    os.makedirs(model_root, exist_ok=True)

    app.logger.info("加载 Whisper tiny 模型（首次从 Azure CDN 下载 ~70MB）...")
    try:
        _whisper_model = whisper.load_model("tiny", download_root=model_root)
        app.logger.info("Whisper 模型就绪")
        return _whisper_model
    except Exception as e:
        # 清理损坏缓存
        cache = os.path.join(model_root, "tiny.pt")
        if os.path.exists(cache):
            os.remove(cache)
        raise RuntimeError(f"模型下载失败: {e}\n已清除损坏缓存，请重启重试。") from e


def download_audio(bvid: str, url: str) -> str:
    """yt-dlp 下载音频，返回文件路径"""
    import yt_dlp

    tmp_dir = tempfile.mkdtemp(prefix="bili_audio_")
    output_path = os.path.join(tmp_dir, bvid)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "quiet": False,
        "no_warnings": False,
    }

    app.logger.info("下载音频: %s", url)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("音频下载失败，请检查网络或视频是否可访问")

    # 找到生成的文件
    for ext in (".m4a", ".webm", ".opus", ".mp3", ".mp4"):
        candidate = output_path + ext
        if os.path.exists(candidate):
            break
    else:
        # yt-dlp 可能用其他扩展名
        files = os.listdir(tmp_dir)
        candidate = os.path.join(tmp_dir, files[0]) if files else None

    if not candidate:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("音频下载失败：未生成文件，请查看上方 yt-dlp 日志")

    app.logger.info("音频: %s (%.1f MB)", candidate, os.path.getsize(candidate) / 1048576)
    return candidate


def transcribe_audio(audio_path: str) -> str:
    """Whisper 转录，返回带时间戳文本"""
    model = _load_whisper()
    segments = model.transcribe(audio_path, language="zh", verbose=False)["segments"]
    return "\n".join(f"[{fmt_time(seg['start'])}] {seg['text'].strip()}" for seg in segments)


def fetch_subtitle_with_fallback(url: str, bvid: str) -> dict:
    """B站字幕 → 无则 Whisper"""
    app.logger.info("获取字幕: bvid=%s", bvid)
    result = fetch_bilibili_subtitle(bvid)

    if "text" in result:  # 成功
        return result
    if not result.get("no_subtitle"):  # 真正的错误
        return result

    # ── Whisper 降级 ──
    app.logger.info("无内置字幕，启动 Whisper 语音识别")
    audio_path = None
    try:
        audio_path = download_audio(bvid, url)
        result["text"] = transcribe_audio(audio_path)
        result["method"] = "Whisper 语音识别"
        result["language"] = "zh (Whisper)"
        return result
    except ImportError as e:
        name = str(e).rsplit("'", 2)[-2] if "'" in str(e) else str(e)
        return {**result, "error": f"缺少依赖 {name}，请运行 pip install yt-dlp openai-whisper"}
    except Exception as e:
        return {**result, "error": f"语音识别失败: {e}"}
    finally:
        if audio_path:
            try:
                os.remove(audio_path)
                os.rmdir(os.path.dirname(audio_path))
            except OSError:
                pass


# ── DeepSeek AI 总结 ──────────────────────────────────────────

SYSTEM_PROMPT = """你是一个专业的学习笔记整理助手。用户通过视频学习编程（主要是 C++），
请根据字幕/语音识别文本整理 Markdown 笔记。

要求：
1. **章节结构**：按自然内容分段，使用 ## 标题
2. **核心知识点**：列表，**加粗**重要术语
3. **代码示例**：整理到 ```cpp 代码块
4. **易错点**：讲师提到的陷阱、最佳实践单独列出
5. **容错**：修正语音识别的同音错别字
6. 简洁专业，不废话，不遗漏

输出纯 Markdown，无开场白无结尾语。"""


def summarize_with_deepseek(text: str, title: str, url: str, api_key: str) -> str:
    """DeepSeek API 总结"""
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"**视频标题**：{title}\n**链接**：{url}\n\n**内容**：\n{text}"},
        ],
        temperature=0.3,
        max_tokens=8192,
    )
    return resp.choices[0].message.content


def generate_short_title(title: str, text: str, api_key: str) -> str:
    """AI 生成 ≤15 字的短标题"""
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    # 取字幕前 2000 字作为上下文
    snippet = text[:2000]
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一个标题提炼助手。根据视频内容用中文生成一个简短标题（≤15字），直接输出标题本身，不要引号、标点、解释。"},
                {"role": "user", "content": f"视频原标题：{title}\n\n内容片段：\n{snippet}\n\n请生成不超过15个字的短标题："},
            ],
            temperature=0.1,
            max_tokens=30,
        )
        result = resp.choices[0].message.content.strip()
        # 去掉可能的引号
        result = result.strip("\"'「」『』\"\"''")
        # 截断到 15 个字
        if len(result) > 15:
            result = result[:15]
        return result
    except Exception as e:
        app.logger.warning("短标题生成失败: %s", e)
        return ""  # 降级：返回空字符串


def build_markdown(title: str, url: str, ai_summary: str, method: str = "") -> str:
    """组装 .md"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    src = f"\n> 🎤 来源：{method}" if method else ""
    return (
        f"# {title}\n\n"
        f"> 📺 {url}\n"
        f"> 📅 {now}\n"
        f"> 🤖 DeepSeek AI 自动总结{src}\n\n"
        f"---\n\n{ai_summary}\n\n---\n\n"
        f"*由 B站视频 AI 总结工具 生成*"
    )


# ── Flask 路由 ────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/fetch-subtitle", methods=["POST"])
def api_fetch_subtitle():
    data = request.get_json()
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "请输入视频链接"}), 400

    bvid = extract_bvid(url)
    if not bvid:
        return jsonify({"ok": False, "error": "无法识别 B站链接，请检查 URL"}), 400

    try:
        result = fetch_subtitle_with_fallback(url, bvid)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"网络错误: {e}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"未知错误: {e}"}), 500

    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 400

    return jsonify({
        "ok": True,
        "title": result["title"],
        "text": result["text"],
        "char_count": len(result["text"]),
        "language": result.get("language", ""),
        "page_info": result.get("page_info", ""),
        "method": result.get("method", ""),
    })


@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    data = request.get_json()
    text = (data.get("text") or "").strip()
    title = (data.get("title") or "").strip()
    url = (data.get("url") or "").strip()
    api_key = (data.get("api_key") or "").strip() or DEFAULT_API_KEY
    method = (data.get("method") or "").strip()

    if not text:
        return jsonify({"ok": False, "error": "缺少文本内容"}), 400
    if not api_key:
        return jsonify({"ok": False, "error": "请填写 DeepSeek API Key"}), 400

    app.logger.info("AI 总结: %s (%d 字)", title, len(text))

    try:
        summary = summarize_with_deepseek(text, title, url, api_key)
    except Exception as e:
        msg = str(e)
        if "401" in msg or "auth" in msg.lower():
            return jsonify({"ok": False, "error": "API Key 无效"}), 401
        if "429" in msg:
            return jsonify({"ok": False, "error": "请求太频繁，请稍等"}), 429
        return jsonify({"ok": False, "error": f"AI 接口错误: {msg}"}), 500

    # AI 生成短标题
    short_title = generate_short_title(title, text, api_key)
    app.logger.info("短标题: %s", short_title)

    # 保存到历史记录
    bvid = extract_bvid(url)
    save_summary(
        bvid=bvid or "",
        title=title,
        url=url,
        markdown=build_markdown(title, url, summary, method),
        char_count=len(text),
        method=method,
        short_title=short_title,
    )

    return jsonify({
        "ok": True,
        "markdown": build_markdown(title, url, summary, method),
        "summary": summary,
    })


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json()
    markdown = (data.get("markdown") or "").strip()
    title = (data.get("title") or "笔记").strip()

    if not markdown:
        return jsonify({"ok": False, "error": "缺少内容"}), 400

    filename = f"{re.sub(r'[\\\\/:*?\"<>|]', '_', title)}.md"
    return Response(
        markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ── 历史记录 API ──────────────────────────────────────────


@app.route("/api/history")
def api_history():
    """历史列表（不含正文）"""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    offset = (page - 1) * per_page
    rows = get_history(limit=per_page, offset=offset)
    total = get_history_count()
    return jsonify({
        "ok": True,
        "items": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@app.route("/api/history/<int:sid>")
def api_history_detail(sid):
    """查看一条历史正文"""
    row = get_by_id(sid)
    if not row:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True, **row})


@app.route("/api/history/<int:sid>", methods=["DELETE"])
def api_history_delete(sid):
    """删除一条历史"""
    if delete_history(sid):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "记录不存在"}), 404


@app.route("/api/history/<int:sid>/title", methods=["PUT"])
def api_history_update_title(sid):
    """编辑历史记录短标题（列表显示的标题）"""
    data = request.get_json()
    new_title = (data.get("title") or "").strip()
    if not new_title:
        return jsonify({"ok": False, "error": "标题不能为空"}), 400
    row = update_short_title(sid, new_title)
    if not row:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True, "title": row["short_title"], "markdown": row["markdown"]})


@app.route("/api/history/reorder", methods=["PUT"])
def api_history_reorder():
    """批量调整历史记录排序"""
    data = request.get_json()
    orders = data.get("orders", [])
    if not orders or not isinstance(orders, list):
        return jsonify({"ok": False, "error": "缺少 orders 参数"}), 400
    update_sort_orders(orders)
    return jsonify({"ok": True})


# ── 启动 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🚀 B站视频 AI 总结工具  http://localhost:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG", "0") == "1")
