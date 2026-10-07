"""文档解析（Phase 2 RAG）：把支持的 5 类文件转成纯文本。

为什么单独一层：
    解析是 RAG 链路的入口，也是最容易出问题的一环（编码、扫描件、空表）。
    把"文件 → 文本"收敛到一个模块后，chunking / embedding 只面对纯文本，
    将来加 pptx / html 也只改本文件。

为什么返回 list[ParsedPage] 而不是一整段 str：
    引用来源要标页码。PDF 天然分页，如果在这里就先拼成一大段，
    页码信息就永久丢失，前端没法显示"来自《员工手册》第 3 页"。
    没有页概念的格式（txt / md / csv）page 给 None。

为什么 CSV/Excel 每个 sheet 当一"页"：
    sheet 是表格天然的边界，且"第 2 个 sheet"比"第 37 行"对用户更有意义。

为什么表格要转成"列名: 值"而不是原样保留逗号分隔：
    embedding 模型看"报销类型: 差旅；金额: 500"能直接理解字段含义，
    看"差旅,500"则要靠位置猜。检索命中率差别明显。
"""
import csv
import io
from dataclasses import dataclass
from pathlib import Path

from docx import Document as DocxDocument
from openpyxl import load_workbook
from pypdf import PdfReader

from app.core.exceptions import DocumentParseError, UnsupportedFileTypeError

# 支持的类型，与 docs/05-database-design.md 的 documents.file_type 对应
SUPPORTED_TYPES = frozenset({"pdf", "docx", "txt", "md", "csv", "xlsx"})

# 文本编码探测顺序。顺序有讲究：UTF-8 必须在 GBK 前面 ——
# GBK 解码器会把很多 UTF-8 字节序列"解成功"（结果是乱码但不报错），
# 反过来 UTF-8 解 GBK 通常会直接报错。先试 UTF-8 才不会被乱码蒙混过去。
_ENCODINGS = ("utf-8", "gbk")


@dataclass
class ParsedPage:
    """一段解析结果。page 为 None 表示该格式没有页的概念。"""

    text: str
    page: int | None = None


def parse(path: Path, file_type: str) -> list[ParsedPage]:
    """把文件解析成若干段文本。

    空结果会抛 DocumentParseError —— 扫描版 PDF 的典型症状就是
    "文件能打开、页数也正常，但提取不到任何文本"，必须当成失败上报，
    否则会安静地入库 0 个 chunk，之后检索永远查不到，很难排查。
    """
    ft = file_type.lower().lstrip(".")
    if ft not in SUPPORTED_TYPES:
        raise UnsupportedFileTypeError(f"不支持的文件类型：{file_type}")

    try:
        if ft == "pdf":
            pages = _parse_pdf(path)
        elif ft == "docx":
            pages = _parse_docx(path)
        elif ft == "csv":
            pages = _parse_csv(path)
        elif ft == "xlsx":
            pages = _parse_xlsx(path)
        else:  # txt / md
            pages = [ParsedPage(text=read_text(path))]
    except (UnsupportedFileTypeError, DocumentParseError):
        raise
    except Exception as exc:
        # 第三方库的异常五花八门（PdfReadError / BadZipFile / InvalidFileException…），
        # 在这里统一翻译成业务异常，上层就只用认 DocumentParseError 一种
        raise DocumentParseError(f"解析失败：{exc}") from exc

    pages = [p for p in pages if p.text.strip()]
    if not pages:
        raise DocumentParseError("没有可提取的文本（扫描版或空文件？）")
    return pages


def _parse_pdf(path: Path) -> list[ParsedPage]:
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise DocumentParseError("PDF 已加密，无法解析")
    # 页码从 1 开始：给人看，不跟代码下标对齐
    return [
        ParsedPage(text=page.extract_text() or "", page=i + 1)
        for i, page in enumerate(reader.pages)
    ]


def _parse_docx(path: Path) -> list[ParsedPage]:
    doc = DocxDocument(str(path))
    # docx 没有固定页概念（分页由 Word 渲染时决定），所以整篇当一段，page=None
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        body = _table_rows_to_text(rows)
        if body:
            parts.append(body)
    return [ParsedPage(text="\n".join(parts))]


def _parse_csv(path: Path) -> list[ParsedPage]:
    rows = list(csv.reader(io.StringIO(read_text(path))))
    return [ParsedPage(text=_table_rows_to_text(rows))]


def _parse_xlsx(path: Path) -> list[ParsedPage]:
    # read_only=True：流式读，大表不会一次性把内存吃满
    # data_only=True：取单元格的计算结果，而不是公式字符串（否则拿到 "=A1+B1"）
    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        pages = []
        for i, ws in enumerate(wb.worksheets):
            rows = [
                ["" if c is None else c for c in row]
                for row in ws.iter_rows(values_only=True)
            ]
            body = _table_rows_to_text(rows)
            if body.strip():
                pages.append(ParsedPage(text=body, page=i + 1))
        return pages
    finally:
        wb.close()


def _table_rows_to_text(rows: list[list]) -> str:
    """首行当表头，其余行转成"列名: 值；列名: 值"。

    列数不齐、表头为空都不报错，按能对上的部分拼 ——
    真实表格里合并单元格、空列很常见，为此让整份文件解析失败不值得。
    """
    if not rows:
        return ""
    headers = [str(c).strip() for c in rows[0]]
    lines = []
    for row in rows[1:]:
        parts = [
            f"{h}: {str(v).strip()}"
            for h, v in zip(headers, row)
            if h and str(v).strip()
        ]
        if parts:
            lines.append("；".join(parts))
    return "\n".join(lines)


def read_text(path: Path) -> str:
    """读文本文件，按 UTF-8 → GBK 顺序试编码，并把换行统一成 \\n。"""
    raw = path.read_bytes()
    text = ""
    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        # 两种都失败：宽松模式兜底。宁可丢个别生僻字符，也不要让整份文档入库失败
        text = raw.decode("utf-8", errors="ignore")
    # Windows 导出的文件换行是 \r\n。不归一化的话 \r 会一路带进 chunk：
    # 既污染 embedding 的输入文本，也可能让前端多出空行
    return text.replace("\r\n", "\n").replace("\r", "\n")
