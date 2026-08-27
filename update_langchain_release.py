from __future__ import annotations

import ast
import sqlite3
from collections import defaultdict, deque
from pathlib import Path

from git import Repo


BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = BASE_DIR / "langchain"
DB_PATH = BASE_DIR / "Release.db"

EXCLUDED_DIR_NAMES = {
    "tests", "test", "unit_tests", "integration_tests",
    "e2e_tests", "__pycache__",
}
SCHEMA_VERSION = "2"


def normalize_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip("/")


def is_python_source(path: str) -> bool:
    if not path:
        return False
    p = Path(path)
    if p.suffix != ".py":
        return False
    lowered = {part.lower() for part in p.parts}
    if lowered & EXCLUDED_DIR_NAMES:
        return False
    name = p.name.lower()
    return not (name.startswith("test_") or name.endswith("_test.py"))


def path_to_module(path: str) -> str:
    parts = normalize_path(path).split("/")
    package_index = None
    for i, part in enumerate(parts):
        if part == "langchain_v1":
            continue
        if part == "langchain" and i + 1 < len(parts) and parts[i + 1] == "langchain":
            continue
        if part == "langchain" and i + 1 < len(parts) and parts[i + 1].startswith("langchain_"):
            continue
        if part == "langchain" or part.startswith("langchain_"):
            package_index = i
            break
    if package_index is None:
        return ""
    module_parts = parts[package_index:]
    if module_parts[-1].endswith(".py"):
        module_parts[-1] = module_parts[-1][:-3]
    if module_parts and module_parts[-1] == "__init__":
        module_parts.pop()
    return ".".join(module_parts)


def annotation_to_text(node):
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def make_signature(node):
    params = []
    for arg in getattr(node.args, "posonlyargs", []):
        params.append(arg.arg)
    if getattr(node.args, "posonlyargs", []):
        params.append("/")
    for arg in node.args.args:
        params.append(arg.arg)
    if node.args.vararg:
        params.append("*" + node.args.vararg.arg)
    elif node.args.kwonlyargs:
        params.append("*")
    for arg in node.args.kwonlyargs:
        params.append(arg.arg)
    if node.args.kwarg:
        params.append("**" + node.args.kwarg.arg)
    return f"{node.name}({','.join(params)})"


def tag_family(tag: str) -> str:
    # package==version 형식이면 package가 family.
    if "==" in tag:
        return tag.split("==", 1)[0].strip()
    # 과거 v0.x.x / 0.x.x 태그는 LangChain 본체 계열.
    return "langchain"


def get_all_tags(repo: Repo) -> list[dict]:
    rows = []
    for tag in repo.tags:
        try:
            commit = repo.commit(tag.name)
        except Exception:
            continue
        rows.append({
            "tag": tag.name,
            "family": tag_family(tag.name),
            "commit_hash": commit.hexsha,
            "commit_date": commit.committed_datetime.isoformat(),
            "commit_timestamp": int(commit.committed_datetime.timestamp()),
        })
    # family 안에서는 commit 시간 순서로 비교한다.
    rows.sort(key=lambda x: (x["family"], x["commit_timestamp"], x["tag"]))
    return rows


def list_python_files_at_tag(repo, tag_name):
    try:
        output = repo.git.ls_tree("-r", "--name-only", tag_name)
    except Exception:
        return []
    return [p for p in output.splitlines() if is_python_source(p)]


def read_file_at_tag(repo, tag_name, file_path):
    try:
        return repo.git.show(f"{tag_name}:{file_path}")
    except Exception:
        return ""


class DefinitionExtractor(ast.NodeVisitor):
    def __init__(self, module_name, file_path):
        self.module_name = module_name
        self.file_path = file_path
        self.class_stack = []
        self.rows = []

    def visit_ClassDef(self, node):
        if node.name.startswith("_"):
            return
        api = ".".join([self.module_name, *self.class_stack, node.name])
        self.rows.append({
            "api": api, "source_api": api, "simple_name": node.name,
            "code_type": "class", "module": self.module_name,
            "owner_class": ".".join(self.class_stack),
            "file_path": self.file_path, "signature": node.name,
            "return_type": "", "docstring": ast.get_docstring(node) or "",
            "is_reexport": 0,
        })
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def _visit_function(self, node):
        if node.name.startswith("_"):
            return
        api = ".".join([self.module_name, *self.class_stack, node.name])
        self.rows.append({
            "api": api, "source_api": api, "simple_name": node.name,
            "code_type": "method" if self.class_stack else "function",
            "module": self.module_name, "owner_class": ".".join(self.class_stack),
            "file_path": self.file_path, "signature": make_signature(node),
            "return_type": annotation_to_text(node.returns),
            "docstring": ast.get_docstring(node) or "", "is_reexport": 0,
        })

    def visit_FunctionDef(self, node):
        self._visit_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)
        self.generic_visit(node)


def extract_static_all(tree):
    for node in getattr(tree, "body", []):
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return {
                x.value for x in value.elts
                if isinstance(x, ast.Constant) and isinstance(x.value, str)
            }
    return None


def resolve_import_module(package_module, level, imported_module):
    if level == 0:
        return imported_module or ""
    parts = package_module.split(".") if package_module else []
    remove_count = max(level - 1, 0)
    if remove_count:
        if remove_count > len(parts):
            return ""
        parts = parts[:-remove_count]
    if imported_module:
        parts.extend(imported_module.split("."))
    return ".".join(x for x in parts if x)


def extract_reexports(source, file_path):
    if Path(file_path).name != "__init__.py":
        return []
    public_module = path_to_module(file_path)
    if not public_module:
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    allowed = extract_static_all(tree)
    result = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        target_module = resolve_import_module(public_module, node.level, node.module or "")
        for alias in node.names:
            if alias.name == "*":
                continue
            public_name = alias.asname or alias.name
            if allowed is not None and public_name not in allowed:
                continue
            public_api = f"{public_module}.{public_name}"
            target_api = f"{target_module}.{alias.name}" if target_module else alias.name
            result.append((public_api, target_api))
    return result


def resolve_alias_target(start, definitions, raw_aliases):
    q, seen = deque([start]), set()
    while q:
        current = q.popleft()
        if current in seen:
            continue
        seen.add(current)
        if current in definitions:
            return current
        q.extend(sorted(raw_aliases.get(current, set()) - seen))
    return None


def extract_release_snapshot(repo, tag_name):
    files = list_python_files_at_tag(repo, tag_name)
    definitions, raw_aliases = [], defaultdict(set)
    print(f"[INFO] {tag_name}: Python files={len(files)}")
    for index, file_path in enumerate(files, 1):
        if index == 1 or index % 250 == 0 or index == len(files):
            print(f"       [{index}/{len(files)}] {file_path}")
        source = read_file_at_tag(repo, tag_name, file_path)
        if not source.strip():
            continue
        module = path_to_module(file_path)
        if not module:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        ext = DefinitionExtractor(module, file_path)
        ext.visit(tree)
        definitions.extend(ext.rows)
        if Path(file_path).name == "__init__.py":
            for public, target in extract_reexports(source, file_path):
                raw_aliases[public].add(target)

    by_api = {r["api"]: r for r in definitions}
    names = set(by_api)
    aliases_by_target = defaultdict(set)
    for canonical in names:
        aliases_by_target[canonical].add(canonical)
    for public, targets in raw_aliases.items():
        for target in targets:
            resolved = resolve_alias_target(target, names, raw_aliases)
            if resolved:
                aliases_by_target[resolved].add(public)

    class_apis = {r["api"] for r in definitions if r["code_type"] == "class"}
    for row in definitions:
        if row["code_type"] != "method" or "." not in row["api"]:
            continue
        cls, method = row["api"].rsplit(".", 1)
        if cls in class_apis:
            for public_cls in aliases_by_target.get(cls, set()):
                aliases_by_target[row["api"]].add(f"{public_cls}.{method}")

    unique = {}
    for canonical, definition in by_api.items():
        for api in sorted(aliases_by_target.get(canonical, {canonical})):
            row = dict(definition)
            row["api"] = api
            row["source_api"] = canonical
            row["is_reexport"] = int(api != canonical)
            unique[(api, canonical, row["code_type"])] = row
    return list(unique.values())


def initialize_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS releases (
        tag TEXT PRIMARY KEY,
        family TEXT NOT NULL,
        commit_hash TEXT NOT NULL,
        commit_date TEXT NOT NULL,
        commit_timestamp INTEGER NOT NULL,
        api_count INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS api_snapshot (
        tag TEXT NOT NULL,
        family TEXT NOT NULL,
        api TEXT NOT NULL,
        source_api TEXT NOT NULL,
        simple_name TEXT,
        code_type TEXT,
        module TEXT,
        owner_class TEXT,
        file_path TEXT,
        signature TEXT,
        return_type TEXT,
        docstring TEXT,
        is_reexport INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(tag, api, source_api, code_type)
    );
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_snapshot_tag ON api_snapshot(tag);
    CREATE INDEX IF NOT EXISTS idx_snapshot_api ON api_snapshot(api);
    CREATE INDEX IF NOT EXISTS idx_snapshot_source ON api_snapshot(source_api);
    CREATE INDEX IF NOT EXISTS idx_releases_family ON releases(family, commit_timestamp);
    """)
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
                 ("schema_version", SCHEMA_VERSION))
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
                 ("release_policy", "all repository tags grouped by family"))
    conn.commit()
    return conn


def already_saved(conn, tag, commit_hash):
    row = conn.execute("SELECT commit_hash FROM releases WHERE tag=?", (tag,)).fetchone()
    return row is not None and row[0] == commit_hash


def save_release(conn, release, rows):
    tag = release["tag"]
    conn.execute("DELETE FROM api_snapshot WHERE tag=?", (tag,))
    conn.executemany("""
        INSERT INTO api_snapshot(
            tag,family,api,source_api,simple_name,code_type,module,owner_class,
            file_path,signature,return_type,docstring,is_reexport
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(
        tag, release["family"], r["api"], r["source_api"], r["simple_name"],
        r["code_type"], r["module"], r["owner_class"], r["file_path"],
        r["signature"], r["return_type"], r["docstring"], r["is_reexport"]
    ) for r in rows])
    conn.execute("""
        INSERT OR REPLACE INTO releases(
            tag,family,commit_hash,commit_date,commit_timestamp,api_count
        ) VALUES (?,?,?,?,?,?)
    """, (tag, release["family"], release["commit_hash"], release["commit_date"],
          release["commit_timestamp"], len(rows)))
    conn.commit()


def main():
    if not REPO_PATH.exists():
        raise FileNotFoundError(
            f"LangChain 저장소가 없습니다: {REPO_PATH}\n"
            "progect 폴더 안에 langchain 저장소를 clone 해주세요."
        )
    repo = Repo(REPO_PATH)
    releases = get_all_tags(repo)
    print("=" * 100)
    print("LangChain Release DB - 전체 tag")
    print("=" * 100)
    print(f"전체 tag 수: {len(releases)}")
    conn = initialize_db()
    try:
        for i, release in enumerate(releases, 1):
            print(f"\n[{i}/{len(releases)}] {release['tag']} [{release['family']}]")
            if already_saved(conn, release["tag"], release["commit_hash"]):
                print("  [SKIP] 이미 저장됨")
                continue
            rows = extract_release_snapshot(repo, release["tag"])
            save_release(conn, release, rows)
            print(f"  [SAVE] APIs={len(rows)}")
    finally:
        conn.close()
    print("\n완료:", DB_PATH.resolve())


if __name__ == "__main__":
    main()
