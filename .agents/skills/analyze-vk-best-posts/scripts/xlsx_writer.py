from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from common import InputValidationError, load_json, sha256_file, write_json
from xlsx_contract import finalize_freeze_panes, inspect_xlsx


EXPECTED_SHEETS = ["Сводка", "Лучшие посты", "Проверенные посты", "Недоступно"]
BEST_COLUMNS = [
    "Сообщество", "Тип конкурента", "Ссылка на пост", "Текст", "Тип поста",
    "Опрос", "Розыгрыш", "Лайки", "Комментарии", "ER поста", "Бенчмарк",
    "Расшифровка", "Изображения", "ID поста",
]
CHECKED_COLUMNS = [
    "Сообщество", "VK сообщества", "Подписчики", "Дата поста", "Ссылка на пост",
    "Текст", "Тип поста", "Опрос", "Розыгрыш", "Лайки", "Комментарии",
    "Репосты", "Просмотры", "ER поста", "Бенчмарк", "Результат", "Статус данных",
]
UNAVAILABLE_COLUMNS = ["Объект", "Ссылка", "Конкретная причина"]


def check_xlsx_environment(node: Path, node_modules: Path) -> dict[str, str]:
    if not node.is_file():
        raise InputValidationError("Node.js executable is missing")
    artifact_tool = node_modules / "@oai" / "artifact-tool"
    if not artifact_tool.exists():
        raise InputValidationError("@oai/artifact-tool is missing")
    if importlib.util.find_spec("openpyxl") is None:
        raise InputValidationError("openpyxl is missing")
    return {
        "node": node.name,
        "artifact_tool": "available",
        "openpyxl": "available",
    }


def prepare_node_modules(run_dir: Path, node_modules: Path) -> Path:
    link = run_dir / "node_modules"
    if link.exists():
        return link
    try:
        os.symlink(node_modules, link, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(node_modules)],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            raise
    return link


def build_workbook(
    run_dir: Path,
    node: Path,
    node_modules: Path,
    builder: Path,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    node = node.resolve()
    node_modules = node_modules.resolve()
    builder = builder.resolve()
    check_xlsx_environment(node, node_modules)
    prepare_node_modules(run_dir, node_modules)
    dataset = run_dir / "outputs" / "vk_posts_dataset.json"
    output = run_dir / "outputs" / "vk_best_posts.xlsx"
    runtime_builder = run_dir / "_build_workbook.mjs"
    shutil.copyfile(builder, runtime_builder)
    try:
        result = subprocess.run(
            [
                str(node),
                str(runtime_builder),
                "--dataset",
                str(dataset),
                "--run-dir",
                str(run_dir),
                "--output",
                str(output),
            ],
            cwd=run_dir,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    finally:
        runtime_builder.unlink(missing_ok=True)
    if result.returncode:
        diagnostic = (result.stderr.strip() or result.stdout.strip())[-1200:]
        raise InputValidationError(
            f"XLSX builder failed with code {result.returncode}: {diagnostic}"
        )
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise InputValidationError("XLSX builder returned invalid audit JSON") from exc
    if report.get("sheets") != EXPECTED_SHEETS:
        raise InputValidationError("XLSX sheet list mismatch")
    if report.get("best_columns") != BEST_COLUMNS:
        raise InputValidationError("Best-post column list mismatch")
    if report.get("checked_columns") != CHECKED_COLUMNS:
        raise InputValidationError("Checked-post column list mismatch")
    if report.get("unavailable_columns") != UNAVAILABLE_COLUMNS:
        raise InputValidationError("Unavailable column list mismatch")
    if "matched 0 entries" not in str(report.get("formula_errors")):
        raise InputValidationError("XLSX formula error scan failed")
    if not output.is_file() or output.read_bytes()[:2] != b"PK":
        raise InputValidationError("XLSX output is missing or invalid")
    finalize_freeze_panes(
        output,
        {
            "Сводка": (2, "A3"),
            "Лучшие посты": (4, "A5"),
            "Проверенные посты": (4, "A5"),
            "Недоступно": (4, "A5"),
        },
    )
    report["deep_validation"] = inspect_xlsx(
        output,
        load_json(dataset),
        EXPECTED_SHEETS,
        BEST_COLUMNS,
        CHECKED_COLUMNS,
        UNAVAILABLE_COLUMNS,
    )
    validation_path = run_dir / "outputs" / "workbook_validation.json"
    write_json(validation_path, report)
    passport_path = run_dir / "outputs" / "run_passport.json"
    if passport_path.is_file():
        passport = load_json(passport_path)
        passport.setdefault("hashes", {})["vk_best_posts.xlsx"] = sha256_file(output)
        passport["hashes"]["workbook_validation.json"] = sha256_file(validation_path)
        write_json(passport_path, passport)
    return report
