from __future__ import annotations

import json
import math
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable


COMPETITOR_HEADERS = [
    "Название",
    "VK",
    "Сайт",
    "Тип конкурента",
    "Подписчики",
    "Последняя публикация",
    "Средний ER за месяц",
    "Ассортимент",
    "Аудитория",
    "География",
    "Ценовой сегмент",
    "Модель продаж",
    "Производство (если нужно)",
    "Онлайн/офлайн",
    "Почему конкурент",
    "Проверенные источники",
]

SHEET_NAMES = [
    "Сводка",
    "Конкуренты",
    "Проверены, но исключены",
    "Посты за месяц",
    "Методика",
]

TYPE_LABELS = {
    "direct": "Прямой",
    "indirect": "Косвенный",
    "attention": "За внимание",
    "Прямой": "Прямой",
    "Косвенный": "Косвенный",
    "За внимание": "За внимание",
}


def column_name(index: int) -> str:
    result = ""
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def xml_text(value: Any) -> str:
    return escape(str(value), quote=False)


def cell_xml(
    row: int,
    column: int,
    value: Any,
    *,
    style: int = 3,
) -> str:
    reference = f"{column_name(column)}{row}"
    if isinstance(value, dict) and "formula" in value:
        formula = xml_text(value["formula"].lstrip("="))
        cached = value.get("value")
        formula_style = int(value.get("style", style))
        if cached is None:
            return (
                f'<c r="{reference}" s="{formula_style}" t="str">'
                f"<f>{formula}</f><v></v></c>"
            )
        return (
            f'<c r="{reference}" s="{formula_style}">'
            f"<f>{formula}</f><v>{cached}</v></c>"
        )
    if value is None:
        return f'<c r="{reference}" s="{style}"/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" s="{style}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return f'<c r="{reference}" s="{style}"/>'
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    text = xml_text(value)
    preserve = ' xml:space="preserve"' if str(value).strip() != str(value) else ""
    return (
        f'<c r="{reference}" s="{style}" t="inlineStr">'
        f"<is><t{preserve}>{text}</t></is></c>"
    )


def sheet_xml(
    rows: list[list[Any]],
    *,
    widths: list[float],
    merged_ranges: Iterable[str] = (),
    freeze_rows: int = 4,
    auto_filter: str | None = None,
    row_styles: dict[int, int] | None = None,
    column_styles: dict[int, int] | None = None,
    row_heights: dict[int, float] | None = None,
) -> str:
    row_styles = row_styles or {}
    column_styles = column_styles or {}
    row_heights = row_heights or {}
    max_columns = max((len(row) for row in rows), default=1)
    max_row = max(len(rows), 1)
    dimension = f"A1:{column_name(max_columns)}{max_row}"
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    rendered_rows: list[str] = []
    for row_number, row_values in enumerate(rows, start=1):
        height = row_heights.get(row_number)
        height_attr = (
            f' ht="{height}" customHeight="1"' if height is not None else ""
        )
        cells = []
        for column_number, value in enumerate(row_values, start=1):
            style = row_styles.get(
                row_number,
                column_styles.get(column_number, 3),
            )
            cells.append(cell_xml(row_number, column_number, value, style=style))
        rendered_rows.append(
            f'<row r="{row_number}"{height_attr}>{"".join(cells)}</row>'
        )
    merge_xml = ""
    merged_ranges = list(merged_ranges)
    if merged_ranges:
        merge_xml = (
            f'<mergeCells count="{len(merged_ranges)}">'
            + "".join(f'<mergeCell ref="{value}"/>' for value in merged_ranges)
            + "</mergeCells>"
        )
    pane_xml = (
        f'<sheetViews><sheetView workbookViewId="0">'
        f'<pane ySplit="{freeze_rows}" topLeftCell="A{freeze_rows + 1}" '
        f'activePane="bottomLeft" state="frozen"/>'
        f"</sheetView></sheetViews>"
        if freeze_rows
        else '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
    )
    filter_xml = f'<autoFilter ref="{auto_filter}"/>' if auto_filter else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<dimension ref=\"{dimension}\"/>"
        f"{pane_xml}"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{columns}</cols>"
        f"<sheetData>{''.join(rendered_rows)}</sheetData>"
        f"{filter_xml}{merge_xml}"
        '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" '
        'header="0.2" footer="0.2"/>'
        "</worksheet>"
    )


def styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="4">
    <font><sz val="10"/><name val="Aptos"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="16"/><name val="Aptos Display"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Aptos"/></font>
    <font><b/><color rgb="FF3B2C27"/><sz val="10"/><name val="Aptos"/></font>
  </fonts>
  <fills count="5">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF984C3D"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF3B2C27"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF5E8E0"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left/><right/><top/>
      <bottom style="thin"><color rgb="FFD8C9C1"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="8">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
    <xf numFmtId="3" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
    <xf numFmtId="10" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
    <xf numFmtId="0" fontId="3" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="3" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def title_rows(title: str, subtitle: str, columns: int) -> list[list[Any]]:
    return [
        [title] + [None] * (columns - 1),
        [subtitle] + [None] * (columns - 1),
        [None] * columns,
    ]


def build_summary(
    competitor_set: dict[str, Any],
    audit: dict[str, Any],
    passport: dict[str, Any],
) -> tuple[list[list[Any]], list[float], list[str], dict[int, int], dict[int, float]]:
    competitors = competitor_set.get("competitors", [])
    counts = {
        kind: sum(item.get("competitor_type_code") == kind for item in competitors)
        for kind in ("direct", "indirect", "attention")
    }
    rows = title_rows(
        "Анализ конкурентов VK",
        (
            f"Окно: {competitor_set['window']['start']} — "
            f"{competitor_set['window']['end']} • status: {competitor_set['status']}"
        ),
        8,
    )
    rows.extend(
        [
            [
                "Показатель",
                "Значение",
                "Показатель",
                "Значение",
                "Показатель",
                "Значение",
                "Показатель",
                "Значение",
            ],
            [
                "Включено",
                len(competitors),
                "Исключено",
                audit["counts"]["excluded"],
                "Постов",
                len(competitor_set.get("posts_in_window", [])),
                "Цель",
                competitor_set["criteria"]["target_count"],
            ],
            [None] * 8,
            [None] * 8,
            ["Тип конкурента", "Количество", "Доля", None, "Контроль", "Значение", None, None],
            [
                "Прямой",
                counts["direct"],
                counts["direct"] / len(competitors) if competitors else 0,
                None,
                "Статус",
                competitor_set["status"],
                None,
                None,
            ],
            [
                "Косвенный",
                counts["indirect"],
                counts["indirect"] / len(competitors) if competitors else 0,
                None,
                "SHA-256 карточки",
                (
                    "sha256:"
                    f"{competitor_set['provenance']['business_context_sha256'][:12]}"
                    "…"
                    f"{competitor_set['provenance']['business_context_sha256'][-12:]}"
                ),
                None,
                None,
            ],
            [
                "За внимание",
                counts["attention"],
                counts["attention"] / len(competitors) if competitors else 0,
                None,
                "Цель выполнена",
                competitor_set["validation"]["target_met"],
                None,
                None,
            ],
            ["Итого", len(competitors), 1 if competitors else 0, None, None, None, None, None],
        ]
    )
    return (
        rows,
        [24, 16, 24, 16, 24, 34, 12, 12],
        ["A1:H1", "A2:H2"],
        {1: 1, 4: 2, 8: 2, 12: 6},
        {2: 4, 3: 5, 4: 4, 6: 4, 8: 4},
    )


def build_competitors(
    competitor_set: dict[str, Any],
) -> tuple[list[list[Any]], list[float], list[str], dict[int, int], dict[int, int]]:
    rows = title_rows(
        "Конкуренты",
        "Только проекты, прошедшие измеримые фильтры и проверку первичных источников.",
        len(COMPETITOR_HEADERS),
    )
    rows.append(COMPETITOR_HEADERS)
    for item in competitor_set.get("competitors", []):
        rows.append(
            [
                item.get("name"),
                item.get("vk"),
                item.get("site"),
                TYPE_LABELS.get(item.get("competitor_type"), item.get("competitor_type")),
                item.get("followers"),
                item.get("last_own_publication_at"),
                item.get("average_er"),
                item.get("assortment"),
                item.get("audience"),
                item.get("geography"),
                item.get("price_segment"),
                item.get("sales_model"),
                item.get("production"),
                item.get("online_offline"),
                item.get("why_competitor"),
                "\n".join(item.get("verified_sources", [])),
            ]
        )
    widths = [26, 27, 27, 16, 13, 21, 17, 42, 34, 22, 26, 36, 34, 28, 42, 44]
    return (
        rows,
        widths,
        [f"A1:P1", f"A2:P2"],
        {1: 1, 4: 2},
        {5: 4, 7: 5},
    )


def build_excluded(
    audit: dict[str, Any],
) -> tuple[list[list[Any]], list[float], list[str], dict[int, int], dict[int, int]]:
    headers = [
        "VK ID",
        "Название",
        "VK",
        "Подписчики",
        "География",
        "Последняя публикация",
        "Постов в окне",
        "Средний ER",
        "Этап исключения",
        "Код причины",
        "Причина исключения",
        "Поисковые запросы",
    ]
    rows = title_rows(
        "Проверены, но исключены",
        "Полный аудит кандидатов с конкретной причиной решения.",
        len(headers),
    )
    rows.append(headers)
    for item in audit.get("candidates", []):
        if item.get("decision") != "excluded":
            continue
        rows.append(
            [
                item.get("community_id"),
                item.get("name"),
                item.get("vk"),
                item.get("followers"),
                item.get("geography"),
                item.get("last_own_publication_at"),
                item.get("posts_in_window_count"),
                item.get("average_er"),
                item.get("exclusion_stage"),
                item.get("exclusion_code"),
                item.get("exclusion_reason"),
                ", ".join(item.get("matched_queries", [])),
            ]
        )
    return (
        rows,
        [13, 30, 28, 13, 20, 21, 14, 14, 22, 25, 55, 42],
        ["A1:L1", "A2:L2"],
        {1: 1, 4: 2},
        {1: 4, 4: 4, 7: 4, 8: 5},
    )


def build_posts(
    competitor_set: dict[str, Any],
) -> tuple[list[list[Any]], list[float], list[str], dict[int, int], dict[int, int]]:
    headers = [
        "VK ID сообщества",
        "Сообщество",
        "Тип конкурента",
        "ID поста",
        "Дата публикации",
        "URL поста",
        "Лайки",
        "Комментарии",
        "Репосты",
        "Просмотры",
        "Взаимодействия",
        "ER",
        "Причина исключения из ER",
        "Закреплён",
        "Есть copy_history",
        "Текст",
    ]
    rows = title_rows(
        "Посты за месяц",
        "Собственные публикации в едином окне; дубли исключены по owner_id + post_id.",
        len(headers),
    )
    rows.append(headers)
    for index, item in enumerate(competitor_set.get("posts_in_window", []), start=5):
        views = item.get("views")
        interactions = item.get("interactions")
        er = item.get("er")
        rows.append(
            [
                item.get("community_id"),
                item.get("community_name"),
                TYPE_LABELS.get(item.get("competitor_type"), item.get("competitor_type")),
                item.get("post_id"),
                item.get("published_at"),
                item.get("url"),
                item.get("likes"),
                item.get("comments"),
                item.get("reposts"),
                views,
                {"formula": f"SUM(G{index}:I{index})", "value": interactions, "style": 4},
                {
                    "formula": f'IFERROR(K{index}/J{index},"")',
                    "value": er,
                    "style": 5,
                },
                item.get("er_exclusion_reason"),
                item.get("is_pinned"),
                item.get("has_copy_history"),
                item.get("text"),
            ]
        )
    return (
        rows,
        [16, 32, 17, 13, 21, 33, 11, 13, 11, 13, 16, 12, 24, 12, 16, 65],
        ["A1:P1", "A2:P2"],
        {1: 1, 4: 2},
        {1: 4, 4: 4, 7: 4, 8: 4, 9: 4, 10: 4, 11: 4, 12: 5},
    )


def build_method(
    competitor_set: dict[str, Any],
    audit: dict[str, Any],
    passport: dict[str, Any],
) -> tuple[list[list[Any]], list[float], list[str], dict[int, int], dict[int, int]]:
    rows = title_rows(
        "Методика и паспорт запуска",
        "Параметры воспроизводимости, фильтры, пагинация, ошибки и ограничения.",
        3,
    )
    rows.append(["Параметр", "Значение", "Пояснение"])
    method_rows = [
        ["Статус", competitor_set["status"], "Меняется только после явного подтверждения"],
        ["Дата подтверждения", competitor_set.get("confirmation_date"), ""],
        ["SHA-256 business_context.json", f"sha256:{competitor_set['provenance']['business_context_sha256']}", "Цепочка происхождения"],
        ["SHA-256 raw_candidates.json", f"sha256:{competitor_set['provenance']['raw_candidates_sha256']}", "Цепочка происхождения"],
        ["SHA-256 candidate_reviews.json", f"sha256:{competitor_set['provenance']['candidate_reviews_sha256']}", "Цепочка происхождения"],
        ["Начало окна UTC", competitor_set["window"]["start"], "Включительно"],
        ["Конец окна UTC", competitor_set["window"]["end"], "Включительно"],
        ["Длительность, часов", competitor_set["window"]["duration_hours"], ""],
        ["Цель", competitor_set["criteria"]["target_count"], "Условия не ослабляются при недоборе"],
        ["Включено", len(competitor_set.get("competitors", [])), ""],
        ["Исключено", audit["counts"]["excluded"], "У каждого кандидата есть причина"],
        ["ER", "(likes + comments + reposts) / views", "Нулевые и отсутствующие просмотры дают null"],
        ["Средний ER", "Среднее по измеримым постам", "Посты с ER = null не входят"],
        ["Окно ER", "31 × 24 = 744 часа", "Единое фиксированное окно для всех сообществ"],
        ["VK API", passport.get("vk_api_version"), ", ".join(passport.get("vk_methods", []))],
        ["Расширение поиска", json.dumps(passport.get("search", {}), ensure_ascii=False), "Остановка: 2 прироста <5% или пул 3× цели"],
        ["Смысловые пакеты", json.dumps(passport.get("semantic_batches", {}), ensure_ascii=False), "Последовательно, не более 200 кандидатов"],
        ["Пагинация", json.dumps(passport.get("pagination", {}), ensure_ascii=False), "Старый закреплённый пост не останавливает обход"],
        ["Ошибки", json.dumps(passport.get("errors", []), ensure_ascii=False), ""],
        ["Ограничения", json.dumps(passport.get("limitations", []), ensure_ascii=False), ""],
    ]
    rows.extend(method_rows)
    return (
        rows,
        [31, 74, 58],
        ["A1:C1", "A2:C2"],
        {1: 1, 4: 2},
        {},
    )


def workbook_parts(
    competitor_set: dict[str, Any],
    audit: dict[str, Any],
    passport: dict[str, Any],
) -> list[str]:
    builders = [
        build_summary(competitor_set, audit, passport),
        build_competitors(competitor_set),
        build_excluded(audit),
        build_posts(competitor_set),
        build_method(competitor_set, audit, passport),
    ]
    parts = []
    for index, (rows, widths, merged, row_styles, column_styles) in enumerate(
        builders,
        start=1,
    ):
        max_column = max((len(row) for row in rows), default=1)
        max_row = len(rows)
        filter_ref = (
            f"A4:{column_name(max_column)}{max_row}" if max_row >= 4 else None
        )
        heights = {1: 30, 2: 24, 4: 34}
        if index in (2, 3, 4):
            for row_number in range(5, max_row + 1):
                heights[row_number] = 58 if index != 4 else 48
        parts.append(
            sheet_xml(
                rows,
                widths=widths,
                merged_ranges=merged,
                freeze_rows=4,
                auto_filter=filter_ref,
                row_styles=row_styles,
                column_styles=column_styles,
                row_heights=heights,
            )
        )
    return parts


def write_workbook(
    output_path: Path,
    competitor_set: dict[str, Any],
    audit: dict[str, Any],
    passport: dict[str, Any],
) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheets = workbook_parts(competitor_set, audit, passport)
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
    ]
    for index in range(1, len(sheets) + 1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types.append("</Types>")

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<bookViews><workbookView/></bookViews><sheets>'
        + "".join(
            f'<sheet name="{escape(name, quote=True)}" sheetId="{index}" r:id="rId{index}"/>'
            for index, name in enumerate(SHEET_NAMES, start=1)
        )
        + '</sheets><calcPr calcId="191029" fullCalcOnLoad="1" forceFullCalc="1"/>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + f'<Relationship Id="rId{len(sheets) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/></Relationships>'
    )
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>VK competitor analysis</dc:title>
  <dc:creator>search-vk-competitors</dc:creator>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
</cp:coreProperties>"""
    app = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>search-vk-competitors</Application>
</Properties>"""

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "".join(content_types))
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles_xml())
        for index, content in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", content)


def workbook_sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("xl/workbook.xml"))
    return [
        element.attrib["name"]
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "sheet"
        and "name" in element.attrib
    ]


def workbook_rows(path: Path) -> dict[str, list[list[Any]]]:
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    names = workbook_sheet_names(path)
    result: dict[str, list[list[Any]]] = {}
    with zipfile.ZipFile(path) as archive:
        for index, name in enumerate(names, start=1):
            root = ET.fromstring(
                archive.read(f"xl/worksheets/sheet{index}.xml")
            )
            rows: list[list[Any]] = []
            for row_element in root.findall(".//x:sheetData/x:row", namespace):
                values: list[Any] = []
                for cell in row_element.findall("x:c", namespace):
                    reference = cell.attrib.get("r", "A1")
                    letters = "".join(
                        character for character in reference if character.isalpha()
                    )
                    column = 0
                    for character in letters:
                        column = column * 26 + ord(character.upper()) - 64
                    while len(values) < column - 1:
                        values.append(None)
                    cell_type = cell.attrib.get("t")
                    inline = cell.find("x:is/x:t", namespace)
                    raw = cell.find("x:v", namespace)
                    if inline is not None:
                        value: Any = inline.text or ""
                    elif raw is None or raw.text in {None, ""}:
                        value = None
                    elif cell_type == "b":
                        value = raw.text == "1"
                    elif cell_type == "str":
                        value = raw.text
                    else:
                        try:
                            number = float(raw.text)
                            value = int(number) if number.is_integer() else number
                        except ValueError:
                            value = raw.text
                    values.append(value)
                while values and values[-1] is None:
                    values.pop()
                rows.append(values)
            result[name] = rows
    return result
