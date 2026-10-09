
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, TextIO

from .model import Item, LedgerError, Revision, Summary, Transaction

"""
@core:
   validation
   revision transitions
   file persistence.

The three stores have different record shapes, but all mutations use the same
exclusive lock and same-file atomic replacement primitive. Transactions are a
date-sorted materialized view of immutable per-item revision chains.

"""

CSV_COLUMNS = ("date", "type", "category", "amount", "memo", "tags")
REQUIRED_CSV = frozenset(CSV_COLUMNS[:4])
ID_RE = re.compile(r"TX-(\d{6,})\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
MONTH_RE = re.compile(r"\d{4}-\d{2}\Z")
HEX_RE = re.compile(r"[0-9a-f]{64}\Z")

#Error propagation helper
def valid_date(value: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise LedgerError(f"날짜 형식 오류: {value!r}", "YYYY-MM-DD 형식으로 입력해 줘.")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError(f"존재하지 않는 날짜: {value}", "달력에 있는 날짜를 입력해 줘.") from exc
    return value


def valid_month(value: str) -> str:
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise LedgerError(f"월 형식 오류: {value!r}", "YYYY-MM 형식으로 입력해 줘.")
    try:
        date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise LedgerError(f"존재하지 않는 월: {value}", "01부터 12까지의 월을 입력해 줘.") from exc
    return value


def valid_amount(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LedgerError(f"금액 오류: {value!r}", "0보다 큰 정수를 입력해 줘.")
    return value


def parse_amount(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value.strip()):
        raise LedgerError(f"금액 오류: {value!r}", "0보다 큰 정수를 입력해 줘.")
    try:
        number = int(value.strip())
    except ValueError as exc:
        raise LedgerError("금액 자릿수 오류", "짧은 양수 정수로 입력해 줘.") from exc
    return valid_amount(number)


def valid_kind(value: str) -> str:
    if value not in ("income", "expense"):
        raise LedgerError(f"거래 타입 오류: {value!r}", "income 또는 expense를 입력해 줘.")
    return value


def valid_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
        raise LedgerError(f"카테고리 이름 오류: {value!r}", "비어 있지 않은 한 줄의 이름을 입력해 줘.")
    return value.strip()


def parse_tags(value: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise LedgerError("태그 형식 오류", "쉼표로 구분한 문자열을 입력해 줘.")
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _payload(date_value: str, kind: str, category: str, amount: int,
             memo: str, tags: tuple[str, ...]) -> dict[str, object]:
    valid_date(date_value)
    valid_kind(kind)
    valid_name(category)
    valid_amount(amount)
    if not isinstance(memo, str) or not isinstance(tags, tuple) or any(
        not isinstance(tag, str) or not tag or tag != tag.strip() for tag in tags
    ):
        raise LedgerError("메모 또는 태그 형식 오류", "문자열 메모와 쉼표로 구분한 태그를 사용해 줘.")
    return {"date": date_value, "type": kind, "category": category,
            "amount": amount, "memo": memo, "tags": tags}


def _tx_dict(tx: Transaction) -> dict[str, object]:
    result = asdict(tx)
    result["tags"] = list(tx.tags)
    return result


def _digest(id_value: str, version: int, op: str, parent: str,
            state: Transaction | None) -> str:
    body = {"id": id_value, "version": version, "op": op,
            "parent_hash": parent, "state": _tx_dict(state) if state else None}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _revision(id_value: str, op: str, state: Transaction | None,
              prior: Revision | None = None) -> Revision:
    version = prior.version + 1 if prior else 1
    parent = prior.hash if prior else ""
    return Revision(version, op, parent, state,
                    _digest(id_value, version, op, parent, state))


def _item_dict(item: Item) -> dict[str, object]:
    return {"id": item.id, "sort_date": item.sort_date, "revisions": [
        {"version": rev.version, "op": rev.op, "parent_hash": rev.parent_hash,
         "state": _tx_dict(rev.state) if rev.state else None, "hash": rev.hash}
        for rev in item.revisions]}


def _corrupt(path: Path, line: int, reason: str) -> LedgerError:
    return LedgerError(f"저장 파일 손상: {path.name}:{line} ({reason})",
                       "파일을 점검하거나 백업에서 복원해 줘.")


def _parse_item(raw: object, path: Path, line: int) -> Item:
    try:
        if not isinstance(raw, dict) or set(raw) != {"id", "sort_date", "revisions"}:
            raise ValueError("item schema")

        id_value = raw["id"]
        if not isinstance(id_value, str) or not ID_RE.fullmatch(id_value) or int(id_value[3:]) < 1:
            raise ValueError("id")

        versions = raw["revisions"]
        if not isinstance(versions, list) or not versions:
            raise ValueError("empty revisions")

        parsed: list[Revision] = []
        last_date: str | None = None
        for data in versions:
            if not isinstance(data, dict) or set(data) != {
                "version", "op", "parent_hash", "state", "hash"
            }:
                raise ValueError("revision schema")
            version, op = data["version"], data["op"]
            parent, stored_hash = data["parent_hash"], data["hash"]

            if type(version) is not int or version != len(parsed) + 1:
                raise ValueError("revision version")
            if not isinstance(parent, str) or not isinstance(stored_hash, str):
                raise ValueError("hash type")
            expected_parent = parsed[-1].hash if parsed else ""

            if parent != expected_parent or (parent and not HEX_RE.fullmatch(parent)):
                raise ValueError("revision predecessor")
            if not HEX_RE.fullmatch(stored_hash):
                raise ValueError("hash shape")

            state_raw = data["state"]
            state: Transaction | None

            if state_raw is None:
                state = None

            else:
                if not isinstance(state_raw, dict) or set(state_raw) != {
                    "id", "date", "type", "category", "amount", "memo", "tags"
                }:
                    raise ValueError("transaction schema")
                if state_raw["id"] != id_value or not isinstance(state_raw["tags"], list):
                    raise ValueError("transaction id/tags")
                tags = tuple(state_raw["tags"])
                fields = _payload(state_raw["date"], state_raw["type"],
                                  state_raw["category"], state_raw["amount"],
                                  state_raw["memo"], tags)
                state = Transaction(id_value, **fields)  # type: ignore[arg-type]
                last_date = state.date
            if not parsed and (op != "create" or state is None):
                raise ValueError("first transition")
            if parsed and (parsed[-1].state is None or op not in ("update", "delete")
                           or (op == "delete") != (state is None)):
                raise ValueError("invalid transition")
            if _digest(id_value, version, op, parent, state) != stored_hash:
                raise ValueError("revision hash")
            parsed.append(Revision(version, op, parent, state, stored_hash))
        if raw["sort_date"] != last_date or last_date is None:
            raise ValueError("sort date")
        return Item(id_value, last_date, tuple(parsed))
    except (LedgerError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise _corrupt(path, line, str(exc)) from exc


def _key(item: Item) -> tuple[str, int]:
    return item.sort_date, int(item.id[3:])


def _fingerprint(tx: Transaction) -> tuple[object, ...]:
    return (tx.date, tx.type, tx.category, tx.amount, tx.memo, tx.tags)


class Ledger:
    """Central state machine and persistence boundary; one data file per command."""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self.transactions_path = self.data_dir / "transactions.jsonl"
        self.categories_path = self.data_dir / "categories.jsonl"
        self.budgets_path = self.data_dir / "budgets.jsonl"
        self.lock_path = self.data_dir / ".lock"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._locked():
            for path in (self.transactions_path, self.categories_path, self.budgets_path):
                if not path.exists():
                    self._atomic_write(path, lambda stream: None)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, writer: Callable[[TextIO], None]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        committed = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            committed = True
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if committed:
                raise LedgerError(f"{path.name} 교체는 완료됐지만 디렉터리 동기화 실패: {exc}",
                                  "저장 결과를 다시 조회하고 디스크 상태를 확인해 줘.") from exc
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _iter_items(self) -> Iterator[Item]:
        previous: tuple[str, int] | None = None
        # Date order and stable IDs are separate axes. A temporary on-disk key
        # set catches duplicate IDs without growing Python memory with the file.
        with tempfile.TemporaryDirectory(prefix="budget-seen-") as scratch:
            connection = sqlite3.connect(str(Path(scratch) / "ids.sqlite3"))
            try:
                connection.execute("PRAGMA journal_mode=OFF")
                connection.execute("PRAGMA synchronous=OFF")
                connection.execute("PRAGMA cache_size=-64")
                connection.execute("CREATE TABLE seen (id TEXT PRIMARY KEY) WITHOUT ROWID")
                with self.transactions_path.open("r", encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, 1):
                        try:
                            item = _parse_item(json.loads(line), self.transactions_path, line_number)
                        except json.JSONDecodeError as exc:
                            raise _corrupt(self.transactions_path, line_number, "JSON syntax") from exc
                        order = _key(item)
                        try:
                            connection.execute("INSERT INTO seen (id) VALUES (?)", (item.id,))
                        except sqlite3.IntegrityError as exc:
                            raise _corrupt(self.transactions_path, line_number, "duplicate id") from exc
                        if previous is not None and order > previous:
                            raise _corrupt(self.transactions_path, line_number, "sort order")
                        previous = order
                        yield item
            except sqlite3.Error as exc:
                raise LedgerError(f"임시 ID 검증 실패: {exc}",
                                  "임시 저장 공간을 확인해 줘.") from exc
            finally:
                connection.close()

    def iter_transactions(self) -> Iterator[Transaction]:
        for item in self._iter_items():
            if item.head is not None:
                yield item.head

    def search(self, *, from_date: str | None = None, to_date: str | None = None,
               category: str | None = None, kind: str | None = None,
               q: str | None = None, tag: str | None = None) -> Iterator[Transaction]:
        if from_date is not None:
            valid_date(from_date)
        if to_date is not None:
            valid_date(to_date)
        if from_date and to_date and from_date > to_date:
            raise LedgerError("검색 기간 역전", "--from 날짜를 --to 이하로 지정해 줘.")
        if kind is not None:
            valid_kind(kind)
        if category is not None:
            valid_name(category)

        def walk() -> Iterator[Transaction]:
            for tx in self.iter_transactions():
                if to_date and tx.date > to_date:
                    continue
                if from_date and tx.date < from_date:
                    break
                if category and tx.category != category:
                    continue
                if kind and tx.type != kind:
                    continue
                if q and q.casefold() not in tx.memo.casefold():
                    continue
                if tag and tag not in tx.tags:
                    continue
                yield tx

        return walk()

    def _categories(self) -> list[str]:
        names: list[str] = []
        with self.categories_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or set(raw) != {"name"}:
                        raise ValueError("schema")
                    name = valid_name(raw["name"])
                    if raw["name"] != name or (names and name <= names[-1]):
                        raise ValueError("duplicate/order")
                except (json.JSONDecodeError, ValueError, LedgerError) as exc:
                    raise _corrupt(self.categories_path, line_number, str(exc)) from exc
                names.append(name)
        return names

    def categories(self) -> tuple[str, ...]:
        return tuple(self._categories())

    def _budgets(self) -> dict[str, int]:
        result: dict[str, int] = {}
        with self.budgets_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or set(raw) != {"month", "amount"}:
                        raise ValueError("schema")
                    month = valid_month(raw["month"])
                    amount = valid_amount(raw["amount"])
                    if month in result or (result and month <= next(reversed(result))):
                        raise ValueError("duplicate/order")
                except (json.JSONDecodeError, ValueError, LedgerError) as exc:
                    raise _corrupt(self.budgets_path, line_number, str(exc)) from exc
                result[month] = amount
        return result

    def get_budget(self, month: str) -> int | None:
        return self._budgets().get(valid_month(month))

    def set_budget(self, month: str, amount: int) -> None:
        valid_month(month)
        valid_amount(amount)
        with self._locked():
            budgets = self._budgets()
            budgets[month] = amount

            def write(stream: TextIO) -> None:
                for key in sorted(budgets):
                    self._line(stream, {"month": key, "amount": budgets[key]})

            self._atomic_write(self.budgets_path, write)

    @staticmethod
    def _line(stream: TextIO, value: dict[str, object]) -> None:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")) + "\n")

    def add_category(self, name: str) -> None:
        name = valid_name(name)
        with self._locked():
            names = self._categories()
            if name in names:
                raise LedgerError(f"이미 있는 카테고리: {name}", "다른 이름을 입력해 줘.")
            names.append(name)

            def write(stream: TextIO) -> None:
                for item in sorted(names):
                    self._line(stream, {"name": item})

            self._atomic_write(self.categories_path, write)

    def remove_category(self, name: str) -> None:
        name = valid_name(name)
        with self._locked():
            names = self._categories()
            if name not in names:
                raise LedgerError(f"없는 카테고리: {name}", "category list로 목록을 확인해 줘.")
            with closing(self.iter_transactions()) as transactions:
                if any(tx.category == name for tx in transactions):
                    raise LedgerError(f"사용 중인 카테고리: {name}",
                                      "해당 거래의 카테고리를 먼저 수정하거나 삭제해 줘.")
            names.remove(name)

            def write(stream: TextIO) -> None:
                for item in names:
                    self._line(stream, {"name": item})

            self._atomic_write(self.categories_path, write)

    def _require_category(self, name: str) -> None:
        if name not in self._categories():
            raise LedgerError(f"등록되지 않은 카테고리: {name}",
                              "category add로 등록한 뒤 다시 시도해 줘.")

    def _find_item(self, id_value: str) -> Item:
        if not ID_RE.fullmatch(id_value):
            raise LedgerError(f"거래 ID 형식 오류: {id_value}", "TX-000001 형식을 사용해 줘.")
        found: Item | None = None
        for item in self._iter_items():
            if item.id == id_value:
                found = item
        if found is None or found.head is None:
            raise LedgerError(f"없는 거래: {id_value}", "list 또는 search로 ID를 확인해 줘.")
        return found

    def _replace_item(self, fresh: Item, old_id: str | None = None) -> None:
        def write(stream: TextIO) -> None:
            inserted = False
            found = False
            for item in self._iter_items():
                if item.id == fresh.id and item.id != old_id:
                    raise LedgerError(f"중복 ID: {fresh.id}", "저장 파일을 점검해 줘.")
                if not inserted and _key(fresh) > _key(item):
                    self._line(stream, _item_dict(fresh))
                    inserted = True
                if item.id == old_id:
                    found = True
                    continue
                self._line(stream, _item_dict(item))
            if old_id is not None and not found:
                raise LedgerError(f"없는 거래: {old_id}", "list로 ID를 확인해 줘.")
            if not inserted:
                self._line(stream, _item_dict(fresh))

        self._atomic_write(self.transactions_path, write)

    def add(self, date_value: str, kind: str, category: str, amount: int,
            memo: str = "", tags: tuple[str, ...] = ()) -> Transaction:
        fields = _payload(date_value, kind, category, amount, memo, tags)
        with self._locked():
            self._require_category(category)
            maximum = max((int(item.id[3:]) for item in self._iter_items()), default=0)
            id_value = f"TX-{maximum + 1:06d}"
            tx = Transaction(id_value, **fields)  # type: ignore[arg-type]
            item = Item(id_value, tx.date, (_revision(id_value, "create", tx),))
            self._replace_item(item)
            return tx

    def update(self, id_value: str, **changes: object) -> Transaction:
        allowed = {"date", "type", "category", "amount", "memo", "tags"}
        if not changes or set(changes) - allowed:
            raise LedgerError("수정 필드 오류", "--date, --type, --category, --amount, --memo, --tags 중 하나를 지정해 줘.")
        with self._locked():
            old = self._find_item(id_value)
            assert old.head is not None
            tx = replace(old.head, **changes)
            _payload(tx.date, tx.type, tx.category, tx.amount, tx.memo, tx.tags)
            self._require_category(tx.category)
            new_revision = _revision(id_value, "update", tx, old.revisions[-1])
            self._replace_item(Item(id_value, tx.date, old.revisions + (new_revision,)), id_value)
            return tx

    def delete(self, id_value: str) -> None:
        with self._locked():
            old = self._find_item(id_value)
            new_revision = _revision(id_value, "delete", None, old.revisions[-1])
            self._replace_item(Item(id_value, old.sort_date,
                                    old.revisions + (new_revision,)), id_value)

    def summary(self, month: str) -> Summary:
        valid_month(month)
        income = expense = count = 0
        categories: dict[str, int] = defaultdict(int)
        with closing(self.iter_transactions()) as transactions:
            for tx in transactions:
                if tx.date[:7] > month:
                    continue
                if tx.date[:7] < month:
                    break
                count += 1
                if tx.type == "income":
                    income += tx.amount
                else:
                    expense += tx.amount
                    categories[tx.category] += tx.amount
        ranked = tuple(sorted(categories.items(), key=lambda part: (-part[1], part[0])))
        return Summary(income, expense, ranked, count, self.get_budget(month))

    def import_csv(self, path: str | Path) -> tuple[int, int]:
        source = Path(path)
        with self._locked():
            categories = set(self._categories())
            incoming: list[dict[str, object]] = []
            with source.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, strict=True)
                header = reader.fieldnames
                if (header is None or len(header) != len(set(header))
                        or not REQUIRED_CSV.issubset(header)
                        or set(header) - set(CSV_COLUMNS)):
                    raise LedgerError("CSV 헤더 오류", "date,type,category,amount 필수 열과 memo,tags 선택 열을 확인해 줘.")
                for number, row in enumerate(reader, 2):
                    try:
                        if None in row or any(row[key] is None for key in REQUIRED_CSV):
                            raise LedgerError("열 수 오류", "CSV 행의 열 수를 헤더와 맞춰 줘.")
                        category = valid_name(row["category"])
                        if category not in categories:
                            raise LedgerError(f"등록되지 않은 카테고리: {category}",
                                              "category add로 먼저 등록해 줘.")
                        incoming.append(_payload(row["date"], row["type"], category,
                                                 parse_amount(row["amount"]),
                                                 row.get("memo") or "",
                                                 parse_tags(row.get("tags") or "")))
                    except LedgerError as exc:
                        raise LedgerError(f"CSV {number}행: {exc}", exc.hint) from exc

            existing: set[tuple[object, ...]] = set()
            maximum = 0
            for item in self._iter_items():
                maximum = max(maximum, int(item.id[3:]))
                if item.head is not None:
                    existing.add(_fingerprint(item.head))
            fresh: list[Item] = []
            skipped = 0
            for fields in incoming:
                candidate = Transaction("", **fields)  # type: ignore[arg-type]
                fingerprint = _fingerprint(candidate)
                if fingerprint in existing:
                    skipped += 1
                    continue
                existing.add(fingerprint)
                maximum += 1
                id_value = f"TX-{maximum:06d}"
                tx = replace(candidate, id=id_value)
                fresh.append(Item(id_value, tx.date, (_revision(id_value, "create", tx),)))
            if not fresh:
                return 0, skipped
            fresh.sort(key=_key, reverse=True)

            def write(stream: TextIO) -> None:
                pending = iter(fresh)
                next_new = next(pending, None)
                for old in self._iter_items():
                    while next_new is not None and _key(next_new) > _key(old):
                        self._line(stream, _item_dict(next_new))
                        next_new = next(pending, None)
                    self._line(stream, _item_dict(old))
                while next_new is not None:
                    self._line(stream, _item_dict(next_new))
                    next_new = next(pending, None)

            self._atomic_write(self.transactions_path, write)
            return len(fresh), skipped

    def export_csv(self, path: str | Path, *, month: str | None = None,
                   from_date: str | None = None, to_date: str | None = None) -> int:
        if month is not None:
            valid_month(month)
        if not month and (not from_date or not to_date):
            raise LedgerError("내보내기 기간 누락", "--month 또는 --from과 --to를 함께 지정해 줘.")
        if month and (from_date or to_date):
            raise LedgerError("내보내기 기간 중복", "--month와 날짜 범위 중 하나를 선택해 줘.")
        if from_date:
            valid_date(from_date)
        if to_date:
            valid_date(to_date)
        if from_date and to_date and from_date > to_date:
            raise LedgerError("내보내기 기간 역전", "--from 날짜를 --to 이하로 지정해 줘.")
        target = Path(path)
        if target.resolve() in {item.resolve() for item in
                                (self.transactions_path, self.categories_path,
                                 self.budgets_path, self.lock_path)}:
            raise LedgerError("저장 파일을 덮어쓰는 출력 경로", "다른 CSV 파일명을 지정해 줘.")
        count = 0

        def write(stream: TextIO) -> None:
            nonlocal count
            writer = csv.writer(stream)
            writer.writerow(CSV_COLUMNS)
            for tx in self.search(from_date=from_date, to_date=to_date):
                if month and tx.date[:7] != month:
                    continue
                writer.writerow((tx.date, tx.type, tx.category, tx.amount,
                                 tx.memo, ",".join(tx.tags)))
                count += 1

        self._atomic_write(target, write)
        return count

    def snapshot(self) -> dict[str, object]:
        """Full diagnostic snapshot for CTS; normal list/search use generators."""
        items = list(self._iter_items())
        return {"items": {item.id: item for item in items},
                "categories": self.categories(), "budgets": self._budgets()}
