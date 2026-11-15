"""Строгая загрузка CSV и проверка связей до расчёта отчёта."""

import csv
import re
from datetime import date
from pathlib import Path
from typing import Iterator

from .models import Contract, Dataset, Payment, Work


class DataError(ValueError):
    """Ошибка выгрузки, пригодная для показа пользователю."""


def parse_date(value: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("ожидается дата YYYY-MM-DD")
    return date.fromisoformat(value)


def parse_money(value: str) -> int:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError("ожидается неотрицательная сумма с точностью до копейки")
    rubles, _, kopecks = value.partition(".")
    return int(rubles) * 100 + int(kopecks.ljust(2, "0"))


def format_money(kopecks: int) -> str:
    sign = "-" if kopecks < 0 else ""
    rubles, cents = divmod(abs(kopecks), 100)
    return f"{sign}{rubles}.{cents:02d}"


def _rows(path: Path, required: tuple[str, ...]) -> Iterator[tuple[int, dict[str, str]]]:
    line = 1
    reader = None
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            headers = reader.fieldnames
            if not headers:
                raise DataError(f"{path}: строка 1: отсутствует заголовок CSV")
            if len(headers) != len(set(headers)):
                raise DataError(f"{path}: строка 1: повторяющиеся колонки")
            missing = set(required) - set(headers)
            if missing:
                raise DataError(f"{path}: строка 1: отсутствуют колонки: {', '.join(sorted(missing))}")
            for row in reader:
                line = reader.line_num
                if None in row or any(value is None for value in row.values()):
                    raise DataError(f"{path}: строка {line}: число значений не совпадает с заголовком")
                yield line, {key: value.strip() for key, value in row.items()}
    except (OSError, UnicodeError, csv.Error) as exc:
        if reader is not None:
            line = max(line, reader.reader.line_num)
        raise DataError(f"{path}: строка {line}: не удалось прочитать CSV: {exc}") from exc


def _required(row: dict[str, str], field: str) -> str:
    value = row[field]
    if not value:
        raise ValueError(f"{field}: значение обязательно")
    return value


def _date(row: dict[str, str], field: str) -> date:
    try:
        return parse_date(row[field])
    except ValueError as exc:
        raise ValueError(f"{field}: некорректная дата {row[field]!r}; ожидается YYYY-MM-DD") from exc


def _optional_date(row: dict[str, str], field: str) -> date | None:
    return _date(row, field) if row[field] else None


def _money(row: dict[str, str], field: str) -> int:
    try:
        return parse_money(row[field])
    except ValueError as exc:
        raise ValueError(f"{field}: некорректная сумма {row[field]!r}; {exc}") from exc


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _unique(identifier: str, seen: dict) -> None:
    _check(identifier not in seen, f"повторяющийся идентификатор {identifier!r}")


def load_dataset(data_dir: Path) -> Dataset:
    contracts: dict[str, Contract] = {}
    works: dict[str, Work] = {}
    payments: dict[str, Payment] = {}

    path = data_dir / "contracts.csv"
    for line, row in _rows(path, ("contract_id", "contractor", "amount", "signed_at")):
        try:
            cid = _required(row, "contract_id")
            _unique(cid, contracts)
            contracts[cid] = Contract(cid, _required(row, "contractor"), _money(row, "amount"),
                                      _date(row, "signed_at"))
        except ValueError as exc:
            raise DataError(f"{path}: строка {line}: {exc}") from exc

    path = data_dir / "works.csv"
    for line, row in _rows(path, ("work_id", "contract_id", "title", "price", "status",
                                 "completed_at", "cancelled_at")):
        try:
            wid = _required(row, "work_id")
            _unique(wid, works)
            cid = _required(row, "contract_id")
            _check(cid in contracts, f"неизвестный договор {cid!r}")
            status = row["status"]
            _check(status in {"planned", "done", "cancelled"}, f"неизвестный статус работы {status!r}")
            completed = _optional_date(row, "completed_at")
            cancelled = _optional_date(row, "cancelled_at")
            if status == "planned":
                _check(completed is None and cancelled is None, "planned несовместим с датами принятия/отмены")
            elif status == "done":
                _check(completed is not None and cancelled is None, "done требует completed_at и пустой cancelled_at")
            else:
                _check(cancelled is not None, "cancelled требует cancelled_at")
            if completed and cancelled:
                _check(completed <= cancelled, "отмена раньше принятия работы")
            for event in (completed, cancelled):
                _check(event is None or event >= contracts[cid].signed_at, "событие работы раньше подписания договора")
            works[wid] = Work(wid, cid, _required(row, "title"), _money(row, "price"), status,
                              completed, cancelled)
        except ValueError as exc:
            raise DataError(f"{path}: строка {line}: {exc}") from exc

    path = data_dir / "payments.csv"
    for line, row in _rows(path, ("payment_id", "contract_id", "work_id", "purpose", "amount",
                                 "status", "created_at", "paid_at", "refunded_at")):
        try:
            pid = _required(row, "payment_id")
            _unique(pid, payments)
            cid = _required(row, "contract_id")
            _check(cid in contracts, f"неизвестный договор {cid!r}")
            wid = row["work_id"]
            if wid:
                _check(wid in works, f"неизвестная работа {wid!r}")
                _check(works[wid].contract_id == cid, f"работа {wid!r} принадлежит другому договору")
            status = row["status"]
            _check(status in {"paid", "queued", "refunded"}, f"неизвестный статус платежа {status!r}")
            created = _date(row, "created_at")
            paid = _optional_date(row, "paid_at")
            refunded = _optional_date(row, "refunded_at")
            if status == "queued":
                _check(paid is None and refunded is None, "queued несовместим с датами оплаты/возврата")
            elif status == "paid":
                _check(paid is not None and refunded is None, "paid требует paid_at и пустой refunded_at")
            else:
                _check(paid is not None and refunded is not None, "refunded требует paid_at и refunded_at")
            _check(created >= contracts[cid].signed_at, "платёж создан раньше подписания договора")
            if paid:
                _check(paid >= created, "оплата раньше создания платежа")
            if refunded and paid:
                _check(refunded >= paid, "возврат раньше оплаты")
            payments[pid] = Payment(pid, cid, wid, row["purpose"], _money(row, "amount"), status,
                                    created, paid, refunded)
        except ValueError as exc:
            raise DataError(f"{path}: строка {line}: {exc}") from exc

    return Dataset(tuple(contracts.values()), tuple(works.values()), tuple(payments.values()))
