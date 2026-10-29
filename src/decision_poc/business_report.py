"""Parser for the legacy four-section equipment decision report."""


SECTIONS = ("结论", "依据", "推理链", "建议")


def parse_business_report(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        matched = next((section for section in SECTIONS if line.startswith(f"{section}：")), None)
        if matched:
            current = matched
            result[current] = line[len(matched) + 1 :].strip()
        elif current and line:
            result[current] = f"{result[current]}\n{line}".strip()
    missing = [section for section in SECTIONS if not result.get(section)]
    if missing:
        raise ValueError(f"business report missing sections: {', '.join(missing)}")
    return result

