"""Bounded CSV/XLSX parameter import. No macros, formulas, external links or extraction."""

from __future__ import annotations

import copy
import csv
import io
import math
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from fastapi import HTTPException

COLUMNS = {
    "type": "对象类型",
    "index": "序号",
    "name": "名称",
    "cycle_time_seconds": "加工时间(s)",
    "availability": "可用率(%)",
    "mttr_seconds": "平均维修时间(s)",
    "idle_power_kw": "空闲功率(kW)",
    "processing_power_kw": "加工功率(kW)",
    "capacity": "容量",
    "delay_seconds": "转运时间(s)",
}
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _xml(data):
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("XML declarations are not supported")
    return ET.fromstring(data)


def read_table(data, filename):
    if len(data) > 1_000_000:
        raise HTTPException(413, "参数表限 1 MB。")
    try:
        if filename.lower().endswith(".csv"):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("gb18030")
            rows = list(csv.reader(io.StringIO(text)))
        elif filename.lower().endswith(".xlsx"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if (
                    len(archive.infolist()) > 2000
                    or sum(i.file_size for i in archive.infolist()) > 10_000_000
                ):
                    raise ValueError("expanded workbook too large")
                shared = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    root = _xml(archive.read("xl/sharedStrings.xml"))
                    shared = [
                        "".join(t.text or "" for t in item.findall(".//s:t", NS))
                        for item in root.findall("s:si", NS)
                    ]
                book = _xml(archive.read("xl/workbook.xml"))
                sheets = book.findall("s:sheets/s:sheet", NS)
                if len(sheets) != 1:
                    raise HTTPException(422, "请上传仅含一个参数工作表的 Excel 文件。")
                rel_id = sheets[0].get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                )
                relations = _xml(archive.read("xl/_rels/workbook.xml.rels"))
                relation = next(r for r in relations if r.get("Id") == rel_id)
                if relation.get("TargetMode") == "External":
                    raise ValueError("external sheet")
                target = relation.get("Target", "")
                path = posixpath.normpath(
                    target.lstrip("/") if target.startswith("/") else "xl/" + target
                )
                if not path.startswith("xl/") or ".." in path:
                    raise ValueError("invalid sheet path")
                sheet = _xml(archive.read(path))
                rows = []
                for row in sheet.findall("s:sheetData/s:row", NS):
                    cells = {}
                    for cell in row.findall("s:c", NS):
                        if cell.find("s:f", NS) is not None:
                            raise HTTPException(
                                422, f"单元格 {cell.get('r')} 含公式，请粘贴为数值后导入。"
                            )
                        letters = re.match(r"[A-Z]+", cell.get("r", ""))
                        if not letters:
                            raise ValueError("missing cell address")
                        index = 0
                        for letter in letters[0]:
                            index = index * 26 + ord(letter) - 64
                        if index > 40:
                            raise ValueError("too many columns")
                        value = cell.findtext("s:v", "", NS)
                        if cell.get("t") == "s":
                            value = shared[int(value)]
                        elif cell.get("t") == "inlineStr":
                            value = "".join(t.text or "" for t in cell.findall(".//s:t", NS))
                        cells[index - 1] = value
                    rows.append([cells.get(i, "") for i in range(max(cells, default=-1) + 1)])
        else:
            raise HTTPException(422, "支持 .csv 和 .xlsx；旧版 .xls 请另存为 .xlsx。")
        if not rows or len(rows) > 501 or any(len(row) > 40 for row in rows):
            raise ValueError("invalid table dimensions")
        headers = [str(v).strip() for v in rows[0]]
        if not all(headers) or len(headers) != len(set(headers)):
            raise HTTPException(422, "表头不能为空或重复。")
        body = [
            [str(v).strip() for v in row] for row in rows[1:] if any(str(v).strip() for v in row)
        ]
        if not body or any(len(row) > len(headers) for row in body):
            raise ValueError("invalid row width")
        reverse = {label: key for key, label in COLUMNS.items()}
        return {
            "columns": headers,
            "rows": body,
            "fields": COLUMNS,
            "mapping": {
                header: reverse.get(header, header if header in COLUMNS else "")
                for header in headers
            },
        }
    except HTTPException:
        raise
    except (
        ValueError,
        KeyError,
        IndexError,
        StopIteration,
        ET.ParseError,
        zipfile.BadZipFile,
    ) as exc:
        raise HTTPException(
            422, "无法读取参数表：检查文件格式、表头和行列；最多 500 行、40 列。"
        ) from exc


def apply_table(table, config, mapping):
    mapped = [mapping.get(header, "") for header in table["columns"]]
    selected = [field for field in mapped if field]
    if (
        not {"type", "index"}.issubset(selected)
        or len(selected) != len(set(selected))
        or not set(selected).issubset(COLUMNS)
    ):
        raise HTTPException(422, "请映射对象类型、序号，且每个目标字段只能映射一次。")
    result, errors, seen = copy.deepcopy(config), [], set()
    for line, row in enumerate(table["rows"], 2):
        values = {key: row[i] for i, key in enumerate(mapped) if key and i < len(row)}
        try:
            kind = {
                "设备": "machines",
                "machine": "machines",
                "容器": "buffers",
                "缓冲区": "buffers",
                "buffer": "buffers",
            }.get(values.get("type"))
            index = int(values.get("index", "")) - 1
            if not kind or not 0 <= index < len(result[kind]):
                raise ValueError("对象类型或序号不存在（序号从 1 开始，按当前产线顺序）")
            if (kind, index) in seen:
                raise ValueError("同一个对象重复出现")
            seen.add((kind, index))
            item = result[kind][index]
            for key, value in values.items():
                if key in {"type", "index"} or value == "":
                    continue
                if key not in item:
                    raise ValueError(f"{COLUMNS[key]} 不适用于此对象")
                if key == "name":
                    item[key] = value
                else:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError(f"{COLUMNS[key]} 必须是有限数值")
                    if key == "capacity" and not number.is_integer():
                        raise ValueError("容量必须为整数")
                    item[key] = (
                        int(number)
                        if key == "capacity"
                        else (number / 100 if key == "availability" else number)
                    )
        except (ValueError, KeyError) as exc:
            errors.append({"row": line, "message": str(exc)})
    if errors:
        raise HTTPException(422, {"message": "参数表存在错误，未导入任何行。", "rows": errors})
    return result


def template_csv(config):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(COLUMNS.values()))
    writer.writeheader()
    for kind, label in (("machines", "设备"), ("buffers", "容器")):
        for index, item in enumerate(config[kind], 1):
            values = {"type": label, "index": index, **item}
            if "availability" in values:
                values["availability"] *= 100
            row = {COLUMNS[k]: v for k, v in values.items()}
            for key, value in row.items():
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    row[key] = "'" + value
            writer.writerow(row)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")
