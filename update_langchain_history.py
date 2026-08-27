from __future__ import annotations

import difflib
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RELEASE_DB_PATH = BASE_DIR / "Release.db"
HISTORY_DB_PATH = BASE_DIR / "History.db"

ALGORITHM_VERSION = "2"
MOVE_THRESHOLD_CLASS = 0.74
MOVE_THRESHOLD_FUNCTION = 0.80
MOVE_THRESHOLD_METHOD = 0.84
RENAME_THRESHOLD = 0.90


def similarity(a, b):
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def connect_release_db():
    if not RELEASE_DB_PATH.exists():
        raise FileNotFoundError(f"Release.db가 없습니다: {RELEASE_DB_PATH.resolve()}")
    conn = sqlite3.connect(RELEASE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_releases(conn):
    return [dict(r) for r in conn.execute("""
        SELECT tag,family,commit_hash,commit_date,commit_timestamp,api_count
        FROM releases ORDER BY family,commit_timestamp,tag
    """).fetchall()]


def load_snapshot(conn, tag):
    return [dict(r) for r in conn.execute("""
        SELECT api,source_api,simple_name,code_type,module,owner_class,file_path,
               signature,return_type,docstring,is_reexport
        FROM api_snapshot WHERE tag=?
    """, (tag,)).fetchall()]


def preferred_api_rows(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["source_api"], row["code_type"])].append(row)
    result = []
    for candidates in groups.values():
        candidates.sort(key=lambda r: (
            0 if r["is_reexport"] else 1,
            r["api"].count("."), len(r["api"]), r["api"]
        ))
        result.append(candidates[0])
    return result


def source_map(rows):
    return {(r["source_api"], r["code_type"]): r for r in preferred_api_rows(rows)}


def detect_signature_changes(old_rows, new_rows):
    old_map, new_map = source_map(old_rows), source_map(new_rows)
    out = []
    for key in set(old_map) & set(new_map):
        old, new = old_map[key], new_map[key]
        if old["signature"] == new["signature"]:
            continue
        out.append({
            "action": "SIGNATURE_CHANGE", "old_api": old["api"], "new_api": new["api"],
            "code_type": old["code_type"], "old_file": old["file_path"],
            "new_file": new["file_path"], "old_signature": old["signature"],
            "new_signature": new["signature"], "role": new["docstring"], "confidence": 1.0
        })
    return out


def removed_added(old_rows, new_rows):
    old_map, new_map = source_map(old_rows), source_map(new_rows)
    return ([old_map[k] for k in set(old_map)-set(new_map)],
            [new_map[k] for k in set(new_map)-set(old_map)])


def owner_simple(owner):
    return (owner or "").rsplit(".", 1)[-1]


def valid_owner(old, new):
    if old["code_type"] != "method":
        return True
    return bool(owner_simple(old["owner_class"])) and owner_simple(old["owner_class"]) == owner_simple(new["owner_class"])


def threshold(t):
    return {"method": MOVE_THRESHOLD_METHOD, "function": MOVE_THRESHOLD_FUNCTION}.get(t, MOVE_THRESHOLD_CLASS)


def detect_moves(removed, added):
    index = defaultdict(list)
    for new in added:
        index[(new["simple_name"], new["code_type"])].append(new)
    changes, used_old, used_new = [], set(), set()
    for old in removed:
        scored = []
        for new in index.get((old["simple_name"], old["code_type"]), []):
            if new["source_api"] in used_new or not valid_owner(old, new):
                continue
            sig = similarity(old["signature"], new["signature"])
            doc = similarity(old["docstring"], new["docstring"])
            owner = similarity(old["owner_class"], new["owner_class"])
            module = similarity(old["module"], new["module"])
            conf = (sig*.55 + doc*.30 + owner*.10 + module*.05) if old["code_type"] == "method" else (sig*.45 + doc*.40 + module*.15)
            if conf >= threshold(old["code_type"]):
                scored.append((conf, new))
        if not scored:
            continue
        conf, best = max(scored, key=lambda x: x[0])
        changes.append({
            "action":"MOVE","old_api":old["api"],"new_api":best["api"],
            "code_type":old["code_type"],"old_file":old["file_path"],"new_file":best["file_path"],
            "old_signature":old["signature"],"new_signature":best["signature"],
            "role":best["docstring"],"confidence":round(conf,4)
        })
        used_old.add(old["source_api"]); used_new.add(best["source_api"])
    return changes, used_old, used_new


def detect_renames(removed, added, used_old, used_new):
    candidates = []
    for old in removed:
        if old["source_api"] in used_old: continue
        for new in added:
            if new["source_api"] in used_new or old["code_type"] != new["code_type"] or not valid_owner(old,new):
                continue
            name = similarity(old["simple_name"].lower(), new["simple_name"].lower())
            sig = similarity(old["signature"],new["signature"])
            doc = similarity(old["docstring"],new["docstring"])
            module = similarity(old["module"],new["module"])
            conf = name*.25 + sig*.30 + doc*.35 + module*.10
            if conf >= RENAME_THRESHOLD and (doc >= .75 or sig >= .92):
                candidates.append((conf,old,new))
    candidates.sort(key=lambda x:x[0], reverse=True)
    out=[]
    for conf,old,new in candidates:
        if old["source_api"] in used_old or new["source_api"] in used_new: continue
        out.append({
            "action":"RENAME","old_api":old["api"],"new_api":new["api"],
            "code_type":old["code_type"],"old_file":old["file_path"],"new_file":new["file_path"],
            "old_signature":old["signature"],"new_signature":new["signature"],
            "role":new["docstring"],"confidence":round(conf,4)
        })
        used_old.add(old["source_api"]); used_new.add(new["source_api"])
    return out


def detect_add_remove(removed, added, used_old, used_new):
    out=[]
    for old in removed:
        if old["source_api"] not in used_old:
            out.append({"action":"REMOVE","old_api":old["api"],"new_api":None,"code_type":old["code_type"],
                        "old_file":old["file_path"],"new_file":None,"old_signature":old["signature"],
                        "new_signature":None,"role":old["docstring"],"confidence":1.0})
    for new in added:
        if new["source_api"] not in used_new:
            out.append({"action":"ADD","old_api":None,"new_api":new["api"],"code_type":new["code_type"],
                        "old_file":None,"new_file":new["file_path"],"old_signature":None,
                        "new_signature":new["signature"],"role":new["docstring"],"confidence":1.0})
    return out


def compare_releases(conn, old_tag, new_tag):
    old, new = load_snapshot(conn,old_tag), load_snapshot(conn,new_tag)
    changes = detect_signature_changes(old,new)
    removed,added = removed_added(old,new)
    moves,used_old,used_new = detect_moves(removed,added)
    changes.extend(moves)
    changes.extend(detect_renames(removed,added,used_old,used_new))
    changes.extend(detect_add_remove(removed,added,used_old,used_new))
    return changes


def initialize_history_db():
    conn=sqlite3.connect(HISTORY_DB_PATH)
    conn.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS release_comparison(
      family TEXT NOT NULL, from_tag TEXT NOT NULL, to_tag TEXT NOT NULL,
      from_commit TEXT NOT NULL, to_commit TEXT NOT NULL,
      algorithm_version TEXT NOT NULL, change_count INTEGER NOT NULL,
      compared_at TEXT DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(family,from_tag,to_tag)
    );
    CREATE TABLE IF NOT EXISTS api_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      family TEXT NOT NULL, from_tag TEXT NOT NULL, to_tag TEXT NOT NULL,
      action TEXT NOT NULL, old_api TEXT, new_api TEXT, code_type TEXT,
      old_file TEXT,new_file TEXT,old_signature TEXT,new_signature TEXT,
      role TEXT,confidence REAL
    );
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_history_old ON api_history(old_api);
    CREATE INDEX IF NOT EXISTS idx_history_new ON api_history(new_api);
    CREATE INDEX IF NOT EXISTS idx_history_family ON api_history(family,from_tag,to_tag);
    """)
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",("algorithm_version",ALGORITHM_VERSION))
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",("mapping_policy","old_api=KEY,new_api=VALUE"))
    conn.commit()
    return conn


def comparison_exists(conn,old,new):
    row=conn.execute("""
      SELECT from_commit,to_commit,algorithm_version FROM release_comparison
      WHERE family=? AND from_tag=? AND to_tag=?
    """,(old["family"],old["tag"],new["tag"])).fetchone()
    return row and row[0]==old["commit_hash"] and row[1]==new["commit_hash"] and row[2]==ALGORITHM_VERSION


def save_comparison(conn,old,new,changes):
    conn.execute("DELETE FROM api_history WHERE family=? AND from_tag=? AND to_tag=?",
                 (old["family"],old["tag"],new["tag"]))
    conn.executemany("""
      INSERT INTO api_history(family,from_tag,to_tag,action,old_api,new_api,code_type,
      old_file,new_file,old_signature,new_signature,role,confidence)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,[(
      old["family"],old["tag"],new["tag"],c["action"],c["old_api"],c["new_api"],c["code_type"],
      c["old_file"],c["new_file"],c["old_signature"],c["new_signature"],c["role"],c["confidence"]
    ) for c in changes])
    conn.execute("""
      INSERT OR REPLACE INTO release_comparison(
      family,from_tag,to_tag,from_commit,to_commit,algorithm_version,change_count)
      VALUES(?,?,?,?,?,?,?)
    """,(old["family"],old["tag"],new["tag"],old["commit_hash"],new["commit_hash"],ALGORITHM_VERSION,len(changes)))
    conn.commit()


def main():
    rconn=connect_release_db()
    hconn=initialize_history_db()
    try:
        releases=get_releases(rconn)
        families=defaultdict(list)
        for r in releases: families[r["family"]].append(r)
        total=sum(max(0,len(v)-1) for v in families.values())
        done=0
        print("="*100); print("LangChain History DB - family별 인접 tag 비교"); print("="*100)
        print("비교 구간:",total)
        for family,items in sorted(families.items()):
            if len(items)<2: continue
            print(f"\n[FAMILY] {family} ({len(items)} tags)")
            for old,new in zip(items,items[1:]):
                done+=1
                print(f"[{done}/{total}] {old['tag']} -> {new['tag']}")
                if comparison_exists(hconn,old,new):
                    print("  [SKIP] 이미 비교됨"); continue
                changes=compare_releases(rconn,old["tag"],new["tag"])
                save_comparison(hconn,old,new,changes)
                print("  [SAVE] changes=",len(changes))
    finally:
        rconn.close(); hconn.close()
    print("\n완료:",HISTORY_DB_PATH.resolve())


if __name__=="__main__":
    main()
