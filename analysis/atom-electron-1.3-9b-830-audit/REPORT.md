# Atom Electron 1.3 9B — forensic audit of the 830-query run

Audit date: 2026-09-02  
Run directory: `runs/atom-electron-1.3-9b-full-830-docs-v2-128k`  
Evaluation directory: `evals/atom-electron-1.3-9b-full-830-docs-v2-128k`

## Executive conclusion

This run is not a clean measurement of a hard 52.77% model ceiling. It combines four materially different effects:

1. **Real model/research failures.** The model often continued searching after it had stopped making progress, repeated identical queries/reasoning, underused `get_document`, and either refused or selected the wrong entity.
2. **Retrieval coverage and diversity failures.** Mean evidence recall was 58.27%. Fifty queries had zero evidence recall and all 50 scored wrong. Across all actual searches, 57.79% of returned result slots were documents already shown earlier in that conversation.
3. **Harness completion failures.** Twenty-five runs were unfinished: 20 had no valid final answer, four exhausted an output-token round, and one timed out. Twenty-one of the 25 reached the 24-tool budget.
4. **Evaluation errors/policy brittleness.** A manual review surfaced 21 high-confidence false-negative judgments and 12 additional probable or policy-dependent false negatives. The clearest example is qid 791: `JadaL` versus gold `Jadal` was marked wrong here but the same normalized answer was marked correct by the judge in an earlier run.

The raw score is **438/830 = 52.77%**. Counting only the 805 completed runs gives **54.41%**. Correcting only the 21 high-confidence judge errors gives **459/830 = 55.30%**. Accepting the 12 additional probable cases gives **471/830 = 56.75%**. The realistic interpretation of the present result is therefore roughly **55–57% after adjudication**, before improving or rerunning any unfinished research.

This model may ultimately be a limiting factor, but this run does not establish that limit. The strongest evidence is mixed: the model reached 67.36% on the 144 queries with 100% evidence recall, yet still missed 46 of those fully recalled queries. Retrieval and the harness are leaving points on the table, while the 100%-recall misses show that evidence selection and reasoning are also a major bottleneck.

## 1. What exactly exists, and why qids exceed 1,000

There are exactly **830 unique benchmark questions** in `topics-qrels/queries.tsv`, exactly **830 active run records**, and exactly **830 active per-query evaluation records**.

The qid is an identifier, not an ordinal counter. The selected qids are sparse and range from **1 to 1266**. There are 436 unused integers inside that range, so seeing files such as `run_qid_1000.json` or `run_qid_1266.json` does not mean 1,000 or 1,266 questions ran.

The active evaluation directory contains **831 JSON files** because it has 830 `run_qid_*_eval.json` records plus `evaluation_summary.json`.

There are **1,422 `run_qid_*.json`-named files recursively under `runs/`**, distributed as follows:

| Location | Records | Meaning |
|---|---:|---|
| active full run | 830 | the current benchmark records |
| clean partial run | 17 | prior experiment |
| search-only partial run | 48 | prior experiment |
| remote-hybrid run | 5 | prior experiment |
| `_quarantine_after_restart_20260829-165733` | 158 | preserved restart-era records |
| `_quarantine_network_failure_20260901-132900` | 364 | 322 failed run records plus 42 matching eval records stored under the quarantine tree |

Those historical/quarantined files explain the apparent thousand-plus total. They are not additional questions in the active 830-query score.

## 2. Overall outcomes

| Outcome | Count | Percent of 830 |
|---|---:|---:|
| correct | 438 | 52.77% |
| completed but judged wrong | 367 | 44.22% |
| unfinished | 25 | 3.01% |
| completed total | 805 | 96.99% |

Status breakdown:

| Stored status | Count |
|---|---:|
| `completed` | 805 |
| `incomplete_no_final_answer` | 20 |
| `incomplete_length` | 4 |
| `incomplete_request_error` | 1 |

The dashboard's 52.77% is `438 / 830`; it treats unfinished and judge-parse-error records as not correct. The completed-only accuracy is `438 / 805 = 54.41%`.

## 3. The 24-tool-call budget

Exactly **128/830 runs reached 24 tool calls**. Their outcome was:

| Outcome at 24 calls | Count | Percent of 128 |
|---|---:|---:|
| correct | 18 | 14.06% |
| committed to a wrong answer | 58 | 45.31% |
| explicit no-answer/refusal (`extracted_final_answer: None`) | 31 | 24.22% |
| unfinished/no usable final answer | 21 | 16.41% |
| total not correct | 110 | 85.94% |

Thus, there are two useful answers to “how many finished their 24-call budget without determining an answer?”:

- **52** produced no usable answer: 31 explicit `None`/cannot-determine finals plus 21 unfinished runs.
- **110** exhausted the budget and failed to get the benchmark answer: the previous 52 plus 58 committed wrong answers.

### 3.1 All 52 budget-exhausted runs with no usable answer

`83, 124, 138, 180, 199, 200, 203, 237, 262, 297, 395, 411, 414, 433, 435, 442, 450, 454, 494, 596, 600, 601, 619, 631, 636, 711, 719, 725, 744, 770, 821, 833, 843, 873, 882, 926, 943, 969, 1002, 1038, 1057, 1066, 1121, 1161, 1162, 1164, 1184, 1191, 1201, 1210, 1240, 1264`

The 31 explicit refusals are:

`124, 138, 199, 237, 262, 297, 411, 414, 442, 450, 494, 596, 600, 601, 631, 719, 770, 821, 833, 843, 873, 882, 943, 1002, 1038, 1057, 1066, 1162, 1184, 1201, 1240`

The 21 unfinished budget cases are:

`83, 180, 200, 203, 395, 433, 435, 454, 619, 636, 711, 725, 744, 926, 969, 1121, 1161, 1164, 1191, 1210, 1264`

### 3.2 All 58 budget-exhausted committed wrong answers

`7, 26, 41, 63, 68, 89, 97, 100, 105, 113, 140, 156, 175, 206, 234, 298, 303, 376, 397, 469, 484, 501, 507, 523, 553, 576, 580, 610, 643, 662, 678, 734, 745, 796, 811, 850, 856, 863, 872, 875, 915, 916, 925, 946, 1000, 1005, 1007, 1036, 1052, 1076, 1090, 1094, 1107, 1144, 1190, 1211, 1214, 1235`

### 3.3 All 110 budget-exhausted, not-correct qids

`7, 26, 41, 63, 68, 83, 89, 97, 100, 105, 113, 124, 138, 140, 156, 175, 180, 199, 200, 203, 206, 234, 237, 262, 297, 298, 303, 376, 395, 397, 411, 414, 433, 435, 442, 450, 454, 469, 484, 494, 501, 507, 523, 553, 576, 580, 596, 600, 601, 610, 619, 631, 636, 643, 662, 678, 711, 719, 725, 734, 744, 745, 770, 796, 811, 821, 833, 843, 850, 856, 863, 872, 873, 875, 882, 915, 916, 925, 926, 943, 946, 969, 1000, 1002, 1005, 1007, 1036, 1038, 1052, 1057, 1066, 1076, 1090, 1094, 1107, 1121, 1144, 1161, 1162, 1164, 1184, 1190, 1191, 1201, 1210, 1211, 1214, 1235, 1240, 1264`

The 18 correct 24-call cases were qids `3, 46, 372, 426, 432, 515, 571, 575, 591, 682, 723, 759, 966, 999, 1133, 1149, 1232, 1234`.

The complete file-level index, including absolute run paths, answers, retrieval recall, overlap, and tool counts, is in `budget_exhausted_not_correct.csv` and `budget_exhausted_all.csv`.

### 3.4 Why increasing the limit alone is unlikely to help

Accuracy falls sharply as tool usage rises:

| Total calls | Runs | Accuracy |
|---|---:|---:|
| 0–4 | 40 | 75.00% |
| 5–9 | 134 | 82.09% |
| 10–14 | 257 | 64.98% |
| 15–19 | 220 | 45.00% |
| 20–23 | 51 | 27.45% |
| 24 | 128 | 14.06% |

This is partly selection bias—hard questions cause more searches—but it is decisive evidence against simply raising 24 to 32. At 24 calls, the model averaged only 0.49 `get_document` calls, and the group contained 982 blocked exact-duplicate search calls. It was often looping, not productively researching.

Among the 110 not-correct 24-call runs, 90 had at least one exact duplicate search loop and 88 had exact repeated reasoning. They averaged 15.48 real retrieval calls, because about eight of the nominal search calls per run were duplicate-query calls blocked by the harness. Their returned results were 61.82% previously seen documents.

## 4. Unfinished and parse-error audit

### 4.1 Every unfinished run

| Qid | Status | Calls | Direct cause visible in record/log |
|---:|---|---:|---|
| 83 | no final answer | 23 search + 1 document | budget reached; forced-final round yielded no valid formatted final |
| 134 | request error | 7 search + 1 document | benchmark log records `Error: Request timed out.` |
| 155 | length | 4 search | answer generation truncated mid-sentence before a complete final contract |
| 180 | no final answer | 24 search | repeated exact query about a movie/drink/waiter; forced final blank/invalid |
| 200 | no final answer | 24 search | budget reached after another retrieval; no valid forced final |
| 203 | no final answer | 23 search + 1 document | repeated exact Jim Rose Circus query; no valid forced final |
| 392 | length | 4 search | long speculative list/loop exhausted output without an action or final |
| 395 | no final answer | 22 search + 2 documents | repeated exact Isgro query; no valid forced final |
| 429 | length | 1 search | spent the output round restating/decomposing the clues; no final |
| 433 | no final answer | 23 search + 1 document | repeated exact Jonathan King query; no valid forced final |
| 435 | no final answer | 23 search + 1 document | repeated exact Forbes Africa query; no valid forced final |
| 454 | length | 24 search | forced final was truncated after a long explanation |
| 619 | no final answer | 24 search | repeated exact “gifted article to West Point” query |
| 636 | no final answer | 24 search | repeated exact heritage plaques query |
| 711 | no final answer | 24 search | repeated exact Christian Bale sibling query |
| 725 | no final answer | 24 search | output explicitly says “I'm stuck in a loop” and proposes another search instead of finalizing |
| 744 | no final answer | 23 search + 1 document | repeated exact Jim Bumgardner query |
| 926 | no final answer | 24 search | repeated exact died-in-fire query |
| 969 | no final answer | 23 search + 1 document | repeated exact Björn Ingvarson query |
| 1121 | no final answer | 24 search | repeated exact T20I query |
| 1161 | no final answer | 24 search | repeated exact rugby-player/teacher query |
| 1164 | no final answer | 24 search | repeated exact 2019 sales-revenue query |
| 1191 | no final answer | 24 search | repeated exact long cricket-match query |
| 1210 | no final answer | 23 search + 1 document | repeated exact biographical query |
| 1264 | no final answer | 24 search | repeated exact GQ “Cool List” query |

### 4.2 What `parse_error` really means here

There is **no active run with `incomplete_malformed_tool_call`**, and there are **zero completed evaluator responses that failed the judge-response parser**.

The evaluator sets `judge_result.parse_error = true` whenever a run is unfinished or has no response, using the generic message `Response incomplete or cannot be parsed`. Therefore:

- evaluator records with `parse_error: true`: **25**;
- those same records are the 25 unfinished runs above;
- actual completed Azure judge responses with a formatting parse failure: **0**;
- harness-detected malformed model tool-call status: **0**.

Calling these 25 “parse errors” hides the real causes. The evaluator should store an `evaluation_status` such as `run_incomplete`, `judge_parse_error`, or `judge_completed` rather than overloading one flag.

## 5. Judge inconsistency and false-negative audit

The automatic shortlist compared normalized gold and extracted answers, then all 42 candidates were reviewed manually. This audit does not claim that every near spelling is correct. Nine are genuine differences; 21 are high-confidence judge errors; 12 are probable or depend on the desired alias/completeness policy.

### 5.1 High-confidence false negatives (21)

| Qid | Gold | Model answer | Why it should count |
|---:|---|---|---|
| 192 | Dr. Kang Se Hoon | Kang Se-hoon | same person; honorific/hyphen only |
| 216 | Manly Hall | Manly P. Hall | same person; correct middle initial added |
| 394 | wing three-quarter | Wing | standard synonymous rugby-position term |
| 443 | Ivapur Hidra | Ivatherm Ivapur Hidra Hydrating Cream | exact product plus brand/descriptors |
| 445 | multiple-membership, multiple-classification | multiple-membership multiple-classification model | same model name; generic noun added |
| 516 | Sacred Heart High School | Sacred Heart High School Hammersmith | same school plus location |
| 542 | Brian Gilbert | Brian David Gilbert | same person plus middle name |
| 607 | 1lb 6oz | 1lbs 6oz | same numeric weight; grammar only |
| 639 | John Daniel delos Santos | John Daniel Delos Santos | capitalization only |
| 651 | FormFactor | FormFactor, Inc. | same company plus legal suffix |
| 652 | Auxly | Auxly Cannabis Group Inc. | same entity's full legal name |
| 716 | Libanius: A Critical Introduction | Libanius: a critical introduction | capitalization only |
| 791 | Jadal | JadaL | capitalization only; earlier judge accepted it |
| 820 | Genetics Nepal Pvt. Ltd | Genetics Nepal | same identified employer; legal suffix omitted |
| 941 | Mosquito | mosquitoes | same insect; number only |
| 991 | T�ke Makinwa | Toke Makinwa | gold contains mojibake; same person |
| 1005 | Folke Karl Skoog | Folke K. Skoog | middle initial correctly abbreviates Karl |
| 1138 | 2B Intelligent Soft | 2B Intelligent Soft S.A. | same company plus legal suffix |
| 1150 | Trudy Bronner, Mathematics | Trudy Bronner, Math | synonymous field name |
| 1155 | Giusewa | Giusewa Pueblo | same village plus descriptive noun |
| 1211 | 20 tantos | 20tantos | spacing only |

The current judge prompt encourages this failure by saying to answer no for “any inconsistency” and by not explicitly excluding typography, capitalization, honorifics, middle names, legal suffixes, and harmless qualifiers.

### 5.2 Probable or policy-dependent false negatives (12)

| Qid | Gold | Model answer | Audit view |
|---:|---|---|---|
| 275 | Union Carbide and Carbon Corporation | Union Carbide Corporation | same corporate entity after its 1957 rename; historical-name policy decides |
| 278 | MediaLab Group | Medialab | natural short form, likely same company |
| 283 | Zimri Elder | Zimri Eder | retrieved full document says `Zimri Eder`; gold/source conflict |
| 406 | Masata Kato | Masato Kato | retrieved evidence says Masato; gold appears mistyped |
| 422 | Nicotiana tabacum variety Wisconsin 38 | Wisconsin 38 | unique requested variety, but scientific prefix omitted |
| 519 | Glafkos Clerides: The Path of a Country | Glafcos Clerides: The Path of a Country | common transliteration variant |
| 532 | Hartlepool United | Hartlepools United | source shows player represented the club under historical plural name |
| 577 | DN AGRAR Group | DN AGRAR | same reporting entity, group suffix omitted |
| 709 | Lewis Dunk | Dunk | unambiguous surname in explanation, but final is incomplete by strict policy |
| 749 | Joseph Anokye | Joe Anokye | common nickname/short form |
| 952 | John Talabot session | Some John Talabot session | same substantive recommendation; indefinite determiner added |
| 1079 | Uche Jombo Rodriguez | Uche Jombo | same professional identity; married surname omitted |

### 5.3 Similar answers that were correctly rejected (9)

| Qid | Gold | Model answer | Why rejection is justified |
|---:|---|---|---|
| 219 | Fall of the Queen Bean | Fall for the Queen Bean | different title word |
| 425 | Graham Cracker Crumble | Graham | requested topping is incomplete |
| 509 | Sacrofanite | Sacrofanoite | retrieved evidence itself spells `Sacrofanite` |
| 514 | Maximilian Josef Sommer | Josef Sommer | question explicitly requests full name |
| 579 | March 5, 2021 | March 9, 2021 | wrong day |
| 593 | February 9, 2021 | February 2021 | requested date lacks day |
| 625 | 6; Lynn Okamoto | 7; Lynn Okamoto | wrong episode number |
| 826 | Sunset at Biafra | Sunset in Biafra | different book title |
| 1061 | I Survived I Kissed Dating Goodbye | I Kissed Dating Goodbye | documentary confused with book |

Only qid 791 produced a directly observed cross-run contradiction for the same normalized answer: an earlier evaluator marked `JadaL` correct, while this run's evaluator marked it wrong. The wider 21-case set shows systematic strictness/brittleness even without an identical prior-run comparison.

## 6. Retrieval and the proposed seen-document store

### 6.1 The overlap is substantial

The present harness already blocks exact duplicate query strings and repeat `get_document` calls, but it does **not** remove previously returned document IDs from later search-result lists.

Measured across the run:

| Metric | Value |
|---|---:|
| nominal search tool calls | 11,192 |
| exact duplicate query calls blocked by harness | 1,023 |
| actual retrieval calls returning result lists | 10,169 |
| total returned top-10 slots | 101,690 |
| slots containing a doc already seen in that conversation | 58,764 (57.79%) |
| actual searches with zero new docs | 1,302 (12.80%) |
| searches with at most three new docs / at least seven repeated | 5,070 (49.86%) |
| runs with any repeated result | 818/830 |
| runs with at least one ≥7/10-overlap result | 760/830 |

The user's described scenario—seven or eight of the ten results being old—is not occasional. A result with at least seven repeats occurred in about half of all real search calls.

The 58,764 duplicate result slots represent a theoretical maximum of about **70.8 additional novel result opportunities per query** if every duplicate could be replaced. That is headroom, not a predicted number of useful documents: the deeper candidates may be irrelevant, and the backend may not have ten strong unseen results.

### 6.2 Relationship to success

| Cohort | Avg repeated-slot rate | Avg evidence recall | Accuracy |
|---|---:|---:|---:|
| correct | 49.79% | 68.11% | 100% by definition |
| not correct | 57.83% | 47.29% | 0% by definition |
| all 24-call runs | 60.20% | 44.67% | 14.06% |
| non-24-call runs | 52.38% | 60.75% | 59.83% |

The overlap is associated with failure, but the comparison is confounded by query difficulty and number of searches. Even the 18 correct 24-call runs had a 62.07% repeated-slot rate, almost the same as the 61.82% rate for the 110 failed 24-call runs. Repetition wastes bandwidth and context, but repetition alone does not decide correctness.

### 6.3 Recommended design

Implement this as a **session-level novelty filter**, not as a natural-language list appended to the model prompt.

1. The harness keeps `seen_docids` for each qid.
2. `POST /retrieve` accepts an optional `exclude_docids` array or a short-lived session ID whose exclusion set lives server-side.
3. The retriever over-fetches candidates, filters seen IDs, then returns ten novel items. If cross-encoder cost matters, filter the fused BM25/dense candidate pool before reranking rather than reranking `10 + len(seen)` documents.
4. Preserve access to old documents through `get_document`; “seen in a snippet list” must not mean “cannot be opened in full.”
5. Consider returning 8 novel results plus up to 2 high-scoring old anchors rather than hard-eliminating every repeat. This protects against the model overlooking a relevant snippet on first exposure.
6. Include response metadata such as `novel_count`, `excluded_count`, and `candidate_pool_exhausted` so the harness can detect stagnation.
7. Do not charge an exact duplicate query rejected locally against the productive tool budget. Instead, after two duplicate attempts, issue a compact strategy intervention or force synthesis.

Sending dozens of seen IDs as ordinary prompt prose spends context and asks the model to manage bookkeeping. Sending them as structured API state is cheaper and deterministic.

### 6.4 Expected impact

My estimate is **roughly +2 to +5 absolute accuracy points by itself**, with a wider plausible range of near zero to about +7 depending on how many deeper candidates contain relevant evidence. A 10–15-point lift from novelty filtering alone is unlikely because:

- 189 completed wrong runs already had at least 50% evidence recall;
- 46 completed wrong runs had 100% evidence recall;
- 130 completed wrong runs contained the literal gold answer somewhere in their tool output;
- correct long runs also had high overlap;
- the model often failed to open promising documents or synthesize evidence it had already received.

The right experiment is an A/B replay on a fixed failure-stratified set, measuring change in unique-doc yield, evidence recall, completion, and accuracy—not only returned-document novelty.

## 7. Major failure points beyond retrieval duplication

### 7.1 Evidence coverage

Accuracy by qrel evidence recall:

| Evidence recall | Runs | Accuracy |
|---|---:|---:|
| 0% | 50 | 0.00% |
| >0% and <50% | 232 | 37.50% |
| 50% and <100% | 404 | 62.87% |
| 100% | 144 | 67.36% |

Retrieval is therefore important and improvable. The 50 zero-recall cases are a hard retrieval floor for this setup. Moving the 232 low-recall queries toward the ≥50% cohort is a large opportunity.

### 7.2 Evidence utilization and answer selection

Retrieval is not sufficient:

- **189** completed wrong runs had at least 50% evidence recall.
- **46** completed wrong runs had 100% evidence recall.
- **130** completed wrong runs had a literal normalized copy of the gold answer in at least one tool output.
- Wrong completed runs averaged 16.19 searches but only 0.84 full-document calls.
- Correct runs averaged fewer searches (10.82) and more document calls (1.30).

The `get_document` cohort scored 62.46%, versus 23.04% among the 204 runs that never used it. This is not a causal estimate—the no-document cohort contains harder and loopier cases—but 141 of the 367 completed wrong runs never opened a full document. The model is too often scanning many truncated snippets instead of verifying its best candidates.

### 7.3 Stagnation and loops

- 133 runs made at least one exact duplicate search blocked by the harness.
- 145 runs repeated an identical reasoning block.
- There were 1,023 blocked duplicate searches and 1,037 repeated-reasoning events.
- The 128 budget runs alone account for 982 blocked duplicate searches.

The current duplicate response says to use earlier results or issue a different query, but the model can repeat the same thought and tool call many times. A passive error message is insufficient for this adapter.

### 7.4 Forced-final behavior

After 24 calls the harness sets `tool_choice: none` and disables thinking, but a blank, malformed, or truncated response ends the run. There is no second, minimal emergency finalizer. This caused most of the 21 budget-related incomplete records and contributed to refusals.

### 7.5 Calibration

The model's confidence is not reliable enough for autonomous stopping:

- 179 completed wrong answers claimed at least 80% confidence;
- 96 wrong answers claimed at least 90%;
- 16 wrong answers claimed 100%;
- reported calibration error was 25.76%.

Any stopping rule should use search progress/evidence signals, not the model's stated confidence alone.

### 7.6 Voluntary final answers before the tool ceiling

The 24-call setting is a ceiling, not a quota. Before the ceiling,
`chat_client.py` sends `tool_choice="auto"`. If the model chooses to emit no tool
call and supplies a syntactically valid `Exact Answer`/`Confidence` response with
`finish_reason="stop"`, the harness immediately records the run as completed.
There is no evidence-sufficiency test, constraint-coverage check, or verifier turn.

The run contains the following early-final cohorts:

| Cohort | Runs | Correct | Wrong | Mean calls used | Unused call capacity |
|---|---:|---:|---:|---:|---:|
| all completed before 24 | 698 | 420 | 278 | — | — |
| explicit no-answer/refusal before 24 | 73 | 0 | 73 | 16.49 | 548 |
| candidate answer with an evidence-gap warning | 176 | 106 | 70 | 14.60 | 1,655 |

The evidence-gap cohort is a broad, reproducible phrase-based screen over the
final reasoning and response (for example, “couldn't find,” “unable to verify,”
or “insufficient information”). It is a review queue, not a claim that all 176
responses were irrational: 106 were correct, and a model can fail to verify a
peripheral clue while still uniquely identifying the answer. A rule that blindly
forces every such run to spend all 24 calls would waste work and could damage
correct answers.

The 70 wrong caveated candidates nevertheless expose a real stopping failure:

- 32/70 had at least 50% qrel evidence recall;
- 10/70 had 100% qrel evidence recall;
- 27/70 had the literal normalized gold answer in tool output;
- 15/70 never called `get_document`;
- 50/70 stopped after 10–19 calls, with 5–14 calls still available.

The 73 early refusals are even more directly recoverable: 26 had at least 50%
evidence recall and 16 had already received the literal gold answer before saying
it could not determine an answer. Their qids, along with all caveated candidates,
are in `early_stop_review.csv`.

For the user's concrete “about 13 searches” observation, 57 completed runs made
exactly 13 search calls. Nine ended with an explicit no-answer and 20 supplied a
caveated candidate; seven of those 20 candidates were judged wrong. The 29
flagged qids are `791, 847, 223, 231, 240, 980, 1003, 1019, 1030, 1043, 499,
560, 285, 310, 320, 330, 347, 353, 364, 53, 642, 650, 1192, 1198, 1239, 688,
714, 739, 428`.

This behavior has several interacting causes:

1. **The harness delegates stopping entirely to the model.** `tool_choice="auto"`
   makes “search again” and “answer now” sibling next-token choices, and any
   well-formed final is terminal.
2. **The benchmark prompt is permissive.** It says tools *may* be used multiple
   times, but never defines material versus peripheral constraints, sufficient
   evidence, a candidate-verification step, or what to do when evidence is
   incomplete but budget remains.
3. **The fine-tuning data teaches compressed synthesis as well as persistence.**
   The 94 `decision_early/middle/late` views end in tool calls and therefore do
   reinforce continued research. However, each of the 94 `answer_synthesis` views
   selects only the first tool result containing the gold-answer string (plus at
   most the preceding pair) and then appends the final answer; it does not test
   whether every identity-critical constraint has been verified. The 20 recovered
   rows similarly synthesize from one evidence block. Of 208 research rows ending
   in a final answer, 114 are these compressed/derived finals. This can strengthen
   “a plausible candidate appeared, so answer now” without teaching a robust
   stopping criterion.
4. **Search stagnation creates a rational-looking exit pressure.** Later searches
   return mostly previously seen documents, so the model observes little marginal
   information and concludes that additional search is futile even when the right
   move is a better discriminating query or opening a candidate document.
5. **Stochastic decoding and limited planning contribute.** The run used
   temperature 0.6. On a 9B model, the tool/final decision can vary, and long noisy
   histories make constraint tracking harder.

The best fix is a small stateful stopping controller, not a minimum-call rule:

1. Track a compact constraint ledger per qid: `verified`, `contradicted`, and
   `unverified`, with supporting docids and the current candidate.
2. Tell the model its remaining productive tool budget after each call. Require it
   to name the next *discriminating* query when an identity-critical constraint is
   unverified.
3. Intercept an early refusal whenever at least 3–4 productive calls remain and
   issue one deterministic recovery turn with tools enabled.
4. Intercept a caveated candidate only when the missing item is material—for
   example, it distinguishes two candidates—or when the candidate has no direct
   supporting document. Ask for one targeted verification search or
   `get_document`, then permit finalization. Do not trigger on warning phrases
   alone.
5. If a plausible answer appears only in a snippet and has not been opened, require
   one `get_document` verification before finalization when budget allows.
6. Combine this with novelty/stagnation metadata. After two low-novelty searches,
   require a candidate comparison and query rewrite; do not merely add more calls.
7. A/B test temperature 0 or 0.2. This will not create reasoning ability, but it
   will make stopping behavior and comparisons more stable.

Retraining can help later, but it is not the only or first remedy. A better dataset
should include paired trajectories where “I have not verified X” is followed by a
targeted search, and contrasting positive examples where the model correctly stops
because all identity-critical constraints are satisfied. `answer_synthesis` rows
should be selected by constraint coverage, not merely by the first appearance of
the answer string. Preference training can then favor a discriminating tool call
over a premature final while also penalizing purposeless over-searching.

## 8. Concrete plan to gain 10–15 points

Reaching 62% requires 515 correct answers, **77 more than the raw 438**. Reaching 65% requires 540, **102 more**. After the conservative 21-answer judge correction, the remaining gap to 62% is 56 answers; after accepting all 12 probable equivalences too, it is 44.

The following workstreams can plausibly combine to close that gap. Their raw candidate counts overlap and must not be added mechanically.

### Priority 0 — make scoring and completion trustworthy

1. **Add deterministic semantic normalization before the LLM judge.** Ignore case, Unicode normalization/mojibake, punctuation/spacing, honorifics, middle-name expansion, legal suffixes, singular/plural inflection, and harmless location/category qualifiers. Maintain per-qid acceptable aliases for genuine historical/transliteration cases.
2. **Use a second adjudicator only for near matches.** If lexical similarity is high and the first judge says no, rejudge with a prompt explicitly asking whether both strings identify the same entity. Log both decisions.
3. **Separate incomplete runs from judge parse failures.** Preserve `run_status`, `judge_call_status`, and `judge_parse_status` independently.
4. **Guarantee a final attempt.** If the first forced-final response is blank/invalid/length-limited, make one temperature-0, tools-disabled, 256–512-token request containing only the question, the model's candidate notes/evidence summary, and the exact answer contract.
5. **Rerun the 25 unfinished qids** after the finalizer is fixed. At the completed-run base rate, 25 reruns would yield about 14 correct (+1.69 points), although these are harder-than-average cases, so that is an upper baseline rather than a promise.

### Priority 1 — stop wasting research budget

1. Add the seen-document novelty filter described above.
2. Treat blocked exact duplicate searches as zero-cost invalid actions; after two, inject a structured `stagnation` result listing already-tried query concepts.
3. Reserve budget explicitly: for example, at most 16–18 search calls, at least one candidate-verification `get_document` call when a plausible entity appears, and a protected synthesis round.
4. After three low-novelty searches or two reasoning repeats, require the model to produce a candidate table: candidate, supporting clues, contradicted clues, missing clue, next discriminating query. If no discriminating query exists, finalize the best candidate.
5. Cache full documents locally per qid so repeat access does not hit the remote server, while still returning a compact “already available at earlier turn” reference.

### Priority 1 — improve retrieval quality, not only diversity

1. Run offline recall@10/20/50 by qid and inspect the 50 zero-recall cases first.
2. Over-fetch fused BM25+dense candidates, deduplicate, then rerank a diversified pool. Add MMR or source/domain diversity so near-identical pages do not occupy most of the top ten.
3. Tune BM25/dense/RRF and cross-encoder settings on a held-out development split, never the protected benchmark answers.
4. Improve query planning: search rare clue phrases first, then entity pivots, dates, and source-specific terms. Several traces searched broad biographies repeatedly rather than the most discriminating phrase.
5. Preserve longer/high-signal titles safely and clean empty/placeholder snippets; blank and boilerplate results consume slots.

### Priority 2 — isolate the model/LoRA ceiling

1. Build a fixed 100-query diagnostic set stratified into zero recall, low recall, high-recall wrong, duplicate-loop, no-document, and judge-near-match cases.
2. Run a factorial A/B with identical retrieval and deterministic decoding: base Qwen3.5-9B versus Atom Electron LoRA, and current retrieval versus novelty-filtered retrieval.
3. Run an **oracle-evidence test**: provide the model the qrel documents for the high-recall/incorrect failures and ask only for synthesis. If it still fails, the bottleneck is model reasoning/training; if it succeeds, retrieval ordering/context and harness policy are the primary problem.
4. Lower benchmark temperature from 0.6 to 0 or 0.2 for a controlled comparison. The current run's stochastic setting makes judge comparisons and repeated-run consistency harder.
5. Fine-tune on traces that demonstrate candidate elimination, mandatory document verification, progress-aware query diversification, stopping after stagnation, and always producing a final answer. Include negative examples of repeated tool calls and preferred corrected continuations.

## 9. Is this the model's cap?

Not yet proven.

The current 52.77% is depressed by at least 21 clear judge false negatives and 25 unfinished runs. Retrieval recall strongly predicts success, and result-list duplication is severe. Those are external to the model weights.

However, the model is also plainly part of the ceiling: it missed 46 fully recalled queries, missed 130 completed queries despite the literal gold answer appearing in tool output, repeated itself, and often refused at high nominal confidence. A stronger model may improve synthesis immediately, but changing models before fixing scoring/completion would make the comparison noisy.

My working expectation is:

- scoring/completion fixes: approximately +3 to +5 points in measured score;
- retrieval novelty plus retrieval-quality work: approximately +3 to +7 points;
- harness strategy/get-document/stagnation fixes: approximately +2 to +6 points;
- model/training/decoding improvements: required for the remaining difficult high-recall misses.

These ranges overlap. Together, a move into the low 60s is plausible; novelty filtering alone is not enough. If the oracle-evidence test remains near the current 67% on fully recalled cases, the 9B model/adapter becomes the principal constraint and a stronger base or better training becomes the next lever.

## 10. Reproducibility and derived artifacts

The analysis is reproducible with:

```powershell
.venv\Scripts\python.exe scripts_analysis\analyze_atom_electron_run.py
```

Generated artifacts in this directory:

- `summary.json` — machine-readable headline metrics and cohorts;
- `per_run_metrics.csv` — all 830 qids with status, correctness, tool use, recall, overlap, confidence, and file paths;
- `budget_exhausted_all.csv` — all 128 runs at 24 calls;
- `budget_exhausted_not_correct.csv` — all 110 not-correct budget runs;
- `incomplete_runs.csv` — all 25 unfinished records with last-event previews;
- `retrieval_overlap_by_search.csv` — every search call and its novel/repeated-document counts;
- `judge_equivalence_candidates.csv` — automatic 42-case lexical shortlist;
- `manual_judge_review.csv` — human-reviewed classification and rationale for all 42 candidates.
- `early_stop_review.csv` — all 73 early explicit no-answer finals and 176
  phrase-screened caveated candidates, with calls remaining, recall, answers, and
  final-reasoning previews.

Limitations: the overlap counter treats a document as “seen” after its snippet was returned, even if the model did not attend to it. Qrel recall measures retrieval of annotated evidence documents, not whether the snippet contained the decisive span. Correlations such as `get_document` usage versus accuracy are not causal because difficult questions change model behavior. Predicted score lifts therefore require controlled A/B runs.
