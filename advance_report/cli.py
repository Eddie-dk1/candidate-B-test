"""CLI и сериализация CSV; зависимости — только стандартная библиотека."""

import argparse
import csv
import io
import os
import sys
import tempfile
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path

from .input import DataError, format_money, load_dataset, parse_date
from .models import ContractResult, Diagnostic, Report, WorkResult
from .report import build_report


WORK_MONEY = frozenset({"price", "obligation", "direct_paid", "allocated_advance", "paid", "remaining", "overpayment"})
CONTRACT_MONEY = frozenset({"contract_amount", "works_price", "active_works_price", "gross_paid", "refunded",
                            "net_paid", "allocated_paid", "unallocated_advance", "remaining",
                            "accepted_remaining", "nominal_balance"})
OUTPUT_FILES = ("works.csv", "contracts.csv", "diagnostics.csv")


def _csv_text(records: tuple, model: type, money_fields: frozenset[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=[field.name for field in fields(model)])
    writer.writeheader()
    for record in records:
        values = asdict(record)
        for key, value in values.items():
            if key in money_fields:
                values[key] = format_money(value)
            elif isinstance(value, date):
                values[key] = value.isoformat()
        writer.writerow(values)
    return output.getvalue()


def write_report(report: Report, output_dir: Path, data_dir: Path) -> None:
    sources = {(data_dir / name).resolve() for name in ("works.csv", "contracts.csv", "payments.csv")}
    destinations = [output_dir / name for name in OUTPUT_FILES]
    for destination in destinations:
        if destination.resolve() in sources:
            raise DataError(f"путь результата {destination} совпадает с исходным CSV; выберите другой --output-dir")
        if destination.exists() and not destination.is_file():
            raise DataError(f"путь результата {destination} не является файлом")

    # Сначала рассчитываем и сериализуем всё; не трогаем результаты при ошибке входа.
    contents = (
        _csv_text(report.works, WorkResult, WORK_MONEY),
        _csv_text(report.contracts, ContractResult, CONTRACT_MONEY),
        _csv_text(report.diagnostics, Diagnostic, frozenset()),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary: list[Path] = []
    try:
        for content in contents:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                             dir=output_dir, prefix=".advance-", delete=False) as target:
                temporary.append(Path(target.name))
                target.write(content)
        for source, destination in zip(temporary, destinations):
            os.replace(source, destination)
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)


def _cli_date(value: str) -> date:
    try:
        return parse_date(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ожидается существующая дата YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Распределить авансы по работам на указанную дату (включительно).")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="каталог исходных CSV (по умолчанию data)")
    parser.add_argument("--as-of", type=_cli_date, required=True, help="дата отчёта YYYY-MM-DD")
    parser.add_argument("--output-dir", type=Path, required=True, help="каталог трёх CSV; прежние отчёты в нём заменяются")
    args = parser.parse_args(argv)
    try:
        data = load_dataset(args.data_dir)
        report = build_report(data, args.as_of)
        write_report(report, args.output_dir, args.data_dir)
    except (DataError, OSError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Отчёт на {args.as_of.isoformat()}: договоров {len(report.contracts)}, работ {len(report.works)}.")
    print(f"Файлы: {args.output_dir.resolve()}")
    print(f"Пояснений: {len(report.diagnostics)} — см. diagnostics.csv. Оплата работ распределена расчётно.")
    return 0
