import csv
import io
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

from advance_report.cli import main
from advance_report.input import DataError, format_money, load_dataset, parse_date, parse_money
from advance_report.models import Contract, Dataset, Payment, Work
from advance_report.report import build_report


ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 10)
HEADERS = {
    "contracts.csv": "contract_id,contractor,amount,signed_at".split(","),
    "works.csv": "work_id,contract_id,title,price,status,completed_at,cancelled_at".split(","),
    "payments.csv": "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at".split(","),
}


def contract(**changes):
    row = dict(contract_id="D", contractor="Подрядчик", amount="100.00", signed_at="2026-01-01")
    row.update(changes)
    return row


def work(**changes):
    row = dict(work_id="W", contract_id="D", title="Работа", price="100.00", status="planned",
               completed_at="", cancelled_at="")
    row.update(changes)
    return row


def payment(**changes):
    row = dict(payment_id="P", contract_id="D", work_id="", purpose="Аванс", amount="50.00",
               status="paid", created_at="2026-01-01", paid_at="2026-02-01", refunded_at="")
    row.update(changes)
    return row


class FixtureCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()

    def write_data(self, contracts=None, works=None, payments=None):
        tables = {
            "contracts.csv": [contract()] if contracts is None else contracts,
            "works.csv": [work()] if works is None else works,
            "payments.csv": [payment()] if payments is None else payments,
        }
        for name, rows in tables.items():
            with (self.data_dir / name).open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=HEADERS[name])
                writer.writeheader()
                writer.writerows(rows)
        return self.data_dir

    def report(self, as_of=AS_OF):
        return build_report(load_dataset(self.data_dir), as_of)

    def run_cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--data-dir", str(self.data_dir), "--as-of", "2026-11-10",
                         "--output-dir", str(self.root / "out"), *extra])
        return code, stdout.getvalue(), stderr.getvalue()


class MoneyAndDateTests(unittest.TestCase):
    def test_exact_kopecks_and_large_numbers(self):
        for source, cents in [("0", 0), ("0.01", 1), ("10.1", 1010), ("001.20", 120),
                              ("9007199254740993.01", 900719925474099301)]:
            with self.subTest(source=source):
                self.assertEqual(parse_money(source), cents)
                self.assertEqual(parse_money(format_money(cents)), cents)
        self.assertEqual(format_money(-1), "-0.01")
        self.assertEqual(format_money(-101), "-1.01")

    def test_invalid_money(self):
        for value in ("", "-1", "+1", "1.001", "NaN", "Infinity", "1e3", "1,20", "1.", ".5", "１２.００"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_money(value)

    def test_strict_calendar_dates(self):
        self.assertEqual(parse_date("2024-02-29"), date(2024, 2, 29))
        for value in ("2026-02-29", "20261110", "2026-W46-2", "2026-1-01", "0000-01-01", "2026-11-10T00:00:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_date(value)


class SourceDataTests(unittest.TestCase):
    def setUp(self):
        self.data = load_dataset(ROOT / "data")

    def test_reference_report(self):
        report = build_report(self.data, AS_OF)
        actual = {w.work_id: (w.status_as_of, w.direct_paid, w.allocated_advance, w.paid, w.remaining)
                  for w in report.works}
        self.assertEqual(actual, {
            "W-11": ("done", 0, 10000000, 10000000, 0),
            "W-12": ("done", 0, 10000000, 10000000, 0),
            "W-13": ("cancelled", 0, 0, 0, 0),
            "W-14": ("done", 1500000, 8500000, 10000000, 0),
            "W-15": ("planned", 0, 1500000, 1500000, 8500000),
            "W-16": ("planned", 0, 0, 0, 10000000),
            "W-21": ("done", 0, 2000000, 2000000, 0),
            "W-22": ("done", 0, 3000000, 3000000, 0),
            "W-23": ("planned", 0, 2500000, 2500000, 1500000),
        })
        first, second = report.contracts
        self.assertEqual((first.contract_amount, first.works_price, first.active_works_price,
                          first.gross_paid, first.refunded, first.net_paid, first.allocated_paid,
                          first.unallocated_advance, first.remaining, first.accepted_remaining,
                          first.nominal_balance),
                         (60000000, 60000000, 50000000, 36500000, 5000000, 31500000,
                          31500000, 0, 18500000, 0, 28500000))
        self.assertEqual((second.net_paid, second.remaining, second.accepted_remaining,
                          second.nominal_balance), (7500000, 1500000, 0, 1500000))
        self.assertTrue({"CANCELLED_WORK", "CANCELLATION_BALANCE", "QUEUED_PAYMENT"}
                        <= {d.code for d in report.diagnostics})

    def test_future_acceptance_and_payment_on_real_data(self):
        accepted = build_report(self.data, date(2026, 11, 11)).contracts[1]
        self.assertEqual((accepted.net_paid, accepted.accepted_remaining), (7500000, 1500000))
        paid = build_report(self.data, date(2026, 11, 12)).contracts[1]
        self.assertEqual((paid.net_paid, paid.remaining, paid.unallocated_advance, paid.nominal_balance),
                         (9500000, 0, 500000, -500000))

    def test_refund_boundary_on_real_data(self):
        before = build_report(self.data, date(2026, 9, 9)).contracts[0]
        after = build_report(self.data, date(2026, 9, 10)).contracts[0]
        self.assertEqual((before.net_paid, before.refunded), (35000000, 0))
        self.assertEqual((after.net_paid, after.refunded), (30000000, 5000000))

    def test_input_order_does_not_change_report(self):
        expected = build_report(self.data, AS_OF)
        rng = random.Random(42)
        for _ in range(10):
            contracts, works, payments = map(list, (self.data.contracts, self.data.works, self.data.payments))
            for group in (contracts, works, payments):
                rng.shuffle(group)
            self.assertEqual(build_report(Dataset(tuple(contracts), tuple(works), tuple(payments)), AS_OF), expected)


class AllocationTests(FixtureCase):
    def test_paid_date_is_inclusive_not_creation_date(self):
        self.write_data(payments=[payment(paid_at="2026-11-10")])
        for day, expected in [(9, 0), (10, 5000), (11, 5000)]:
            with self.subTest(day=day):
                self.assertEqual(self.report(date(2026, 11, day)).works[0].paid, expected)

    def test_refund_is_effective_only_on_refund_date(self):
        for target in ("", "W"):
            with self.subTest(work_id=target):
                self.write_data(payments=[payment(work_id=target, status="refunded", refunded_at="2026-11-10")])
                self.assertEqual(self.report(date(2026, 1, 31)).works[0].paid, 0)
                self.assertEqual(self.report(date(2026, 2, 1)).works[0].paid, 5000)
                self.assertEqual(self.report(date(2026, 11, 9)).works[0].paid, 5000)
                result = self.report().contracts[0]
                self.assertEqual((result.gross_paid, result.refunded, result.net_paid), (5000, 5000, 0))
                self.assertEqual(self.report().works[0].remaining, 10000)
                self.assertEqual(self.report(date(2026, 11, 11)).works[0].paid, 0)

    def test_queued_payment_never_counts(self):
        self.write_data(payments=[payment(status="queued", paid_at="", created_at="2026-11-10")])
        self.assertEqual(self.report().contracts[0].net_paid, 0)
        self.assertIn("QUEUED_PAYMENT", {d.code for d in self.report().diagnostics})
        self.assertNotIn("QUEUED_PAYMENT", {d.code for d in self.report(date(2026, 11, 9)).diagnostics})

    def test_acceptance_and_cancellation_are_historical(self):
        self.write_data(works=[work(status="cancelled", completed_at="2026-10-10", cancelled_at="2026-11-10")])
        for as_of, expected in [(date(2026, 10, 9), "planned"), (date(2026, 10, 10), "done"),
                                (date(2026, 11, 9), "done"), (AS_OF, "cancelled"),
                                (date(2026, 11, 11), "cancelled")]:
            with self.subTest(as_of=as_of):
                self.assertEqual(self.report(as_of).works[0].status_as_of, expected)
        result = self.report()
        self.assertEqual((result.works[0].price, result.works[0].obligation, result.works[0].paid,
                          result.works[0].remaining, result.contracts[0].unallocated_advance),
                         (10000, 0, 0, 0, 5000))

    def test_cancelled_work_without_acceptance_is_planned_before_cancellation(self):
        self.write_data(works=[work(status="cancelled", cancelled_at="2026-11-10")])
        self.assertEqual(self.report(date(2026, 11, 9)).works[0].status_as_of, "planned")

    def test_contract_signing_is_inclusive(self):
        self.write_data(payments=[])
        self.assertEqual(self.report(date(2025, 12, 31)).contracts, ())
        self.assertEqual(len(self.report(date(2026, 1, 1)).works), 1)

    def test_direct_payment_has_priority_over_pool(self):
        self.write_data(works=[work(work_id="A"), work(work_id="B")], payments=[
            payment(payment_id="P1", amount="100.00"),
            payment(payment_id="P2", amount="60.00", work_id="A"),
        ])
        first, second = self.report().works
        self.assertEqual((first.direct_paid, first.allocated_advance, first.paid, second.paid),
                         (6000, 4000, 10000, 6000))

    def test_done_by_completion_then_id_before_planned(self):
        self.write_data(works=[work(work_id="A-planned"),
                               work(work_id="C", status="done", completed_at="2026-04-01"),
                               work(work_id="B", status="done", completed_at="2026-03-01"),
                               work(work_id="A-done", status="done", completed_at="2026-03-01")],
                        payments=[payment(amount="250.00")])
        self.assertEqual({w.work_id: w.paid for w in self.report().works},
                         {"A-done": 10000, "B": 10000, "C": 5000, "A-planned": 0})

    def test_planned_order_is_lexicographic(self):
        self.write_data(works=[work(work_id="W-2"), work(work_id="W-10")])
        self.assertEqual({w.work_id: w.paid for w in self.report().works}, {"W-10": 5000, "W-2": 0})

    def test_direct_overpayment_is_not_moved_to_other_work(self):
        self.write_data(works=[work(work_id="A"), work(work_id="B")],
                        payments=[payment(work_id="A", amount="120.00"),
                                  payment(payment_id="P2", amount="10.00")])
        report = self.report()
        first, second = report.works
        self.assertEqual((first.paid, first.overpayment, first.remaining, second.paid, second.remaining),
                         (12000, 2000, 0, 1000, 9000))
        self.assertEqual(report.contracts[0].remaining, 9000)
        self.assertIn("WORK_OVERPAYMENT", {d.code for d in report.diagnostics})

    def test_direct_cancelled_payment_stays_with_cancelled_work(self):
        self.write_data(works=[work(work_id="A", status="cancelled", cancelled_at="2026-03-01"),
                               work(work_id="B")],
                        payments=[payment(work_id="A", amount="25.00"), payment(payment_id="P2", amount="40.00")])
        report = self.report()
        first, second = report.works
        self.assertEqual((first.obligation, first.paid, first.overpayment, first.remaining, second.paid),
                         (0, 2500, 2500, 0, 4000))
        self.assertEqual((report.contracts[0].net_paid, report.contracts[0].remaining), (6500, 6000))
        self.assertIn("CANCELLED_WORK_PAID", {d.code for d in report.diagnostics})

    def test_surplus_and_empty_work_list(self):
        for works, allocated, unallocated in [([work()], 10000, 5000), ([], 0, 15000)]:
            with self.subTest(works=works):
                self.write_data(works=works, payments=[payment(amount="150.00")])
                result = self.report().contracts[0]
                self.assertEqual((result.allocated_paid, result.unallocated_advance, result.nominal_balance),
                                 (allocated, unallocated, -5000))

    def test_empty_dataset_and_no_payments(self):
        self.write_data(contracts=[], works=[], payments=[])
        report = self.report()
        self.assertEqual((report.contracts, report.works), ((), ()))
        self.write_data(payments=[])
        result = self.report().works[0]
        self.assertEqual((result.paid, result.remaining), (0, 10000))

    def test_kopecks_large_amount_and_purpose_not_interpreted(self):
        self.write_data(works=[work(price="9007199254740993.01")], payments=[
            payment(amount="9007199254740993.00", purpose="Материалы, не обычный аванс")])
        self.assertEqual(self.report().works[0].remaining, 1)

    def test_contracts_do_not_share_payments(self):
        second = contract()
        second["contract_id"] = "D2"
        self.write_data(contracts=[contract(), second],
                        works=[work(), work(work_id="W2", contract_id="D2")])
        self.assertEqual([w.paid for w in self.report().works], [5000, 0])

    def test_randomized_conservation_and_no_mutation(self):
        rng = random.Random(197)
        for case in range(100):
            contracts = tuple(Contract(cid, cid, 10000, date(2026, 1, 1)) for cid in ("A", "B"))
            works = tuple(Work(f"{c.contract_id}-{i}", c.contract_id, "Тест", rng.randrange(20000),
                               state, date(2026, 3, 1) if state == "done" else None,
                               date(2026, 4, 1) if state == "cancelled" else None)
                          for c in contracts for i in range(5)
                          for state in [rng.choice(("planned", "done", "cancelled"))])
            payments = []
            for c in contracts:
                for i in range(10):
                    state = rng.choice(("paid", "refunded", "queued"))
                    payments.append(Payment(f"{c.contract_id}-P{i}", c.contract_id,
                                            rng.choice([""] + [w.work_id for w in works if w.contract_id == c.contract_id]),
                                            "Тест", rng.randrange(20000), state, date(2026, 1, 2),
                                            date(2026, 2, 1) if state != "queued" else None,
                                            date(2026, 5, 1) if state == "refunded" else None))
            data = Dataset(contracts, works, tuple(payments))
            original = repr(data)
            with self.subTest(case=case):
                report = build_report(data, AS_OF)
                for c in report.contracts:
                    group = [w for w in report.works if w.contract_id == c.contract_id]
                    expected = sum(p.amount for p in data.payments if p.contract_id == c.contract_id and p.status == "paid")
                    self.assertEqual(c.net_paid, expected)
                    self.assertEqual(c.net_paid, sum(w.paid for w in group) + c.unallocated_advance)
                    self.assertEqual(c.remaining, sum(w.remaining for w in group))
                    self.assertGreaterEqual(c.unallocated_advance, 0)
                    for w in group:
                        self.assertGreaterEqual(w.remaining, 0)
                        self.assertGreaterEqual(w.overpayment, 0)
                        self.assertEqual(w.remaining - w.overpayment, w.obligation - w.paid)
                        self.assertEqual(w.paid, w.direct_paid + w.allocated_advance)
                        self.assertLessEqual(w.allocated_advance, max(w.obligation - w.direct_paid, 0))
                self.assertEqual(repr(data), original)


class ValidationTests(FixtureCase):
    def assert_invalid(self, filename, message):
        with self.assertRaises(DataError) as context:
            load_dataset(self.data_dir)
        self.assertIn(filename, str(context.exception))
        self.assertIn("строка", str(context.exception))
        self.assertIn(message, str(context.exception))

    def test_invalid_work_fields(self):
        for changes, expected in [({"status": "unknown"}, "статус"),
                                  ({"work_id": ""}, "work_id"),
                                  ({"contract_id": "missing"}, "неизвестный договор"),
                                  ({"status": "done"}, "completed_at"),
                                  ({"status": "cancelled"}, "cancelled_at"),
                                  ({"completed_at": "2026-02-01"}, "planned"),
                                  ({"status": "done", "completed_at": "2026-02-30"}, "дата"),
                                  ({"status": "cancelled", "cancelled_at": "2026-02-01", "completed_at": "2026-03-01"}, "отмена раньше"),
                                  ({"status": "done", "completed_at": "2025-12-31"}, "раньше подписания"),
                                  ({"status": "done", "completed_at": "2026-02-01", "cancelled_at": "2026-03-01"}, "done требует"),
                                  ({"price": "-1"}, "сумма")]:
            with self.subTest(changes=changes):
                self.write_data(works=[work(**changes)])
                self.assert_invalid("works.csv", expected)

    def test_invalid_payment_fields(self):
        for changes, expected in [({"status": "unknown"}, "статус"),
                                  ({"payment_id": ""}, "payment_id"),
                                  ({"contract_id": "missing"}, "неизвестный договор"),
                                  ({"work_id": "missing"}, "неизвестная работа"),
                                  ({"paid_at": ""}, "paid требует"),
                                  ({"status": "queued"}, "queued"),
                                  ({"status": "refunded"}, "refunded требует"),
                                  ({"refunded_at": "2026-03-01"}, "paid требует"),
                                  ({"created_at": "2026-03-01"}, "оплата раньше создания"),
                                  ({"created_at": "2025-12-31"}, "раньше подписания"),
                                  ({"status": "refunded", "refunded_at": "2026-01-31"}, "возврат раньше"),
                                  ({"amount": "1.001"}, "сумма")]:
            with self.subTest(changes=changes):
                self.write_data(payments=[payment(**changes)])
                self.assert_invalid("payments.csv", expected)

    def test_foreign_work_reference(self):
        other = contract()
        other["contract_id"] = "D2"
        self.write_data(contracts=[contract(), other], payments=[payment(contract_id="D2", work_id="W")])
        self.assert_invalid("payments.csv", "другому договору")

    def test_duplicate_identifiers(self):
        for filename, changes in [("contracts.csv", {"contracts": [contract(), contract()]}),
                                  ("works.csv", {"works": [work(), work()]}),
                                  ("payments.csv", {"payments": [payment(), payment()]})]:
            with self.subTest(filename=filename):
                self.write_data(**changes)
                self.assert_invalid(filename, "повторяющийся идентификатор")

    def test_bad_contract_fields(self):
        for field, value in [("amount", "NaN"), ("signed_at", "2026-13-01"), ("contractor", "")]:
            with self.subTest(field=field):
                row = contract()
                row[field] = value
                self.write_data(contracts=[row])
                self.assert_invalid("contracts.csv", field)

    def test_bad_csv_structure(self):
        for contents, expected in [("", "заголовок"),
                                    ("contract_id,contract_id\n", "повторяющиеся колонки"),
                                    ("contract_id\nD\n", "отсутствуют колонки"),
                                    ("contract_id,contractor,amount,signed_at\nD,Name,100\n", "число значений"),
                                    ("contract_id,contractor,amount,signed_at\nD,Name,100,2026-01-01,extra\n", "число значений"),
                                    ('contract_id,contractor,amount,signed_at\nD,"unclosed', "прочитать CSV")]:
            with self.subTest(contents=contents):
                self.write_data()
                (self.data_dir / "contracts.csv").write_text(contents, encoding="utf-8")
                self.assert_invalid("contracts.csv", expected)

    def test_missing_file_and_invalid_encoding(self):
        self.write_data()
        (self.data_dir / "payments.csv").unlink()
        self.assert_invalid("payments.csv", "прочитать CSV")
        (self.data_dir / "payments.csv").write_bytes(b"\xff\xfe\xff")
        self.assert_invalid("payments.csv", "прочитать CSV")

    def test_csv_syntax_error_reports_actual_physical_line(self):
        self.write_data()
        (self.data_dir / "contracts.csv").write_text(
            'contract_id,contractor,amount,signed_at\nD,"не закрытая строка\nпродолжение', encoding="utf-8")
        with self.assertRaises(DataError) as context:
            load_dataset(self.data_dir)
        self.assertIn("строка 3", str(context.exception))

    def test_future_invalid_records_are_not_silently_ignored(self):
        self.write_data(payments=[payment(created_at="2027-01-01", paid_at="2026-12-31")])
        self.assert_invalid("payments.csv", "оплата раньше создания")

    def test_zero_prices_and_same_day_events_are_valid(self):
        self.write_data(works=[work(price="0.00", status="cancelled", completed_at="2026-01-01", cancelled_at="2026-01-01")],
                        payments=[payment(amount="0.00", status="refunded", paid_at="2026-01-01", refunded_at="2026-01-01")])
        self.assertEqual(self.report().contracts[0].net_paid, 0)


class CliTests(FixtureCase):
    def test_csv_round_trip_bom_and_reproducible_output(self):
        title = 'Дизайн, "осень"\nВторая строка'
        self.write_data(works=[work(title=title)])
        code, stdout, stderr = self.run_cli()
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("2026-11-10", stdout)
        before = {}
        for name in ("works.csv", "contracts.csv", "diagnostics.csv"):
            path = self.root / "out" / name
            before[name] = path.read_bytes()
            self.assertTrue(before[name].startswith(b"\xef\xbb\xbf"))
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertTrue(rows)
            if name == "works.csv":
                self.assertEqual((rows[0]["title"], rows[0]["paid"], rows[0]["remaining"]), (title, "50.00", "50.00"))
            if name == "contracts.csv":
                self.assertEqual((rows[0]["gross_paid"], rows[0]["net_paid"]), ("50.00", "50.00"))
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(before, {name: (self.root / "out" / name).read_bytes() for name in before})
        self.assertFalse(list((self.root / "out").glob(".advance-*")))

    def test_invalid_input_does_not_create_or_replace_output(self):
        self.write_data()
        self.assertEqual(self.run_cli()[0], 0)
        existing = {p.name: p.read_bytes() for p in (self.root / "out").iterdir()}
        self.write_data(payments=[payment(amount="invalid")])
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("payments.csv", stderr)
        self.assertIn("строка 2", stderr)
        self.assertEqual(existing, {p.name: p.read_bytes() for p in (self.root / "out").iterdir()})
        new_output = self.root / "not-created"
        self.assertEqual(self.run_cli("--output-dir", str(new_output))[0], 1)
        self.assertFalse(new_output.exists())

    def test_output_cannot_overwrite_sources_including_symlink(self):
        self.write_data()
        original = {p.name: p.read_bytes() for p in self.data_dir.iterdir()}
        for directory in (self.data_dir, self.root / "alias"):
            if directory != self.data_dir:
                directory.symlink_to(self.data_dir, target_is_directory=True)
            code, _, stderr = self.run_cli("--output-dir", str(directory))
            self.assertEqual(code, 1)
            self.assertIn("исходным CSV", stderr)
        output = self.root / "out"
        output.mkdir()
        (output / "works.csv").symlink_to(self.data_dir / "works.csv")
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.data_dir.iterdir()})

    def test_unwritable_output_shape_has_clear_error(self):
        self.write_data()
        (self.root / "out").write_text("file, not directory", encoding="utf-8")
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("Ошибка", stderr)

    def test_existing_directory_instead_of_report_is_rejected_before_writing(self):
        self.write_data()
        (self.root / "out" / "contracts.csv").mkdir(parents=True)
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("не является файлом", stderr)
        self.assertFalse((self.root / "out" / "works.csv").exists())

    def test_empty_report_still_has_headers(self):
        self.write_data(contracts=[], works=[], payments=[])
        self.assertEqual(self.run_cli()[0], 0)
        for name in ("works.csv", "contracts.csv"):
            with (self.root / "out" / name).open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                self.assertIn("as_of", reader.fieldnames)
                self.assertEqual(list(reader), [])

    def test_module_entrypoint_and_bad_arguments(self):
        result = subprocess.run([sys.executable, "-m", "advance_report", "--help"], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--as-of", result.stdout)
        for args in ([], ["--as-of", "2026-02-30", "--output-dir", str(self.root / "unused")]):
            result = subprocess.run([sys.executable, "-m", "advance_report", *args], cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
