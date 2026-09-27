from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from pathlib import Path


# ============================================================
# 설정
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

RELEASE_DB_PATH = BASE_DIR / "Release.db"
HISTORY_DB_PATH = BASE_DIR / "History.db"


# ============================================================
# DB 연결
# ============================================================

def connect_history_db() -> sqlite3.Connection:
    if not HISTORY_DB_PATH.exists():
        raise FileNotFoundError(
            f"History.db가 없습니다: "
            f"{HISTORY_DB_PATH.resolve()}"
        )

    conn = sqlite3.connect(
        HISTORY_DB_PATH
    )

    conn.row_factory = sqlite3.Row

    return conn


def connect_release_db() -> sqlite3.Connection:
    if not RELEASE_DB_PATH.exists():
        raise FileNotFoundError(
            f"Release.db가 없습니다: "
            f"{RELEASE_DB_PATH.resolve()}"
        )

    conn = sqlite3.connect(
        RELEASE_DB_PATH
    )

    conn.row_factory = sqlite3.Row

    return conn


# ============================================================
# 조회용 KEY 색인
# ============================================================

def rebuild_api_lookup(
    conn: sqlite3.Connection,
) -> None:
    """
    기존 api_history는 절대 수정하지 않는다.

    A -> B -> C 라면:
      KEY A -> C
      KEY B -> C
      KEY C -> C

    즉 과거/중간/최신 API 중 무엇을 입력해도
    최종 API를 바로 찾을 수 있게 조회용 테이블만 만든다.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_lookup (
            lookup_api TEXT PRIMARY KEY,
            latest_api TEXT,
            status TEXT NOT NULL
        )
        """
    )

    rows = conn.execute(
        """
        SELECT *
        FROM api_history
        WHERE old_api IS NOT NULL
           OR new_api IS NOT NULL
        ORDER BY id
        """
    ).fetchall()

    forward = defaultdict(list)
    all_apis = set()

    for row in rows:
        if row["old_api"]:
            all_apis.add(row["old_api"])
            forward[row["old_api"]].append(row)

        if row["new_api"]:
            all_apis.add(row["new_api"])

    def resolve(start_api: str):
        current = start_api
        visited = set()

        while current and current not in visited:
            visited.add(current)

            edges = forward.get(current, [])
            edge = best_forward_edge(edges)

            if edge is None:
                return current, "ACTIVE"

            if edge["action"] == "REMOVE":
                return None, "REMOVED"

            # 같은 문자열의 signature 변경만 있으면 경로는 그대로다.
            if (
                edge["action"] == "SIGNATURE_CHANGE"
                and edge["new_api"] == current
            ):
                path_edges = [
                    e for e in edges
                    if e["action"] in {"MOVE", "RENAME", "REMOVE"}
                ]

                edge = best_forward_edge(path_edges)

                if edge is None:
                    return current, "ACTIVE"

                if edge["action"] == "REMOVE":
                    return None, "REMOVED"

            if not edge["new_api"]:
                return current, "ACTIVE"

            current = edge["new_api"]

        return current, "ACTIVE"

    conn.execute("DELETE FROM api_lookup")

    for api in sorted(all_apis):
        latest_api, status = resolve(api)

        conn.execute(
            """
            INSERT OR REPLACE INTO api_lookup(
                lookup_api,
                latest_api,
                status
            )
            VALUES (?, ?, ?)
            """,
            (
                api,
                latest_api,
                status,
            ),
        )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lookup_latest_api
        ON api_lookup(latest_api)
        """
    )

    conn.commit()


def lookup_latest_api(
    conn: sqlite3.Connection,
    api: str,
):
    return conn.execute(
        """
        SELECT
            lookup_api,
            latest_api,
            status
        FROM api_lookup
        WHERE lookup_api = ?
        LIMIT 1
        """,
        (api,),
    ).fetchone()


# ============================================================
# API 검색
# ============================================================

def api_simple_name(api: str) -> str:
    if not api:
        return ""

    return (
        api
        .split("(", 1)[0]
        .rsplit(".", 1)[-1]
    )


def find_exact_edges(
    conn: sqlite3.Connection,
    api: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM api_history
        WHERE old_api = ?
           OR new_api = ?
        ORDER BY id
        """,
        (
            api,
            api,
        ),
    ).fetchall()


def find_candidates(
    conn: sqlite3.Connection,
    api: str,
) -> list[str]:
    simple = api_simple_name(api)

    rows = conn.execute(
        """
        SELECT old_api AS api
        FROM api_history
        WHERE old_api LIKE ?

        UNION

        SELECT new_api AS api
        FROM api_history
        WHERE new_api LIKE ?

        ORDER BY api
        LIMIT 100
        """,
        (
            f"%.{simple}",
            f"%.{simple}",
        ),
    ).fetchall()

    return [
        row["api"]
        for row in rows
        if row["api"]
    ]


# ============================================================
# 전체 evolution graph 만들기
# ============================================================

def load_graph(
    conn: sqlite3.Connection,
):
    rows = conn.execute(
        """
        SELECT *
        FROM api_history
        WHERE old_api IS NOT NULL
           OR new_api IS NOT NULL
        ORDER BY id
        """
    ).fetchall()

    forward: dict[
        str,
        list[sqlite3.Row],
    ] = defaultdict(list)

    backward: dict[
        str,
        list[sqlite3.Row],
    ] = defaultdict(list)

    for row in rows:
        old_api = row["old_api"]
        new_api = row["new_api"]

        if old_api:
            forward[
                old_api
            ].append(row)

        if new_api:
            backward[
                new_api
            ].append(row)

    return (
        rows,
        forward,
        backward,
    )


def best_forward_edge(
    edges: list[sqlite3.Row],
) -> sqlite3.Row | None:
    """
    한 API에서 여러 변화가 잡히면
    MOVE / RENAME을 우선하고 confidence가 높은 것을 사용한다.
    """
    if not edges:
        return None

    priority = {
        "MOVE": 4,
        "RENAME": 4,
        "SIGNATURE_CHANGE": 3,
        "REMOVE": 2,
        "ADD": 1,
    }

    return sorted(
        edges,
        key=lambda row: (
            priority.get(
                row["action"],
                0,
            ),
            row["confidence"] or 0,
            row["id"],
        ),
        reverse=True,
    )[0]


def best_backward_edge(
    edges: list[sqlite3.Row],
) -> sqlite3.Row | None:
    if not edges:
        return None

    priority = {
        "MOVE": 4,
        "RENAME": 4,
        "SIGNATURE_CHANGE": 3,
        "ADD": 1,
    }

    return sorted(
        edges,
        key=lambda row: (
            priority.get(
                row["action"],
                0,
            ),
            row["confidence"] or 0,
            row["id"],
        ),
        reverse=True,
    )[0]


# ============================================================
# 과거 방향 추적
# ============================================================

def trace_backward(
    start_api: str,
    backward: dict,
) -> list[sqlite3.Row]:
    result = []
    current = start_api
    visited = set()

    while current and current not in visited:
        visited.add(current)

        edges = backward.get(
            current,
            [],
        )

        edge = best_backward_edge(
            edges
        )

        if edge is None:
            break

        if edge["action"] == "ADD":
            break

        result.append(edge)

        current = edge["old_api"]

    result.reverse()

    return result


# ============================================================
# 미래 방향 추적
# ============================================================

def trace_forward(
    start_api: str,
    forward: dict,
) -> list[sqlite3.Row]:
    result = []
    current = start_api
    visited = set()

    while current and current not in visited:
        visited.add(current)

        edges = forward.get(
            current,
            [],
        )

        edge = best_forward_edge(
            edges
        )

        if edge is None:
            break

        result.append(edge)

        if edge["action"] == "REMOVE":
            break

        if not edge["new_api"]:
            break

        # signature change는 API 문자열이 같을 수 있다.
        if (
            edge["action"]
            ==
            "SIGNATURE_CHANGE"
            and
            edge["new_api"]
            ==
            current
        ):
            later_edges = [
                candidate
                for candidate in edges
                if candidate["id"] > edge["id"]
                and candidate["action"] in {
                    "MOVE",
                    "RENAME",
                    "REMOVE",
                }
            ]

            later = best_forward_edge(
                later_edges
            )

            if later is None:
                break

            result.append(later)

            if (
                later["action"] == "REMOVE"
                or not later["new_api"]
            ):
                break

            current = later["new_api"]
            continue

        current = edge["new_api"]

    return result


# ============================================================
# 현재/최종 API 계산
# ============================================================

def final_api_from_chain(
    input_api: str,
    chain: list[sqlite3.Row],
) -> tuple[str | None, str]:
    current = input_api
    status = "ACTIVE"

    if chain:
        first = chain[0]

        if first["old_api"]:
            current = first["old_api"]

    for row in chain:
        if row["action"] == "REMOVE":
            return None, "REMOVED"

        if row["new_api"]:
            current = row["new_api"]

    return current, status


# ============================================================
# Release.db에서 최종 API 정보 조회
# ============================================================

def latest_api_info(
    conn: sqlite3.Connection,
    api: str,
):
    row = conn.execute(
        """
        SELECT
            s.version,
            s.api,
            s.simple_name,
            s.code_type,
            s.module,
            s.owner_class,
            s.file_path,
            s.signature,
            s.return_type,
            s.docstring

        FROM api_snapshot AS s

        WHERE s.api = ?

        ORDER BY
            CAST(
                substr(
                    s.version,
                    1,
                    instr(s.version, '.') - 1
                )
                AS INTEGER
            ) DESC,

            s.version DESC

        LIMIT 1
        """,
        (
            api,
        ),
    ).fetchone()

    return row


def latest_api_info_fallback(
    conn: sqlite3.Connection,
    api: str,
):
    row = latest_api_info(
        conn,
        api,
    )

    if row:
        return row

    simple = api_simple_name(
        api
    )

    return conn.execute(
        """
        SELECT
            version,
            api,
            simple_name,
            code_type,
            module,
            owner_class,
            file_path,
            signature,
            return_type,
            docstring

        FROM api_snapshot

        WHERE api LIKE ?

        ORDER BY version DESC

        LIMIT 1
        """,
        (
            f"%.{simple}",
        ),
    ).fetchone()


# ============================================================
# 역할 설명
# ============================================================

def role_text(
    row: sqlite3.Row | None,
) -> str:
    if row is None:
        return (
            "Release.db에서 현재 API의 "
            "상세 정보를 찾지 못했습니다."
        )

    docstring = (
        row["docstring"]
        or ""
    ).strip()

    if docstring:
        return docstring

    code_type = row["code_type"] or "API"
    simple_name = row["simple_name"] or row["api"]

    return (
        f"{simple_name}은(는) "
        f"{code_type} 형태의 API입니다. "
        f"소스 docstring은 저장되어 있지 않습니다."
    )


# ============================================================
# 전체 evolution 출력
# ============================================================

def analyze_api(
    requested_api: str,
):
    history_conn = connect_history_db()
    release_conn = connect_release_db()

    try:
        # 기존 api_history를 건드리지 않고 조회용 KEY만 갱신한다.
        rebuild_api_lookup(history_conn)

        lookup = lookup_latest_api(
            history_conn,
            requested_api,
        )

        exact = find_exact_edges(
            history_conn,
            requested_api,
        )

        selected_api = requested_api

        if not exact:
            candidates = find_candidates(
                history_conn,
                requested_api,
            )

            unique_candidates = sorted(
                set(candidates)
            )

            if len(unique_candidates) == 1:
                selected_api = (
                    unique_candidates[0]
                )

                print()
                print(
                    "[자동 연결]"
                )
                print(
                    requested_api
                )
                print("        ↓")
                print(
                    selected_api
                )

            elif len(unique_candidates) > 1:
                print()
                print("=" * 100)
                print(
                    "정확한 API를 하나로 "
                    "확정할 수 없습니다."
                )
                print("=" * 100)

                for index, api in enumerate(
                    unique_candidates,
                    start=1,
                ):
                    print(
                        f"{index:>2}. {api}"
                    )

                return

            else:
                print()
                print(
                    "History.db에서 "
                    "해당 API를 찾지 못했습니다."
                )
                return

        (
            _,
            forward,
            backward,
        ) = load_graph(
            history_conn
        )

        previous = trace_backward(
            selected_api,
            backward,
        )

        future = trace_forward(
            selected_api,
            forward,
        )

        # 입력 API를 중심으로 앞/뒤를 합친다.
        chain = (
            previous
            +
            future
        )

        # id 기준 중복 제거
        unique_chain = []
        seen_ids = set()

        for row in chain:
            if row["id"] in seen_ids:
                continue

            seen_ids.add(
                row["id"]
            )
            unique_chain.append(
                row
            )

        print()
        print("=" * 100)
        print(
            "LangChain API Evolution"
        )
        print("=" * 100)

        print()
        print(
            "입력 API:"
        )
        print(
            requested_api
        )

        if lookup is not None:
            print()
            print("KEY 검색 결과:")
            print(
                "최신 API:",
                lookup["latest_api"]
                or "없음 (삭제됨)",
            )
            print(
                "상태:",
                lookup["status"],
            )

        if selected_api != requested_api:
            print()
            print(
                "History DB 연결 API:"
            )
            print(
                selected_api
            )

        if not unique_chain:
            print()
            print(
                "변경 기록이 없습니다."
            )

            info = (
                latest_api_info_fallback(
                    release_conn,
                    selected_api,
                )
            )

            if info:
                print()
                print(
                    "[API 정보]"
                )
                print(
                    "API       :",
                    info["api"],
                )
                print(
                    "종류      :",
                    info["code_type"],
                )
                print(
                    "Signature :",
                    info["signature"],
                )
                print(
                    "파일      :",
                    info["file_path"],
                )
                print()
                print(
                    "역할:"
                )
                print(
                    role_text(info)
                )

            return

        print()
        print(
            "[전체 변경 경로]"
        )

        for index, row in enumerate(
            unique_chain,
            start=1,
        ):
            print()
            print(
                "-" * 100
            )

            print(
                f"STEP {index}"
            )

            print(
                f"Release : "
                f"{row['from_version']} "
                f"-> "
                f"{row['to_version']}"
            )

            print(
                "Action  :",
                row["action"],
            )

            print(
                "Type    :",
                row["code_type"],
            )

            print(
                "Conf.   :",
                row["confidence"],
            )

            print()

            print(
                row["old_api"]
                or "∅"
            )

            print(
                "        ↓"
            )

            print(
                row["new_api"]
                or "∅"
            )

            if (
                row["old_signature"]
                !=
                row["new_signature"]
            ):
                print()
                print(
                    "Old Signature:",
                    row["old_signature"],
                )
                print(
                    "New Signature:",
                    row["new_signature"],
                )

        final_api, status = (
            final_api_from_chain(
                selected_api,
                unique_chain,
            )
        )

        print()
        print("=" * 100)

        print(
            "현재 상태:",
            status,
        )

        print(
            "현재/최종 API:",
            final_api or "없음 (삭제됨)",
        )

        if status == "REMOVED":
            last_remove = None

            for row in reversed(
                unique_chain
            ):
                if row["action"] == "REMOVE":
                    last_remove = row
                    break

            if last_remove:
                print(
                    "삭제 확인 구간:",
                    f"{last_remove['from_version']} "
                    f"-> "
                    f"{last_remove['to_version']}",
                )

            print("=" * 100)
            return

        print("=" * 100)

        if final_api:
            info = (
                latest_api_info_fallback(
                    release_conn,
                    final_api,
                )
            )

            print()
            print(
                "[현재 API 정보]"
            )

            if info is None:
                print(
                    "Release.db에서 "
                    "상세 정보를 찾지 못했습니다."
                )

            else:
                print(
                    "API       :",
                    info["api"],
                )

                print(
                    "Release   :",
                    info["version"],
                )

                print(
                    "종류      :",
                    info["code_type"],
                )

                print(
                    "소속 클래스:",
                    info["owner_class"]
                    or "-",
                )

                print(
                    "Signature :",
                    info["signature"]
                    or "-",
                )

                print(
                    "Return    :",
                    info["return_type"]
                    or "-",
                )

                print(
                    "파일      :",
                    info["file_path"],
                )

                print()
                print(
                    "역할:"
                )

                print(
                    role_text(info)
                )

    finally:
        history_conn.close()
        release_conn.close()


# ============================================================
# 단순 검색
# ============================================================

# ============================================================
# 단순 검색
# ============================================================

def search_api(
    keyword: str,
):
    conn = connect_history_db()

    try:
        rows = conn.execute(
            """
            SELECT DISTINCT
                old_api,
                new_api,
                action,
                from_tag,
                to_tag,
                code_type,
                confidence

            FROM api_history

            WHERE old_api LIKE ?
               OR new_api LIKE ?

            ORDER BY
                old_api,
                new_api

            LIMIT 300
            """,
            (
                f"%{keyword}%",
                f"%{keyword}%",
            ),
        ).fetchall()

        print()
        print("=" * 100)
        print(
            f"API 검색: {keyword}"
        )
        print("=" * 100)

        if not rows:
            print(
                "검색 결과 없음"
            )
            return

        for index, row in enumerate(
            rows,
            start=1,
        ):
            print()
            print(
                f"[{index}]"
            )

            print(
                "Release:",
                row["from_tag"],
                "->",
                row["to_tag"],
            )

            print(
                "OLD:",
                row["old_api"],
            )

            print(
                "NEW:",
                row["new_api"],
            )

            print(
                "TYPE:",
                row["action"],
            )

            print(
                "CODE:",
                row["code_type"],
            )

            print(
                "CONF:",
                row["confidence"],
            )

    finally:
        conn.close()
        
# ============================================================
# DB 정보
# ============================================================

def show_database_info():
    history_conn = connect_history_db()
    release_conn = connect_release_db()

    try:
        release_count = release_conn.execute(
            """
            SELECT COUNT(*)
            FROM releases
            """
        ).fetchone()[0]

        api_count = release_conn.execute(
            """
            SELECT COUNT(*)
            FROM api_snapshot
            """
        ).fetchone()[0]

        pair_count = history_conn.execute(
            """
            SELECT COUNT(*)
            FROM release_comparison
            """
        ).fetchone()[0]

        history_count = history_conn.execute(
            """
            SELECT COUNT(*)
            FROM api_history
            """
        ).fetchone()[0]

        actions = history_conn.execute(
            """
            SELECT
                action,
                COUNT(*) AS n
            FROM api_history
            GROUP BY action
            ORDER BY action
            """
        ).fetchall()

        print()
        print("=" * 100)
        print("DB 정보")
        print("=" * 100)

        print(
            "Release.db:",
            RELEASE_DB_PATH.resolve(),
        )

        print(
            "History.db:",
            HISTORY_DB_PATH.resolve(),
        )

        print()
        print(
            "Release 수:",
            release_count,
        )

        print(
            "Release API rows:",
            api_count,
        )

        print(
            "비교 구간:",
            pair_count,
        )

        print(
            "History 변화:",
            history_count,
        )

        print()
        print(
            "[Action]"
        )

        for row in actions:
            print(
                f"{row['action']:<24}"
                f"{row['n']}"
            )

        release_integrity = (
            release_conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        )

        history_integrity = (
            history_conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        )

        print()
        print(
            "Release Integrity:",
            release_integrity,
        )

        print(
            "History Integrity:",
            history_integrity,
        )

    finally:
        history_conn.close()
        release_conn.close()


# ============================================================
# MAIN
# ============================================================

def main():
    while True:
        print()
        print("=" * 100)
        print(
            "LangChain API Evolution Database"
        )
        print("=" * 100)

        print(
            "1. API 전체 변화 분석"
        )

        print(
            "2. API 검색"
        )

        print(
            "3. DB 정보"
        )

        print(
            "4. 종료"
        )

        choice = input(
            "\n선택: "
        ).strip()

        if choice == "1":
            api = input(
                "\n분석할 API 입력:\n> "
            ).strip()

            if api:
                analyze_api(
                    api
                )

        elif choice == "2":
            keyword = input(
                "\n검색할 API 문자열:\n> "
            ).strip()

            if keyword:
                search_api(
                    keyword
                )

        elif choice == "3":
            show_database_info()

        elif choice == "4":
            break

        else:
            print(
                "1~4 중 하나를 입력하세요."
            )


if __name__ == "__main__":
    main()
