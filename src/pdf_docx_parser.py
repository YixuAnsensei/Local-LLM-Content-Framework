"""
文档解析模块 - pdf_docx_parser.py
负责将 data/raw/ 目录下的 PDF 和 Word 文件转换为纯文本，
并保存到 data/processed/ 目录中。
"""

import os
import sys
from pathlib import Path

# PyMuPDF (fitz) 用于 PDF 解析，支持多种 PDF 格式，兼容性好
import fitz  # PyMuPDF
from docx import Document  # python-docx 用于 Word 文档解析


def get_project_root() -> Path:
    """
    获取项目根目录路径。
    约定：本脚本位于 src/ 下，项目根目录为其父目录。
    """
    return Path(__file__).resolve().parent.parent


def parse_pdf(file_path: Path) -> str:
    """
    使用 PyMuPDF 解析单个 PDF 文件，提取所有页面的纯文本。

    Args:
        file_path: PDF 文件的完整路径

    Returns:
        提取出的全部文本内容（字符串），如果失败则返回空字符串。
    """
    text_parts = []
    try:
        doc = fitz.open(str(file_path))
        for page_num in range(len(doc)):
            page = doc[page_num]
            # get_text() 提取当前页的纯文本
            page_text = page.get_text()
            if page_text:
                text_parts.append(page_text)
        doc.close()
    except Exception as e:
        print(f"[错误] 解析 PDF 失败: {file_path.name} — {e}")
        return ""
    return "\n".join(text_parts)


def parse_docx(file_path: Path) -> str:
    """
    使用 python-docx 解析单个 Word 文件，提取所有段落的文本。

    Args:
        file_path: Word 文件的完整路径

    Returns:
        提取出的全部文本内容（字符串），如果失败则返回空字符串。
    """
    text_parts = []
    try:
        doc = Document(str(file_path))
        for para in doc.paragraphs:
            if para.text:
                text_parts.append(para.text)
    except Exception as e:
        print(f"[错误] 解析 Word 失败: {file_path.name} — {e}")
        return ""
    return "\n".join(text_parts)


def convert_all(raw_dir: Path, processed_dir: Path) -> None:
    """
    遍历 raw_dir 下所有 PDF 和 Word 文件，逐一转换为纯文本，
    并按原文件名（后缀改为 .txt）保存到 processed_dir。

    Args:
        raw_dir:   原始文件存放目录 (data/raw/)
        processed_dir: 转换后文本输出目录 (data/processed/)
    """
    # 确保输出目录存在
    processed_dir.mkdir(parents=True, exist_ok=True)

    # 支持的源文件格式与对应的解析函数
    parsers = {
        ".pdf": parse_pdf,
        ".docx": parse_docx,
    }

    converted_count = 0

    for file_path in sorted(raw_dir.iterdir()):
        # 只处理文件，跳过子目录
        if not file_path.is_file():
            continue

        suffix = file_path.suffix.lower()
        if suffix not in parsers:
            print(f"[跳过] 不支持的文件格式: {file_path.name}")
            continue

        print(f"[处理中] {file_path.name} ...")
        parser = parsers[suffix]
        text_content = parser(file_path)

        if not text_content:
            print(f"[警告] {file_path.name} 提取内容为空，跳过保存。")
            continue

        # 输出文件：同文件名，后缀改为 .txt
        out_name = file_path.stem + ".txt"
        out_path = processed_dir / out_name

        try:
            out_path.write_text(text_content, encoding="utf-8")
            print(f"[完成] {file_path.name} → {out_name}")
            converted_count += 1
        except Exception as e:
            print(f"[错误] 保存 {out_name} 失败: {e}")

    print(f"\n===== 全部处理完毕，共转换 {converted_count} 个文件 =====")


def main():
    """
    入口：从 data/raw/ 读取所有 PDF/DOCX，输出到 data/processed/。
    """
    project_root = get_project_root()
    raw_dir = project_root / "data" / "raw"
    processed_dir = project_root / "data" / "processed"

    if not raw_dir.exists():
        print(f"[错误] raw 目录不存在: {raw_dir}")
        print("请先将 PDF 和 Word 文件放入 data/raw/ 目录后再运行。")
        sys.exit(1)

    print(f"原始文件目录: {raw_dir}")
    print(f"输出目录:     {processed_dir}")
    print("-" * 50)

    convert_all(raw_dir, processed_dir)


if __name__ == "__main__":
    main()
