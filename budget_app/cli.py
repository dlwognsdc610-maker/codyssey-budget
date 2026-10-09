"""Argument and terminal interaction only; state transitions live in core."""

from __future__ import annotations

import argparse
import csv
import functools
import itertools
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Callable

from .core import Ledger, parse_amount, parse_tags, valid_amount
from .model import LedgerError, Transaction


class HelpParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, f"[오류] 인자 오류: {message}\n[힌트] --help로 사용법을 확인해 줘.\n")


def guarded(fn: Callable[[], int]) -> Callable[[], int]:
    """One applied decorator for consistent errors and nonzero exit codes."""

    @functools.wraps(fn)
    def wrapper() -> int:
        try:
            return fn()
        except LedgerError as exc:
            print(f"[오류] {exc}", file=sys.stderr)
            print(f"[힌트] {exc.hint}", file=sys.stderr)
            return 2
        except (OSError, UnicodeError, csv.Error, json.JSONDecodeError,
                sqlite3.Error) as exc:
            print(f"[오류] 파일 처리 실패: {exc}", file=sys.stderr)
            print("[힌트] 경로, 권한, 인코딩과 저장 공간을 확인해 줘.", file=sys.stderr)
            return 2
        except EOFError:
            print("[오류] 입력이 끝났어.", file=sys.stderr)
            print("[힌트] 필요한 값을 모두 입력해 줘.", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n[오류] 입력이 중단됐어.", file=sys.stderr)
            print("[힌트] 명령을 다시 실행해 줘.", file=sys.stderr)
            return 130

    return wrapper


def _parser() -> HelpParser:
    parser = HelpParser(prog="python -m budget_app", description="JSONL 용돈 기입장")
    parser.add_argument("--data-dir", default="data", metavar="DIR", help="저장 폴더 (기본 ./data)")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=HelpParser)

    commands.add_parser("add", help="대화형 거래 추가")
    listing = commands.add_parser("list", help="거래일·생성 ID 최신순 목록")
    listing.add_argument("--limit", type=int, default=20)

    search = commands.add_parser("search", help="AND 조건 검색")
    search.add_argument("--from", dest="from_date")
    search.add_argument("--to", dest="to_date")
    search.add_argument("--category")
    search.add_argument("--type", dest="kind")
    search.add_argument("--q")
    search.add_argument("--tag")

    summary = commands.add_parser("summary", help="월별 요약")
    summary.add_argument("--month", required=True)
    summary.add_argument("--top", type=int, default=3)

    budget = commands.add_parser("budget", help="월 예산 설정·조회")
    budget_ops = budget.add_subparsers(dest="operation", required=True, parser_class=HelpParser)
    set_budget = budget_ops.add_parser("set", help="예산 설정")
    set_budget.add_argument("--month", required=True)
    set_budget.add_argument("--amount", required=True)
    get_budget = budget_ops.add_parser("get", help="예산 조회")
    get_budget.add_argument("--month", required=True)

    category = commands.add_parser("category", help="카테고리 관리")
    category_ops = category.add_subparsers(dest="operation", required=True, parser_class=HelpParser)
    category_ops.add_parser("add", help="카테고리 추가").add_argument("--name")
    category_ops.add_parser("list", help="카테고리 목록")
    category_ops.add_parser("remove", help="카테고리 삭제").add_argument("--name")

    update = commands.add_parser("update", help="옵션 기반 거래 수정")
    update.add_argument("--id", required=True)
    update.add_argument("--date")
    update.add_argument("--type", dest="kind")
    update.add_argument("--category")
    update.add_argument("--amount")
    update.add_argument("--memo")
    update.add_argument("--tags")

    delete = commands.add_parser("delete", help="거래 삭제")
    delete.add_argument("--id", required=True)

    importing = commands.add_parser("import", help="CSV 일괄 가져오기")
    importing.add_argument("--from", dest="source", required=True)

    exporting = commands.add_parser("export", help="CSV 내보내기")
    exporting.add_argument("--out", required=True)
    exporting.add_argument("--month")
    exporting.add_argument("--from", dest="from_date")
    exporting.add_argument("--to", dest="to_date")
    return parser


def _show(tx: Transaction) -> None:
    tags = ",".join(tx.tags)
    print(f"{tx.id} | {tx.date} | {tx.type} | {tx.category} | "
          f"{tx.amount} | {tx.memo} | {tags}")


@guarded
def main() -> int:
    args = _parser().parse_args()
    ledger = Ledger(Path(args.data_dir))
    if args.command == "add":
        tx = ledger.add(input("날짜(YYYY-MM-DD): ").strip(),
                        input("타입(income/expense): ").strip(),
                        input("카테고리: ").strip(),
                        parse_amount(input("금액(양수 정수): ")),
                        input("메모(선택): "),
                        parse_tags(input("태그(쉼표로 구분, 없으면 엔터): ")))
        print(f"[저장 완료] id={tx.id}")
    elif args.command == "list":
        valid_amount(args.limit)
        with closing(ledger.iter_transactions()) as transactions:
            for tx in itertools.islice(transactions, args.limit):
                _show(tx)
    elif args.command == "search":
        with closing(ledger.search(from_date=args.from_date, to_date=args.to_date,
                                   category=args.category, kind=args.kind,
                                   q=args.q, tag=args.tag)) as transactions:
            for tx in transactions:
                _show(tx)
    elif args.command == "summary":
        valid_amount(args.top)
        info = ledger.summary(args.month)
        if not info.count:
            print("데이터 없음")
        print(f"총 수입: {info.income}원")
        print(f"총 지출: {info.expense}원")
        print(f"잔액: {info.balance}원")
        if info.budget is not None:
            percent = info.expense * 100 / info.budget
            print(f"예산: {info.budget}원 (사용률 {percent:.1f}%)")
            if info.expense > info.budget:
                print("[경고] 예산 초과")
        print(f"지출 TOP {args.top}")
        for rank, (name, amount) in enumerate(info.category_expense[:args.top], 1):
            print(f"{rank}) {name} {amount}원")
    elif args.command == "budget":
        if args.operation == "set":
            amount = parse_amount(args.amount)
            ledger.set_budget(args.month, amount)
            print(f"[저장 완료] {args.month} 예산 {amount}원")
        else:
            amount = ledger.get_budget(args.month)
            print(f"{args.month}: {amount}원" if amount is not None else
                  f"{args.month}: 설정된 예산 없음")
    elif args.command == "category":
        if args.operation == "list":
            for name in ledger.categories():
                print(f"- {name}")
        else:
            name = args.name or input("카테고리명: ")
            if args.operation == "add":
                ledger.add_category(name)
                print(f"[저장 완료] category={name.strip()}")
            else:
                ledger.remove_category(name)
                print(f"[삭제 완료] category={name.strip()}")
    elif args.command == "update":
        changes: dict[str, object] = {}
        for key in ("date", "category", "memo"):
            value = getattr(args, key)
            if value is not None:
                changes[key] = value
        if args.kind is not None:
            changes["type"] = args.kind
        if args.amount is not None:
            changes["amount"] = parse_amount(args.amount)
        if args.tags is not None:
            changes["tags"] = parse_tags(args.tags)
        tx = ledger.update(args.id, **changes)
        print(f"[수정 완료] id={tx.id}")
    elif args.command == "delete":
        ledger.delete(args.id)
        print(f"[삭제 완료] id={args.id}")
    elif args.command == "import":
        imported, skipped = ledger.import_csv(args.source)
        print(f"[완료] imported={imported}, skipped={skipped}")
    elif args.command == "export":
        count = ledger.export_csv(args.out, month=args.month,
                                  from_date=args.from_date, to_date=args.to_date)
        print(f"[완료] {args.out} ({count} records)")
    return 0
