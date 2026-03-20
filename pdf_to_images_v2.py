"""
PDF 转图片 v2：配置驱动 + 题干/答案分流

功能：
- 遍历当前目录（或 MATH_PDF）下的 PDF，仅处理 PDF_MAPPING 中配置的文件
- 为每个 PDF 创建同名文件夹，内含 /questions 和 /answers 两个子文件夹
- 题干页（q_range）保存为 questions/q_page_1.png 等，答案页（a_range）保存为 answers/a_page_53.png 等
- 使用 pdf2image 以 300 DPI 转换

依赖：pip install pdf2image Pillow tqdm
Poppler：需安装并加入 PATH，参见 pdf_processor.py 内说明。
"""

import sys
from pathlib import Path

try:
    from pdf2image import convert_from_path
    from tqdm import tqdm
except ImportError:
    print("请先安装: pip install pdf2image Pillow tqdm")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 配置：PDF 文件名 -> 题干页范围、答案页范围（1-based 闭区间）
# 请按实际题本补全或修改页码
# ---------------------------------------------------------------------------
PDF_MAPPING = {
    "01：有理数计算（500题）.pdf": {"q_range": (1, 52), "a_range": (53, 64)},# 请按实际补全
}

DPI = 300
POPPLER_PATH = None  # 若 Poppler 未加入 PATH，可设为 r"C:\poppler\Library\bin"


def sanitize_foldername(name: str) -> str:
    """将 PDF 文件名（去扩展名）整理为合法文件夹名。"""
    invalid = '<>:"/\\|?*'
    for c in invalid:
        name = name.replace(c, "_")
    return name.strip(". ")


def process_one_pdf(pdf_path: Path, q_range: tuple, a_range: tuple, poppler_path=None) -> bool:
    """
    将单个 PDF 的题干页、答案页分别转为 PNG，存入同名目录下的 questions/ 与 answers/。
    命名：q_page_<页码>.png、a_page_<页码>.png
    """
    if not pdf_path.exists():
        tqdm.write(f"跳过（不存在）: {pdf_path}")
        return False

    folder_name = sanitize_foldername(pdf_path.stem)
    base_out = pdf_path.parent / folder_name
    questions_dir = base_out / "questions"
    answers_dir = base_out / "answers"
    questions_dir.mkdir(parents=True, exist_ok=True)
    answers_dir.mkdir(parents=True, exist_ok=True)

    kwargs = {"dpi": DPI}
    if poppler_path:
        kwargs["poppler_path"] = poppler_path

    try:
        q_start, q_end = q_range
        if q_end >= q_start:
            images_q = convert_from_path(
                pdf_path,
                first_page=q_start,
                last_page=q_end,
                **kwargs
            )
            for i, img in enumerate(tqdm(images_q, desc="题干页", leave=False, unit="页")):
                page_num = q_start + i
                out_file = questions_dir / f"q_page_{page_num}.png"
                img.save(out_file, "PNG")

        a_start, a_end = a_range
        if a_end >= a_start:
            images_a = convert_from_path(
                pdf_path,
                first_page=a_start,
                last_page=a_end,
                **kwargs
            )
            for i, img in enumerate(tqdm(images_a, desc="答案页", leave=False, unit="页")):
                page_num = a_start + i
                out_file = answers_dir / f"a_page_{page_num}.png"
                img.save(out_file, "PNG")

        tqdm.write(f"  已写入: {questions_dir.relative_to(pdf_path.parent)} 与 {answers_dir.relative_to(pdf_path.parent)}")
        return True
    except Exception as e:
        tqdm.write(f"转换失败 {pdf_path.name}: {e}")
        return False


def main():
    base_dir = Path(__file__).resolve().parent
    # 在脚本目录或 MATH_PDF 下查找 PDF_MAPPING 中配置的 PDF
    pdf_list = []
    for name in PDF_MAPPING:
        for d in [base_dir, base_dir / "MATH_PDF"]:
            if not d.exists():
                continue
            p = d / name
            if p.exists():
                pdf_list.append(p)
                break
    pdf_list = sorted(set(pdf_list))

    if not pdf_list:
        print("未找到 PDF_MAPPING 中配置的任一 PDF。请将 PDF 放在脚本所在目录或 MATH_PDF 下。")
        sys.exit(1)

    print("=" * 60)
    print("PDF 转图片 v2（题干/答案分流，300 DPI）")
    print("=" * 60)
    print(f"工作目录: {base_dir}")
    print(f"待处理: {len(pdf_list)} 个 PDF")

    success = 0
    for pdf_path in tqdm(pdf_list, desc="PDF 转图", unit="个"):
        cfg = PDF_MAPPING.get(pdf_path.name)
        if not cfg:
            tqdm.write(f"跳过（未在 PDF_MAPPING 中）: {pdf_path.name}")
            continue
        if process_one_pdf(
            pdf_path,
            cfg["q_range"],
            cfg["a_range"],
            poppler_path=POPPLER_PATH,
        ):
            success += 1

    print("\n" + "=" * 60)
    print(f"完成: 成功 {success}/{len(pdf_list)} 个 PDF")
    print("=" * 60)


if __name__ == "__main__":
    main()
