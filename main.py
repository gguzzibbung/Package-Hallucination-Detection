from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE_DIR=Path(__file__).resolve().parent
RELEASE_DB_PATH=BASE_DIR/"Release.db"
HISTORY_DB_PATH=BASE_DIR/"History.db"


def connect(path):
    if not path.exists(): raise FileNotFoundError(f"DB가 없습니다: {path.resolve()}")
    c=sqlite3.connect(path); c.row_factory=sqlite3.Row; return c


def api_simple_name(api):
    return (api or "").split("(",1)[0].rsplit(".",1)[-1]


def load_graph(conn):
    rows=conn.execute("SELECT * FROM api_history ORDER BY id").fetchall()
    f,b=defaultdict(list),defaultdict(list)
    for r in rows:
        if r["old_api"]: f[r["old_api"]].append(r)
        if r["new_api"]: b[r["new_api"]].append(r)
    return f,b


def best(edges, backward=False):
    if not edges:return None
    priority={"MOVE":5,"RENAME":5,"SIGNATURE_CHANGE":4,"REMOVE":2,"ADD":1}
    return max(edges,key=lambda r:(priority.get(r["action"],0),r["confidence"] or 0,r["id"]))


def trace_backward(api,b):
    out=[]; seen=set(); cur=api
    while cur and cur not in seen:
        seen.add(cur); e=best(b.get(cur,[]),True)
        if not e or e["action"]=="ADD":break
        out.append(e); cur=e["old_api"]
    return list(reversed(out))


def trace_forward(api,f):
    out=[]; seen=set(); cur=api
    while cur and cur not in seen:
        seen.add(cur)
        candidates=f.get(cur,[])
        e=best(candidates)
        if not e:break
        out.append(e)
        if e["action"]=="REMOVE" or not e["new_api"]:break
        if e["action"]=="SIGNATURE_CHANGE" and e["new_api"]==cur:
            later=[x for x in candidates if x["id"]>e["id"] and x["action"] in {"MOVE","RENAME","REMOVE"}]
            e2=best(later)
            if not e2:break
            out.append(e2)
            if e2["action"]=="REMOVE" or not e2["new_api"]:break
            cur=e2["new_api"]
        else: cur=e["new_api"]
    return out


def find_candidates(conn,api):
    simple=api_simple_name(api)
    return [r["api"] for r in conn.execute("""
      SELECT old_api api FROM api_history WHERE old_api LIKE ?
      UNION SELECT new_api api FROM api_history WHERE new_api LIKE ?
      ORDER BY api LIMIT 100
    """,(f"%.{simple}",f"%.{simple}")).fetchall() if r["api"]]


def latest_info(conn,api):
    return conn.execute("""
      SELECT s.*,r.commit_timestamp FROM api_snapshot s
      JOIN releases r ON r.tag=s.tag
      WHERE s.api=? ORDER BY r.commit_timestamp DESC LIMIT 1
    """,(api,)).fetchone()


def analyze_api(requested):
    hc,rc=connect(HISTORY_DB_PATH),connect(RELEASE_DB_PATH)
    try:
        exact=hc.execute("SELECT 1 FROM api_history WHERE old_api=? OR new_api=? LIMIT 1",(requested,requested)).fetchone()
        selected=requested
        if not exact:
            cand=sorted(set(find_candidates(hc,requested)))
            if len(cand)==1:
                selected=cand[0]; print(f"\n[자동 연결]\n{requested}\n        ↓\n{selected}")
            elif cand:
                print("\n정확한 API를 하나로 확정할 수 없습니다.")
                for i,x in enumerate(cand,1):print(f"{i:>2}. {x}")
                return
            else:
                info=latest_info(rc,requested)
                if info:
                    print("\n변경 기록은 없지만 Release.db에 존재합니다.")
                    print("API:",info["api"]); print("종류:",info["code_type"]); print("역할:",info["docstring"] or "-")
                else: print("\nAPI를 찾지 못했습니다.")
                return
        f,b=load_graph(hc)
        chain=trace_backward(selected,b)+trace_forward(selected,f)
        unique=[]; ids=set()
        for r in chain:
            if r["id"] not in ids: ids.add(r["id"]); unique.append(r)
        print("\n"+"="*100);print("LangChain API Evolution");print("="*100);print("입력 API:",requested)
        if not unique: print("변경 기록이 없습니다."); return
        current=unique[0]["old_api"] or selected; removed=False
        for i,r in enumerate(unique,1):
            print("\n"+"-"*100);print("STEP",i)
            print(f"{r['from_tag']} -> {r['to_tag']} [{r['family']}]")
            print("Action:",r["action"]," Confidence:",r["confidence"])
            print(r["old_api"] or "∅");print("   ↓");print(r["new_api"] or "∅")
            if r["action"]=="REMOVE": current=None; removed=True
            elif r["new_api"]: current=r["new_api"]
        print("\n"+"="*100)
        print("현재 상태:","REMOVED" if removed else "ACTIVE")
        print("현재/최종 API:",current or "없음 (삭제됨)")
        if current:
            info=latest_info(rc,current)
            if info:
                print("종류:",info["code_type"]);print("Signature:",info["signature"] or "-")
                print("파일:",info["file_path"]);print("\n역할:\n",info["docstring"] or "docstring 없음")
    finally: hc.close();rc.close()


def search_api(keyword):
    c=connect(HISTORY_DB_PATH)
    try:
        rows=c.execute("""SELECT DISTINCT family,from_tag,to_tag,old_api,new_api,action,code_type,confidence
          FROM api_history WHERE old_api LIKE ? OR new_api LIKE ? LIMIT 300""",
          (f"%{keyword}%",f"%{keyword}%")).fetchall()
        print("\n"+"="*100);print("검색:",keyword);print("="*100)
        if not rows: print("검색 결과 없음");return
        for i,r in enumerate(rows,1):
            print(f"\n[{i}] {r['from_tag']} -> {r['to_tag']} [{r['family']}]")
            print("OLD :",r["old_api"]);print("NEW :",r["new_api"])
            print("TYPE:",r["action"]," CODE:",r["code_type"]," CONF:",r["confidence"])
    finally:c.close()


def show_database_info():
    hc,rc=connect(HISTORY_DB_PATH),connect(RELEASE_DB_PATH)
    try:
        print("\nRelease tags:",rc.execute("SELECT COUNT(*) FROM releases").fetchone()[0])
        print("API snapshots:",rc.execute("SELECT COUNT(*) FROM api_snapshot").fetchone()[0])
        print("비교 구간:",hc.execute("SELECT COUNT(*) FROM release_comparison").fetchone()[0])
        print("변화 기록:",hc.execute("SELECT COUNT(*) FROM api_history").fetchone()[0])
    finally:hc.close();rc.close()


def export_history_json():
    c=connect(HISTORY_DB_PATH)
    try:
        rows=c.execute("SELECT * FROM api_history WHERE old_api IS NOT NULL ORDER BY old_api,id").fetchall()
        data={}
        for r in rows:
            data.setdefault(r["old_api"],[]).append({
                "new_api":r["new_api"],"action":r["action"],"family":r["family"],
                "from_tag":r["from_tag"],"to_tag":r["to_tag"],"code_type":r["code_type"],
                "old_file":r["old_file"],"new_file":r["new_file"],
                "old_signature":r["old_signature"],"new_signature":r["new_signature"],
                "role":r["role"],"confidence":r["confidence"]
            })
        tmp=BASE_DIR/"History.json.tmp"; out=BASE_DIR/"History.json"
        with tmp.open("w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)
        tmp.replace(out)
        print(f"\nHistory.json 생성 완료: {out.resolve()}")
        print("KEY(old_api):",len(data)," 변화 기록:",len(rows))
    finally:c.close()


def main():
    while True:
        print("\n"+"="*100);print("LangChain API History");print("="*100)
        print("1. API 전체 변화 분석\n2. API 검색\n3. DB 정보\n4. History.json 내보내기\n5. 종료")
        choice=input("\n선택: ").strip()
        if choice=="1":
            api=input("\n분석할 API:\n> ").strip()
            if api:analyze_api(api)
        elif choice=="2":
            k=input("\n검색할 API:\n> ").strip()
            if k:search_api(k)
        elif choice=="3":show_database_info()
        elif choice=="4":export_history_json()
        elif choice=="5":break
        else:print("1~5 중 하나를 입력하세요.")


if __name__=="__main__":
    main()
