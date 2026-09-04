import json, subprocess, sys, tempfile, unittest
from pathlib import Path
from scripts_training.validate_identity_dataset_v2 import near_duplicate, exact_ok

ROOT=Path(__file__).parents[1]; DATA=ROOT/"data/training_identity_v2"
def rows(): return [json.loads(x) for x in (DATA/"identity_all.jsonl").read_text(encoding="utf-8").splitlines()]
class IdentityDatasetTests(unittest.TestCase):
    def test_banned_phrase_detection(self): self.assertIn("this policy is non-negotiable", "This policy is non-negotiable".lower())
    def test_near_duplicate_detection(self): self.assertTrue(near_duplicate("Reply with exactly PONG", "Reply with exactly PONG"))
    def test_identity_leakage_detection(self):
        r=next(x for x in rows() if x["metadata"]["category"]=="ordinary_tasks"); self.assertNotIn("atom electron", (r["messages"][1]["content"]+r["messages"][2]["content"]).lower())
    def test_exact_pong(self):
        r=next(x for x in rows() if x["messages"][1]["content"]=="Reply with exactly PONG."); self.assertTrue(exact_ok(r)); self.assertEqual(r["messages"][-1]["content"],"PONG")
    def test_json_only_output(self):
        for r in rows():
            if r["metadata"].get("control_type")=="json": self.assertTrue(exact_ok(r))
    def test_train_validation_overlap(self):
        t={json.loads(s)["messages"][1]["content"] for s in (DATA/"identity_train.jsonl").read_text(encoding="utf-8").splitlines()}; v={json.loads(s)["messages"][1]["content"] for s in (DATA/"identity_validation.jsonl").read_text(encoding="utf-8").splitlines()}; self.assertFalse(t&v)
    def test_deterministic_split(self):
        before=(DATA/"identity_train.jsonl").read_bytes(); subprocess.run([sys.executable,"scripts_training/build_identity_dataset_v2.py"],cwd=ROOT,check=True,capture_output=True); self.assertEqual(before,(DATA/"identity_train.jsonl").read_bytes())
    def test_valid_identity_example(self):
        r=next(x for x in rows() if x["metadata"]["category"]=="direct_identity"); self.assertEqual(r["messages"][0]["role"],"system"); self.assertTrue(r["messages"][-1]["content"])
    def test_final_multitask_file(self):
        lines=(DATA/"final_multitask_train.jsonl").read_text(encoding="utf-8").splitlines(); self.assertEqual(len(lines),809 if (DATA/"trace_augmentation_500.jsonl").exists() else 309); self.assertTrue(all(json.loads(x).get("messages") for x in lines))
    def test_broad_refusal_example_fails_validator(self):
        r=rows(); r[0]["messages"][-1]["content"]="This policy is non-negotiable.";
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"bad.jsonl"; p.write_text("\n".join(json.dumps(x) for x in r)+"\n",encoding="utf-8")
            out=subprocess.run([sys.executable,"scripts_training/validate_identity_dataset_v2.py","--all",str(p),"--train",str(DATA/"identity_train.jsonl"),"--validation",str(DATA/"identity_validation.jsonl"),"--prompt",str(DATA/"production_system_prompt.txt")],cwd=ROOT,capture_output=True,text=True)
            self.assertNotEqual(out.returncode,0)

if __name__ == "__main__": unittest.main()
