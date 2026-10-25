# 📺 B站视频 AI 总结工具

输入一个 B 站视频链接，自动提取字幕（无字幕时用 Whisper 语音识别降级），交给 DeepSeek AI 生成结构化的 Markdown 学习笔记，并支持本地历史记录管理。

> 纯本地运行，无后端服务依赖，笔记和历史数据都保存在你自己机器上。

## ✨ 功能特性

- 🔗 **一键解析** — 支持 `BV` 号链接和 `b23.tv` 短链，自动提取视频标题与字幕
- 🀄 **字幕智能选择** — 优先中文 AI 字幕 → 中文 CC 字幕 → 任意字幕
- 🎙️ **Whisper 降级** — 无内置字幕时自动下载音频并用 Whisper 做语音识别（首次使用自动下载 tiny 模型 ~70MB）
- 🤖 **AI 结构化总结** — 调用 DeepSeek 生成带章节、知识点、代码示例、易错点的 Markdown 笔记
- ✂️ **自动短标题** — AI 提炼 ≤15 字的列表标题
- 📚 **历史记录** — 本地 SQLite 存储，支持预览/下载/复制、编辑标题、上移下移排序、删除
- 💾 **导出** — 一键下载 `.md` 文件或复制 Markdown 源码

## 🧭 工作原理

```
B站链接
  ├─ 有字幕 ──→ 提取字幕文本
  └─ 无字幕 ──→ yt-dlp 下载音频 ──→ Whisper 转写
                        ↓
              DeepSeek AI 总结
                        ↓
              生成 Markdown 笔记 ──→ 存入本地历史
```

## 📦 环境要求

- **Python 3.10+**（推荐 3.10 ~ 3.12，`openai-whisper` 对 3.13 支持可能不稳定）
- **FFmpeg** — 仅语音识别（Whisper 降级）时需要，需加入系统 PATH
- 一个 [DeepSeek API Key](https://platform.deepseek.com/api_keys)

## 🚀 安装

```bash
# 1. 克隆仓库
git clone https://github.com/Freedom-Del/bilibili-ai-summary.git
cd bilibili-ai-summary

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量（把 .env.example 复制为 .env 并填入你的 Key）
#    Windows:
copy .env.example .env
#    macOS / Linux:
cp .env.example .env
```

> ⚠️ 依赖里的 `yt-dlp` 和 `openai-whisper` 体积较大，如果只用「有字幕的视频」、不需要语音识别降级，也可以只装前四个：`pip install flask openai requests python-dotenv`。

## ⚙️ 配置

编辑 `.env`：

```ini
DEEPSEEK_API_KEY=sk-你的key
```

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ❌ | DeepSeek 的 API Key（在页面填写即可；也可写在此处作为后端默认值） |
| `FFMPEG_PATH` | ❌ | FFmpeg 可执行文件路径，不设则自动从 PATH 查找 |
| `WHISPER_MODEL_DIR` | ❌ | Whisper 模型缓存目录，默认 `~/.cache/whisper` |
| `FLASK_DEBUG` | ❌ | 设为 `1` 开启调试模式 |

## 🖥️ 使用

```bash
python app.py
```

打开浏览器访问 <http://localhost:5000>，粘贴视频链接，填写你的 DeepSeek API Key，点「🚀 开始提取总结」。

API Key 需在页面填写；填写过的 Key 会存在浏览器 `localStorage` 里，下次自动带出。

## 📁 目录结构

```
bilibili-ai-summary/
├── app.py              # Flask 主程序（字幕提取 / AI 总结 / 路由）
├── db.py               # SQLite 历史记录持久化
├── requirements.txt    # Python 依赖
├── .env.example        # 环境变量示例
├── static/
│   └── style.css       # 页面样式
└── templates/
    └── index.html      # 前端页面（含全部交互逻辑）
```

## ❓ 常见问题

**Q：提示「FFmpeg 未找到」？**
语音识别功能需要 FFmpeg。安装后把它加入 PATH，或设置 `FFMPEG_PATH` 环境变量指向 `ffmpeg.exe`。只用字幕功能可忽略此警告。

**Q：首次用语音识别很慢？**
第一次会从 CDN 下载 Whisper `tiny` 模型（约 70MB），之后走本地缓存。

**Q：为什么关闭了 SSL 证书校验？**
`app.py` 中为兼容国内网络环境，全局关闭了 SSL 验证并忽略证书警告。如果你在意安全，可自行移除 `ssl._create_default_https_context = ssl._create_unverified_context` 及各处 `verify=False`。

**Q：AI 提示词能改吗？**
可以。`app.py` 里的 `SYSTEM_PROMPT`（默认面向 C++ 编程学习）和 `generate_short_title` 的提示词都可以按需修改。

## ⚖️ 免责声明

本项目仅供个人学习与研究使用。请遵守 [B 站](https://www.bilibili.com) 及相关平台的用户协议与版权规定，勿用于商业用途或大规模抓取；使用 Whisper 语音识别及 AI 总结产生的所有内容版权与责任由使用者自行承担。

## 📄 许可证

[MIT](./LICENSE) © 2026