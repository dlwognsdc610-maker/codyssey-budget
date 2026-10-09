"""Immutable values shared by the CLI and the central ledger system."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Kind = Literal["income", "expense"]


class LedgerError(Exception):
    def __init__(self, reason: str, hint: str):
        super().__init__(reason)
        self.hint = hint


@dataclass(frozen=True)
class Transaction:
    id: str
    date: str
    type: Kind
    category: str
    amount: int
    memo: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Revision:
    version: int
    op: Literal["create", "update", "delete"]
    parent_hash: str
    state: Transaction | None
    hash: str


@dataclass(frozen=True)
class Item:
    id: str
    sort_date: str
    revisions: tuple[Revision, ...]

    @property
    def head(self) -> Transaction | None:
        return self.revisions[-1].state


@dataclass(frozen=True)
class Summary:
    income: int
    expense: int
    category_expense: tuple[tuple[str, int], ...]
    count: int
    budget: int | None

    @property
    def balance(self) -> int:
        return self.income - self.expense
