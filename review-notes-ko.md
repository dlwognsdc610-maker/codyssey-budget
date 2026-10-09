# Codyssey 용돈 기입장: 코드 흐름 설명 노트

평가에서 설명할 순서는 **값이 담기는 구조 → 호출과 값의 이동 → 지켜야 할 조건 → 구현 선택 → 검증 질문**이다. 아래 명령과 값은 RPi5의 현재 코드에서 임시 데이터 폴더로 실행해 확인했다. 소스 위치는 모두 이 문서가 있는 `Codyssey/budget/` 기준이다.

## 1. 핵심 구조체와 필드

`budget_app/model.py`는 값을 frozen dataclass로 담는다. 핵심 값은 다음과 같다.

| 구조 | 필드 | 이 프로그램에서 하는 일 |
|---|---|---|
| `Transaction` | `id`, `date`, `type`, `category`, `amount`, `memo`, `tags` | 한 거래의 내용. `id`는 `TX-000001` 형식이고, `tags`는 문자열 tuple이다. |
| `Revision` | `version`, `op`, `parent_hash`, `state`, `hash` | 한 거래가 `create`·`update`·`delete`를 거치며 정의한 버전. `state`는 당시의 `Transaction`이며 삭제 버전에서는 `None`이다. |
| `Item` | `id`, `sort_date`, `revisions` | 같은 ID의 revision들을 순서대로 가진 저장 단위. `head`는 마지막 revision의 `state`를 반환한다. |
| `Summary` | `income`, `expense`, `category_expense`, `count`, `budget` | 한 달을 순회해 얻은 집계 결과. `balance`는 `income - expense`로 계산한다. |

`Ledger`(`budget_app/core.py`)가 이 값들의 생성·검증·상태 전이·저장을 맡는다. 저장 폴더에는 `transactions.jsonl`, `categories.jsonl`, `budgets.jsonl`이 생긴다. 거래 파일의 **한 줄이 Item 하나**이며, 그 안에 해당 ID의 revision 배열이 들어 있다. CLI 인자와 화면 출력은 `budget_app/cli.py`가 맡는다.

## 2. 값이 오가는 흐름: add → update → 재조회

먼저 `category add --name food`로 카테고리를 등록했다. 이어 대화형 `add`에 `2024-01-01`, `expense`, `food`, `1`, 빈 메모, 빈 태그를 입력했다. 출력은 `[저장 완료] id=TX-000001`이었다.

1. `cli.main()`이 `input()`으로 받은 문자열을 `parse_amount()`·`parse_tags()`를 거쳐 `Ledger.add()`에 전달한다 (`cli.py:117–128`).
2. `Ledger.add()`는 날짜·타입·양수 금액을 검증하고, 등록된 `food`를 확인한다. 잠금을 잡고 거래 파일의 최대 생성 순번을 읽어 `TX-000001`을 정한다 (`core.py:452–462`).
3. `Transaction(id='TX-000001', date='2024-01-01', type='expense', category='food', amount=1, memo='', tags=())`가 만들어진다. `_revision(..., 'create', tx)`가 버전 1을 정의한다. 첫 `parent_hash`는 빈 문자열이다 (`core.py:110–124`).
4. `Item`을 거래일·생성 순번 기준 위치에 놓고, 임시 파일을 fsync한 뒤 `transactions.jsonl`로 원자 교체한다 (`core.py:232–255, 431–450`).

그다음 `update --id TX-000001 --amount 2`를 실행했다. `cli.main()`은 바뀔 필드를 `changes={'amount': 2}`로 만들어 `Ledger.update()`에 준다 (`cli.py:177–190`). `Ledger.update()`는 저장된 Item을 찾고 기존 frozen `Transaction`에서 금액만 바뀐 새 값을 만든다. 새 `Revision(version=2, op='update', state=<amount 2>)`의 `parent_hash`에는 버전 1의 해시가 들어간다. Item의 revision 배열 끝에 버전 2가 더해지고 거래 파일이 다시 교체된다 (`core.py:464–476`).

임시 폴더에서 실제로 기록된 두 버전의 핵심 값은 이렇다.

| 버전 | `op` | `state.amount` | `parent_hash` | `hash` 앞 12자리 |
|---:|---|---:|---|---|
| 1 | `create` | 1 | `""` | `570c96c98ccc` |
| 2 | `update` | 2 | `570c96c98ccc78ce189612679ffd60658c8c5ec4a2d09304108580f5ed0220fc` | `a6f6fc5d0980` |

저장된 행의 바깥 필드는 `id=TX-000001`, `sort_date=2024-01-01`, `revisions=[버전 1, 버전 2]`이다. 각 버전 안에는 표의 값과 당시 `Transaction`을 담은 `state`가 있다.

새 프로세스에서 `list --limit 1`을 실행한 결과는 `TX-000001 | 2024-01-01 | expense | food | 2 |  |`였다. `_iter_items()`가 JSONL 행을 다시 읽어 버전 순서·부모 해시·내용 해시를 확인하고, `iter_transactions()`가 `Item.head`의 금액 2를 내보냈기 때문이다 (`core.py:139–196, 257–292`). 재시작 후의 결과가 거래 파일의 마지막 정의와 연결된다.

이 흐름의 def-use 관계는 간단하다. `create`가 ID의 버전 1을 **정의(def)**하고, `update`가 그 해시를 `parent_hash`로 **사용(use)**해 버전 2를 정의한다. 조회는 버전 2의 `state`를 사용한다. `_digest()`는 ID·버전·연산·부모 해시·상태를 정규 JSON으로 만들어 SHA-256을 계산한다. 같은 거래 내용으로 되돌려도 버전과 부모가 달라 새 정의의 해시가 달라진다.

## 3. 이 연결이 요구하는 조건

- 거래의 `date`는 실제 존재하는 `YYYY-MM-DD`, `type`은 `income` 또는 `expense`, `amount`는 0보다 큰 정수다. `bool`도 금액으로 받지 않는다. 거래 카테고리는 등록된 이름이어야 한다 (`core.py:39–101`).
- ID는 생성 순번으로 정해진다. 삭제는 마지막 `state=None`인 revision을 남기므로, 삭제 뒤 새 거래를 넣어도 이전 ID가 유지된다 (`core.py:478–483`).
- 거래 파일은 `(date, 생성 순번)` 내림차순이다. 그래서 과거 날짜의 추가나 날짜 수정 때 Item의 위치를 다시 잡는다. `list`·`search`는 정렬된 행을 제너레이터로 순회한다 (`core.py:199–200, 257–320, 431–450`).
- 한 명령의 변경은 거래·카테고리·예산 중 저장 파일 하나에 반영된다. `fcntl` 잠금 아래에서 새 파일 작성 → flush/fsync → `os.replace` → 디렉터리 fsync 순으로 처리한다 (`core.py:210–255`).

## 4. 그래서 선택한 구현

상태 전이와 저장 판단은 `Ledger` 한곳에 두었다. `model.py`는 불변 값, `cli.py`는 입력·출력과 `@guarded` 오류 데코레이터를 맡아 과제의 모듈·클래스·타입 힌트 조건을 채운다. 수정은 `--id`와 변경 필드 옵션을 받는 방식이다. 일반 입력·파일 오류는 원인과 힌트를 출력하고 종료 코드 2로 끝난다 (`cli.py:25–50, 88–95, 117–201`).

정렬된 JSONL을 유지하므로 `list --limit N`은 필요한 거래를 얻으면 순회를 멈춘다. `_iter_items()`는 각 행을 읽으며 중복 ID를 임시 디스크 키 집합에서 검사한다. 큰 파일에서도 Python이 거래 전체를 목록으로 보관할 필요가 없고, 한 Item의 revision 행은 읽을 때 검증된다. 정렬을 유지하는 추가·수정·삭제는 거래 파일을 재작성한다 (`core.py:257–292, 431–483`).

## 5. 평가 자리에서 확인할 질문

1. **금액 1을 2로 수정하면 어떤 값이 새로 정의되고, 재시작 뒤 무엇이 출력되는가?** 위 두 revision의 `version`·`parent_hash`·`state`와 `Item.head`를 따라가면 된다. `test_a_b_a_content_has_distinct_revision_identity`, `test_deterministic_fsm_walks`가 이 관계를 확인한다.
2. **같은 날짜 두 건과 과거 날짜 추가·날짜 수정은 어떤 순서로 나오는가?** `(date, 생성 순번)` 키와 재배치 경로를 설명한다. `test_same_day_order_backdated_and_month_move`가 실제 순서를 확인한다.
3. **없는 카테고리·잘못된 날짜·사용 중 카테고리 삭제가 저장 파일에 어떤 영향을 주는가?** 검증 단계에서 오류가 나고 파일 바이트가 유지된다. `test_date_amount_kind_and_month_validation`, `test_category_guard_and_id_never_reused`, CTS walk의 거부 전이가 증거다.
4. **쓰다가 실패하거나 다시 실행해도 어떤 상태를 읽는가?** replace 전 실패는 기존 바이트를 남기며, 재시작해서 같은 거래를 읽는다. `test_transaction_replace_failure_preserves_reopen`, `test_atomic_write_failures_before_replace`가 이를 확인한다.
5. **제너레이터가 큰 파일에서 어떤 값을 보관하는가?** 현재 행과 검증 상태를 따라가며 설명한다. `test_generator_laziness_and_early_termination`, `test_streaming_memory_bound`는 조기 종료와 500건·1만 건 순회 peak를 확인한다.

전체 검증은 이 폴더에서 `python3 cts.py`로 실행한다. 현재 CTS는 24개 테스트와 독립 oracle의 240단계 전이를 포함한다.
