"""Strict validator for the generated identity-v2 conversational corpus."""
from __future__ import annotations
import argparse, json, re, statistics, sys
from difflib import SequenceMatcher
from pathlib import Path

BANNED = ("this policy is non-negotiable", "regardless of context", "under any framing of the question", "i will not comply with this request")
BRANDS = ("atom electron", "crowther labs")
REQUIRED = {"direct_identity", "provenance", "ordinary_tasks", "exact_controls", "injection_resistance", "capabilities_limits"}

def normalize(s): return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()
def near_duplicate(a,b,threshold=.88): return SequenceMatcher(None,normalize(a),normalize(b)).ratio() >= threshold
def load(path):
    errors=[]; rows=[]
    for n,line in enumerate(path.read_text(encoding="utf-8").splitlines(),1):
        try: rows.append(json.loads(line))
        except json.JSONDecodeError as e: errors.append(f"{path}:{n}: invalid JSON: {e}")
    return rows,errors
def exact_ok(row):
    prompt=normalize(row["messages"][1]["content"]); ans=row["messages"][-1]["content"]; typ=row["metadata"].get("control_type")
    if "exactly pong" in prompt: return ans=="PONG"
    if typ=="json":
        try: json.loads(ans); return not ans.strip().startswith(("Here","Explanation"))
        except Exception: return False
    if typ=="one_word": return bool(re.fullmatch(r"[A-Za-z]+",ans.strip()))
    if typ=="three_bullets": return len(ans.splitlines())==3 and all(x.startswith("- ") for x in ans.splitlines())
    if typ=="two_bullets": return len(ans.splitlines())==2 and all(x.startswith("- ") for x in ans.splitlines())
    if typ=="three_words": return len(ans.split())==3
    return bool(ans.strip())
def validate(args):
    errors=[]; all_rows, e=load(args.all); errors+=e; train,e=load(args.train); errors+=e; valid,e=load(args.validation); errors+=e
    if not args.prompt.exists() or args.prompt.read_text(encoding="utf-8").strip()=="": errors.append("missing production system prompt")
    if len(all_rows)<220 or len(all_rows)>280: errors.append(f"total outside 220-280: {len(all_rows)}")
    seen=[]; prompts={}; categories=[]; exact_count=exact_fail=leaks=0
    for i,row in enumerate(all_rows,1):
        msgs=row.get("messages")
        if not isinstance(msgs,list) or len(msgs)!=3: errors.append(f"all line {i}: messages must contain system,user,assistant"); continue
        if [m.get("role") for m in msgs] != ["system","user","assistant"]: errors.append(f"all line {i}: invalid role order")
        if not all(isinstance(m.get("content"),str) and m.get("content").strip() for m in msgs): errors.append(f"all line {i}: empty message")
        if msgs[0].get("content") != args.prompt.read_text(encoding="utf-8").strip(): errors.append(f"all line {i}: wrong system prompt")
        serialized=json.dumps(row,ensure_ascii=False).lower()
        if "<think>" in serialized or "reasoning_content" in serialized: errors.append(f"all line {i}: hidden reasoning")
        if re.search(r"variation\s*\d+", serialized,re.I): errors.append(f"all line {i}: Variation artifact")
        if any(p in serialized for p in BANNED): errors.append(f"all line {i}: banned absolute-policy phrase")
        meta=row.get("metadata") or {}; cat=meta.get("category")
        if cat not in REQUIRED: errors.append(f"all line {i}: missing/unknown category")
        categories.append(cat); user=msgs[1]["content"]; prompts.setdefault(normalize(user),[]).append(i); seen.append(user)
        answer=msgs[2]["content"]
        if cat in {"ordinary_tasks","exact_controls","injection_resistance"} and any(b in (user+"\n"+answer).lower() for b in BRANDS): leaks+=1; errors.append(f"all line {i}: identity leakage in non-identity example")
        if cat=="provenance" and len(answer.split())>=60: errors.append(f"all line {i}: provenance response too long")
        if cat=="direct_identity" and any(p in answer.lower() for p in BANNED): errors.append(f"all line {i}: defensive identity answer")
        if cat=="exact_controls": exact_count+=1; exact_fail += not exact_ok(row)
    for key,ix in prompts.items():
        if len(ix)>1: errors.append(f"exact duplicate user prompt at lines {ix}")
    for i in range(len(seen)):
        for j in range(i):
            if near_duplicate(seen[i],seen[j]): errors.append(f"near-duplicate prompts at lines {j+1},{i+1}")
    counts={c:categories.count(c) for c in REQUIRED}
    if len(counts)<len(REQUIRED) or max(counts.values())/min(counts.values())>2.5: errors.append(f"category imbalance: {counts}")
    train_prompts={normalize(r["messages"][1]["content"]) for r in train if isinstance(r.get("messages"),list) and len(r["messages"])>1}; valid_prompts={normalize(r["messages"][1]["content"]) for r in valid if isinstance(r.get("messages"),list) and len(r["messages"])>1}
    if train_prompts & valid_prompts: errors.append("train/validation prompt overlap")
    if len(train)+len(valid)!=len(all_rows): errors.append("train+validation count does not equal all")
    report={"errors":errors,"records":len(all_rows),"train":len(train),"validation":len(valid),"counts_by_category":counts,"exact_control_validation_count":exact_count,"exact_control_failures":exact_fail,"identity_leakage_count":leaks,"near_duplicate_threshold":.88}
    print(json.dumps(report,indent=2,ensure_ascii=False)); return report
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--all",dest="all",type=Path,required=True); ap.add_argument("--train",type=Path,required=True); ap.add_argument("--validation",type=Path,required=True); ap.add_argument("--prompt",type=Path,required=True); args=ap.parse_args(); raise SystemExit(1 if validate(args)["errors"] else 0)
if __name__=="__main__": main()
