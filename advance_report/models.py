"""Неизменяемые входные данные и результаты; деньги — целые копейки."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: int
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: int
    status: str
    completed_at: date | None
    cancelled_at: date | None


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str
    purpose: str
    amount: int
    status: str
    created_at: date
    paid_at: date | None
    refunded_at: date | None


@dataclass(frozen=True)
class Dataset:
    contracts: tuple[Contract, ...]
    works: tuple[Work, ...]
    payments: tuple[Payment, ...]


@dataclass(frozen=True)
class WorkResult:
    as_of: date
    contract_id: str
    work_id: str
    title: str
    status_as_of: str
    price: int
    obligation: int
    direct_paid: int
    allocated_advance: int
    paid: int
    remaining: int
    overpayment: int


@dataclass(frozen=True)
class ContractResult:
    as_of: date
    contract_id: str
    contractor: str
    contract_amount: int
    works_price: int
    active_works_price: int
    gross_paid: int
    refunded: int
    net_paid: int
    allocated_paid: int
    unallocated_advance: int
    remaining: int
    accepted_remaining: int
    nominal_balance: int


@dataclass(frozen=True)
class Diagnostic:
    as_of: date
    code: str
    entity_type: str
    entity_id: str
    message: str


@dataclass(frozen=True)
class Report:
    works: tuple[WorkResult, ...]
    contracts: tuple[ContractResult, ...]
    diagnostics: tuple[Diagnostic, ...]
