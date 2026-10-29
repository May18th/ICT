from decision_poc.business_report import parse_business_report


def test_business_report_parser_keeps_four_sections_without_json_assumption():
    report = parse_business_report("结论：停机\n依据：振动超标\n推理链：趋势恶化\n建议：安排检修")

    assert report == {
        "结论": "停机",
        "依据": "振动超标",
        "推理链": "趋势恶化",
        "建议": "安排检修",
    }
