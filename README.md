# PDF-QA-TO-EXCEL

把“初中习题册”的**题干页**与**答案页**从 PDF 中分离出来，进行 OCR 识别并按题号对齐，最后导出为 Excel（便于人工核查与后续训练/分析）。

> 适用场景：题干与答案在 PDF 中页面连续，且题本版式相对固定（便于按页码范围切图、并稳定识别题号）。

## 1. 项目简介

很多数学题本的题干与答案并非结构化数据。该项目提供一个端到端脚本流程：  
从 PDF 中切分题干/答案页面 -> 对图片进行 OCR -> 以题号合并 -> 输出 Excel，形成可复用的数据表。

## 2. 功能说明

### `pdf_to_images_v2.py`
将配置的 PDF 按页码范围切成图片：
- 题干页 -> `questions/q_page_<n>.png`
- 答案页 -> `answers/a_page_<n>.png`

并为每个 PDF 创建同名文件夹，便于后续 OCR。

### `ocr_processor_v2.py`
对 `questions/` 与 `answers/` 内的图片分别进行 OCR：
- 题干 OCR：输出 `[{ "id": 1, "question": "..." }, ...]`
- 答案 OCR：输出 `[{ "id": 1, "answer": "..." }, ...]`

中间结果会写入 `--cache` 指定目录（JSON 文件），支持并发与 RPM 限流。

### `data_aligner.py`
读取 OCR 生成的 JSON：
- 按 `id`（题号）合并题干与答案
- 缺失答案的题号填入 `待核查`

最终导出为 Excel（每个 PDF 对应一个 Sheet）。

## 3. 处理流程

```text
PDF
  │  (1) pdf_to_images_v2.py：按页码范围切图
  ▼
questions/ 与 answers/
  │  (2) ocr_processor_v2.py：对图片做 OCR（题干/答案分别识别）
  ▼
output_cache/
  │  (3) data_aligner.py：按题号对齐 + 导出 Excel
  ▼
aligned_result.xlsx
```

## 4. 安装方式

1) 安装 Python 依赖：

```bash
pip install -r requirements.txt
```

2) 安装 Poppler（给 `pdf2image` 用）：
- Windows 下需要安装 Poppler 并加入系统 PATH
- 你可以搜索 “Poppler for Windows” 下载并配置到 PATH

## 5. 环境依赖

- `openai`（与通义千问 DashScope 兼容接口的 OpenAI SDK 调用）
- `pandas`、`openpyxl`
- `pdf2image`、`Pillow`
- `tqdm`
- Poppler（用于 PDF -> 图片）

## 6. 如何配置 API Key（不包含真实密钥）

项目会读取 `DASHSCOPE_API_KEY`：
- 方法 A：设置环境变量 `DASHSCOPE_API_KEY=你的Key`
- 方法 B：在本目录放置 `apikey.env`，内容形如：
  - `DASHSCOPE_API_KEY=你的Key`

> 注意：**不要把密钥文件提交到 GitHub**（`.gitignore` 已忽略 `apikey.env`）。

## 7. 如何准备 PDF 输入文件

你需要把目标 PDF 放在项目目录中，或放在 `MATH_PDF/` 目录下。  
然后根据你的题本实际页码结构，在 `pdf_to_images_v2.py` 里维护：

- `PDF_MAPPING`：`{PDF文件名: {q_range: (题干起止页), a_range: (答案起止页)}}`
- 页码从 1 开始，且是**闭区间**（`(start, end)` 包含 start 与 end）。

同时在 `ocr_processor_v2.py` 里维护文件夹匹配的配置（脚本会按文件夹名发现要处理的题本）：
- `FOLDER_NAMES`：与 `pdf_to_images_v2.py` 生成的同名文件夹一致

## 8. 如何运行整个流程

推荐使用命令行按顺序运行：

### (1) PDF -> 图片
```bash
python pdf_to_images_v2.py
```

### (2) 图片 -> OCR JSON
```bash
python -u ocr_processor_v2.py --rpm 60 --workers 4 --cache output_cache
```

### (3) 对齐题干/答案 -> 导出 Excel
```bash
python -u data_aligner.py output_cache -o aligned_result.xlsx
```

你也可以直接运行：
```bat
run_all.bat
```
> Windows PowerShell 下运行时建议使用：`& .\run_all.bat`（或在 CMD 中直接运行）。

## 9. 输出结果说明

最终会得到一个 Excel 文件（默认可通过 `-o` 指定输出名），结构如下：
- 每个题本（PDF 对应的文件夹）一个 Sheet
- 列通常包含：
  - `题号`
  - `题干`
  - `答案`

当 OCR 未识别到答案时，对应题号的 `答案` 会填入：`待核查`。

## 10. 注意事项

1) **不要上传/分享受版权保护的 PDF**（尤其是你不具备公开授权的题本原文件）。  
   GitHub 仓库建议只提交代码与配置示例，不提交原始 PDF。

2) **不要提交 `apikey.env`**（密钥文件已被 `.gitignore` 忽略）。

3) **不要提交输出文件与中间缓存**（例如 OCR JSON、图片、Excel 导出等），这些文件会随运行生成且体积较大。

4) 若 OCR 返回内容不符合严格 JSON 格式，可能需要调整 OCR 识别 prompt 或降低并发/缩放图片。

5) `ocr_processor_v2.py` 会将题干/答案图片发送到第三方 OCR/大模型 API 进行识别。请不要把敏感内容或受限/不允许公开分享的材料放入图片（尤其是不要用于需要保密或侵权风险的题库）。

## 11. 最小示例（可直接参考）

假设你已经把 PDF 放入项目目录，并确认题干/答案页码范围：

1) 修改 `pdf_to_images_v2.py`：
- 在 `PDF_MAPPING` 添加你的 PDF 文件名与 `q_range/a_range`

2) 修改 `ocr_processor_v2.py`：
- 在 `FOLDER_NAMES` 添加与 `pdf_to_images_v2.py` 生成一致的文件夹名

3) 运行：
```bash
python pdf_to_images_v2.py
python -u ocr_processor_v2.py --rpm 60 --workers 4 --cache output_cache
python -u data_aligner.py output_cache -o aligned_result.xlsx
```

然后你会在生成的 `aligned_result.xlsx` 中看到题号对齐后的 `题干/答案` 数据。

