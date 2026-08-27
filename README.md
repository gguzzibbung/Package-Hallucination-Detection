# progect

LangChain의 여러 Git tag를 분석해서 API 경로와 변화 기록을 추적하는 프로젝트입니다.

## 파일 구성

- `update_langchain_release.py`
  - LangChain 저장소의 전체 tag를 분석합니다.
  - 각 tag의 API 정보를 `Release.db`에 저장합니다.
  - 이미 저장된 tag는 다시 분석하지 않고 SKIP합니다.

- `update_langchain_history.py`
  - `Release.db`에 저장된 데이터를 이용해 API 변화를 비교합니다.
  - 같은 package family의 인접 tag끼리 비교합니다.
  - MOVE, RENAME, SIGNATURE_CHANGE, REMOVE, ADD 등의 변화 기록을 `History.db`에 저장합니다.
  - 기본 관계는 `old_api = KEY`, `new_api = VALUE`입니다.

- `main.py`
  - 완성된 `Release.db`와 `History.db`를 조회하는 프로그램입니다.
  - 과거 API 또는 새로운 API를 입력해서 전체 변경 경로를 확인할 수 있습니다.
  - API 검색, DB 정보 확인, JSON 내보내기를 지원합니다.

## 1. 설치

먼저 필요한 패키지를 설치합니다.

```bash
pip install -r requirements.txt
```

## 2. LangChain 저장소 준비

`progect` 폴더 안에 LangChain 저장소가 `langchain`이라는 이름으로 있어야 합니다.

```bash
git clone https://github.com/langchain-ai/langchain.git
```

폴더 구조는 다음과 같이 됩니다.

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

## 3. Release.db 생성

먼저 전체 Git tag의 API 정보를 저장합니다.

```bash
python update_langchain_release.py
```

완료되면 같은 폴더에 다음 파일이 생성됩니다.

```text
Release.db
```

이미 정상적으로 저장된 tag는 다음 실행에서 SKIP되므로 중간에 프로그램을 종료했다가 다시 실행할 수 있습니다.

## 4. History.db 생성

`Release.db` 생성이 끝난 다음 실행합니다.

```bash
python update_langchain_history.py
```

완료되면 다음 파일이 생성됩니다.

```text
History.db
```

`History.db`에는 API의 이동, 이름 변경, 시그니처 변경, 추가, 삭제 등의 변화 기록이 저장됩니다.

## 5. main.py 실행 방법

`Release.db`와 `History.db`가 모두 만들어진 뒤 실행합니다.

Windows CMD 또는 PowerShell에서 `progect` 폴더로 이동한 다음:

```bash
python main.py
```

실행하면 다음 메뉴가 나타납니다.

```text
====================================================================================================
LangChain API History
====================================================================================================
1. API 전체 변화 분석
2. API 검색
3. DB 정보
4. History.json 내보내기
5. 종료

선택:
```

### 1번 - API 전체 변화 분석

과거 API 또는 새로운 API를 입력해서 API가 어떻게 변경되었는지 추적합니다.

예:

```text
선택: 1

분석할 API:
> langchain.document_loaders.PyPDFLoader.load
```

History DB에 변화가 정상적으로 저장되어 있다면 MOVE, RENAME, REMOVE 등의 변경 경로와 최종 API를 확인할 수 있습니다.

### 2번 - API 검색

API 이름의 일부 또는 전체를 입력해서 History DB의 관련 기록을 검색합니다.

예:

```text
선택: 2

검색할 API:
> PyPDFLoader
```

### 3번 - DB 정보

현재 생성된 DB의 tag 수, API snapshot 수, History 비교 구간 수, 변화 기록 수를 확인합니다.

```text
선택: 3
```

### 4번 - History.json 내보내기

`History.db`의 변화 기록을 JSON으로 내보냅니다.

```text
선택: 4
```

같은 폴더에 다음 파일이 생성됩니다.

```text
History.json
```

JSON은 과거 API를 key로 사용합니다.

예:

```json
{
  "langchain.document_loaders.PyPDFLoader.load": [
    {
      "new_api": "langchain_community.document_loaders.pdf.PyPDFLoader.load",
      "action": "MOVE"
    }
  ]
}
```

### 5번 - 종료

```text
선택: 5
```

프로그램을 종료합니다.

## 전체 실행 순서

처음부터 실행한다면 다음 순서입니다.

```bash
pip install -r requirements.txt
git clone https://github.com/langchain-ai/langchain.git
python update_langchain_release.py
python update_langchain_history.py
python main.py
```

데이터 흐름은 다음과 같습니다.

```text
LangChain Git tags
        ↓
update_langchain_release.py
        ↓
Release.db
        ↓
update_langchain_history.py
        ↓
History.db
        ↓
main.py
        ↓
API 변화 조회 / 검색 / History.json 생성
```

## WAL / SHM 파일

DB 작업 중 다음과 같은 파일이 일시적으로 생성될 수 있습니다.

```text
Release.db-wal
Release.db-shm
History.db-wal
History.db-shm
```

SQLite의 WAL 모드에서 사용하는 정상적인 파일입니다. DB 생성이나 업데이트가 실행 중일 때는 삭제하지 마세요.
