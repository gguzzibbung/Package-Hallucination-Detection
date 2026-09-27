# Package Hallucination Detection

LangChain의 여러 Git tag를 분석하여 **API 경로와 버전별 변화 기록을 추적하고, 과거 API를 현재/최종 API로 연결하기 위한 프로젝트**입니다.

기존 API 변화 기록은 그대로 보존하면서, 조회 시에는 과거 API·중간 API·최신 API 중 어느 경로를 입력해도 최종 API를 찾을 수 있도록 조회용 KEY 색인을 사용합니다.

## 파일 구성

- `update_langchain_release.py`
  - LangChain 저장소의 전체 Git tag를 분석합니다.
  - 각 tag의 API 정보를 `Release.db`에 저장합니다.
  - 이미 정상적으로 저장된 tag는 다시 분석하지 않고 SKIP합니다.

- `update_langchain_history.py`
  - `Release.db`에 저장된 데이터를 이용해 API 변화를 비교합니다.
  - 인접 release 사이에서 API 변화를 분석합니다.
  - `MOVE`, `RENAME`, `SIGNATURE_CHANGE`, `REMOVE`, `ADD` 등의 변화 기록을 `History.db`의 `api_history`에 저장합니다.
  - 기존 API 변화 기록은 수정하지 않고 그대로 유지합니다.

- `main.py`
  - 완성된 `Release.db`와 `History.db`를 조회합니다.
  - 기존 `api_history`를 이용해 API의 전체 변경 경로를 확인합니다.
  - 실행 시 조회용 `api_lookup` 테이블을 생성/갱신합니다.
  - 과거 API, 중간 API, 최신 API를 KEY로 사용해 최종 API를 조회할 수 있습니다.
  - API 검색, DB 정보 확인, JSON 내보내기 기능을 제공합니다.

## API KEY 조회 방식

예를 들어 API가 다음과 같이 변경되었다고 가정합니다.

```text
A -> B -> C
```

기존 `api_history`에는 실제 변화 기록이 그대로 저장됩니다.

```text
A -> B
B -> C
```

`main.py`는 이 기록을 이용해 조회용 `api_lookup`을 구성합니다.

```text
KEY   VALUE
A  -> C
B  -> C
C  -> C
```

따라서 과거 API인 `A`, 중간 API인 `B`, 최신 API인 `C` 중 어느 것을 입력해도 최종 API `C`를 확인할 수 있습니다.

API가 최종적으로 삭제된 경우에는 최종 API가 없으며 상태가 `REMOVED`로 표시됩니다.

> `api_lookup`은 검색을 위한 별도 색인입니다. 기존 `api_history`의 변화 기록을 삭제하거나 덮어쓰지 않습니다.

## 1. 설치

필요한 패키지를 설치합니다.

```bash
pip install -r requirements.txt
```

## 2. LangChain 저장소 준비

프로젝트 폴더 안에 LangChain 저장소가 `langchain`이라는 이름으로 있어야 합니다.

```bash
git clone https://github.com/langchain-ai/langchain.git
```

전체 tag와 history를 분석해야 하므로 shallow clone(`--depth 1`)은 사용하지 않습니다.

폴더 구조 예시는 다음과 같습니다.

```text
progect/
├── langchain/
├── main.py
├── update_langchain_release.py
├── update_langchain_history.py
├── README.md
├── requirements.txt
└── .gitignore
```

`langchain/`은 분석 대상 원본 저장소이므로 자신의 GitHub 저장소에 다시 업로드하지 않는 것을 권장합니다.

`.gitignore` 예시:

```gitignore
__pycache__/
*.pyc
*.db-wal
*.db-shm
langchain/
```

## 3. Release.db 생성

전체 Git tag의 API 정보를 저장합니다.

```bash
python update_langchain_release.py
```

완료되면 같은 폴더에 다음 파일이 생성됩니다.

```text
Release.db
```

이미 정상적으로 저장된 tag는 다음 실행에서 SKIP되므로 작업을 중간에 종료했다가 다시 실행할 수 있습니다.

## 4. History.db 생성

`Release.db` 생성이 끝난 다음 실행합니다.

```bash
python update_langchain_history.py
```

완료되면 다음 파일이 생성됩니다.

```text
History.db
```

`History.db`의 `api_history`에는 API의 이동, 이름 변경, 시그니처 변경, 추가, 삭제 등의 변화 기록이 저장됩니다.

## 5. main.py 실행

`Release.db`와 `History.db`가 모두 만들어진 뒤 실행합니다.

Windows CMD 또는 PowerShell에서 프로젝트 폴더로 이동한 다음:

```bash
python main.py
```

`main.py` 실행 시 기존 `api_history`를 바탕으로 조회용 `api_lookup` 테이블이 자동으로 생성 또는 갱신됩니다.

API를 분석하면 입력한 API의 변경 경로와 함께 조회용 KEY에 연결된 현재/최종 API를 확인할 수 있습니다.

## 검색 예시

다음과 같은 변화가 있다고 가정합니다.

```text
langchain.old.API
        ↓
langchain.middle.API
        ↓
langchain.current.API
```

아래 세 API는 모두 검색 KEY가 될 수 있습니다.

```text
langchain.old.API
langchain.middle.API
langchain.current.API
```

세 KEY 모두 최종적으로 다음 API를 가리킵니다.

```text
langchain.current.API
```

즉 오래된 코드에서 발견한 API뿐 아니라 중간 버전이나 현재 버전의 API를 입력해도 동일한 최종 API를 확인할 수 있습니다.

## 기존 History 데이터와의 관계

이번 조회 방식은 기존 분석 결과를 다시 만드는 방식이 아닙니다.

```text
Release.db
    ↓
update_langchain_history.py
    ↓
History.db / api_history
    ↓
main.py
    ↓
api_lookup 생성
    ↓
과거/중간/최신 API KEY -> 최종 API
```

따라서 이미 `Release.db`와 `History.db` 생성이 끝난 상태라면 KEY 조회 기능을 사용하기 위해 전체 tag 분석을 처음부터 다시 실행할 필요는 없습니다.

## 전체 실행 순서

처음부터 DB를 생성하는 경우:

```bash
pip install -r requirements.txt
git clone https://github.com/langchain-ai/langchain.git
python update_langchain_release.py
python update_langchain_history.py
python main.py
```

이미 `Release.db`와 `History.db`가 정상적으로 존재하는 경우에는:

```bash
python main.py
```

만 실행하면 됩니다.

## WAL / SHM 파일

DB 작업 중 다음과 같은 파일이 일시적으로 생성될 수 있습니다.

```text
Release.db-wal
Release.db-shm
History.db-wal
History.db-shm
```

SQLite의 WAL 모드에서 사용하는 정상적인 파일입니다. DB 생성이나 업데이트가 실행 중일 때는 삭제하지 마세요.

## 주의사항

- `langchain/` 폴더는 원본 LangChain Git 저장소이므로 `.gitignore`에 포함하는 것을 권장합니다.
- DB 생성 중에는 프로그램을 강제로 종료하거나 WAL/SHM 파일을 임의로 삭제하지 않는 것이 좋습니다.
- `api_lookup`은 조회 편의를 위한 테이블이며 원본 변화 기록인 `api_history`는 그대로 유지됩니다.
- API 이동/이름 변경 판별은 프로젝트의 비교 로직에 의해 생성된 `History.db` 결과를 기반으로 합니다.
