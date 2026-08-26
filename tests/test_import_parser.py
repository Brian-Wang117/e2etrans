"""Tests for customer list parsing and phone column detection."""

import io

import pytest
from openpyxl import Workbook

from app.batch import import_parser as parser

CSV_SAMPLE = (
    "姓名,性别,手机号\n"
    "王芳,女,13800000001\n"
    "\n"
    "李强,男,13800000002\n"
)


def make_excel_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_parse_csv_basic_with_blank_row():
    table = parser.parse_table("list.csv", CSV_SAMPLE.encode("utf-8"))
    assert table.columns == ["姓名", "性别", "手机号"]
    assert table.total == 2
    # Blank rows are skipped but row numbers follow the spreadsheet.
    assert [row.row_number for row in table.rows] == [2, 4]
    assert table.rows[0].data == {"姓名": "王芳", "性别": "女", "手机号": "13800000001"}


def test_parse_csv_utf8_bom_supported():
    content = ("\ufeff" + CSV_SAMPLE).encode("utf-8")
    table = parser.parse_table("list.csv", content)
    assert table.columns[0] == "姓名"


def test_parse_csv_non_utf8_rejected():
    content = CSV_SAMPLE.encode("gbk")
    with pytest.raises(parser.ImportParseError, match="UTF-8"):
        parser.parse_table("list.csv", content)


def test_parse_excel_numeric_phone_survives():
    content = make_excel_bytes(
        [
            ["客户", "联系方式"],
            ["王芳", 13800000001],  # stored as a number by Excel
            ["李强", "138 0000 0002"],
        ]
    )
    table = parser.parse_table("list.xlsx", content)
    assert table.total == 2
    assert table.rows[0].data["联系方式"] == "13800000001"
    assert table.rows[1].data["联系方式"] == "138 0000 0002"


def test_parse_excel_corrupt_rejected():
    with pytest.raises(parser.ImportParseError, match="损坏"):
        parser.parse_table("list.xlsx", b"not an excel file at all")


def test_unsupported_extension_and_legacy_xls():
    with pytest.raises(parser.ImportParseError):
        parser.parse_table("list.txt", b"a,b")
    with pytest.raises(parser.ImportParseError, match="xls"):
        parser.parse_table("list.xls", b"whatever")


def test_empty_file_rejected():
    with pytest.raises(parser.ImportParseError, match="没有有效数据行"):
        parser.parse_table("list.csv", "姓名,手机号\n\n\n".encode("utf-8"))


def test_row_limit_enforced(monkeypatch):
    monkeypatch.setattr(parser, "MAX_ROWS", 2)
    content = "手机号\n13800000001\n13800000002\n13800000003\n".encode("utf-8")
    with pytest.raises(parser.ImportParseError, match="最多"):
        parser.parse_table("list.csv", content)


def test_detect_phone_by_column_name():
    table = parser.parse_table("l.csv", "姓名,电话号码\n王芳,13800000001\n".encode())
    assert parser.detect_phone_column(table) == "电话号码"


def test_detect_phone_by_content_when_name_gives_no_hint():
    table = parser.parse_table(
        "l.csv",
        "姓名,联系方式,产品\n王芳,138-0000-0001,扫地机\n李强,13800000002,净化器\n".encode(),
    )
    assert parser.detect_phone_column(table) == "联系方式"


def test_detect_phone_landline_supported():
    table = parser.parse_table("l.csv", "门店,座机\n总店,010-88886666\n".encode())
    assert parser.detect_phone_column(table) == "座机"


def test_detect_phone_none_when_nothing_matches():
    table = parser.parse_table("l.csv", "姓名,备注\n王芳,老客户\n".encode())
    assert parser.detect_phone_column(table) is None


def test_looks_like_phone_boundaries():
    assert parser.looks_like_phone("13800000001")
    assert parser.looks_like_phone("138 0000 0001")
    assert not parser.looks_like_phone("12800000001")  # bad prefix
    assert not parser.looks_like_phone("1380000000")  # too short
    assert parser.looks_like_phone("0571-8888666")
    assert not parser.looks_like_phone("hello")


def test_new_batch_id_format():
    batch_id = parser.new_batch_id()
    assert batch_id.startswith("B-")
    parts = batch_id.split("-")
    assert len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 4


def test_extract_gender_variants():
    assert parser.extract_gender({"姓名": "王芳", "性别": "女"}) == "female"
    assert parser.extract_gender({"Gender": "Male"}) == "male"
    assert parser.extract_gender({"客户性别": "男"}) == "male"
    assert parser.extract_gender({"sex": "F"}) == "female"
    assert parser.extract_gender({"性别": ""}) == ""
    assert parser.extract_gender({"姓名": "王芳"}) == ""
    assert parser.extract_gender(None) == ""
