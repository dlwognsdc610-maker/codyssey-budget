#!/usr/bin/env python3
"""Independent CTS: reference item FSM, transition walks, and fault probes."""

from __future__ import annotations

import ast
import csv
import concurrent.futures
import gc
import hashlib
import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from budget_app import Ledger, LedgerError


HERE = Path(__file__).resolve().parent
FILES = ("transactions.jsonl", "categories.jsonl", "budgets.jsonl")
WALK_TRANSITIONS = 0
WALK_DISTRIBUTION: dict[str, int] = {}
MEMORY_PEAKS: tuple[int, int] | None = None


def plain(tx: object) -> dict[str, object] | None:
    if tx is None:
        return None
    value = asdict(tx)
    value["tags"] = list(value["tags"])
    return value


class Oracle:
    """Plain dict/list reference; no production validation, sort, or sums."""

    def __init__(self) -> None:
        self.categories: set[str] = set()
        self.budgets: dict[str, int] = {}
        self.items: dict[str, list[dict[str, object]]] = {}
        self.next_id = 1

    @staticmethod
    def define(id_value: str, op: str, state: dict[str, object] | None,
               old: dict[str, object] | None) -> dict[str, object]:
        version = int(old["version"]) + 1 if old else 1
        predecessor = str(old["hash"]) if old else ""
        body = {"id": id_value, "version": version, "op": op,
                "parent_hash": predecessor, "state": state}
        digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        return {**body, "hash": digest}

    def apply(self, operation: tuple[object, ...]) -> object:
        action = operation[0]
        if action == "category_add":
            name = str(operation[1])
            if name in self.categories:
                raise ValueError("duplicate category")
            self.categories.add(name)
        elif action == "category_remove":
            name = str(operation[1])
            if name not in self.categories or any(
                chain[-1]["state"] is not None and chain[-1]["state"]["category"] == name
                for chain in self.items.values()
            ):
                raise ValueError("missing/used category")
            self.categories.remove(name)
        elif action == "add":
            _, day, kind, category, amount, memo, tags = operation
            if category not in self.categories:
                raise ValueError("category")
            id_value = f"TX-{self.next_id:06d}"
            self.next_id += 1
            state = {"id": id_value, "date": day, "type": kind,
                     "category": category, "amount": amount, "memo": memo,
                     "tags": list(tags)}
            self.items[id_value] = [self.define(id_value, "create", state, None)]
            return id_value
        elif action == "update":
            _, id_value, changes = operation
            chain = self.items.get(str(id_value))
            if not chain or chain[-1]["state"] is None:
                raise ValueError("missing item")
            state = deepcopy(chain[-1]["state"])
            state.update(changes)
            if state["category"] not in self.categories:
                raise ValueError("category")
            if isinstance(state["tags"], tuple):
                state["tags"] = list(state["tags"])
            chain.append(self.define(str(id_value), "update", state, chain[-1]))
        elif action == "delete":
            id_value = str(operation[1])
            chain = self.items.get(id_value)
            if not chain or chain[-1]["state"] is None:
                raise ValueError("missing item")
            chain.append(self.define(id_value, "delete", None, chain[-1]))
        elif action == "budget":
            self.budgets[str(operation[1])] = int(operation[2])
        else:
            raise AssertionError(action)
        return None

    def active(self) -> list[dict[str, object]]:
        states = [chain[-1]["state"] for chain in self.items.values()
                  if chain[-1]["state"] is not None]
        return sorted(states, key=lambda state: (state["date"], int(state["id"][3:])),
                      reverse=True)

    def month(self, month: str) -> tuple[int, int, int, tuple[tuple[str, int], ...]]:
        rows = [state for state in self.active() if state["date"].startswith(month)]
        income = sum(row["amount"] for row in rows if row["type"] == "income")
        expense = sum(row["amount"] for row in rows if row["type"] == "expense")
        category_totals: dict[str, int] = {}
        for row in rows:
            if row["type"] == "expense":
                category_totals[row["category"]] = category_totals.get(row["category"], 0) + row["amount"]
        ranked = tuple(sorted(category_totals.items(), key=lambda pair: (-pair[1], pair[0])))
        return income, expense, len(rows), ranked


class LedgerCTS(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = tempfile.TemporaryDirectory(prefix="budget-cts-")
        self.addCleanup(self.sandbox.cleanup)
        self.folder = Path(self.sandbox.name)
        self.ledger = Ledger(self.folder / "data")

    def bytes(self) -> tuple[bytes, ...]:
        return tuple((self.ledger.data_dir / name).read_bytes() for name in FILES)

    def cli(self, *args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "budget_app", "--data-dir", str(self.ledger.data_dir), *args],
            cwd=HERE, input=input_text, text=True, capture_output=True, timeout=15,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_python_310_syntax_and_required_boundaries(self) -> None:
        parsed = {path.name: ast.parse(path.read_text(encoding="utf-8"),
                                       filename=str(path), feature_version=(3, 10))
                  for path in (HERE / "budget_app").glob("*.py")}
        self.assertGreaterEqual(len(parsed), 3)
        self.assertTrue(any(isinstance(node, ast.ClassDef) and node.name == "Ledger"
                            for node in parsed["core.py"].body))
        self.assertTrue(any(isinstance(node, ast.ClassDef) and node.name == "Transaction"
                            for node in parsed["model.py"].body))
        self.assertTrue(any(isinstance(node, ast.FunctionDef) and node.name == "main"
                            and any(isinstance(decorator, ast.Name) and decorator.id == "guarded"
                                    for decorator in node.decorator_list)
                            for node in parsed["cli.py"].body))

    def invoke(self, operation: tuple[object, ...]) -> object:
        action = operation[0]
        if action == "category_add":
            return self.ledger.add_category(operation[1])
        if action == "category_remove":
            return self.ledger.remove_category(operation[1])
        if action == "add":
            return self.ledger.add(*operation[1:])
        if action == "update":
            return self.ledger.update(operation[1], **operation[2])
        if action == "delete":
            return self.ledger.delete(operation[1])
        if action == "budget":
            return self.ledger.set_budget(operation[1], operation[2])
        raise AssertionError(action)

    def compare(self, reference: Oracle) -> None:
        for ledger in (self.ledger, Ledger(self.ledger.data_dir)):
            snapshot = ledger.snapshot()
            self.assertEqual(set(snapshot["categories"]), reference.categories)
            self.assertEqual(snapshot["budgets"], reference.budgets)
            self.assertEqual(set(snapshot["items"]), set(reference.items))
            for id_value, expected in reference.items.items():
                item = snapshot["items"][id_value]
                actual = [{"id": id_value, "version": revision.version,
                           "op": revision.op, "parent_hash": revision.parent_hash,
                           "state": plain(revision.state), "hash": revision.hash}
                          for revision in item.revisions]
                self.assertEqual(actual, expected)
                self.assertEqual(item.sort_date, next(
                    revision["state"]["date"] for revision in reversed(expected)
                    if revision["state"] is not None))
            self.assertEqual([plain(tx) for tx in ledger.iter_transactions()], reference.active())
            for month in ("2024-01", "2024-02", "2024-03"):
                actual_summary = ledger.summary(month)
                self.assertEqual((actual_summary.income, actual_summary.expense,
                                  actual_summary.count, actual_summary.category_expense),
                                 reference.month(month))
                self.assertEqual(actual_summary.budget, reference.budgets.get(month))

    def step(self, reference: Oracle, operation: tuple[object, ...]) -> None:
        global WALK_TRANSITIONS
        before = self.bytes()
        try:
            expected_id = reference.apply(operation)
            should_fail = False
        except ValueError:
            should_fail = True
            expected_id = None
        if should_fail:
            with self.assertRaises(LedgerError):
                self.invoke(operation)
            self.assertEqual(self.bytes(), before, "rejected operation changed storage bytes")
        else:
            result = self.invoke(operation)
            if expected_id is not None:
                self.assertEqual(result.id, expected_id)
            self.assertEqual(sum(old != new for old, new in zip(before, self.bytes())), 1,
                             "successful transition must replace exactly one data file")
        self.compare(reference)
        WALK_TRANSITIONS += 1
        label = f"{operation[0]}:{'rejected' if should_fail else 'accepted'}"
        WALK_DISTRIBUTION[label] = WALK_DISTRIBUTION.get(label, 0) + 1

    def test_deterministic_fsm_walks(self) -> None:
        # Each combination traverses absent -> active -> updated -> deleted.
        # Rejected transitions and category references are compared at every step.
        for day, kind, category, shift in itertools.product(
            ("2024-01-01", "2024-01-31", "2024-02-29"),
            ("income", "expense"), ("food", "rent"), ("2024-02-01", "2024-03-01")
        ):
            with self.subTest(day=day, kind=kind, category=category, shift=shift):
                self.ledger = Ledger(self.folder / f"walk-{day}-{kind}-{category}-{shift}")
                reference = Oracle()
                alternate = "rent" if category == "food" else "food"
                operations = [
                    ("category_add", "food"), ("category_add", "rent"),
                    ("add", day, kind, category, 7, "A", ("x",)),
                    ("add", day, "expense", alternate, 11, "B", ()),
                    ("update", "TX-000001", {"date": shift, "amount": 13}),
                    ("category_remove", alternate),  # used: reject
                    ("budget", "2024-02", 13),
                    ("delete", "TX-000002"),
                    ("update", "TX-000002", {"amount": 99}),  # deleted: reject
                    ("category_remove", alternate),
                ]
                for operation in operations:
                    self.step(reference, operation)

    def test_a_b_a_content_has_distinct_revision_identity(self) -> None:
        self.ledger.add_category("food")
        tx = self.ledger.add("2024-01-01", "expense", "food", 1)
        self.ledger.update(tx.id, amount=2)
        self.ledger.update(tx.id, amount=1)
        chain = self.ledger.snapshot()["items"][tx.id].revisions
        self.assertEqual([revision.state.amount for revision in chain], [1, 2, 1])
        self.assertEqual(len({revision.hash for revision in chain}), 3)
        self.assertEqual(chain[2].parent_hash, chain[1].hash)

    def test_initial_files_and_empty_category_policy(self) -> None:
        self.assertEqual({path.name for path in self.ledger.data_dir.iterdir()
                          if path.name in FILES}, set(FILES))
        self.assertEqual(self.bytes(), (b"", b"", b""))
        with self.assertRaises(LedgerError):
            self.ledger.add("2024-01-01", "expense", "food", 1)
        self.assertEqual(self.bytes(), (b"", b"", b""))

    def test_date_amount_kind_and_month_validation(self) -> None:
        self.ledger.add_category("food")
        original = self.bytes()
        for day in ("2023-02-29", "2024-02-30", "2024-13-01", "2024-1-01"):
            with self.subTest(day=day), self.assertRaises(LedgerError):
                self.ledger.add(day, "expense", "food", 1)
        for amount in (0, -1, True, 1.2):
            with self.subTest(amount=amount), self.assertRaises(LedgerError):
                self.ledger.add("2024-02-29", "expense", "food", amount)
        with self.assertRaises(LedgerError):
            self.ledger.add("2024-02-29", "other", "food", 1)
        with self.assertRaises(LedgerError):
            self.ledger.set_budget("2024-13", 10)
        self.assertEqual(self.bytes(), original)
        self.assertEqual(self.ledger.add("2024-02-29", "expense", "food", 1).id, "TX-000001")

    def test_rejected_update_preserves_bytes(self) -> None:
        self.ledger.add_category("food")
        tx = self.ledger.add("2024-01-01", "expense", "food", 1)
        before = self.bytes()
        for change in ({"date": "2024-02-30"}, {"amount": True},
                       {"category": "missing"}, {"type": "other"}):
            with self.subTest(change=change), self.assertRaises(LedgerError):
                self.ledger.update(tx.id, **change)
            self.assertEqual(self.bytes(), before)

    def test_same_day_order_backdated_and_month_move(self) -> None:
        self.ledger.add_category("food")
        first = self.ledger.add("2024-02-01", "expense", "food", 1)
        second = self.ledger.add("2024-02-01", "expense", "food", 2)
        third = self.ledger.add("2024-01-31", "expense", "food", 3)
        self.assertEqual([tx.id for tx in self.ledger.iter_transactions()],
                         [second.id, first.id, third.id])
        self.ledger.update(third.id, date="2024-02-02")
        self.assertEqual([tx.id for tx in self.ledger.iter_transactions()],
                         [third.id, second.id, first.id])
        self.assertEqual(self.ledger.summary("2024-01").count, 0)
        self.assertEqual(self.ledger.summary("2024-02").expense, 6)

    def test_search_and_budget_boundary(self) -> None:
        for name in ("food", "rent"):
            self.ledger.add_category(name)
        self.ledger.add("2024-01-01", "expense", "food", 1, "점심", ("meal",))
        self.ledger.add("2024-01-31", "expense", "food", 2, "점심", ("meal",))
        self.ledger.add("2024-02-01", "income", "rent", 20, "급여", ("pay",))
        rows = list(self.ledger.search(from_date="2024-01-01", to_date="2024-01-31",
                                       category="food", kind="expense", q="점심", tag="meal"))
        self.assertEqual([row.amount for row in rows], [2, 1])
        self.ledger.set_budget("2024-01", 3)
        self.assertNotIn("초과", self.cli("summary", "--month", "2024-01").stdout)
        self.ledger.set_budget("2024-01", 2)
        self.assertIn("[경고] 예산 초과", self.cli("summary", "--month", "2024-01").stdout)
        with self.assertRaises(LedgerError):
            list(self.ledger.search(from_date="2024-02-01", to_date="2024-01-01"))

    def test_summary_empty_top_and_budget_get_cli(self) -> None:
        self.assertIn("데이터 없음", self.cli("summary", "--month", "2024-03").stdout)
        self.assertIn("설정된 예산 없음", self.cli("budget", "get", "--month", "2024-03").stdout)
        self.ledger.add_category("food")
        self.ledger.add_category("rent")
        self.ledger.add("2024-03-01", "expense", "food", 8)
        self.ledger.add("2024-03-02", "expense", "rent", 5)
        self.ledger.set_budget("2024-03", 13)
        self.assertIn("13원", self.cli("budget", "get", "--month", "2024-03").stdout)
        report = self.cli("summary", "--month", "2024-03", "--top", "1").stdout
        self.assertIn("1) food 8원", report)
        self.assertNotIn("rent 5원", report)
        self.assertNotIn("초과", report)

    def test_category_guard_and_id_never_reused(self) -> None:
        self.ledger.add_category("food")
        first = self.ledger.add("2024-01-01", "expense", "food", 1)
        before = self.bytes()
        with self.assertRaises(LedgerError):
            self.ledger.remove_category("food")
        self.assertEqual(self.bytes(), before)
        self.assertEqual(self.cli("category", "remove", "--name", "food").returncode, 2)
        self.assertEqual(self.bytes(), before)
        self.ledger.delete(first.id)
        self.assertEqual(Ledger(self.ledger.data_dir).add("2024-01-01", "expense", "food", 2).id,
                         "TX-000002")
        for action in (lambda: self.ledger.delete(first.id),
                       lambda: self.ledger.update("TX-999999", amount=1)):
            with self.assertRaises(LedgerError):
                action()

    def test_csv_quoting_unicode_and_date_range(self) -> None:
        self.ledger.add_category("food")
        self.ledger.add("2024-01-01", "expense", "food", 1, "한글, 첫줄\n둘째줄", ("가", "나"))
        self.ledger.add("2024-01-31", "expense", "food", 2, "말일")
        self.ledger.add("2024-02-01", "expense", "food", 3, "다음달")
        target = self.folder / "export.csv"
        self.assertEqual(self.ledger.export_csv(target, from_date="2024-01-01",
                                                to_date="2024-01-31"), 2)
        with target.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["memo"], "한글, 첫줄\n둘째줄")
        self.assertEqual(rows[1]["tags"], "가,나")
        receiver = Ledger(self.folder / "receiver")
        receiver.add_category("food")
        self.assertEqual(receiver.import_csv(target), (2, 0))
        self.assertEqual([plain(tx) | {"id": ""} for tx in receiver.iter_transactions()],
                         [plain(tx) | {"id": ""} for tx in self.ledger.search(
                             from_date="2024-01-01", to_date="2024-01-31")])

    def test_import_invalid_last_row_is_all_or_nothing(self) -> None:
        self.ledger.add_category("food")
        source = self.folder / "incoming.csv"
        source.write_text("date,type,category,amount,memo,tags\n"
                          "2024-01-01,expense,food,10,valid,x\n"
                          "2024-02-30,expense,food,20,invalid,y\n", encoding="utf-8")
        before = self.bytes()
        with self.assertRaises(LedgerError):
            self.ledger.import_csv(source)
        self.assertEqual(self.bytes(), before)
        self.assertEqual(list(Ledger(self.ledger.data_dir).iter_transactions()), [])
        result = self.cli("import", "--from", str(source))
        self.assertEqual(result.returncode, 2)
        self.assertIn("CSV 3행", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.bytes(), before)
        source.write_text("date,type,category,amount,memo,tags\n"
                          "2024-01-01,expense,food,10,valid,x\n", encoding="utf-8")
        self.assertIn("imported=1, skipped=0", self.cli("import", "--from", str(source)).stdout)
        self.assertEqual(next(self.ledger.iter_transactions()).id, "TX-000001")

    def test_import_duplicate_policy_and_missing_category(self) -> None:
        self.ledger.add_category("food")
        source = self.folder / "incoming.csv"
        source.write_text("date,type,category,amount,memo,tags\n"
                          "2024-01-01,expense,food,10,same,x\n"
                          "2024-01-01,expense,food,10,same,x\n", encoding="utf-8")
        self.assertEqual(self.ledger.import_csv(source), (1, 1))
        self.assertEqual(self.ledger.import_csv(source), (0, 2))
        self.assertEqual(self.ledger.add("2024-01-02", "expense", "food", 1).id,
                         "TX-000002")
        source.write_text("date,type,category,amount\n"
                          "2024-01-03,expense,absent,3\n", encoding="utf-8")
        before = self.bytes()
        with self.assertRaises(LedgerError):
            self.ledger.import_csv(source)
        self.assertEqual(self.bytes(), before)
        self.ledger.add_category("absent")
        self.assertEqual(self.ledger.import_csv(source), (1, 0))

    def test_corrupt_storage_is_reported(self) -> None:
        self.ledger.add_category("food")
        self.ledger.add("2024-01-02", "expense", "food", 1)
        self.ledger.add("2024-01-01", "expense", "food", 2)
        path = self.ledger.transactions_path
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines()
        changed_hash = json.loads(lines[0])
        changed_hash["revisions"][0]["hash"] = "0" * 64
        variants = ("{bad json}\n", '{"bad":1}\n', original + lines[0] + "\n",
                    json.dumps(changed_hash) + "\n" + lines[1] + "\n",
                    lines[1] + "\n" + lines[0] + "\n")
        for variant in variants:
            with self.subTest(variant=variant[:30]):
                path.write_text(variant, encoding="utf-8")
                with self.assertRaises(LedgerError):
                    list(self.ledger.iter_transactions())
        path.write_text(original, encoding="utf-8")
        self.assertEqual(len(list(self.ledger.iter_transactions())), 2)

    def test_category_and_budget_file_corruption(self) -> None:
        category = self.ledger.categories_path
        budget = self.ledger.budgets_path
        category.write_text('{"name":"food"}\n{"name":"food"}\n', encoding="utf-8")
        with self.assertRaises(LedgerError):
            self.ledger.categories()
        category.write_text('', encoding="utf-8")
        budget.write_text('{"month":"2024-01","amount":true}\n', encoding="utf-8")
        with self.assertRaises(LedgerError):
            self.ledger.get_budget("2024-01")

    def test_concurrent_adds_have_unique_monotone_ids(self) -> None:
        self.ledger.add_category("food")

        def add_one(number: int) -> subprocess.CompletedProcess[str]:
            return self.cli("add", input_text=f"2024-01-01\nexpense\nfood\n{number}\n\n\n")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(add_one, range(1, 7)))
        self.assertTrue(all(result.returncode == 0 for result in results), results)
        ids = [tx.id for tx in Ledger(self.ledger.data_dir).iter_transactions()]
        self.assertEqual(ids, [f"TX-{number:06d}" for number in range(6, 0, -1)])

    def test_generator_laziness_and_early_termination(self) -> None:
        self.ledger.add_category("food")
        self.ledger.add("2024-01-02", "expense", "food", 1)
        self.ledger.add("2024-01-01", "expense", "food", 2)
        path = self.ledger.transactions_path
        path.write_bytes(path.read_bytes() + b"{bad json}\n")
        iterator = self.ledger.iter_transactions()
        self.assertEqual(next(iterator).amount, 1)
        self.assertEqual(next(iterator).amount, 2)
        with self.assertRaises(LedgerError):
            next(iterator)
        self.assertEqual(self.cli("list", "--limit", "1").returncode, 0)
        self.assertEqual(self.cli("list", "--limit", "3").returncode, 2)

    def test_streaming_memory_bound(self) -> None:
        global MEMORY_PEAKS
        # The fixture uses the independent oracle's wire-level revision recipe.
        # Consumption discards each yielded transaction immediately.
        path = self.ledger.transactions_path

        def write_fixture(count: int) -> None:
            with path.open("w", encoding="utf-8") as stream:
                for number in range(count, 0, -1):
                    id_value = f"TX-{number:06d}"
                    state = {"id": id_value, "date": "2024-01-01",
                             "type": "expense", "category": "food", "amount": number,
                             "memo": "m" * 128, "tags": []}
                    revision = Oracle.define(id_value, "create", state, None)
                    revision.pop("id")
                    record = {"id": id_value, "sort_date": state["date"],
                              "revisions": [revision]}
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        peaks: list[int] = []
        for size in (500, 10000):
            write_fixture(size)
            gc.collect()
            tracemalloc.start()
            seen = sum(1 for _ in self.ledger.iter_transactions())
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(seen, size)
            peaks.append(peak)
        self.assertLess(peaks[1] - peaks[0], 700_000, peaks)
        MEMORY_PEAKS = peaks[0], peaks[1]

    def test_atomic_write_failures_before_replace(self) -> None:
        self.ledger.add_category("food")
        before = self.bytes()
        with mock.patch("budget_app.core.os.fsync", side_effect=OSError("file fsync fault")):
            with self.assertRaises(OSError):
                self.ledger.add_category("rent")
        self.assertEqual(self.bytes(), before)
        with mock.patch("budget_app.core.os.replace", side_effect=OSError("replace fault")):
            with self.assertRaises(OSError):
                self.ledger.add_category("rent")
        self.assertEqual(self.bytes(), before)
        self.assertEqual(list(self.ledger.data_dir.glob(".categories.jsonl.*")), [])
        self.assertEqual(Ledger(self.ledger.data_dir).categories(), ("food",))

    def test_transaction_replace_failure_preserves_reopen(self) -> None:
        self.ledger.add_category("food")
        transaction = self.ledger.add("2024-01-01", "expense", "food", 1)
        before = self.bytes()
        with mock.patch("budget_app.core.os.replace", side_effect=OSError("replace fault")):
            with self.assertRaises(OSError):
                self.ledger.update(transaction.id, amount=2)
        self.assertEqual(self.bytes(), before)
        reopened = Ledger(self.ledger.data_dir)
        self.assertEqual(next(reopened.iter_transactions()).amount, 1)

    def test_writer_failure_after_partial_temp_write(self) -> None:
        self.ledger.add_category("food")
        before = self.bytes()
        original_line = Ledger._line
        calls = 0

        def fail_second(stream: object, value: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("partial write fault")
            original_line(stream, value)

        with mock.patch.object(Ledger, "_line", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.ledger.add_category("rent")
        self.assertEqual(self.bytes(), before)
        self.assertEqual(list(self.ledger.data_dir.glob(".categories.jsonl.*")), [])

    def test_directory_fsync_failure_reports_committed_state(self) -> None:
        original_fsync = os.fsync

        def fail_directory(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory fsync fault")
            original_fsync(fd)

        with mock.patch("budget_app.core.os.fsync", side_effect=fail_directory):
            with self.assertRaisesRegex(LedgerError, "교체는 완료"):
                self.ledger.add_category("food")
        self.assertEqual(Ledger(self.ledger.data_dir).categories(), ("food",))

    def test_cli_commands_help_and_errors(self) -> None:
        paths = (("add",), ("list",), ("search",), ("summary",), ("budget",),
                 ("budget", "set"), ("budget", "get"), ("category",),
                 ("category", "add"), ("category", "list"),
                 ("category", "remove"), ("update",), ("delete",),
                 ("import",), ("export",))
        for path in paths:
            result = self.cli(*path, "--help")
            self.assertEqual(result.returncode, 0, path)
            self.assertIn("usage:", result.stdout)
        update_help = self.cli("update", "--help").stdout
        for option in ("--id", "--date", "--type", "--category", "--amount",
                       "--memo", "--tags"):
            self.assertIn(option, update_help)
        self.assertEqual(self.cli("category", "add", "--name", "food").returncode, 0)
        alternate = self.folder / "custom-data"
        result = subprocess.run([sys.executable, "-m", "budget_app", "--data-dir",
                                 str(alternate), "category", "add", "--name", "rent"],
                                cwd=HERE, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(Ledger(alternate).categories(), ("rent",))
        input_text = "2024-01-01\nexpense\nfood\n2\n점심\nmeal\n"
        self.assertIn("TX-000001", self.cli("add", input_text=input_text).stdout)
        self.assertEqual(self.cli("update", "--id", "TX-000001", "--amount", "3").returncode, 0)
        self.assertIn("3원", self.cli("summary", "--month", "2024-01").stdout)
        self.assertIn("TX-000001", self.cli("search", "--tag", "meal").stdout)
        exported = self.folder / "cli.csv"
        self.assertIn("(1 records)", self.cli("export", "--out", str(exported),
                                              "--month", "2024-01").stdout)
        self.assertIn("skipped=1", self.cli("import", "--from", str(exported)).stdout)
        self.assertEqual(self.cli("update", "--id", "TX-000001", "--memo", "",
                                  "--tags", "").returncode, 0)
        self.assertNotIn("TX-000001", self.cli("search", "--tag", "meal").stdout)
        self.assertEqual(self.cli("delete", "--id", "TX-000001").returncode, 0)
        for result in (self.cli("delete", "--id", "TX-000001"),
                       self.cli("summary", "--month", "2024-13"),
                       self.cli("list", "--limit", "0"),
                       self.cli("budget", "set", "--month", "2024-01",
                                "--amount", "9" * 5000),
                       self.cli("export", "--out", str(self.folder / "bad.csv"))):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("[오류]", result.stderr)
            self.assertIn("[힌트]", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_export_requires_period_and_protects_store(self) -> None:
        before = self.bytes()
        with self.assertRaises(LedgerError):
            self.ledger.export_csv(self.folder / "x.csv")
        with self.assertRaises(LedgerError):
            self.ledger.export_csv(self.ledger.transactions_path, month="2024-01")
        with self.assertRaises(LedgerError):
            self.ledger.export_csv(self.folder / "x.csv", from_date="2024-01-02",
                                   to_date="2024-01-01")
        self.assertEqual(self.bytes(), before)


if __name__ == "__main__":
    outcome = unittest.main(verbosity=2, exit=False)
    print(f"CTS deterministic transitions: {WALK_TRANSITIONS}")
    print(f"CTS transition distribution: {dict(sorted(WALK_DISTRIBUTION.items()))}")
    print(f"CTS streaming peak bytes (500, 10000): {MEMORY_PEAKS}")
    raise SystemExit(0 if outcome.result.wasSuccessful() else 1)
