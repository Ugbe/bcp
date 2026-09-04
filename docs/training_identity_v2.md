# Atom Electron identity and control dataset v2

This corpus contains 235 hand-authored conversational examples for mixing with
the cleaned research trajectories. It teaches conditional identity awareness,
localized provenance boundaries, ordinary task completion, exact formatting,
prompt-injection resistance, and calibrated uncertainty. It does not contain
hidden reasoning, tool calls, synthetic Variation markers, or the legacy
`crowther_persona` rows.

For training, `data/training_identity_v2/final_multitask_train.jsonl` is the
single combined JSONL artifact: 94 cleaned research-training trajectories, 215
identity/control training examples, and 500 distinct judged-correct trace-derived
QA examples (809 rows total). The held-out identity validation file remains
separate for evaluation.

The builder is deterministic and writes `identity_all.jsonl`, a stratified
stable-hash 90/10 split, a report, rejected seed manifest, production system
prompt, and the recommended sampling manifest. The 100 legacy seed rows are
audited but not copied: broad-refusal rows are rejected and the remaining
useful intents are represented by newly authored localized examples.

Use:

```powershell
.venv\Scripts\python.exe scripts_training\build_identity_dataset_v2.py
.venv\Scripts\python.exe scripts_training\validate_identity_dataset_v2.py --all data\training_identity_v2\identity_all.jsonl --train data\training_identity_v2\identity_train.jsonl --validation data\training_identity_v2\identity_validation.jsonl --prompt data\training_identity_v2\production_system_prompt.txt
```

The recommended optimizer-batch mix is 65% research/tool trajectories, 17.5%
identity-specific examples, and 17.5% ordinary/exact controls. This controls
for the much longer research rows and avoids identity dominating updates. Keep
one multitask adapter from pristine `Qwen/Qwen3.5-9B`; do not stack a second
identity LoRA. If strengthening identity later, continue the same adapter with
identity plus research/control rehearsal.

Human review should spot-check nuanced limitation and injection examples before
production training, and benchmark evaluation must remain query-disjoint from
the research validation set.
