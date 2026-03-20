"""
OCR 处理器 v2：题干/答案分流 + 多线程 + RPM 限制 + 中间存档

功能：
- 遍历当前目录（或 MATH_PDF 下）的 7 个题目文件夹（含 questions/ 与 answers/）
- 题干：识别数学题干，输出 [{"id": 1, "question": "..."}]（LaTeX）
- 答案：精准提取题号对应答案，输出 [{"id": 1, "answer": "..."}]
- 多线程处理图片，并设置每分钟请求数（RPM）限制
- 每处理完一个 PDF 文件夹，将 Q/A 结果存入 output_cache/ 对应 JSON

依赖：pip install openai Pillow tqdm
环境变量：DASHSCOPE_API_KEY（通义千问）
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import List, Tuple

try:
    import openai
    from PIL import Image
    import io
    import base64
    from tqdm import tqdm
except ImportError as e:
    print("请先安装: pip install openai Pillow tqdm")
    sys.exit(1)

from concurrent.futures import ThreadPoolExecutor, as_completed


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 与 pdf_to_images_v2 对应：7 个 PDF 对应的文件夹名（无 .pdf，用于匹配目录）
FOLDER_NAMES = [
    "01：有理数计算（500题）",
    "02：绝对值化简（110题）",
    "03：整式加减运算（200题）",
    "04：一元一次方程（50题）",
    "05：整式乘法与因式分解（500题）",
    "06：不等式专项练习（200题）",
    "07：二元一次方程组（50题）",
]

OUTPUT_CACHE_DIR = "output_cache"
APIKEY_ENV_FILE = "apikey.env"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_VL_MODEL = "qwen-vl-plus"

# 每分钟请求数上限（防 API 限流）
DEFAULT_RPM = 60
# 最大工作线程数（实际速率仍受 RPM 限制）
MAX_WORKERS = 4


def load_apikey_env(base_dir: Path) -> None:
    """从 apikey.env 读取 KEY=value 并注入环境变量（仅当变量未设置时）。"""
    env_path = base_dir / APIKEY_ENV_FILE
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 题干 / 答案 Prompt
# ---------------------------------------------------------------------------

QUESTION_SYSTEM = """你是一个数学题目 OCR 与 LaTeX 转换专家。
任务：识别图片中的数学题干，必须包含题号，公式使用标准 LaTeX 格式。
要求：
1. 只识别题目内容，忽略页眉、页脚、水印。
2. 题干可能跨多行，请合并为一道题的完整文本。
3. 数学内容必须用标准 LaTeX：分数 \\frac{分子}{分母}，根号 \\sqrt{x}，幂 x^2，下标 x_1 等。
4. 输出严格的 JSON 数组，每项格式：{"id": 题号整数, "question": "题干 LaTeX 文本"}
5. 不要输出任何非 JSON 内容。"""

QUESTION_USER = """请识别这张题目页图片，按上述要求输出所有题目的 JSON 数组。"""

ANSWER_SYSTEM = """你是一个数学答案页 OCR 专家。
任务：识别紧凑答案页中题号与对应答案。
要求：
1. 按从左到右、从上到下逐题识别，精准提取题号对应的答案（分式、负号等勿遗漏）。
2. 数学内容用标准 LaTeX（如 \\frac{a}{b}、-1）。
3. 输出严格的 JSON 数组，每项格式：{"id": 题号整数, "answer": "该题答案 LaTeX 或空字符串"}
4. 本页出现的题号都要列出，无答案的题 answer 填 ""。
5. 不要输出任何非 JSON 内容。"""

ANSWER_USER = """这里是紧凑的答案页。请精准提取题号对应的答案（如分式、负号）。只输出一个 JSON 数组，格式：[{"id": 1, "answer": "..."}, ...]。"""


# ---------------------------------------------------------------------------
# 图片加载与编码（与 ocr_engine 一致）
# ---------------------------------------------------------------------------

def load_image_base64(path: Path, max_size: int = 2048) -> str:
    if not path.exists():
        raise FileNotFoundError(f"图片不存在: {path}")
    with Image.open(path) as img:
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def get_mime(path: Path) -> str:
    if (path.suffix or "").lower() in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "image/png"


# ---------------------------------------------------------------------------
# 速率限制器（RPM）
# ---------------------------------------------------------------------------

class RPMLimiter:
    def __init__(self, rpm: float):
        self.rpm = max(1.0, rpm)
        self.min_interval = 60.0 / self.rpm
        self._lock = threading.Lock()
        self._last_ts = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._last_ts + self.min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_ts = time.monotonic()


# ---------------------------------------------------------------------------
# 从模型回复解析 JSON 数组
# ---------------------------------------------------------------------------

def _extract_json_array(raw: str, id_key: str, value_key: str, alt_value_keys: Tuple[str, ...]) -> List[dict]:
    """解析 [{"id": n, value_key: "..."}, ...]，value_key 可用 alt_value_keys 备选。"""
    raw = raw.strip()
    if "```json" in raw:
        raw = raw.split("```json")[-1].split("```")[0].strip()
    elif "```" in raw:
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1].split("```")[0].strip() if len(parts) > 2 else parts[1].strip()
    start = raw.find("[")
    if start == -1:
        return []
    depth = 0
    end = -1
    for i in range(start, len(raw)):
        if raw[i] == "[":
            depth += 1
        elif raw[i] == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return []
    try:
        arr = json.loads(raw[start:end])
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    result = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        id_val = item.get("id")
        if id_val is None:
            continue
        try:
            id_int = int(id_val) if not isinstance(id_val, int) else id_val
        except (TypeError, ValueError):
            continue
        val = item.get(value_key)
        if val is None:
            for k in alt_value_keys:
                if k in item:
                    val = item[k]
                    break
        result.append({"id": id_int, value_key: (str(val).strip() if val is not None else "")})
    return result


def extract_questions(raw: str) -> List[dict]:
    return _extract_json_array(raw, "id", "question", ("text",))


def extract_answers(raw: str) -> List[dict]:
    return _extract_json_array(raw, "id", "answer", ("ans", "text",))


# ---------------------------------------------------------------------------
# 单张图片 API 调用（通义千问）
# ---------------------------------------------------------------------------

def _call_vision(
    client: "openai.OpenAI",
    image_path: Path,
    system: str,
    user: str,
    mode: str,
    limiter: RPMLimiter,
) -> List[dict]:
    limiter.acquire()
    b64 = load_image_base64(image_path)
    mime = get_mime(image_path)
    response = client.chat.completions.create(
        model=QWEN_VL_MODEL,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            },
        ],
        max_tokens=4096,
    )
    raw = (response.choices[0].message.content or "").strip()
    if mode == "question":
        return extract_questions(raw)
    return extract_answers(raw)


def process_one_image(
    image_path: Path,
    mode: str,
    client: "openai.OpenAI",
    limiter: RPMLimiter,
) -> Tuple[Path, List[dict]]:
    """处理单张图片，返回 (path, list of {id, question|answer})。"""
    system = QUESTION_SYSTEM if mode == "question" else ANSWER_SYSTEM
    user = QUESTION_USER if mode == "question" else ANSWER_USER
    try:
        items = _call_vision(client, image_path, system, user, mode, limiter)
        return (image_path, items)
    except Exception as e:
        tqdm.write(f"  [ERR] {image_path.name}: {e}")
        return (image_path, [])


# ---------------------------------------------------------------------------
# 合并多页结果
# ---------------------------------------------------------------------------

def merge_questions(all_items: List[dict]) -> List[dict]:
    by_id = {}
    for x in all_items:
        i = x.get("id")
        q = x.get("question", "")
        if i is None:
            continue
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        by_id.setdefault(i, []).append(q)
    return [{"id": i, "question": "\n".join(by_id[i])} for i in sorted(by_id)]


def merge_answers(all_items: List[dict]) -> List[dict]:
    by_id = {}
    for x in all_items:
        i = x.get("id")
        a = x.get("answer", "")
        if i is None:
            continue
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        by_id[i] = a
    return [{"id": i, "answer": by_id[i]} for i in sorted(by_id)]


# ---------------------------------------------------------------------------
# 文件夹发现与主流程
# ---------------------------------------------------------------------------

def sanitize_foldername(name: str) -> str:
    for c in '<>:"/\\|?*':
        name = name.replace(c, "_")
    return name.strip(". ")


def safe_cache_name(folder_name: str) -> str:
    """用于 output_cache 下 JSON 文件名，避免非法字符。"""
    s = re.sub(r'[\\/:*?\[\]]', '_', folder_name)
    return s.strip() or "sheet"


def discover_folders(base_dir: Path) -> List[Path]:
    """发现包含 questions/ 与 answers/ 的文件夹（限定 FOLDER_NAMES）。"""
    candidates = [base_dir, base_dir / "MATH_PDF"]
    found = []
    seen = set()
    for root in candidates:
        if not root.exists():
            continue
        for name in FOLDER_NAMES:
            folder = root / name
            if folder in seen:
                continue
            if folder.is_dir():
                q_dir = folder / "questions"
                a_dir = folder / "answers"
                if q_dir.is_dir() and a_dir.is_dir():
                    found.append(folder)
                    seen.add(folder)
    return sorted(found, key=lambda p: p.name)


def run_folder(
    folder_path: Path,
    client: "openai.OpenAI",
    limiter: RPMLimiter,
    cache_dir: Path,
    workers: int = MAX_WORKERS,
) -> bool:
    """处理一个 PDF 文件夹：题干 + 答案，多线程，结果写入 output_cache。"""
    name = folder_path.name
    q_dir = folder_path / "questions"
    a_dir = folder_path / "answers"

    q_images = sorted(q_dir.glob("*.png"), key=lambda p: (p.stat().st_mtime, p.name))
    a_images = sorted(a_dir.glob("*.png"), key=lambda p: (p.stat().st_mtime, p.name))

    if not q_images and not a_images:
        tqdm.write(f"  跳过 {name}：questions/ 与 answers/ 下无 png")
        return False

    all_q_items = []
    all_a_items = []

    def do_questions():
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(process_one_image, p, "question", client, limiter): p
                for p in q_images
            }
            for fut in tqdm(as_completed(futures), total=len(futures), desc="题干", leave=False, unit="张"):
                path, items = fut.result()
                all_q_items.extend(items)

    def do_answers():
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(process_one_image, p, "answer", client, limiter): p
                for p in a_images
            }
            for fut in tqdm(as_completed(futures), total=len(futures), desc="答案", leave=False, unit="张"):
                path, items = fut.result()
                all_a_items.extend(items)

    if q_images:
        do_questions()
    if a_images:
        do_answers()

    merged_q = merge_questions(all_q_items)
    merged_a = merge_answers(all_a_items)

    safe = safe_cache_name(name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    q_path = cache_dir / f"{safe}_questions.json"
    a_path = cache_dir / f"{safe}_answers.json"
    q_path.write_text(json.dumps(merged_q, ensure_ascii=False, indent=2), encoding="utf-8")
    a_path.write_text(json.dumps(merged_a, ensure_ascii=False, indent=2), encoding="utf-8")
    tqdm.write(f"  已存档: {q_path.name} ({len(merged_q)} 题), {a_path.name} ({len(merged_a)} 条)")
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OCR 处理器 v2：题干/答案分流 + 多线程 + RPM 限制")
    parser.add_argument("--rpm", type=float, default=DEFAULT_RPM, help=f"每分钟请求上限（默认 {DEFAULT_RPM}）")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"最大并发线程数（默认 {MAX_WORKERS}）")
    parser.add_argument("--cache", type=str, default=OUTPUT_CACHE_DIR, help="中间结果目录")
    parser.add_argument("--only", type=str, default=None, help="仅处理文件夹名包含此字符串的项，如 --only 03 只跑第三个")
    args = parser.parse_args()

    workers = max(1, args.workers)

    base_dir = Path(__file__).resolve().parent
    load_apikey_env(base_dir)
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("请设置环境变量 DASHSCOPE_API_KEY，或在 apikey.env 中配置。")
        sys.exit(1)

    cache_dir = base_dir / args.cache
    folders = discover_folders(base_dir)
    if args.only:
        folders = [f for f in folders if args.only in f.name]
        if not folders:
            print(f"未找到文件夹名包含 \"{args.only}\" 的项。")
            sys.exit(1)
        print(f"仅处理: {[f.name for f in folders]}")

    if not folders:
        print("未找到任何包含 questions/ 与 answers/ 的题目文件夹（当前目录或 MATH_PDF 下）。")
        sys.exit(1)

    print("=" * 60)
    print("OCR 处理器 v2（题干/答案分流 + 多线程 + RPM 限制）")
    print("=" * 60)
    print(f"工作目录: {base_dir}")
    print(f"发现文件夹: {len(folders)} 个")
    print(f"RPM: {args.rpm}  线程数: {workers}  缓存: {cache_dir}")

    client = openai.OpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)
    limiter = RPMLimiter(args.rpm)

    success = 0
    for folder in tqdm(folders, desc="PDF 文件夹", unit="个"):
        tqdm.write(f"\n处理: {folder.name}")
        if run_folder(folder, client, limiter, cache_dir, workers=workers):
            success += 1

    print("\n" + "=" * 60)
    print(f"完成: 成功 {success}/{len(folders)} 个文件夹，结果在 {cache_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
