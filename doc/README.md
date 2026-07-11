# Auto Report Agent — 通用长文档自动化生成框架

基于本地大模型（llama.cpp_server）的长文档逐章自动生成工具。通过配置文件驱动，可复用于任意长文档撰写任务。

---

## 一、环境准备（Conda）

### 1.1 新建 Conda 环境

```bash
# 创建 Python 3.11 环境（推荐）
conda create -n auto_report python=3.11 -y

# 激活环境
conda activate auto_report

# 进入项目目录
cd C:\Users\yi'xuan\Desktop\Auto_Report_Agent
```

### 1.2 安装依赖

```bash
pip install httpx PyPDF2 python-docx
```

> 如需更好的 PDF 解析效果，可额外安装 `pip install pdfplumber PyMuPDF`（如安装失败，PyPDF2 已足够）。

### 1.3 确认 llama.cpp server 已启动

```bash
# 检查服务状态
curl http://localhost:11434/health
# 应返回 {"status":"ok"}

# 查看已加载的模型
curl http://localhost:11434/v1/models

# 如未启动（示例如下，请按你的实际模型路径执行）
llama-server.exe -m "C:\path\to\your\Qwen3.6-Q4_K_M.gguf" --port 11434 -c 65536
```
> 关键参数：`-c 65536` 设置上下文窗口为 64K，`--port 11434` 指定端口。

## 二、快速开始（例子项目：电子工艺实训报告）

### 项目已就绪

你的 10 份教学 PDF 已提取到 `data/processed/*.txt`，提示词已按报告模板设计好，可以直接开始。

### 使用流程

```bash
# 激活环境
conda activate auto_report
cd C:\Users\yi'xuan\Desktop\Auto_Report_Agent

# Step 1: 预览提示词，确保生成方向正确
python src/scheduler.py --review

# Step 2: 查看各章节需要哪些配图/视频（提前准备素材）
python src/scheduler.py --hints

# Step 3: 确认无误，开始生成
python src/scheduler.py

# Step 4: 查看进度
python src/scheduler.py --list

# Step 5: 在生成的基础上修改（保留原内容，只做局部调整）
python src/scheduler.py --modify 1 --prompt "在开头增加一段项目背景" --modify 3 --prompt "把代码注释改成中文"

# Step 6: 批量修改（编辑 config/modifications.json 后执行）
python src/scheduler.py --batch-modify
```

---

## 三、完整命令参考

| 命令 | 说明 |
|---|---|
| `python src/scheduler.py` | 按序处理所有 pending 章节（全部完成则自动跳过） |
| `python src/scheduler.py --review` | 运行前预览所有提示词（推荐先跑一次） |
| `python src/scheduler.py --hints` | 查看各章节配图/视频素材清单 |
| `python src/scheduler.py --list` | 列出章节及完成状态 |
| `python src/scheduler.py --run N` | 单独运行第 N 章（1-based） |
| `python src/scheduler.py --reset N` | 重置第 N 章为 pending |
| `python src/scheduler.py --reset N --prompt "新提示词"` | 重置并替换提示词 |
| `python src/scheduler.py --reset-all` | 全部重置 |
| `python src/scheduler.py --modify N --prompt "修改建议"` | 在第 N 章原内容基础上修改（可重复多次） |
| `--ref file1 file2` | 与 `--modify` 配合，临时指定参考资料（覆盖 prompts.json） |
| `--dry-run` | 预览模式：构建 payload 但不发送请求（可与 `--modify` 或 `--batch-modify` 配合） |
| `python src/scheduler.py --batch-modify` | 批量修改（读取 config/modifications.json） |
| `--review-modifications` | 预览 modifications.json 中的所有修改指令及 token 估算 |
| `--reset-modifications` | 清除批量修改进度（下次从头开始） |
| `python src/scheduler.py --init` | 交互式新建项目向导 |
| `python src/pdf_docx_parser.py` | 解析 data/raw/ 中的 PDF/Word 为 txt |

---

## 四、修改 vs 重写

### 两种调整方式

| 操作 | 命令 | 行为 | 适用场景 |
|---|---|---|---|
| **重写** | `--reset N` + `--run N` | 丢弃原内容，根据新提示词从零生成 | 需要大改方向、改写整章 |
| **修改** | `--modify N --prompt "..."` | 保留原内容，LLM在此基础上局部调整 | 增补内容、修改措辞、调整格式 |

### 修改的使用方式

**单条/多条命令行修改**（一次指令改多章）：
```bash
# 改一章
python src/scheduler.py --modify 1 --prompt "在开头增加一段项目背景介绍"

# 一条指令改多章
python src/scheduler.py \
    --modify 1 --prompt "在第一章末尾增加对LiteOS-M内核的详细分析" \
    --modify 4 --prompt "将GPIO实验的代码注释全部改成中文" \
    --modify 9 --prompt "补充小车避障的超声波传感器校准步骤"

# 临时指定参考资料（覆盖 prompts.json 中的 ref_files）
python src/scheduler.py --modify 3 --prompt "补充MQTT协议细节" --ref mqtt_spec.txt wiki.txt

# 预览模式：构建 payload 但不发送请求（检查 token 用量）
python src/scheduler.py --modify 1 --prompt "加背景" --dry-run
python src/scheduler.py --batch-modify --dry-run
```

**通过配置文件批量修改**（适合更多条目）：
```bash
# 1. 编辑 config/modifications.json
```
```json
[
  {"chapter_index": 0, "instruction": "在第一章开头增加一段项目背景介绍"},
  {"chapter_index": 2, "instruction": "华为云那章的价格信息更新一下"},
  {"chapter_index": 8, "instruction": "补充小车避障的具体延迟数据"}
]
```
```bash
# 2. 一键执行
python src/scheduler.py --batch-modify
```

### 批量修改管理

```bash
# 预览所有修改指令及 token 估算
python src/scheduler.py --review-modifications

# 清除批量修改进度（中断后从头重来）
python src/scheduler.py --reset-modifications
```

### 注意事项

- 修改不会改变 `task_state.json`，只是原地替换报告内容
- 修改失败时保留原内容不变，不会造成数据丢失
- `--reset` = 重写；`--modify` = 修改。选对工具很重要

---

## 五、上下文窗口管理

### 机制说明

脚本会自动检测每次请求的 token 用量：
- 中文汉字按 ~1.5 token/字估算，ASCII 按 ~0.3 token/字估算
- 当参考资料 + 提示词超过窗口的 75%（max_input_ratio）时，**自动截断**参考资料
- 截断策略（truncation_strategy），四种可选：
  - `smart`：保留开头 + 末尾，砍中间（默认，适用大部分场景）
  - `head`：只保留开头（适用重要性递减的文档）
  - `tail`：只保留结尾（适用结论在末尾的文档）
  - `middle`：保留中间，砍首尾（适用摘要式截断）

### 配置项（config/settings.json）

```json
{
  "context_window_tokens": 65536,   // 你的 llama.cpp server 模型上下文窗口（-c 参数）
  "max_input_ratio": 0.75,          // 输入占比上限（留 25% 给输出）
  "truncation_strategy": "smart",   // 截断策略：head/tail/middle/smart
  "truncation_keep_ratio": 0.68     // 截断时保留的内容比例
}
```

### 运行时日志示例

```
[生成中] MQTT通信实验 (第 8 章)
  [用量] 输入 ~8234 tokens / 窗口 56000 (14.7%)
  [请求] 第 1 次尝试 ...
  [成功] 获取回复，长度: 3245 字符
  [输出] 生成了 3245 字符 (~2300 tokens)
```

如果参考资料太大被截断，会显示：
```
  [上下文] 参考资料 45000 tokens → 截断至约 25000 tokens (窗口 56000, 上限 42000)
  [用量] 输入 ~32000 tokens / 窗口 56000 (57.1%) [已截断参考资料]
```

---

## 六、多模态视觉模型支持（实验性）

当 `settings.json` 中设置 `"vision_model": true` 时，框架会自动将 `ref_files` 中的图片文件以 base64 编码嵌入请求，发送给支持多模态的模型（如 LLaVA、Qwen-VL 等）。

### 配置项

```json
{
  "vision_model": true,
  "vision_image_tokens": 512
}
```

### 工作原理

- 图片文件（png/jpg/jpeg/bmp/gif/webp/svg）自动识别并转为 base64 data URL
- 非图片文件正常以文本形式加载
- 图片以 `image_url` 格式嵌入 multimodal content 数组
- `vision_image_tokens` 用于估算图片占用的上下文空间（每张图按此值计入 token 预算）

### 支持的图片格式

`.png`, `.jpg`, `.jpeg`, `.bmp`, `.gif`, `.webp`, `.svg`

> 未开启 `vision_model` 时，图片文件会被跳过（仅加载文本文件）。

---

## 六、项目文件结构

```
Auto_Report_Agent/
├── data/
│   ├── raw/               # 原始 PDF/Word（放这里后运行 parser）
│   └── processed/         # 转换后的 txt（自动生成）
├── src/
│   ├── pdf_docx_parser.py # 文档解析模块
│   └── scheduler.py       # 核心调度 + CLI
├── config/
│   ├── settings.json      # 【你改】项目配置（模型、窗口、人设）
│   ├── prompts.json       # 【你改】章节提示词 + 配图建议
│   ├── task_state.json    # 进度追踪（自动管理）
│   ├── modifications.json # 【你改】批量修改指令
│   └── modification_state.json # 批量修改进度（自动管理）
├── output/
│   └── Final_Report.md    # 最终生成的报告
└── README.md              # 本文件
```

---

## 七、迁移到其他项目

### 方法一：命令行向导（推荐）

```bash
python src/scheduler.py --init
```

按提示输入：项目名 → 模型名 → API 地址 → 温度 → 系统提示词 → 章节数。向导会自动创建 `settings.json` 和 `prompts.json` 骨架。

### 方法二：手动配置

```bash
# 1. 复制项目文件夹
cp -r Auto_Report_Agent My_New_Project

# 2. 编辑 config/settings.json
#    - project_name: 新项目名
#    - system_prompt: 新的系统提示词
#    - context_window_tokens: 你的新模型窗口

# 3. 编辑 config/prompts.json
#    - 修改 chapter 名称和 prompt
#    - 设置 ref_files 指向对应参考资料（数组格式）
#    - 设置 image_hints 配图建议

# 4. 放入新项目的 PDF/Word 到 data/raw/
# 5. 运行解析
python src/pdf_docx_parser.py

# 6. 预览确认
python src/scheduler.py --review

# 7. 开跑
python src/scheduler.py
```

### prompts.json 格式说明

```json
[
  {
    "chapter": "章节名称（会作为 Markdown 二级标题）",
    "prompt": "生成这一章的具体要求（User Prompt）",
    "ref_files": ["3.txt"],         // data/processed/ 中的参考资料（支持多文件数组）
    "image_hints": [                // 该章建议插入的配图/视频
      "ADC按键接线图",
      "串口监视器ADC读数截图"
    ]
  }
]
```

---

## 八、常见问题

### Q: 资料已经在 data/raw/ 里了，还要再跑 parser 吗？
A: 如果 `data/processed/` 里已经有对应的 `.txt` 文件就不需要。parser 只做 PDF/Word → txt 转换，有 txt 就能直接跑调度器。

### Q: 怎么知道我的 llama.cpp server 上下文窗口多大？
A: 启动参数中的 `-c` 就是上下文窗口，也可通过 `curl http://localhost:11434/v1/models` 查看 `n_ctx` 字段。

### Q: 生成的报告中文乱码？
A: 所有文件都用 UTF-8 编码，用 VS Code / Typora / Notepad++ 打开即可。

### Q: 想换模型（比如从 Qwen3.6 换到 DeepSeek）？
A: 编辑 `config/settings.json` 中的 `model` 字段，并相应调整 `context_window_tokens`。

### Q: 某章生成了但内容不完整？
A: 可能是输出被截断。检查 `context_window_tokens` 是否设得够大，或调低 `max_input_ratio` 给输出留更多空间。
