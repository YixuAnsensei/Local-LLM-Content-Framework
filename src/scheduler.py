"""
状态机调度模块 - scheduler.py
通用长文档自动化生成框架。通过配置文件驱动，可复用于任意项目。

用法：
  python src/scheduler.py                  # 按序处理所有 pending 章节
  python src/scheduler.py --review         # 【新】预览所有提示词，确认后再跑
  python src/scheduler.py --init           # 【新】交互式新建项目向导
  python src/scheduler.py --list           # 列出所有章节及状态
  python src/scheduler.py --reset N        # 重置第 N 章
  python src/scheduler.py --reset N --prompt "新提示词"  # 重置并改提示词
  python src/scheduler.py --reset-all      # 全部重置
  python src/scheduler.py --run N          # 单独运行第 N 章
  python src/scheduler.py --modify N1 --prompt "建议1" --modify N2 --prompt "建议2"   # 修改多章
  python src/scheduler.py --batch-modify   # 批量修改（读取 config/modifications.json）
"""

import argparse
import asyncio
import base64
import json
import mimetypes
import sys
from pathlib import Path

import httpx

# 支持的图片/视频扩展名
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}


# ==================== 默认设置（settings.json 不存在时使用） ====================
DEFAULT_SETTINGS = {
    "project_name": "未命名项目",
    "model": "Qwen3.6",
    "api_url": "http://localhost:11434/v1/chat/completions",
    "temperature": 0.3,
    "request_timeout": 600,
    "max_retries": 3,
    "output_file": "output/Final_Report.md",
    "system_prompt": "你是一个专业的技术文档撰写者。严格输出 Markdown 正文，不要包含分析过程和寒暄。",
    # 上下文窗口管理
    "context_window_tokens": 56000,
    "max_input_ratio": 0.75,
    "truncation_strategy": "smart",
    "truncation_keep_ratio": 0.6,
    # 多模态 / 视觉模型支持（实验性）
    "vision_model": False,
    "_vision_comment": "设为 true 以启用多模态支持（需要模型支持图像输入）。启用后 ref_files 可包含图片文件（.png/.jpg），框架会自动编码为 base64 data URL。",
    "vision_image_tokens": 512,
    "_vision_image_comment": "每张图片的 token 估算值（取决于模型视觉编码器，常见范围 256-1024）。",
}

# prompts.json 不存在时使用的默认提示词（最小示例）
DEFAULT_PROMPTS = [
    {
        "chapter": "第1章 示例章节",
        "prompt": "请撰写关于该主题的详细内容。如需自定义，请编辑 config/prompts.json 文件。",
        "ref_files": [],
        "image_hints": [],
    },
]


# ==================== 文件工具 ====================

def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_json(path: Path, default: dict | list) -> dict | list:
    """读取 JSON 文件，不存在时用 default 创建。"""
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(default, f, ensure_ascii=False, indent=2)
    return default


def save_json(path: Path, data: dict | list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==================== 上下文窗口管理 ====================

def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量。
    中文汉字 ~1.5 token/字，ASCII ~0.3 token/字，其余 ~1.0 token/字。
    这是针对中英文混合 Ollama 模型的启发式估算，误差在 ±15% 以内。
    """
    chinese = 0
    ascii_chars = 0
    other = 0
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef':
            chinese += 1
        elif ord(ch) < 128:
            ascii_chars += 1
        else:
            other += 1
    return int(chinese * 1.5 + ascii_chars * 0.3 + other * 1.0)


def _truncate_head(text: str, max_chars: int, keep_ratio: float) -> str:
    """head 策略：只保留开头，砍掉后面所有内容。"""
    keep_chars = min(int(len(text) * keep_ratio), max_chars)
    return (
        f"{text[:keep_chars]}\n\n"
        f"... [上下文管理：参考资料过长，保留了开头 {int(keep_ratio * 100)}% 的内容] ..."
    )


def _truncate_tail(text: str, max_chars: int, keep_ratio: float) -> str:
    """tail 策略：只保留结尾，砍掉前面内容。"""
    keep_chars = min(int(len(text) * (1 - keep_ratio)), max_chars)
    return (
        f"... [上下文管理：参考资料过长，保留了末尾 {int((1 - keep_ratio) * 100)}% 的内容] ...\n\n"
        f"{text[-keep_chars:]}"
    )


def _truncate_middle(text: str, max_chars: int, keep_ratio: float) -> str:
    """middle 策略：保留中间部分，砍掉首尾。"""
    total = len(text)
    head_cut = int(total * (1 - keep_ratio) / 2)
    tail_cut = int(total * (1 - keep_ratio) / 2)
    # 中间部分不超过 max_chars
    mid_text = text[head_cut:total - tail_cut]
    if len(mid_text) > max_chars:
        mid_text = mid_text[:max_chars]
    return (
        f"... [上下文管理：参考资料过长，保留了中间 {int(keep_ratio * 100)}% 的内容] ...\n\n"
        f"{mid_text}\n\n"
        f"... [上下文管理：截断结束] ..."
    )


def _truncate_smart(text: str, max_chars: int, keep_ratio: float) -> str:
    """smart 策略：保留开头 + 末尾，砍掉中间（当前默认策略）。"""
    total = len(text)
    head_chars = int(total * keep_ratio)
    tail_chars = int(total * (1 - keep_ratio) * 0.3)  # 末尾保留尾部空间的 30%
    head_part = text[:head_chars]
    tail_part = text[-tail_chars:] if tail_chars > 0 else ""

    result = (
        f"{head_part}\n\n"
        f"... [上下文管理：参考资料过长，保留了开头 {int(keep_ratio * 100)}% "
        f"和末尾 {int((1 - keep_ratio) * 30)}% 的内容] ...\n\n"
        f"{tail_part}"
    )

    # 如果还是超限，退化为只保留开头 50%
    if estimate_tokens(result) > estimate_tokens(text) * 0.5 + 100:
        result = _truncate_head(text, max_chars, 0.5)

    return result


# 策略分发表：名称 → 实现函数
TRUNCATION_STRATEGIES = {
    "head": _truncate_head,
    "tail": _truncate_tail,
    "middle": _truncate_middle,
    "smart": _truncate_smart,
}


def apply_truncation(
    text: str, max_tokens: int, strategy: str = "smart", keep_ratio: float = 0.68
) -> tuple[str, bool]:
    """
    统一截断入口。根据 strategy 名称调用对应策略函数。
    返回 (截断后文本, 是否发生了截断)。
    """
    total_tokens = estimate_tokens(text)
    if total_tokens <= max_tokens:
        return text, False

    # tokens → 近似字符数（中文 ~0.67 字/token，英文 ~3.3 字/token，取近似）
    max_chars = int(max_tokens * 2)

    # 从分发表查找策略，找不到则回退到 smart
    strategy_fn = TRUNCATION_STRATEGIES.get(strategy, _truncate_smart)
    result = strategy_fn(text, max_chars, keep_ratio)
    return result, True


def build_context_aware_payload(
    prompt: str, ref_content: str, settings: dict,
    image_refs: list[Path] | None = None,
) -> tuple[dict, dict]:
    """
    组装 LLM 请求 Payload，含上下文窗口管理 + 多模态图片支持。
    返回 (payload, stats_dict)，其中 stats_dict 包含 token 用量信息。
    """
    image_refs = image_refs or []
    context_window = settings.get("context_window_tokens", 56000)
    max_input_ratio = settings.get("max_input_ratio", 0.75)
    truncation_strategy = settings.get("truncation_strategy", "smart")
    truncation_keep_ratio = settings.get("truncation_keep_ratio", 0.6)

    max_input_tokens = int(context_window * max_input_ratio)
    system_prompt = settings["system_prompt"]

    # 图片 token 开销
    img_token_cost = estimate_image_tokens(len(image_refs), settings)

    # 先算固定开销（system + prompt 框架 + 图片）
    system_tokens = estimate_tokens(system_prompt)
    prompt_tokens = estimate_tokens(prompt)
    overhead = system_tokens + prompt_tokens + img_token_cost + 200

    # 计算参考资料可用空间
    ref_available = max_input_tokens - overhead
    ref_original_tokens = estimate_tokens(ref_content)

    was_truncated = False
    if ref_original_tokens > ref_available and ref_available > 0:
        ref_content, was_truncated = apply_truncation(
            ref_content, ref_available, truncation_strategy, truncation_keep_ratio
        )
        print(f"  [上下文] 参考资料 {ref_original_tokens} tokens → "
              f"截断至约 {estimate_tokens(ref_content)} tokens "
              f"(窗口 {context_window}, 上限 {max_input_tokens})")

    user_message = f"{prompt}\n\n参考资料如下：\n{ref_content}"
    if image_refs:
        user_message += f"\n\n（附 {len(image_refs)} 张参考图片）"

    total_input_tokens = estimate_tokens(user_message) + system_tokens + img_token_cost + 200

    # 构建 content（文本或文本+图片多模态）
    user_content = build_multimodal_content(user_message, image_refs, settings)

    payload = {
        "model": settings["model"],
        "temperature": settings["temperature"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    stats = {
        "context_window": context_window,
        "max_input": max_input_tokens,
        "total_input": total_input_tokens,
        "system_tokens": system_tokens,
        "prompt_tokens": prompt_tokens,
        "ref_original_tokens": ref_original_tokens,
        "ref_final_tokens": estimate_tokens(ref_content),
        "truncated": was_truncated,
        "usage_pct": round(total_input_tokens / context_window * 100, 1),
        "image_count": len(image_refs),
        "image_tokens": img_token_cost,
    }

    return payload, stats


def load_settings(settings_path: Path) -> dict:
    """加载项目设置。"""
    return load_json(settings_path, DEFAULT_SETTINGS)


def load_prompts(prompts_path: Path) -> list[dict]:
    """加载章节提示词，向后兼容旧 ref_file 格式。"""
    prompts = load_json(prompts_path, DEFAULT_PROMPTS)
    for p in prompts:
        p.setdefault("image_hints", [])
        # 向后兼容：旧的 ref_file (string) → 新的 ref_files (array)
        if "ref_file" in p and "ref_files" not in p:
            raw = p.pop("ref_file")
            p["ref_files"] = [raw] if raw else []
        if "ref_files" not in p:
            p["ref_files"] = []
    return prompts


def load_task_state(state_path: Path, chapter_count: int) -> dict[str, str]:
    """加载任务状态，不存在时全部初始化为 pending。"""
    default_state = {str(i): "pending" for i in range(chapter_count)}
    state = load_json(state_path, default_state)
    # 确保 key 对齐（章节数可能变化）
    for i in range(chapter_count):
        state.setdefault(str(i), "pending")
    return state


def load_ref_content(processed_dir: Path, ref_files: list[str]) -> str:
    """
    读取并合并多个参考文献文本。

    自动跳过图片/视频等二进制文件（文本模型不支持）。
    如需视觉模型支持，请在 settings.json 中设置 "vision_model": true。

    Args:
        processed_dir: data/processed/ 目录
        ref_files: 文件列表，如 ["7.txt", "8.txt"]

    Returns:
        合并后的文本内容，文件间用分隔线隔开。
    """
    if not ref_files:
        return "（本小节无指定参考资料，请根据该主题的通用知识撰写。）"

    parts = []
    found_any = False
    for fname in ref_files:
        ref_path = processed_dir / fname
        if not ref_path.exists():
            print(f"  [提示] 参考资料不存在: {fname}，已跳过。")
            continue

        ext = ref_path.suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            print(f"  [提示] {fname} 是图片文件，当前文本模型不支持。"
                  f" 如需图像输入请开启 settings.json 中的 vision_model。")
            continue
        if ext in VIDEO_EXTENSIONS:
            print(f"  [提示] {fname} 是视频文件，暂不支持直接作为参考资料。"
                  f" 建议提取关键帧保存为图片后引入。")
            continue

        try:
            content = ref_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"  [提示] {fname} 无法以 UTF-8 解码（可能是二进制文件），已跳过。")
            continue

        parts.append(f"--- 参考资料: {fname} ---\n{content}")
        found_any = True

    if not found_any:
        return "（指定的参考资料均未找到，请根据该章节主题结合通用知识撰写。）"

    return "\n\n".join(parts)


def encode_image_to_data_url(file_path: Path) -> str:
    """读取图片文件，返回 base64 data: URL 字符串。"""
    mime_type = mimetypes.guess_type(str(file_path))[0] or "image/png"
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def collect_image_refs(processed_dir: Path, ref_files: list[str]) -> list[Path]:
    """从 ref_files 中收集实际存在的图片文件路径。"""
    images = []
    for fname in ref_files:
        ref_path = processed_dir / fname
        if ref_path.exists() and ref_path.suffix.lower() in IMAGE_EXTENSIONS:
            images.append(ref_path)
    return images


def build_multimodal_content(
    text: str, image_paths: list[Path], settings: dict
) -> str | list[dict]:
    """
    构建消息 content 字段。
    - 无图片或 vision_model 关闭 → 返回纯文本字符串
    - 有图片且 vision_model 开启 → 返回内容数组 [{"type":"text",...}, {"type":"image_url",...}]
    """
    if not image_paths or not settings.get("vision_model", False):
        return text

    content_parts: list[dict] = [{"type": "text", "text": text}]
    for img_path in image_paths:
        try:
            data_url = encode_image_to_data_url(img_path)
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
            print(f"  [视觉] 已编码图片: {img_path.name} ({len(data_url)} 字符 base64)")
        except Exception as e:
            print(f"  [警告] 图片编码失败 {img_path.name}: {e}")
    return content_parts


def estimate_image_tokens(image_count: int, settings: dict) -> int:
    """估算图片占用的 token 数。"""
    return image_count * settings.get("vision_image_tokens", 512)


def build_payload(prompt: str, ref_content: str, settings: dict) -> dict:
    """组装 LLM 请求 Payload。"""
    user_message = f"{prompt}\n\n参考资料如下：\n{ref_content}"
    return {
        "model": settings["model"],
        "temperature": settings["temperature"],
        "messages": [
            {"role": "system", "content": settings["system_prompt"]},
            {"role": "user", "content": user_message},
        ],
    }


# ==================== 异步 HTTP 调用 ====================

async def call_llm(
    client: httpx.AsyncClient, payload: dict, settings: dict
) -> str | None:
    """异步调用 LLM API，带重试。成功返回 content，失败返回 None。"""
    max_retries = settings.get("max_retries", 3)
    timeout = settings.get("request_timeout", 600)
    api_url = settings["api_url"]

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  [请求] 第 {attempt} 次尝试 ...")
            response = await client.post(api_url, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            print(f"  [成功] 获取回复，长度: {len(content)} 字符")
            return content

        except httpx.TimeoutException:
            print(f"  [超时] 第 {attempt} 次请求超时（{timeout}s）")
        except httpx.HTTPStatusError as e:
            print(f"  [HTTP错误] 第 {attempt} 次: {e.response.status_code}")
        except (httpx.RequestError, KeyError, IndexError) as e:
            print(f"  [请求异常] 第 {attempt} 次: {e}")

        if attempt < max_retries:
            wait_seconds = attempt * 2
            print(f"  [等待] {wait_seconds} 秒后重试 ...")
            await asyncio.sleep(wait_seconds)

    print(f"  [失败] 已重试 {max_retries} 次，放弃该任务。")
    return None


# ==================== 图片位置提示 ====================

def print_image_hints(prompt_def: dict) -> None:
    """在章节生成后打印该章节建议插入图片/视频的位置。"""
    hints = prompt_def.get("image_hints", [])
    if not hints:
        return
    print(f"\n  {'─' * 50}")
    print(f"  [配图/视频建议] 请在最终报告中补充以下素材：")
    for i, hint in enumerate(hints, 1):
        print(f"    {i}. {hint}")
    print(f"  {'─' * 50}")


# ==================== 报告章节解析与替换 ====================

def parse_report_chapter(report_path: Path, chapter_name: str, all_names: list[str]) -> str | None:
    """
    从报告中解析指定章节的当前内容。

    匹配策略（按优先级）：
    1. 精确匹配 \n\n## {chapter_name}\n\n
    2. 前缀回退：若 chapter_name 含 " — "，尝试用第一个 " — " 前的前缀匹配
       （容错 prompt.json 章节名被改过后跟报告标题不一致的情况）

    Returns:
        章节正文内容，若报告不存在或找不到该章节则返回 None。
    """
    if not report_path.exists():
        return None

    content = report_path.read_text(encoding="utf-8")
    marker = f"\n\n## {chapter_name}\n\n"
    start = content.find(marker)

    matched_name = chapter_name
    if start == -1 and " — " in chapter_name:
        # 前缀回退：尝试只用第一个 " — " 前的部分作为标题匹配
        short_name = chapter_name.split(" — ")[0]
        marker = f"\n\n## {short_name}\n\n"
        start = content.find(marker)
        if start != -1:
            matched_name = short_name
            print(f"  [提示] 章节名已变更，使用前缀匹配: \"{short_name}\" ← \"{chapter_name}\"")

    if start == -1:
        return None

    body_start = start + len(marker)

    # 找到下一个顶级章节的位置（排除自身，也排除前缀匹配的同名短标题）
    next_boundary = len(content)
    for name in all_names:
        if name == chapter_name:
            continue
        # 同时检查精确名和前缀名
        for candidate in {name, name.split(" — ")[0]}:
            next_marker = f"\n\n## {candidate}\n\n"
            pos = content.find(next_marker, body_start)
            if pos != -1 and pos < next_boundary:
                next_boundary = pos

    return content[body_start:next_boundary]


def replace_chapter_in_report(
    report_path: Path, chapter_name: str, new_content: str, all_names: list[str]
) -> bool:
    """
    将新内容替换到报告中指定章节的位置。
    匹配策略同 parse_report_chapter（精确 + 前缀回退）。
    """
    if not report_path.exists():
        return False

    content = report_path.read_text(encoding="utf-8")
    marker = f"\n\n## {chapter_name}\n\n"
    start = content.find(marker)

    if start == -1 and " — " in chapter_name:
        short_name = chapter_name.split(" — ")[0]
        marker = f"\n\n## {short_name}\n\n"
        start = content.find(marker)

    if start == -1:
        return False

    body_start = start + len(marker)

    # 找下一个顶级章节边界（同 parse_report_chapter 的前缀回退逻辑）
    next_boundary = len(content)
    for name in all_names:
        if name == chapter_name:
            continue
        for candidate in {name, name.split(" — ")[0]}:
            next_marker = f"\n\n## {candidate}\n\n"
            pos = content.find(next_marker, body_start)
            if pos != -1 and pos < next_boundary:
                next_boundary = pos

    new_report = content[:body_start] + new_content + content[next_boundary:]
    report_path.write_text(new_report, encoding="utf-8")
    return True


# ==================== 核心调度 ====================

async def process_single_chapter(
    client: httpx.AsyncClient,
    index: int,
    prompt_def: dict,
    settings: dict,
    processed_dir: Path,
    output_path: Path,
) -> bool:
    """处理单个章节：调用 LLM 生成并追加写入报告。"""
    chapter = prompt_def["chapter"]
    context_window = settings.get("context_window_tokens", 56000)
    print(f"\n{'=' * 60}")
    print(f"[生成中] {chapter} (第 {index + 1} 章)")

    ref_files = prompt_def.get("ref_files", [])
    image_refs = collect_image_refs(processed_dir, ref_files)
    text_refs = [f for f in ref_files if (processed_dir / f).suffix.lower() not in IMAGE_EXTENSIONS]
    ref_content = load_ref_content(processed_dir, text_refs)
    payload, stats = build_context_aware_payload(
        prompt_def["prompt"], ref_content, settings, image_refs
    )

    # 打印 token 用量
    extra = ""
    if stats.get("image_count"):
        extra += f" + {stats['image_count']} 张图片 (~{stats['image_tokens']} tokens)"
    print(f"  [用量] 输入 ~{stats['total_input']} tokens / 窗口 {stats['context_window']} "
          f"({stats['usage_pct']}%)" + (" [已截断参考资料]" if stats['truncated'] else "") + extra)

    if stats['usage_pct'] > 90:
        print(f"  [警告] 输入占比高于 90%，生成输出空间可能不足！建议调大 context_window_tokens。")

    result = await call_llm(client, payload, settings)

    if result is None:
        print(f"[中断] {chapter} 生成失败，保留 pending 状态。")
        return False

    try:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n## {chapter}\n\n")
            f.write(result)
            f.write("\n")
        print(f"[写入] {chapter} → {output_path.name}")
        print(f"  [输出] 生成了 {len(result)} 字符 (~{estimate_tokens(result)} tokens)")
    except OSError as e:
        print(f"[写入错误] 无法写入 {output_path}: {e}")
        return False

    # 打印配图建议
    print_image_hints(prompt_def)
    return True


async def modify_single_chapter(
    client: httpx.AsyncClient,
    index: int,
    prompt_def: dict,
    instruction: str,
    settings: dict,
    output_path: Path,
    all_chapter_names: list[str],
    processed_dir: Path,
) -> bool:
    """
    修改单个章节：读取现有内容 + 参考资料，发给 LLM 按指令修改，原地替换。
    失败时保留原内容，不做替换。
    """
    chapter = prompt_def["chapter"]
    print(f"\n{'=' * 60}")
    print(f"[修改中] {chapter} (第 {index + 1} 章)")

    # 解析现有内容
    existing = parse_report_chapter(output_path, chapter, all_chapter_names)
    if existing is None:
        print(f"[错误] 报告中找不到章节: {chapter}")
        return False

    existing_tokens = estimate_tokens(existing)
    print(f"  [现有内容] {len(existing)} 字符 (~{existing_tokens} tokens)")

    # 加载参考资料（分离文本与图片）
    ref_files = prompt_def.get("ref_files", [])
    image_refs = collect_image_refs(processed_dir, ref_files)
    text_refs = [f for f in ref_files if (processed_dir / f).suffix.lower() not in IMAGE_EXTENSIONS]
    ref_content = load_ref_content(processed_dir, text_refs)
    ref_tokens = estimate_tokens(ref_content)
    if ref_files:
        has_ref = any((processed_dir / f).exists() for f in ref_files)
        if has_ref:
            img_extra = f" + {len(image_refs)} 张图片" if image_refs else ""
            print(f"  [参考资料] {', '.join(ref_files)} (~{ref_tokens} tokens{img_extra})")

    # 组装修改 prompt
    instruction_header = "请根据以下要求对上述章节内容进行修改，保持整体结构和风格不变，只做局部调整：\n\n"
    instruction_part = f"{instruction_header}{instruction}"

    # 上下文窗口管理：参考资料 + 图片 + 现有内容 + 指令可能超出窗口
    context_window = settings.get("context_window_tokens", 56000)
    max_input_ratio = settings.get("max_input_ratio", 0.75)
    truncation_strategy = settings.get("truncation_strategy", "smart")
    truncation_keep_ratio = settings.get("truncation_keep_ratio", 0.68)
    max_input_tokens = int(context_window * max_input_ratio)

    img_token_cost = estimate_image_tokens(len(image_refs), settings)
    system_tokens = estimate_tokens(settings["system_prompt"])
    instruction_tokens = estimate_tokens(instruction_part)
    fixed_overhead = system_tokens + existing_tokens + instruction_tokens + img_token_cost + 300

    # 参考资料可用空间
    ref_available = max_input_tokens - fixed_overhead
    ref_was_truncated = False
    if ref_available > 0 and ref_tokens > ref_available:
        ref_content, ref_was_truncated = apply_truncation(
            ref_content, ref_available, truncation_strategy, truncation_keep_ratio
        )
        print(f"  [上下文] 参考资料 {ref_tokens} tokens → "
              f"截断至约 {estimate_tokens(ref_content)} tokens "
              f"(窗口 {context_window}, 修改请求上限 {max_input_tokens})")

    # 组装 user_message
    ref_section = f"\n\n参考资料：\n{ref_content}" if ref_content.strip() else ""
    if image_refs:
        ref_section += f"\n\n（附 {len(image_refs)} 张参考图片）"
    user_message = (
        f"以下是已生成的【{chapter}】章节内容：\n\n"
        f"{existing}"
        f"{ref_section}\n\n"
        f"{instruction_part}"
    )

    total_tokens = estimate_tokens(user_message) + system_tokens + img_token_cost + 200
    usage_pct = round(total_tokens / context_window * 100, 1)
    img_info = f" + {len(image_refs)} 图 (~{img_token_cost} tokens)" if image_refs else ""
    print(f"  [用量] 修改请求 ~{total_tokens} tokens / 窗口 {context_window} ({usage_pct}%)"
          + (" [已截断参考资料]" if ref_was_truncated else "") + img_info)

    if usage_pct > 90:
        print(f"  [警告] 输入占比高于 90%，生成输出空间可能不足！")

    # 构建 content（文本或文本+图片多模态）
    user_content = build_multimodal_content(user_message, image_refs, settings)

    payload = {
        "model": settings["model"],
        "temperature": settings["temperature"],
        "messages": [
            {"role": "system", "content": settings["system_prompt"]},
            {"role": "user", "content": user_content},
        ],
    }

    result = await call_llm(client, payload, settings)

    if result is None:
        print(f"[中断] {chapter} 修改失败，保留原内容不变。")
        return False

    # 原地替换
    if replace_chapter_in_report(output_path, chapter, result, all_chapter_names):
        print(f"[写入] {chapter} → {output_path.name} (替换成功)")
        print(f"  [输出] 生成了 {len(result)} 字符 (~{estimate_tokens(result)} tokens)")
        return True
    else:
        print(f"[错误] 替换章节内容失败，保留原内容。")
        return False


async def run_scheduler(run_index: int | None = None, show_hints_only: bool = False) -> None:
    """
    核心调度入口。

    Args:
        run_index: 指定只运行该索引（0-based）的章节；None 则全部 pending
        show_hints_only: 仅显示各章节配图建议，不做生成
    """
    project_root = get_project_root()
    settings_path = project_root / "config" / "settings.json"
    prompts_path = project_root / "config" / "prompts.json"
    state_path = project_root / "config" / "task_state.json"
    processed_dir = project_root / "data" / "processed"

    settings = load_settings(settings_path)
    prompts = load_prompts(prompts_path)
    state = load_task_state(state_path, len(prompts))
    output_path = project_root / settings["output_file"]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 仅显示配图建议模式
    if show_hints_only:
        print(f"\n{'=' * 60}")
        print(f"  各章节配图/视频素材建议")
        print(f"{'=' * 60}")
        for i, p in enumerate(prompts):
            hints = p.get("image_hints", [])
            print(f"\n  [{i + 1}] {p['chapter']}")
            if hints:
                for j, h in enumerate(hints, 1):
                    print(f"      {j}. {h}")
            else:
                print(f"      （无特定建议）")
        print(f"\n{'=' * 60}")
        return

    # 确定待处理章节
    if run_index is not None:
        if run_index < 0 or run_index >= len(prompts):
            print(f"[错误] 章节编号 {run_index + 1} 超出范围（共 {len(prompts)} 章）")
            return
        pending_indices = [run_index]
        state[str(run_index)] = "pending"
    else:
        pending_indices = [
            i for i in range(len(prompts)) if state.get(str(i)) != "completed"
        ]

    if not pending_indices:
        print("[完成] 所有章节已生成完毕。")
        print("如需重新生成某章: python src/scheduler.py --reset N")
        return

    print(f"\n{'=' * 60}")
    print(f"  项目: {settings['project_name']}")
    print(f"  章节: {len(prompts)} 个，待处理 {len(pending_indices)} 个")
    print(f"  模型: {settings['model']}  |  API: {settings['api_url']}")
    ctx = settings.get("context_window_tokens", 56000)
    ratio = settings.get("max_input_ratio", 0.75)
    print(f"  上下文: {ctx} tokens (输入上限 {int(ctx * ratio)})")
    print(f"{'=' * 60}")

    async with httpx.AsyncClient() as client:
        for idx in pending_indices:
            prompt_def = prompts[idx]
            success = await process_single_chapter(
                client, idx, prompt_def, settings, processed_dir, output_path
            )

            if success:
                state[str(idx)] = "completed"
                save_json(state_path, state)
                print(f"[完成] {prompt_def['chapter']} ✓")
            else:
                save_json(state_path, state)
                print("请检查 API 服务后重新运行: python src/scheduler.py")
                return

    print(f"\n{'=' * 60}")
    print("[全部完成] 报告已生成完毕！")
    print(f"输出文件: {output_path}")
    print()
    print("提示: 运行 `python src/scheduler.py --hints` 可查看各章节需要的配图/视频清单。")


# ==================== CLI 命令 ====================

def cmd_review(settings_path: Path, prompts_path: Path, state_path: Path) -> bool:
    """
    预览模式：展示所有 prompt，询问是否继续。
    返回 True 表示用户确认继续。
    """
    settings = load_settings(settings_path)
    prompts = load_prompts(prompts_path)
    state = load_task_state(state_path, len(prompts))

    print(f"\n{'=' * 60}")
    print(f"  项目: {settings['project_name']}")
    print(f"  模型: {settings['model']}")
    print(f"  系统提示词: {settings['system_prompt'][:80]}...")
    print(f"{'=' * 60}")

    for i, p in enumerate(prompts):
        status = state.get(str(i), "pending")
        icon = "✓" if status == "completed" else "○"
        print(f"\n  [{icon}] {p['chapter']}")
        print(f"      提示词: {p['prompt'][:100]}...")
        refs = p.get("ref_files", [])
        if refs:
            print(f"      参考资料: {', '.join(refs)} ({len(refs)} 个文件)")
        hints = p.get("image_hints", [])
        if hints:
            print(f"      配图建议: {len(hints)} 处")
        else:
            print(f"      配图建议: 无")

    print(f"\n{'=' * 60}")
    print("以上是全部章节的提示词预览。")
    print("如需修改，请编辑 config/prompts.json 后重新运行。")
    print("确认无误后运行: python src/scheduler.py")
    return True


def cmd_init(project_root: Path) -> None:
    """交互式新建项目向导。"""
    settings_path = project_root / "config" / "settings.json"
    prompts_path = project_root / "config" / "prompts.json"

    print("\n" + "=" * 60)
    print("  通用长文档自动化框架 — 新建项目向导")
    print("=" * 60)
    print()
    print("按 Enter 使用默认值（括号内），输入 q 退出。")
    print()

    try:
        name = input(f"项目名称 ({DEFAULT_SETTINGS['project_name']}): ").strip()
        if name.lower() == "q":
            return
        name = name or DEFAULT_SETTINGS["project_name"]

        model = input(f"模型名称 ({DEFAULT_SETTINGS['model']}): ").strip()
        if model.lower() == "q":
            return
        model = model or DEFAULT_SETTINGS["model"]

        api = input(f"API 地址 ({DEFAULT_SETTINGS['api_url']}): ").strip()
        if api.lower() == "q":
            return
        api = api or DEFAULT_SETTINGS["api_url"]

        temp_str = input(f"温度/temperature ({DEFAULT_SETTINGS['temperature']}): ").strip()
        if temp_str.lower() == "q":
            return
        temp = float(temp_str) if temp_str else DEFAULT_SETTINGS["temperature"]

        sys_prompt = input(f"系统提示词 ({DEFAULT_SETTINGS['system_prompt'][:60]}...): ").strip()
        if sys_prompt.lower() == "q":
            return
        sys_prompt = sys_prompt or DEFAULT_SETTINGS["system_prompt"]

        num_chapters = input(f"章节数量 (1): ").strip()
        if num_chapters.lower() == "q":
            return
        num_chapters = int(num_chapters) if num_chapters else 1
    except (ValueError, EOFError, KeyboardInterrupt):
        print("\n[取消] 已退出向导。")
        return

    # 写入 settings.json
    settings = {
        "project_name": name,
        "model": model,
        "api_url": api,
        "temperature": temp,
        "request_timeout": DEFAULT_SETTINGS["request_timeout"],
        "max_retries": DEFAULT_SETTINGS["max_retries"],
        "output_file": DEFAULT_SETTINGS["output_file"],
        "system_prompt": sys_prompt,
        "context_window_tokens": DEFAULT_SETTINGS["context_window_tokens"],
        "max_input_ratio": DEFAULT_SETTINGS["max_input_ratio"],
        "truncation_strategy": DEFAULT_SETTINGS["truncation_strategy"],
        "truncation_keep_ratio": DEFAULT_SETTINGS["truncation_keep_ratio"],
    }
    save_json(settings_path, settings)
    print(f"\n[✓] 已创建: {settings_path}")

    # 生成 prompts.json 骨架
    prompts = []
    for i in range(1, num_chapters + 1):
        prompts.append({
            "chapter": f"第{i}章",
            "prompt": f"请撰写【第{i}章】的详细内容。（请编辑此提示词）",
            "ref_files": [],
            "image_hints": [],
        })
    save_json(prompts_path, prompts)
    print(f"[✓] 已创建: {prompts_path}（{num_chapters} 个章节骨架，请编辑提示词）")

    # 重置 task_state.json
    state_path = project_root / "config" / "task_state.json"
    state = {str(i): "pending" for i in range(num_chapters)}
    save_json(state_path, state)

    print(f"\n{'=' * 60}")
    print("  项目初始化完成！")
    print()
    print("  下一步:")
    print(f"  1. 放入参考资料到 data/raw/，运行: python src/pdf_docx_parser.py")
    print(f"  2. 编辑提示词: {prompts_path}")
    print(f"  3. 预览确认: python src/scheduler.py --review")
    print(f"  4. 开始生成: python src/scheduler.py")
    print(f"{'=' * 60}")


def cmd_list(prompts_path: Path, state_path: Path) -> None:
    """列出所有章节及其状态。"""
    prompts = load_prompts(prompts_path)
    state = load_task_state(state_path, len(prompts))

    for i in range(len(prompts)):
        state.setdefault(str(i), "pending")
    save_json(state_path, state)

    print(f"\n{'=' * 60}")
    print(f"  章节列表（共 {len(prompts)} 章）")
    print(f"{'=' * 60}")
    for i, p in enumerate(prompts):
        status = state.get(str(i), "pending")
        icon = "✓" if status == "completed" else "○"
        refs = p.get("ref_files", [])
        ref = f" [参考: {', '.join(refs)}]" if refs else ""
        print(f"  [{icon}] {i + 1}. {p['chapter']}  [{status}]{ref}")
        hints = p.get("image_hints", [])
        if hints:
            print(f"      配图建议 {len(hints)} 处")
    print(f"{'=' * 60}")


def cmd_reset(
    prompts_path: Path, state_path: Path, index: int, new_prompt: str | None = None
) -> None:
    """重置指定章节为 pending，可选修改其 prompt。"""
    prompts = load_prompts(prompts_path)
    state = load_task_state(state_path, len(prompts))

    if index < 0 or index >= len(prompts):
        print(f"[错误] 章节编号 {index + 1} 超出范围（共 {len(prompts)} 章）")
        return

    state[str(index)] = "pending"
    save_json(state_path, state)
    print(f"[重置] {index + 1}. {prompts[index]['chapter']} → pending")

    if new_prompt is not None:
        prompts[index]["prompt"] = new_prompt
        save_json(prompts_path, prompts)
        print(f"[更新] 提示词已更新。")


def cmd_reset_all(state_path: Path, chapter_count: int) -> None:
    """全部重置为 pending。"""
    state = {str(i): "pending" for i in range(chapter_count)}
    save_json(state_path, state)
    print(f"[重置] 全部 {chapter_count} 个章节已重置为 pending。")


def cmd_hints(prompts_path: Path) -> None:
    """仅展示所有章节的配图/视频建议清单。"""
    prompts = load_prompts(prompts_path)
    print(f"\n{'=' * 60}")
    print(f"  配图/视频素材清单（{len(prompts)} 章）")
    print(f"{'=' * 60}")
    total_hints = 0
    for i, p in enumerate(prompts):
        hints = p.get("image_hints", [])
        print(f"\n  [{i + 1}] {p['chapter']}")
        if hints:
            for j, h in enumerate(hints, 1):
                print(f"      {j}. {h}")
            total_hints += len(hints)
        else:
            print(f"      （无特定建议）")
    print(f"\n{'=' * 60}")
    print(f"  共计 {total_hints} 处配图/视频建议")
    print(f"  请将对应素材插入报告中标记【图X】的位置。")
    print(f"{'=' * 60}")


def cmd_review_modifications(
    prompts_path: Path, output_path: Path,
) -> None:
    """预览 modifications.json 中的所有修改指令及其影响的章节。"""
    project_root = get_project_root()
    mods_path = project_root / "config" / "modifications.json"

    if not mods_path.exists():
        print("[提示] modifications.json 不存在，请先创建。")
        print("运行 --batch-modify 可自动创建模板。")
        return

    modifications_raw = load_json(mods_path, [])
    if not modifications_raw:
        print("[提示] modifications.json 为空。")
        return

    prompts = load_prompts(prompts_path)
    all_names = [p["chapter"] for p in prompts]

    # 加载进度状态
    state_path = project_root / "config" / "modification_state.json"
    mod_state = load_json(state_path, {}) if state_path.exists() else {}

    print(f"\n{'=' * 60}")
    print(f"  批量修改预览（共 {len(modifications_raw)} 条）")
    print(f"{'=' * 60}")

    for i, mod in enumerate(modifications_raw):
        chapter_idx = mod.get("chapter_index", -1)
        instruction = mod.get("instruction", "")
        status = mod_state.get(str(i), "pending")
        icon = "✓" if status == "completed" else "○"

        if chapter_idx < 0 or chapter_idx >= len(prompts):
            print(f"\n  [{icon}] 条目 {i + 1}: [错误] chapter_index={chapter_idx} 超出范围")
            continue
        if not instruction.strip():
            print(f"\n  [{icon}] 条目 {i + 1}: [警告] instruction 为空")
            continue

        prompt_def = prompts[chapter_idx]
        chapter_name = prompt_def["chapter"]
        refs = prompt_def.get("ref_files", [])
        ref_str = f" [参考: {', '.join(refs)}]" if refs else ""

        print(f"\n  [{icon}] 条目 {i + 1} → [{chapter_idx + 1}] {chapter_name}{ref_str}")
        print(f"      修改要求: {instruction[:120]}{'...' if len(instruction) > 120 else ''}")

        # 显示现有内容大小
        if output_path.exists():
            existing = parse_report_chapter(output_path, chapter_name, all_names)
            if existing:
                print(f"      现有内容: {len(existing)} 字符 (~{estimate_tokens(existing)} tokens)")
            else:
                print(f"      现有内容: [报告中未找到该章节]")

    completed = sum(1 for v in mod_state.values() if v == "completed")
    pending = len(modifications_raw) - completed
    print(f"\n{'=' * 60}")
    print(f"  已完成 {completed} / {len(modifications_raw)}，待处理 {pending}")
    if completed > 0:
        print(f"  提示: 运行 --reset-modifications 可清除进度重新执行。")
    print(f"{'=' * 60}")


def cmd_modify(
    prompts_path: Path, settings_path: Path, output_path: Path,
    modifications: list[tuple[int, str]],
    custom_refs: list[list[str]] | None = None,
    dry_run: bool = False,
) -> None:
    """修改章节：按 (chapter_index, instruction) 列表依次修改。"""
    project_root = get_project_root()
    settings = load_settings(settings_path)
    prompts = load_prompts(prompts_path)
    all_names = [p["chapter"] for p in prompts]
    processed_dir = project_root / "data" / "processed"

    if not dry_run and not output_path.exists():
        print(f"[错误] 报告文件不存在: {output_path}")
        print("请先运行 python src/scheduler.py 生成报告。")
        return

    # 校验所有索引
    for chapter_idx, _ in modifications:
        if chapter_idx < 0 or chapter_idx >= len(prompts):
            print(f"[错误] 章节编号 {chapter_idx + 1} 超出范围（共 {len(prompts)} 章）")
            return

    # 将 custom_refs 对齐到 modifications
    custom_refs = custom_refs or []
    while len(custom_refs) < len(modifications):
        custom_refs.append([])

    mode_label = "DRY-RUN 预览模式（不发送请求）" if dry_run else "修改模式"
    print(f"\n{'=' * 60}")
    print(f"  {mode_label}")
    print(f"  项目: {settings['project_name']}")
    print(f"  模型: {settings['model']}  |  API: {settings['api_url']}")
    print(f"  共 {len(modifications)} 条修改指令")
    for i, (chapter_idx, instruction) in enumerate(modifications):
        refs = custom_refs[i] if custom_refs[i] else prompts[chapter_idx].get("ref_files", [])
        ref_info = f" [参考: {', '.join(refs)}]" if refs else ""
        img_count = sum(1 for f in refs if Path(processed_dir / f).suffix.lower() in IMAGE_EXTENSIONS)
        img_info = f" (含{img_count}张图片)" if img_count else ""
        print(f"    [{chapter_idx + 1}] {prompts[chapter_idx]['chapter']}{ref_info}{img_info}")
        print(f"        → {instruction[:80]}{'...' if len(instruction) > 80 else ''}")
    print(f"{'=' * 60}")

    async def _run():
        async with httpx.AsyncClient() as client:
            for i, (chapter_idx, instruction) in enumerate(modifications):
                # 应用自定义 refs
                prompt_def = dict(prompts[chapter_idx])
                if custom_refs[i]:
                    prompt_def["ref_files"] = custom_refs[i]

                print(f"\n[进度] {i + 1}/{len(modifications)} → {prompt_def['chapter']}")

                # dry-run: 构建 payload 并展示详情，但不发送
                if dry_run:
                    existing = parse_report_chapter(output_path, prompt_def["chapter"], all_names) if output_path.exists() else None
                    existing_str = f"{len(existing)} 字符 (~{estimate_tokens(existing)} tokens)" if existing else "未找到"
                    print(f"  [DRY-RUN] 现有内容: {existing_str}")
                    print(f"  [DRY-RUN] 系统提示词: {settings['system_prompt'][:100]}...")
                    print(f"  [DRY-RUN] 修改指令: {instruction[:100]}...")

                    # 预览参考资料
                    img_refs = collect_image_refs(processed_dir, prompt_def["ref_files"])
                    text_refs = [f for f in prompt_def["ref_files"]
                                 if (processed_dir / f).suffix.lower() not in IMAGE_EXTENSIONS]
                    ref_preview = load_ref_content(processed_dir, text_refs)
                    ref_tokens = estimate_tokens(ref_preview)
                    print(f"  [DRY-RUN] 文本参考资料: {', '.join(text_refs) if text_refs else '无'} (~{ref_tokens} tokens)")
                    if img_refs:
                        print(f"  [DRY-RUN] 图片参考资料: {', '.join(p.name for p in img_refs)}"
                              f" (~{estimate_image_tokens(len(img_refs), settings)} tokens)")

                    # 估算总的 token
                    total_est = (estimate_tokens(settings["system_prompt"]) +
                                 (estimate_tokens(existing) if existing else 0) +
                                 ref_tokens +
                                 estimate_tokens(instruction) +
                                 estimate_image_tokens(len(img_refs), settings) +
                                 500)
                    ctx = settings.get("context_window_tokens", 56000)
                    print(f"  [DRY-RUN] 预估总输入: ~{total_est} tokens / 窗口 {ctx}"
                          f" ({round(total_est / ctx * 100, 1)}%)")
                    print(f"  [DRY-RUN] 跳过 API 调用。")
                    continue

                success = await modify_single_chapter(
                    client, chapter_idx, prompt_def, instruction,
                    settings, output_path, all_names, processed_dir,
                )
                if success:
                    print(f"  [√] 第 {i + 1} 条修改完成")
                else:
                    print(f"  [×] 第 {i + 1} 条修改失败，继续下一条...")

        print(f"\n{'=' * 60}")
        status = "DRY-RUN 完成（未做任何修改）" if dry_run else "修改完成"
        print(f"[{status}]")
        print(f"{'=' * 60}")

    asyncio.run(_run())


def cmd_batch_modify(
    prompts_path: Path, settings_path: Path, output_path: Path,
    dry_run: bool = False,
) -> None:
    """批量修改：读取 config/modifications.json，支持断点续传。"""
    project_root = get_project_root()
    mods_path = project_root / "config" / "modifications.json"
    state_path = project_root / "config" / "modification_state.json"

    if not mods_path.exists():
        template = [
            {
                "chapter_index": 0,
                "instruction": "请在此处填写对该章节的修改要求（例如：在开头增加一段背景介绍）",
            },
        ]
        save_json(mods_path, template)
        print(f"[初始化] 已创建批量修改模板: {mods_path}")
        print("请编辑该文件后重新运行 --batch-modify")
        return

    modifications_raw = load_json(mods_path, [])
    if not modifications_raw:
        print("[错误] modifications.json 为空，请添加修改指令后重试。")
        return

    # 构建有效条目列表
    all_entries = []
    for mod in modifications_raw:
        chapter_idx = mod.get("chapter_index", -1)
        instruction = mod.get("instruction", "")
        if chapter_idx >= 0 and instruction.strip():
            all_entries.append((chapter_idx, instruction))

    if not all_entries:
        print("[错误] modifications.json 中没有有效的修改条目。")
        return

    # 加载修改进度状态
    mod_state = load_json(state_path, {})
    # 如果条目数量变了（用户增删了条目），自动重置状态
    if len(mod_state) > 0 and max(int(k) for k in mod_state.keys()) >= len(all_entries):
        print("[提示] modifications.json 条目数已变化，重置修改进度。")
        mod_state = {}
        save_json(state_path, mod_state)

    # 过滤已完成条目
    pending_entries = []
    skipped_count = 0
    for i, entry in enumerate(all_entries):
        if mod_state.get(str(i)) == "completed":
            skipped_count += 1
        else:
            pending_entries.append((i, entry))

    if not pending_entries:
        print("[完成] 所有修改条目均已执行完毕。")
        print("如需重新执行，请删除 config/modification_state.json 后重试。")
        return

    settings = load_settings(settings_path)
    prompts = load_prompts(prompts_path)
    all_names = [p["chapter"] for p in prompts]
    processed_dir = project_root / "data" / "processed"

    if not dry_run and not output_path.exists():
        print(f"[错误] 报告文件不存在: {output_path}")
        print("请先运行 python src/scheduler.py 生成报告。")
        return

    mode_label = "批量修改 — DRY-RUN 预览（不发送请求）" if dry_run else "批量修改模式"
    print(f"\n{'=' * 60}")
    print(f"  {mode_label}")
    print(f"  项目: {settings['project_name']}")
    print(f"  模型: {settings['model']}  |  API: {settings['api_url']}")
    print(f"  共 {len(all_entries)} 条指令，已完成 {skipped_count}，待处理 {len(pending_entries)}")
    if skipped_count > 0:
        print(f"  （已完成的条目将跳过，中断后可续传）")
    print(f"{'=' * 60}")

    async def _run():
        async with httpx.AsyncClient() as client:
            for i, (orig_idx, (chapter_idx, instruction)) in enumerate(pending_entries):
                prompt_def = prompts[chapter_idx]
                total_pending = len(pending_entries)
                print(f"\n[进度] {i + 1}/{total_pending} (条目 {orig_idx + 1}/{len(all_entries)}) → {prompt_def['chapter']}")
                print(f"  要求: {instruction[:100]}{'...' if len(instruction) > 100 else ''}")

                if dry_run:
                    existing = parse_report_chapter(output_path, prompt_def["chapter"], all_names)
                    existing_str = f"{len(existing)} 字符 (~{estimate_tokens(existing)} tokens)" if existing else "未找到"
                    print(f"  [DRY-RUN] 现有内容: {existing_str}")
                    refs = prompt_def.get("ref_files", [])
                    img_refs = collect_image_refs(processed_dir, refs)
                    text_refs = [f for f in refs if (processed_dir / f).suffix.lower() not in IMAGE_EXTENSIONS]
                    ref_preview = load_ref_content(processed_dir, text_refs)
                    ref_tokens = estimate_tokens(ref_preview)
                    print(f"  [DRY-RUN] 文本参考: {', '.join(text_refs) if text_refs else '无'} (~{ref_tokens} tokens)")
                    if img_refs:
                        print(f"  [DRY-RUN] 图片参考: {', '.join(p.name for p in img_refs)}"
                              f" (~{estimate_image_tokens(len(img_refs), settings)} tokens)")
                    total_est = (estimate_tokens(settings["system_prompt"]) +
                                 (estimate_tokens(existing) if existing else 0) +
                                 ref_tokens + estimate_tokens(instruction) +
                                 estimate_image_tokens(len(img_refs), settings) + 500)
                    ctx = settings.get("context_window_tokens", 56000)
                    print(f"  [DRY-RUN] 预估总输入: ~{total_est} tokens / 窗口 {ctx} ({round(total_est / ctx * 100, 1)}%)")
                    continue

                success = await modify_single_chapter(
                    client, chapter_idx, prompt_def, instruction,
                    settings, output_path, all_names, processed_dir,
                )
                if success:
                    mod_state[str(orig_idx)] = "completed"
                    save_json(state_path, mod_state)
                    print(f"  [√] 条目 {orig_idx + 1} 修改完成（已保存进度）")
                else:
                    print(f"  [×] 条目 {orig_idx + 1} 修改失败（未保存进度，重跑时可续传）")

        if not dry_run:
            remaining = sum(1 for i in range(len(all_entries)) if mod_state.get(str(i)) != "completed")
            if remaining == 0:
                state_path.unlink(missing_ok=True)
                print(f"\n[清理] 所有条目完成，已清除修改进度记录。")

        print(f"\n{'=' * 60}")
        status = "DRY-RUN 完成（未做任何修改）" if dry_run else "批量修改完成"
        print(f"[{status}]")
        print(f"{'=' * 60}")

    asyncio.run(_run())


# ==================== 入口 ====================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通用长文档自动化生成框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python src/scheduler.py                  # 按序处理所有 pending 章节
  python src/scheduler.py --review         # 预览所有提示词
  python src/scheduler.py --init           # 新建项目向导
  python src/scheduler.py --list           # 列出章节状态
  python src/scheduler.py --hints          # 查看配图/视频建议清单
  python src/scheduler.py --reset 3        # 重置第 3 章
  python src/scheduler.py --reset 3 --prompt "新提示词"
  python src/scheduler.py --reset-all      # 全部重置
  python src/scheduler.py --run 5          # 单独运行第 5 章
  python src/scheduler.py --modify 1 --prompt "增加总结" --modify 3 --prompt "改代码注释"   # 修改多章
  python src/scheduler.py --batch-modify   # 批量修改
        """,
    )
    parser.add_argument("--review", action="store_true", help="预览所有章节提示词")
    parser.add_argument("--init", action="store_true", help="交互式新建项目向导")
    parser.add_argument("--list", action="store_true", help="列出章节及状态")
    parser.add_argument("--hints", action="store_true", help="仅显示配图/视频建议清单")
    parser.add_argument("--reset", type=int, metavar="N", help="重置第 N 章为 pending（1-based）")
    parser.add_argument("--prompt", type=str, action="append", metavar="TEXT", help="与 --reset 或 --modify 配合")
    parser.add_argument("--reset-all", action="store_true", help="全部重置为 pending")
    parser.add_argument("--run", type=int, metavar="N", help="单独运行第 N 章（1-based）")
    parser.add_argument("--modify", type=int, action="append", metavar="N", help="修改第 N 章（可重复多次，与 --prompt 配对）")
    parser.add_argument("--ref", type=str, nargs="*", action="append", metavar="FILE", help="与 --modify 配对：临时指定参考资料（覆盖 prompts.json）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式：构建 payload 但不发送请求（与 --modify 或 --batch-modify 配合）")
    parser.add_argument("--batch-modify", action="store_true", help="批量修改：读取 config/modifications.json 逐条执行")
    parser.add_argument("--reset-modifications", action="store_true", help="清除批量修改进度（下次 --batch-modify 将从头开始）")
    parser.add_argument("--review-modifications", action="store_true", help="预览 modifications.json 中的所有修改指令")
    return parser.parse_args()


def main():
    args = parse_args()
    project_root = get_project_root()
    settings_path = project_root / "config" / "settings.json"
    prompts_path = project_root / "config" / "prompts.json"
    state_path = project_root / "config" / "task_state.json"

    # --init: 交互式新建项目
    if args.init:
        cmd_init(project_root)
        return

    # --review: 预览所有提示词
    if args.review:
        cmd_review(settings_path, prompts_path, state_path)
        return

    # --list: 列出章节状态
    if args.list:
        cmd_list(prompts_path, state_path)
        return

    # --hints: 仅显示配图建议
    if args.hints:
        cmd_hints(prompts_path)
        return

    # --reset-all: 全部重置
    if args.reset_all:
        prompts = load_prompts(prompts_path)
        cmd_reset_all(state_path, len(prompts))
        return

    # --reset N [--prompt TEXT]: 重置单章
    if args.reset is not None:
        prompt_text = args.prompt[0] if args.prompt else None
        idx = args.reset - 1
        cmd_reset(prompts_path, state_path, idx, prompt_text)
        return

    # --modify N --prompt TEXT [...] (可重复多次，可选 --ref 和 --dry-run)
    if args.modify is not None:
        if args.prompt is None or len(args.modify) != len(args.prompt):
            print("[错误] --modify 必须与 --prompt 成对使用，数量需一致。")
            print("用法: python src/scheduler.py --modify 1 --prompt \"修改建议1\" --modify 2 --prompt \"修改建议2\" ...")
            return
        output_path = project_root / load_settings(settings_path)["output_file"]
        modifications = [(n - 1, p) for n, p in zip(args.modify, args.prompt)]
        cmd_modify(prompts_path, settings_path, output_path, modifications,
                   custom_refs=args.ref, dry_run=args.dry_run)
        return

    # --review-modifications: 预览修改指令
    if args.review_modifications:
        output_path = project_root / load_settings(settings_path)["output_file"]
        cmd_review_modifications(prompts_path, output_path)
        return

    # --reset-modifications: 清除批量修改进度
    if args.reset_modifications:
        state_path = project_root / "config" / "modification_state.json"
        if state_path.exists():
            state_path.unlink()
            print("[已清除] 批量修改进度已重置，下次 --batch-modify 将从头开始。")
        else:
            print("[提示] 没有找到批量修改进度文件，无需重置。")
        return

    # --batch-modify: 批量修改
    if args.batch_modify:
        output_path = project_root / load_settings(settings_path)["output_file"]
        cmd_batch_modify(prompts_path, settings_path, output_path, dry_run=args.dry_run)
        return

    # --prompt 不能单独使用
    if args.prompt is not None and args.reset is None and args.modify is None:
        print("[错误] --prompt 必须与 --reset 或 --modify 配合使用。")
        return

    # --run N: 单章运行
    if args.run is not None:
        idx = args.run - 1
        settings = load_settings(settings_path)
        print(f"\n  项目: {settings['project_name']}")
        print(f"  模型: {settings['model']}  |  API: {settings['api_url']}")
        print()
        asyncio.run(run_scheduler(run_index=idx))
        return

    # 默认：全量运行
    settings = load_settings(settings_path)
    print(f"\n  项目: {settings['project_name']}")
    print(f"  模型: {settings['model']}  |  API: {settings['api_url']}")
    print()

    # 先快速展示 pending 章节概况
    prompts = load_prompts(prompts_path)
    state = load_task_state(state_path, len(prompts))
    pending = [i for i in range(len(prompts)) if state.get(str(i)) != "completed"]
    if pending:
        print(f"  待处理 {len(pending)}/{len(prompts)} 章:")
        for i in pending:
            print(f"    [{i + 1}] {prompts[i]['chapter']}")
        print()
        print("  提示: 运行 --review 可预览完整提示词，--hints 可查看配图清单。")
        print()

    asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()
