# Codyssey 용돈 기입장

Python 3.10 이상과 표준 라이브러리만 사용한다. 실행 위치는 이 폴더다.

```sh
cd /home/ljh/self-os/Codyssey/budget
python3 -m budget_app --help
python3 -m budget_app --data-dir ./data category add --name food
python3 -m budget_app --data-dir ./data add
python3 -m budget_app --data-dir ./data list --limit 10
python3 cts.py
```

`--data-dir`는 명령 앞에 쓴다. 기본값은 현재 작업 폴더의 `./data`다. 모든 명령과 하위 명령은 `--help`를 제공한다. 처음 실행하면 저장 폴더와 빈 저장 파일 세 개가 생성된다. 초기 카테고리는 비어 있으므로 `category add`로 먼저 등록해야 거래를 추가하거나 CSV를 가져올 수 있다.

## 명령

| 명령 | 사용 예 |
|---|---|
| 추가 | `python3 -m budget_app add` — 날짜·타입·카테고리·금액·메모·태그 순서로 대화형 입력 |
| 목록 | `python3 -m budget_app list --limit 20` — 기본 20건 |
| 검색 | `python3 -m budget_app search --from 2024-01-01 --to 2024-01-31 --category food --type expense --q 점심 --tag meal` |
| 요약 | `python3 -m budget_app summary --month 2024-01 --top 3` |
| 예산 | `python3 -m budget_app budget set --month 2024-01 --amount 500000`; `budget get --month 2024-01` |
| 카테고리 | `category add --name food`; `category list`; `category remove --name food` — 이름 생략 시 입력 요청 |
| 수정 | `update --id TX-000001 --date 2024-01-16 --amount 20000 --memo 수정 --tags meal,work` |
| 삭제 | `delete --id TX-000001` |
| 가져오기 | `import --from import.csv` |
| 내보내기 | `export --out export.csv --month 2024-01`; 또는 `--from 2024-01-01 --to 2024-01-31` |

수정은 **옵션 방식**으로 고정했다. `--id`와 변경할 필드를 하나 이상 지정한다. `--memo ""`, `--tags ""`는 각각 값을 비운다. 검색 조건은 모두 AND이며 날짜 양끝을 포함한다. 타입은 `income`/`expense`, 금액은 양수 정수다. 잘못된 값·없는 ID·없는 카테고리는 원인과 힌트를 출력하고 종료 코드 2로 끝난다. 카테고리 삭제는 사용 중인 거래가 있으면 거부한다.

거래 출력의 최신순은 **`(date, 생성 순번)` 내림차순**이다. 과거 날짜로 나중에 입력하면 거래일에 맞는 위치에 들어가고, 동일 날짜는 나중에 생성된 ID가 먼저 나온다. 날짜를 수정하면 위치를 다시 잡는다. 검색과 목록은 정렬된 JSONL 파일을 제너레이터로 순회하여 필요한 줄부터 읽는다. `--limit`는 충분한 거래를 얻은 즉시 멈춘다. 중복 ID 검증은 표준 라이브러리 `sqlite3`의 **임시 디스크 키 집합**을 이용한다. 이 임시 파일은 순회가 끝나거나 제너레이터가 닫히면 삭제되며 영구 데이터 포맷은 JSONL뿐이다. 따라서 Python 메모리는 전체 거래 건수와 무관하고 방문한 행의 가장 긴 revision 이력에 좌우된다. 추가·수정·삭제·import는 정렬을 유지하려고 거래 파일을 한 번씩 재작성하므로 큰 파일에서는 쓰기 비용이 O(파일 크기)다. 임시 ID 검사에는 방문 건수만큼 디스크 공간과 인덱스 I/O가 필요하다.

## 저장과 revision

`./data`에는 `transactions.jsonl`, `categories.jsonl`, `budgets.jsonl`이 있다. 세 파일 모두 UTF-8 JSONL이다. `.lock`은 동시 변경을 직렬화하는 잠금 파일이다. 저장 파일이 비어 있어도 정상이며 손상된 JSON·스키마·중복 ID·순서·revision 해시는 오류로 알린다.

`budget_app/core.py`가 검증·상태 전이·파일 I/O를 한곳에서 관리한다. `model.py`는 frozen dataclass 값, `cli.py`는 인자·출력·적용된 오류 데코레이터를 맡는다. 거래 파일 한 줄은 한 item이며, 그 안의 `revisions` 배열은 그 ID가 정의한 불변 버전들의 순서다. `create`가 버전 1을 정의하고, `update`/`delete`는 직전 revision의 `hash`를 `parent_hash`로 **소비(use)**하여 새 버전을 정의(def)한다. 조회·요약·CSV 출력은 마지막 revision의 `state`만 소비한다. 삭제는 `state: null`인 tombstone을 남겨 ID가 다시 쓰이지 않게 한다. 같은 내용으로 되돌리는 A→B→A에서도 버전과 부모가 달라 세 revision의 해시가 다르다. 해시는 정규 JSON의 SHA-256으로 우발적 변경을 검출하는 값이며 인증 서명은 아니다. 거래일 정렬용 `sort_date`와 생성 순번인 ID도 검증한다. 이 설계의 revision 범위는 **item 내부의 선형 이력**이며 전체 저장소에 걸친 트랜잭션 그래프는 아니다. `snapshot()`은 CTS용 `ID → item` 딕셔너리를 만들고, 평상시 `list/search`는 이 전역 딕셔너리를 만들지 않는다. ID 조회는 파일 순회이고, 해시 값으로 항목을 찾는 영구 인덱스는 없다.

한 명령은 거래·카테고리·예산 중 **하나의 저장 파일만 변경**한다. 각 변경은 같은 폴더의 임시 파일 작성 → flush/fsync → `os.replace` → 폴더 fsync 순서다. replace 전 실패 시 기존 파일 바이트가 남는다. replace 후 폴더 fsync 실패는 이미 교체된 상태이므로 별도 메시지로 조회를 권한다. 여러 프로세스의 변경은 `.lock`으로 직렬화한다. 다른 프로세스의 조회는 교체 전 또는 후의 완전한 파일을 읽는다.

## CSV 계약

헤더 포함 UTF-8 CSV다. 최소 열과 출력 열 순서는 다음과 같다.

| 열 | 필수 | 의미 |
|---|---|---|
| `date` | Y | `YYYY-MM-DD`, 실제 존재하는 날짜 |
| `type` | Y | `income` 또는 `expense` |
| `category` | Y | 이미 등록된 카테고리 |
| `amount` | Y | 양수 정수 |
| `memo` | N | 문자열 |
| `tags` | N | 쉼표로 구분한 태그 문자열 |

`memo`/`tags` 열은 입력에서 빠져도 된다. 출력에는 항상 여섯 열이 있다. 태그에 쉼표가 있으면 CSV 자체도 큰따옴표로 감싼다. CSV 표준 quoting 덕분에 메모의 쉼표·줄바꿈·한글도 왕복한다. import는 **모든 행을 먼저 검증**하므로 마지막 행이 틀려도 거래 파일 전체가 그대로 남는다. 동일한 여섯 필드의 활성 거래는 기존 파일이나 같은 import 안에 있으면 건너뛴다(`skipped`); 건너뛴 행은 ID를 소비하지 않는다. 삭제된 거래와 동일한 내용은 새 ID로 다시 가져올 수 있다. export는 월 또는 날짜 양끝의 기간 조건이 필수이며 저장 파일과 같은 경로로 출력할 수 없다.

## CTS

`python3 cts.py`는 tempfile만 사용한다. 독립 dict/list oracle의 item FSM(`absent → active → deleted`)과 `Q(state, operation)`을 조합별로 걷고, 매 단계의 실제 snapshot·예상 상태·재시작 상태를 비교한다. 실패 전이는 세 저장 파일 바이트 불변까지 확인한다. 그 밖에 CLI subprocess, 잘못된 CSV 마지막 행, 중복 정책, Unicode/quoting, 날짜 경계, ID 재사용, hash/schema 손상, 조기 종료 제너레이터, 메모리 증가 경계, 교체 전/후 장애 주입을 검증한다.
