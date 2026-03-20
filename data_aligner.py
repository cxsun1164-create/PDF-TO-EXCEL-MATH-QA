"""
数据处理脚本：对齐题干与答案，导出 Excel

功能：
- 读取步骤二（ocr_engine.py）生成的 JSON 识别结果
- 以题号为 Key，将题干与参考答案横向合并为 DataFrame
- 题干存在但答案缺失的题号标记为「待核查」
- 使用 openpyxl 将结果保存为 Excel，每个 PDF 对应一个 Sheet，Sheet 名为 PDF 简短文件名

输入目录约定（二选一）：
1) 子目录结构：{根目录}/{PDF简短名}/question.json、{PDF简短名}/answer.json
2) 平铺命名：{根目录}/{PDF简短名}_question.json、{PDF简短名}_answer.json

依赖：pip install pandas openpyxl
"""

import argparse
import json
import re
from pathlib import Path
from typing import List, Tuple

import pandas as pd

ILLEGAL_EXCEL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]|\r|\n")


def sanitize_for_excel(val):
    """替换 Excel/openpyxl 不允许的控制字符，避免 IllegalCharacterError。"""
    if isinstance(val, str):
        return ILLEGAL_EXCEL_CHARS.sub(" ", val)
    return val


# Excel Sheet 名最长 31 字符，且不能包含 : \\ / ? * [ ]
def sanitize_sheet_name(name: str, max_len: int = 31) -> str:
    s = re.sub(r'[\\/:*?\[\]]', '_', name)
    if len(s) > max_len:
        s = s[:max_len]
    return s.strip() or "Sheet"


def load_ocr_json(path: Path) -> List[dict]:
    """加载 OCR 输出的 JSON。

    兼容两类字段协议：
    - 旧/通用：[{ "id": 1, "text": "..." }]
    - OCR v2：[{ "id": 1, "question": "..." }] 或 [{ "id": 1, "answer": "..." }]
    """
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    # 只要求包含 id，其它字段由后续对齐逻辑决定
    return [x for x in data if isinstance(x, dict) and "id" in x]


def align_question_answer(questions: List[dict], answers: List[dict]) -> pd.DataFrame:
    """
    以题号为 Key 横向合并题干与答案。
    题干存在但答案缺失的题号，答案列填「待核查」。
    """
    df_q = pd.DataFrame(questions)
    df_a = pd.DataFrame(answers)

    if df_q.empty:
        return pd.DataFrame(columns=["题号", "题干", "答案"])

    # 兼容 question.json / text.json 两种字段协议
    q_content_col = None
    for c in ("question", "text"):
        if c in df_q.columns:
            q_content_col = c
            break
    if q_content_col is None:
        # 无法对齐，尽量输出空题干
        df_q = df_q.rename(columns={"id": "题号"})
        df_q["题干"] = "待核查"
        df_q = df_q[["题号", "题干"]]
    else:
        df_q = df_q.rename(columns={"id": "题号", q_content_col: "题干"})
        df_q = df_q[["题号", "题干"]]

    if df_a.empty:
        df_q["答案"] = "待核查"
        return df_q.sort_values("题号").reset_index(drop=True)

    a_content_col = None
    for c in ("answer", "text", "ans"):
        if c in df_a.columns:
            a_content_col = c
            break
    if a_content_col is None:
        df_a = df_a.rename(columns={"id": "题号"})
        df_a["答案"] = "待核查"
        df_a = df_a[["题号", "答案"]]
    else:
        df_a = df_a.rename(columns={"id": "题号", a_content_col: "答案"})
        df_a = df_a[["题号", "答案"]]

    # 以题干为准左连接，缺失答案的填「待核查」
    merged = df_q.merge(df_a, on="题号", how="left")
    merged["答案"] = merged["答案"].fillna("待核查")
    merged = merged.sort_values("题号").reset_index(drop=True)
    return merged


def discover_pairs(root: Path) -> List[Tuple[str, Path, Path]]:
    """
    在 root 下发现 (sheet_name, question_json_path, answer_json_path) 列表。
    支持：子目录内 question.json/answer.json，或平铺的 {name}_question.json / {name}_answer.json。
    """
    root = Path(root)
    if not root.is_dir():
        return []

    pairs: List[Tuple[str, Path, Path]] = []

    # 1) 子目录：{root}/{name}/question.json, answer.json
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        q_path = sub / "question.json"
        a_path = sub / "answer.json"
        if q_path.exists() or a_path.exists():
            name = sanitize_sheet_name(sub.name)
            pairs.append((name, q_path, a_path))

    if pairs:
        return pairs

    # 2) 平铺：兼容：
    #    - {name}_question.json, {name}_answer.json
    #    - {name}_questions.json, {name}_answers.json
    seen = set()
    question_globs = ("*_question.json", "*_questions.json")
    for q_glob in question_globs:
        for f in sorted(root.glob(q_glob)):
            base = f.stem
            base = base.replace("_question", "").replace("_questions", "").strip("_")
            if not base:
                base = f.stem
            name = sanitize_sheet_name(base)
            if name in seen:
                continue

            # 按 base 尝试找到 answer 文件（单复数）
            a_path = root / f"{base}_answer.json"
            if not a_path.exists():
                a_path = root / f"{base}_answers.json"

            seen.add(name)
            pairs.append((name, f, a_path))

    return pairs


def run(root_dir: Path, output_excel: Path) -> None:
    """从 root_dir 读取所有题干/答案 JSON 对，对齐后写入 output_excel，每个 PDF 一个 Sheet。"""
    pairs = discover_pairs(root_dir)
    if not pairs:
        raise FileNotFoundError(
            f"在 {root_dir} 下未找到题干/答案 JSON 对。"
            "请使用子目录 question.json/answer.json 或 {name}_question.json/{name}_answer.json 命名。"
        )

    used_sheets = set()
    with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:
        for sheet_name, q_path, a_path in pairs:
            questions = load_ocr_json(q_path)
            answers = load_ocr_json(a_path) if a_path.exists() else []
            df = align_question_answer(questions, answers)
            name = sheet_name
            if name in used_sheets:
                i = 0
                while name in used_sheets:
                    i += 1
                    name = sanitize_sheet_name(f"{sheet_name}_{i}")
            used_sheets.add(name)
            # 写入 Excel 前清洗非法字符，避免 IllegalCharacterError
            for col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].apply(sanitize_for_excel)
            df.to_excel(writer, sheet_name=name, index=False)
            n_missing = (df["答案"] == "待核查").sum()
            print(f"  Sheet [{name}]: {len(df)} 行（待核查 {n_missing}）")


def main():
    parser = argparse.ArgumentParser(description="题干与答案 JSON 对齐并导出 Excel")
    parser.add_argument(
        "input_dir",
        type=str,
        nargs="?",
        default="ocr_results",
        help="存放 question/answer JSON 的目录（默认 ocr_results）",
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default="aligned_result.xlsx",
        help="输出 Excel 路径（默认 aligned_result.xlsx）",
    )
    args = parser.parse_args()

    root = Path(args.input_dir)
    out = Path(args.output)

    if not root.exists():
        print(f"错误：输入目录不存在 {root}")
        return 1

    try:
        run(root, out)
        print(f"已保存: {out.absolute()}")
    except FileNotFoundError as e:
        print(e)
        return 1
    except Exception as e:
        print(f"错误: {e}")
        raise
    return 0


if __name__ == "__main__":
    exit(main())
