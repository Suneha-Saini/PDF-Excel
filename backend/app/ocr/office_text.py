"""Text extraction for non-PDF, non-image statements: Excel, CSV/TSV/TXT and Word.

These formats already contain text, so they skip OCR and go straight to the AI step.
No extra dependencies beyond openpyxl (already required); legacy .xls needs `xlrd`.
"""
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import List

OFFICE_EXTENSIONS = {"xlsx", "xlsm", "xls", "csv", "tsv", "txt", "docx"}

MAX_ROWS_PER_SHEET = 20000  # safety cap for huge workbooks


def _read_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _cell(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):  # date / datetime
        return value.strftime("%d %b %Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _from_xlsx(path: str) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)  # data_only: use cached formula results
    parts: List[str] = []
    for ws in wb.worksheets:
        lines = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_ROWS_PER_SHEET:
                lines.append(f"[Sheet truncated after {MAX_ROWS_PER_SHEET} rows]")
                break
            cells = [_cell(c) for c in row]
            if any(cells):
                lines.append(" | ".join(cells).rstrip(" |"))
        if lines:
            parts.append(f"--- Sheet: {ws.title} ---\n" + "\n".join(lines))
    wb.close()
    return "\n\n".join(parts)


def _from_xls(path: str) -> str:
    try:
        import pandas as pd
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
    except ImportError as exc:
        raise ValueError("Legacy .xls needs the 'xlrd' package (pip install xlrd), or save the file as .xlsx.") from exc
    parts = []
    for name, df in sheets.items():
        lines = [" | ".join("" if str(v) == "nan" else str(v).strip() for v in row).rstrip(" |")
                 for row in df.head(MAX_ROWS_PER_SHEET).itertuples(index=False)]
        lines = [l for l in lines if l]
        if lines:
            parts.append(f"--- Sheet: {name} ---\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _from_delimited(path: str, ext: str) -> str:
    text = _read_text(path)
    if ext == "txt":
        return text
    delimiter = "\t" if ext == "tsv" else None
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;|\t").delimiter
        except csv.Error:
            delimiter = ","
    lines = []
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if i >= MAX_ROWS_PER_SHEET:
            lines.append(f"[Truncated after {MAX_ROWS_PER_SHEET} rows]")
            break
        cells = [c.strip() for c in row]
        if any(cells):
            lines.append(" | ".join(cells).rstrip(" |"))
    return "\n".join(lines)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{_W}t")).strip()


def _from_docx(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W}body")
    out: List[str] = []
    for child in list(body) if body is not None else []:
        if child.tag == f"{_W}p":
            text = _para_text(child)
            if text:
                out.append(text)
        elif child.tag == f"{_W}tbl":  # keep table rows as "a | b | c" lines
            for tr in child.iter(f"{_W}tr"):
                cells = [" ".join(_para_text(p) for p in tc.iter(f"{_W}p")).strip() for tc in tr.findall(f"{_W}tc")]
                if any(cells):
                    out.append(" | ".join(cells).rstrip(" |"))
    return "\n".join(out)


def extract_office_text(path: str, ext: str) -> str:
    ext = ext.lower().lstrip(".")
    if ext in ("xlsx", "xlsm"):
        text = _from_xlsx(path)
    elif ext == "xls":
        text = _from_xls(path)
    elif ext in ("csv", "tsv", "txt"):
        text = _from_delimited(path, ext)
    elif ext == "docx":
        text = _from_docx(path)
    else:
        raise ValueError(f"Unsupported file type: .{ext}")

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("No readable text was found in this file.")
    return text
