"""Чистый расчёт снимка. Общий аванс распределяется заново на каждую дату."""

from datetime import date

from .input import format_money
from .models import ContractResult, Dataset, Diagnostic, Report, Work, WorkResult


def status_at(work: Work, as_of: date) -> str:
    if work.cancelled_at and work.cancelled_at <= as_of:
        return "cancelled"
    if work.completed_at and work.completed_at <= as_of:
        return "done"
    return "planned"


def build_report(data: Dataset, as_of: date) -> Report:
    """Принимает проверенный load_dataset набор; не изменяет его и не выполняет I/O."""
    work_results: list[WorkResult] = []
    contract_results: list[ContractResult] = []
    diagnostics: list[Diagnostic] = []

    def note(code: str, kind: str, identifier: str, message: str) -> None:
        diagnostics.append(Diagnostic(as_of, code, kind, identifier, message))

    note("ALLOCATION_POLICY", "report", "", "Оплата работ — расчётное распределение: адресные платежи, затем общий аванс на принятые работы по дате принятия, затем на запланированные по work_id. Это не подтверждённая привязка платежей.")
    note("HISTORICAL_WORK_LIST", "report", "", "Даты появления и история цен работ отсутствуют: все работы считаются известными с подписания договора, цены — неизменными.")
    note("BALANCE_NOT_DUE", "report", "", "Остаток — непокрытая стоимость; сроки оплаты неизвестны. accepted_remaining показывает остаток принятых работ, а не просрочку.")

    for contract in sorted(data.contracts, key=lambda item: item.contract_id):
        if contract.signed_at > as_of:
            continue
        cid = contract.contract_id
        works = sorted((w for w in data.works if w.contract_id == cid), key=lambda w: w.work_id)
        states = {w.work_id: status_at(w, as_of) for w in works}
        direct = {w.work_id: 0 for w in works}
        advances = {w.work_id: 0 for w in works}
        gross_paid = refunded = pool = 0
        for payment in sorted(data.payments, key=lambda item: item.payment_id):
            if payment.contract_id != cid:
                continue
            if payment.status == "queued" and payment.created_at <= as_of:
                note("QUEUED_PAYMENT", "payment", payment.payment_id,
                     f"Платёж {format_money(payment.amount)} в очереди; в оплату не включён.")
            if payment.paid_at is None or payment.paid_at > as_of:
                continue
            gross_paid += payment.amount
            if payment.refunded_at and payment.refunded_at <= as_of:
                refunded += payment.amount
                continue
            if payment.work_id:
                direct[payment.work_id] += payment.amount
            else:
                pool += payment.amount

        eligible = [w for w in works if states[w.work_id] != "cancelled"]
        eligible.sort(key=lambda w: (0, w.completed_at or date.max, w.work_id)
                      if states[w.work_id] == "done" else (1, date.max, w.work_id))
        for work in eligible:
            allocated = min(pool, max(work.price - direct[work.work_id], 0))
            advances[work.work_id] = allocated
            pool -= allocated

        current: list[WorkResult] = []
        for work in works:
            wid = work.work_id
            state = states[wid]
            obligation = 0 if state == "cancelled" else work.price
            paid = direct[wid] + advances[wid]
            result = WorkResult(as_of, cid, wid, work.title, state, work.price, obligation,
                                direct[wid], advances[wid], paid, max(obligation - paid, 0),
                                max(paid - obligation, 0))
            current.append(result)
            if state == "cancelled":
                note("CANCELLED_WORK", "work", wid,
                     f"Работа отменена: исходная цена {format_money(work.price)}, обязательство принято равным нулю. Сумма договора автоматически не изменена.")
            if result.overpayment:
                code = "CANCELLED_WORK_PAID" if state == "cancelled" else "WORK_OVERPAYMENT"
                note(code, "work", wid,
                     f"Адресная оплата превышает действующее обязательство на {format_money(result.overpayment)}; на другие работы не перенесена.")

        works_price = sum(w.price for w in works)
        active_price = sum(w.obligation for w in current)
        net_paid = gross_paid - refunded
        nominal_balance = contract.amount - net_paid
        contract_results.append(ContractResult(
            as_of, cid, contract.contractor, contract.amount, works_price, active_price,
            gross_paid, refunded, net_paid, sum(w.paid for w in current), pool,
            sum(w.remaining for w in current),
            sum(w.remaining for w in current if w.status_as_of == "done"), nominal_balance,
        ))
        work_results.extend(current)
        if works_price != contract.amount:
            note("WORKS_CONTRACT_MISMATCH", "contract", cid,
                 f"Сумма цен работ {format_money(works_price)} отличается от суммы договора {format_money(contract.amount)}; неучтённые работы не созданы.")
        if active_price != works_price:
            note("CANCELLATION_BALANCE", "contract", cid,
                 f"Из расчётных обязательств исключены отменённые работы на {format_money(works_price - active_price)}. Юридическое уменьшение суммы договора требует подтверждения.")
        if pool:
            note("UNALLOCATED_ADVANCE", "contract", cid,
                 f"Аванс {format_money(pool)} не распределён: доступная стоимость работ уже покрыта или действующих работ нет.")
        if nominal_balance < 0:
            note("CONTRACT_OVERPAYMENT", "contract", cid,
                 f"Чистая оплата превышает номинальную сумму договора на {format_money(-nominal_balance)}.")

    diagnostics.sort(key=lambda item: (item.entity_type, item.entity_id, item.code))
    return Report(tuple(work_results), tuple(contract_results), tuple(diagnostics))
