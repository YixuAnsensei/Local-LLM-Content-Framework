# AutoDoc Agent — 本地 LLM 长文档自动化生成框架

> **已升级：见https://github.com/YixuAnsensei/DocLoom**

> 一个轻量级、无状态的本地长文档自动化调度框架。基于外部状态机与章节级精准上下文投喂，将大模型降维为纯粹的文本处理单元，彻底规避对话式客户端中历史记忆累加导致的显存溢出与注意力稀释问题。最初为 OpenHarmony 智能小车实训报告自动化撰写研发，可零代码修改迁移至任何长篇写作、翻译或代码审计任务。

## 核心特性

- **无状态请求，永不爆显存** — 每次 API 请求仅携带当前章节的 System Prompt + 切片参考资料，不携带任何历史对话。8G 显存跑 64K 上下文模型满血输出，告别上下文超载。
- **代码与配置完全分离** — Python 引擎层通用化（0 业务硬编码），所有"人设"、"任务拆解"和"文件映射"由外部 JSON 配置驱动。
- **四种智能截断策略** — 当输入资料逼近上下文上限时，自动压缩参考资料：`smart`（首尾保留）、`head`（开头优先）、`tail`（结尾优先）、`middle`（中间优先）。
- **断点续传** — 外部 `task_state.json` 记录进度，断网、宕机重启后自动从失败章节继续，已完成章节不重复生成。
- **Git 风格 CLI** — `--review`、`--run`、`--reset`、`--init` 等命令精准控制每章的生成与重写，实现 Human-in-the-loop 微调。
- **多文件参考资料投喂** — 单章可引用多个参考文件，自动合并后在上下文窗口内智能裁剪。

---

## 架构原理

```
┌──────────────────────────────────────────────────────────┐
│                  AutoDoc Agent 调度器                      │
│                                                          │
│  prompts.json ─→ 逐章遍历 ─→ 组装 Payload ─→ POST API    │
│       ↑              │              │                    │
│  task_state.json     │        context manager            │
│  (进度追踪)           │        (截断策略)                  │
│       │              │              │                    │
│       └──────────────┴──────────────┘                    │
│                          ↓                               │
│            模型返回 content（thinking 已剥离）              │
│                          ↓                               │
│                  追加写入 Final_Report.md                  │
│                          ↓                               │
│                  标记 completed，下一章                     │
└──────────────────────────────────────────────────────────┘
```

每章是一次独立的 API 请求，**不携带上一章的任何内容**，模型始终在全新的上下文窗口中工作。无论报告有 11 章还是 100 章，每章都拥有完整的上下文空间。

---

## 快速开始

### 环境准备

```bash
# 创建 Python 环境
conda create -n auto_report python=3.11 -y
conda activate auto_report

# 安装依赖
pip install -r requirements.txt

# 确认 llama.cpp server 已启动（或其他兼容 OpenAI 格式的本地服务）
curl http://localhost:11434/health
# 应返回 {"status":"ok"}
```

> **llama.cpp server 启动示例：**
>
> ```bash
> llama-server.exe -m "C:\models\Qwen3.6-Q4_K_M.gguf" --port 11434 -c 65536
> ```
>
> `-c 65536` 设置上下文窗口为 64K，`--port 11434` 指定端口。

### 使用流程

```bash
# 1. 预览提示词，确认生成方向
python src/scheduler.py --review

# 2. 查看配图/视频素材清单
python src/scheduler.py --hints

# 3. 一键开跑
python src/scheduler.py

# 4. 不满意某章？重置后单独重跑
python src/scheduler.py --reset 9 --prompt "多写底层 C 代码和寄存器分析"
python src/scheduler.py --run 9

# 5. 查看进度
python src/scheduler.py --list
```

---

## 完整命令参考

| 命令                                                     | 说明                                                         |
| -------------------------------------------------------- | ------------------------------------------------------------ |
| `python src/scheduler.py`                                | 按序处理所有 pending 章节（全部完成则自动跳过）              |
| `python src/scheduler.py --review`                       | 运行前预览所有提示词（推荐先跑一次）                         |
| `python src/scheduler.py --hints`                        | 查看各章节配图/视频素材清单                                  |
| `python src/scheduler.py --list`                         | 列出章节及完成状态                                           |
| `python src/scheduler.py --run N`                        | 单独运行第 N 章（1-based）                                   |
| `python src/scheduler.py --reset N`                      | 重置第 N 章为 pending                                        |
| `python src/scheduler.py --reset N --prompt "新提示词"`  | 重置并替换提示词                                             |
| `python src/scheduler.py --reset-all`                    | 全部重置                                                     |
| `python src/scheduler.py --modify N --prompt "修改建议"` | 在第 N 章原内容基础上修改（可重复多次）                      |
| `--ref file1 file2`                                      | 与 `--modify` 配合，临时指定参考资料（覆盖 prompts.json）    |
| `--dry-run`                                              | 预览模式：构建 payload 但不发送请求（可与 `--modify` 或 `--batch-modify` 配合） |
| `python src/scheduler.py --batch-modify`                 | 批量修改（读取 config/modifications.json）                   |
| `--review-modifications`                                 | 预览 modifications.json 中的所有修改指令及 token 估算        |
| `--reset-modifications`                                  | 清除批量修改进度（下次从头开始）                             |
| `python src/scheduler.py --init`                         | 交互式新建项目向导                                           |
| `python src/pdf_docx_parser.py`                          | 解析 data/raw/ 中的 PDF/Word 为 txt                          |

---

## 上下文窗口管理

脚本自动检测每次请求的 token 用量，在参考资料过大时自动截断：

```
[生成中] MQTT通信实验 (第 8 章)
  [用量] 输入 ~8234 tokens / 窗口 65536 (12.6%)
  [请求] 第 1 次尝试 ...
  [成功] 获取回复，长度: 3245 字符
  [输出] 生成了 3245 字符 (~2300 tokens)
```

### 配置项 (`config/settings.json`)

```json
{
  "context_window_tokens": 65536,
  "max_input_ratio": 0.75,
  "truncation_strategy": "smart",
  "truncation_keep_ratio": 0.68
}
```

| 参数                    | 说明                                     | 默认值  |
| ----------------------- | ---------------------------------------- | ------- |
| `context_window_tokens` | 模型上下文窗口大小（启动参数 `-c`）      | 65536   |
| `max_input_ratio`       | 输入占比上限（留给输出 1-ratio 的空间）  | 0.75    |
| `truncation_strategy`   | 截断策略：`smart`/`head`/`tail`/`middle` | `smart` |
| `truncation_keep_ratio` | 截断时保留的内容比例（0-1）              | 0.68    |

### 四种截断策略

```
原文：[████████████████████████████████████████████] 100%

smart  → [██████████████████████████  ██████████████] 保留首尾，砍中间
head   → [████████████████████████████               ] 只保留开头
tail   → [                ████████████████████████████] 只保留结尾
middle → [████              ████████████████        ██] 保留中间，砍首尾
```

---

## 多模态视觉模型支持（实验性）

当 `settings.json` 中设置 `"vision_model": true` 时，框架会自动将 `ref_files` 中的图片文件以 base64 编码嵌入请求，发送给支持多模态的模型（如 LLaVA、Qwen-VL 等）。

```json
{
  "vision_model": true,
  "vision_image_tokens": 1024
}
```

- 图片文件（png/jpg/jpeg/bmp/gif/webp/svg）自动识别并转为 base64 data URL
- 非图片文件正常以文本形式加载
- `vision_image_tokens` 用于估算图片占用的上下文空间
- 未开启 `vision_model` 时，图片文件会被跳过

---

## 迁移到其他项目

本框架设计为**极度通用**。迁移只需修改两个配置文件，不碰任何 Python 代码。

### 方法一：交互式向导

```bash
python src/scheduler.py --init
```

按提示输入项目名、模型、系统提示词、章节数，自动生成配置骨架。

### 方法二：手动替换

```bash
# 1. 复制项目文件夹
cp -r Auto_Report_Agent My_New_Project

# 2. 编辑 config/settings.json（改人设）
{
  "project_name": "长篇小说翻译计划",
  "model": "Qwen3.6",
  "system_prompt": "你是一位精通中英双语的技术翻译专家...",
  "context_window_tokens": 65536,
  "max_input_ratio": 0.75
}

# 3. 编辑 config/prompts.json（改任务拆解）
[
  {
    "chapter": "第一部分：前言翻译",
    "prompt": "请翻译参考资料中的前言部分，保持信达雅。",
    "ref_files": ["intro_eng.txt"]
  },
  {
    "chapter": "第二部分：核心架构解析",
    "prompt": "请翻译核心架构说明。专有名词保留英文。",
    "ref_files": ["architecture_eng.txt"]
  }
]

# 4. 放入新项目的 PDF/Word 到 data/raw/
# 5. 运行解析（PDF/Word → txt）
python src/pdf_docx_parser.py

# 6. 开跑
python src/scheduler.py
```

### prompts.json 格式

```json
[
  {
    "chapter": "章节名称（Markdown 二级标题）",
    "prompt": "生成这一章的具体要求（User Prompt）",
    "ref_files": ["3.txt", "4.txt"],
    "image_hints": ["ADC按键接线图", "串口输出截图"]
  }
]
```

| 字段          | 说明                                                     |
| ------------- | -------------------------------------------------------- |
| `chapter`     | 章节名，会作为报告中的 `## 标题`                         |
| `prompt`      | 生成该章节的详细提示词                                   |
| `ref_files`   | `data/processed/` 中的参考资料文件名（数组，支持多文件） |
| `image_hints` | 该章节建议插入的配图/视频清单（用于 `--hints` 命令）     |

---

## 修改 vs 重写

生成完毕不等于工作结束。经常需要微调某些章节的内容，框架提供两种路径：

| 操作     | 命令                        | 行为                     | 适用场景           |
| -------- | --------------------------- | ------------------------ | ------------------ |
| **重写** | `--reset N` + `--run N`     | 丢弃原内容，从头生成     | 方向不对、需要大改 |
| **修改** | `--modify N --prompt "..."` | 原内容 + 指令 → 局部调整 | 补充细节、修改措辞 |

```bash
# 一条指令改多章（--modify 和 --prompt 可重复配对）
python src/scheduler.py \
    --modify 1 --prompt "在第一章末尾增加对LiteOS-M内核的详细分析" \
    --modify 4 --prompt "将GPIO实验的代码注释全部改成中文"

# 临时指定参考资料（覆盖 prompts.json 中的 ref_files）
python src/scheduler.py --modify 3 --prompt "补充MQTT协议细节" --ref mqtt_spec.txt wiki.txt

# 预览模式：构建 payload 但不发送请求（检查 token 用量）
python src/scheduler.py --modify 1 --prompt "加背景" --dry-run
python src/scheduler.py --batch-modify --dry-run

# 或编辑 config/modifications.json 后批量执行
python src/scheduler.py --batch-modify

# 预览所有修改指令及 token 估算
python src/scheduler.py --review-modifications

# 清除批量修改进度（中断后从头重来）
python src/scheduler.py --reset-modifications
```

> 修改不影响 `task_state.json`，失败时原内容不变。每次修改请求独立，不改其他章。

---

## 项目结构

```
Auto_Report_Agent/
├── data/
│   ├── raw/               # 原始 PDF/Word 文件
│   └── processed/         # 转换后的 txt（parser 自动生成）
├── src/
│   ├── pdf_docx_parser.py # 文档解析模块（PDF/Word → txt）
│   └── scheduler.py       # 核心调度 + CLI（通用引擎）
├── config/
│   ├── settings.json      # 项目配置（模型、窗口、人设）
│   ├── prompts.json       # 章节提示词 + 参考资料映射
│   ├── task_state.json    # 任务进度（自动管理）
│   ├── modifications.json # 批量修改指令
│   └── modification_state.json # 批量修改进度（自动管理）
├── output/
│   └── Final_Report.md    # 最终生成的报告
├── requirements.txt
└── README.md
```

---

## 常见问题

### 运行与安全

**Q: 不小心重复运行 `python src/scheduler.py` 会重写已完成的章节吗？**
A: 不会。框架通过 `task_state.json` 追踪进度，所有章节标记为 `completed` 后，再次运行会直接提示"所有章节已生成完毕"并退出，不做任何请求。

**Q: 万一半路服务器崩溃或断电？**
A: 每章生成成功后立即写入报告文件并更新 `task_state.json`。崩溃后重新运行，只会重试失败的那一章，已完成的不动。

### 上下文管理

**Q: 每次 API 请求会携带上一章的内容吗？**
A: 不会。框架采用无状态设计，每次请求只包含 `system prompt` + `当前章 prompt` + `该章的参考资料`，不携带任何历史对话或上一章的生成内容。即使写 100 章，第 100 章也拥有完整的 64K 上下文窗口。

**Q: 但参考文献太长超出窗口了怎么办？**
A: 框架内置四种截断策略自动处理。当参考资料 token 超过窗口的 `max_input_ratio`（如 75%）时，自动按你选的策略（`smart`/`head`/`tail`/`middle`）裁剪参考资料，释放空间给输出。控制台会打印截断详情。

**Q: 模型的"思考过程"（reasoning_content）会影响输出吗？**
A: 不影响。如果你的模型是 thinking 模型（如 QwQ），llama.cpp server 返回的 `reasoning_content` 会被框架直接丢弃，只保留 `content`（实际正文）。thinking 消耗的上下文属于输入侧的内部开销，不会混入报告。

### 本地部署

**Q: 为什么 `request_timeout` 是 `null`？**
A: 本地模型吞吐量有限（如 34B Q4 模型约 20 tok/s），如果窗口塞满 64K，单章可能需要 20-30 分钟。本地部署不存在 HTTP 超时问题，设为 `null` 让 httpx 无限等待更合理。

**Q: 我的 API 服务是 Ollama 还是 llama.cpp server？**
A: 框架使用 OpenAI 兼容格式（`/v1/chat/completions`），两者都支持。区别：Ollama 的健康检查是 `/api/tags`，llama.cpp server 是 `/health`。只需在 `settings.json` 里把 `api_url` 配正确就行。

### 修改与迭代

**Q: `--reset` 和 `--modify` 有什么区别？**
A: `--reset` 是"重写"——把章节标记回 pending，下次 `--run` 从零生成。`--modify` 是"修改"——读取报告中的原内容，加上你的修改指令发给 LLM，在原有基础上局部调整。修改不影响 `task_state.json`。

**Q: 修改多个章节时，上下文会混在一起吗？**
A: 不会。每条修改指令是独立请求，system prompt + 单章原内容 + 单条修改指令。改第 1 章的内容绝不会出现在改第 4 章的请求里。

**Q: 能一条命令同时改好几章吗？**
A: 能。`--modify` 和 `--prompt` 支持重复配对：

```bash
python src/scheduler.py --modify 1 --prompt "加背景" --modify 4 --prompt "改注释"
```

### 迁移与通用化

**Q: 这套框架只能写报告吗？**
A: 不是。框架本身不包含任何业务逻辑，只要能拆成"章节 + prompt + 参考资料"的长文档任务都能做：论文翻译、代码审计、API 文档生成、会议纪要整理等等。

**Q: 换项目需要改代码吗？**
A: 不需要。改 `config/settings.json`（人设、模型）和 `config/prompts.json`（章节、提示词、参考资料映射），Python 代码零改动。

---

## License

MIT License
