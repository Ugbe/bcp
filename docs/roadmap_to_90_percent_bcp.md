# From 57% to 90% on BrowseComp-Plus: a complete roadmap, including reinforcement learning from zero

Date: 2026-09-04
Scope: Qwen3.5-9B + Atom Electron LoRA, custom hybrid retriever, `search_agent/chat_client.py` harness, BrowseComp-Plus (830 questions)
Audience: the whole team, including people who have never trained a model or run reinforcement learning

---

## How to read this document

This is long on purpose. It is written so that someone with no background in machine learning can follow the reasoning and, with a colleague who can run Python, execute every step. It is organised in phases. Each phase says:

- **what** to do,
- **why** it is expected to help, with the evidence from your own run and from the BrowseComp-Plus paper,
- **how** to do it, step by step, broken into sub-steps,
- **what it costs** in time, money, and hardware,
- **how you will know** whether it worked.

Terms in **bold** on first use are defined in the glossary in Appendix A. If a paragraph feels too technical, skip to the next "In plain words" box; every technical section has one.

The single most important message is in Part 1.6, so read that even if you read nothing else. The RL textbook you asked for is Part 7. The "what if not RL yet" answer is Parts 3 to 6.

---

## Part 0. Summary of recommendations

1. **Fix the scoreboard before optimising against it (Phase 0).** Report on the strict clean split (704 questions that were never in training), keep the frozen 100-question failure set, add a synthetic development set, and run the oracle-evidence test so you know the model's own ceiling. Everything after this is measured against these numbers.

2. **Change how evidence reaches the model at test time (Phase 1, no training).** Your audit shows the answer was literally on screen in 130 wrong runs. A 9B model cannot reliably reason over 40,000 tokens of raw snippets. Replace raw snippets in the working context with structured evidence notes, add a fresh-context final answering step, and run several independent research attempts per question with evidence pooling and voting. This is the fastest path to the high 60s or low 70s and it costs no training.

3. **Build a question factory (Phase 2).** You cannot train on the 830 benchmark questions (that is cheating, and you have already done it for 126 of them). You need thousands of BrowseComp-style questions with verified answers, generated from your own 100k-document corpus. Everything that follows depends on this.

4. **Distil a strong teacher into the 9B (Phase 3).** Your current adapter was trained on 114 trajectories that the 9B model produced itself. It taught format and identity, not skill. Generating several thousand correct trajectories from a much stronger model and fine-tuning on them is the largest single training gain available and is a prerequisite for successful RL.

5. **Then, and only then, run GRPO reinforcement learning (Phase 4).** RL is the right tool for persistence and for "use the document in front of you", because it rewards outcomes rather than imitation. But RL can only amplify behaviours that already happen sometimes. Do it after Phase 3, on Phase 2 questions, using your existing harness as the environment. Part 7 explains it from zero.

6. **Be prepared to change the base model (Phase 5).** The honest calibration in Part 1.6 says that 90-91% end-to-end with a 9B model would exceed the best published frontier system by about 20 points. The plan above can plausibly take a 9B into the 70s. Reaching 90 most likely requires a larger base model running the same pipeline, or accepting much heavier test-time compute, or both.

---

## Part 1. Where you actually stand

### 1.1 What the published numbers say

The BrowseComp-Plus paper (Chen et al., 2025, `BrowseCompPlus_paper.pdf` in this repository) evaluated many systems on the same 830 questions and the same corpus. The relevant reference points, copied from Table 1 and Section 4.8.1 of the paper:

| System | Retriever | Accuracy |
|---|---|---|
| GPT-5 (high reasoning) | Qwen3-Embedding-8B, k=5 | 70.1% |
| GPT-5 | Google Search API (live web) | 59.9% |
| o3 | Qwen3-Embedding-8B | 63.5% |
| gpt-oss-120B (high) | Qwen3-Embedding-8B | 42.9% |
| Claude Opus 4 | Qwen3-Embedding-8B | 36.1% |
| Qwen3-32B | Qwen3-Embedding-8B | 10.4% |
| Search-R1-32B (an RL-trained search agent) | Qwen3-Embedding-8B | 10.4% |
| gpt-4.1, **oracle** (given all evidence documents directly) | none | 93.5% |
| Qwen3-32B, **oracle** | none | 83.3% |

Two things matter here.

First, **the best end-to-end published result in the paper is 70.1%**, from GPT-5, the strongest reasoning model available at the time, with 21.7 search calls per question. Later leaderboard entries have improved on this, so check the live leaderboard linked from the README before you set expectations, but the order of magnitude is clear: 90% end-to-end is beyond anything a frontier model achieved in the paper.

Second, **the oracle setting shows what perfect retrieval buys.** When the model is simply handed the human-labelled evidence documents, a non-reasoning model reaches 93.5% and a 32B open model reaches 83.3%. The paper's conclusion is that open models are not far behind proprietary models at reading evidence; the gap is in *interleaved searching and reasoning*, which is exactly your problem.

So the target of 90-91% is, in effect, "reach oracle-level accuracy while finding the evidence yourself, with a 9B model". That framing is what the rest of this document is built around.

### 1.2 Your score, decomposed into two problems

Your per-query audit table (`analysis/atom-electron-1.3-9b-830-audit/per_run_metrics.csv`) records, for each question, whether the literal gold answer string ever appeared in any tool output during the run. Grouping the 830 runs by that flag gives the most useful single picture of where you lose points:

| Did the gold answer string appear in any tool output? | Runs | Correct | Accuracy |
|---|---:|---:|---:|
| Yes (the answer was "on screen" at some point) | 509 | 371 | 72.9% |
| No (the answer never surfaced) | 321 | 67 | 20.9% |
| All | 830 | 438 | 52.8% |

(The "no" group is not 0% because the flag is a literal string match; the model sometimes produced the right answer from a document that phrased it differently.)

Think of the final score as the product of two skills:

> **Accuracy ≈ P(surface the answer) × P(convert it into the final answer | surfaced) + a small remainder**

Today: 61% of questions surface the answer, and 73% of those are converted. To reach 90% you need something like **95% surfacing and 93% conversion**. Both halves have to become nearly perfect. This has a direct consequence for your plan:

- The "persistence and reasoning" problem you describe (the model has the golden document and still fails) is the **conversion** half. Fixing it completely, with today's surfacing rate, would take you from 53% to about **69%**. It is necessary and it is where RL and context engineering help most, but it is not sufficient.
- The **surfacing** half is retrieval quality plus the model's query strategy. It is worth roughly the same number of points. RL can improve query strategy, but it cannot make the retriever return a document that the retriever cannot rank.

Your audit already shows the retrieval-side signal: the 50 zero-recall queries scored 0%, and the 232 low-recall queries scored 37.5%.

### 1.3 Your benchmark number is partly contaminated

The training corpus `data/training_multitask_v3/final_multitask_train.jsonl` and its validation file contain derived views of **126 BrowseComp-Plus questions** (94 full trajectories, 20 recovered trajectories, and 12 validation parents). Those 126 questions are also in the 830-question benchmark you report. Splitting your audited run by that flag:

| Group | Runs | Accuracy |
|---|---:|---:|
| Questions used in training or validation | 126 | 60.3% |
| Questions never used in training | 704 | 51.4% |

The contamination inflates the headline by about 1.3 points on this run. That is small, but it will grow if you keep training on benchmark questions, and it makes any number you publish indefensible. `scripts_evaluation/build_uncontaminated_split.py` already builds the strict split. **From now on, every score you compare should be on the 704-question strict split**, and no future training set, whether SFT, DPO, or RL, may contain any of the 830 questions or paraphrases of them. Part 5 explains how to get training questions without touching the benchmark.

### 1.4 What your fine-tuning actually taught the model

All 7,910 trace files in `artifacts/identity_trace_archive/all_batches_1_to_36` were produced by `Qwen/Qwen3.5-9B` itself. The cleaning pipeline kept only the trajectories whose final answer matched the gold answer, which left 94 usable trajectories plus 20 minimal recovered ones. The adapter was trained for one epoch at LoRA rank 16 on 710 rows, of which 470 are five different "views" of the same 94 trajectories.

In machine learning terms this is **self-distillation by rejection sampling on 114 examples**. It can teach the tool-call format, the answer contract, and the identity, and it evidently did. It cannot teach the model reasoning it did not already have, because every example was written by the same 9B model. The AGENTS.md history confirms the adapter's job was mainly to make the model *complete* research instead of stalling.

This is good news. It means **nothing about the model's reasoning ceiling has been tested yet.** The paper's oracle result for Qwen3-32B (83%) suggests a Qwen3.5-9B given clean evidence should be somewhere in the 70s, well above the 67% you measured on fully-recalled queries with noisy 100k-token contexts.

### 1.5 Why the harness fixes gave only about four points

The audit's recommendations (novelty filtering, stagnation guidance, early-final guard, emergency finaliser, judge normalisation, compaction) are all **guardrails**: they stop the model from wasting budget or being scored wrongly. They do not change what the model does with evidence when it has it. The audit itself predicted this ("a 10-15-point lift from novelty filtering alone is unlikely"). Guardrails bought the cheap points. The remaining points require changing either the information the model sees (Phase 1), or the model (Phases 3 to 5).

### 1.6 The honest answer about 90-91%

You asked for the full truth, so here it is in three sentences.

1. With the plan in this document, a Qwen3.5-9B system can plausibly reach the **high 60s to mid 70s** on the strict split, with the largest gains coming from context engineering, parallel rollouts, teacher distillation, and then RL, in that order.
2. **90-91% end-to-end with a 9B model has, to my knowledge, no precedent** and would beat GPT-5's published 70.1% by twenty points. It is not impossible in principle (the oracle ceiling for small open models is in the 80s), but you should plan for it to require a larger base model running the same pipeline (Phase 5), and heavy test-time compute (many rollouts per question).
3. Whatever the model size, **the pipeline is the same**: clean measurement, evidence-centric harness, synthetic question factory, distillation, RL. Everything you build for the 9B transfers to a larger model unchanged, including the identity adapter.

I recommend the team adopt an explicit intermediate target of **75% on the strict split with the 9B**, and treat the decision to scale the base model as a planned checkpoint rather than a failure.

---

## Part 2. The plan at a glance

| Phase | What | Training? | Expected gain (strict split) | Rough cost | Time |
|---|---|---|---|---|---|
| 0 | Trustworthy measurement | No | 0 (but stops you fooling yourselves) | Judge API calls only | 1 week |
| 1 | Evidence notes, fresh-context answerer, parallel rollouts with pooling, retrieval fixes | No | +8 to +18 | GPU rental for reruns | 3 to 5 weeks |
| 2 | Synthetic question factory (5k to 20k questions) | No | 0 directly; enables 3 and 4 | $300 to $3,000 in LLM calls | 2 to 4 weeks, overlaps with 1 |
| 3 | Teacher distillation SFT, then rejection-sampling loops and step-level DPO | Yes (SFT) | +5 to +12 | $1,000 to $10,000 teacher calls, 1 GPU for training | 3 to 5 weeks |
| 4 | GRPO reinforcement learning | Yes (RL) | +3 to +10 on top of Phase 3 | 2 to 8 GPUs for 1 to 3 weeks | 4 to 8 weeks including learning curve |
| 5 | Larger base model through the same pipeline | Yes | +5 to +15 | Bigger GPUs | 2 to 4 weeks per model |

Gains overlap and do not add linearly. The ranges are deliberately wide; the only way to narrow them is to run Phase 0 and Phase 1.

The order matters:

- Phase 1 before Phase 3 because it changes the *format* of the trajectories you will train on. Do not distil 5,000 trajectories in the old raw-snippet format and then change the harness.
- Phase 2 before Phase 3 and Phase 4 because both need thousands of verified questions.
- Phase 3 before Phase 4 because RL needs a policy that already succeeds sometimes on hard questions; otherwise there is no reward signal to learn from (Part 7.8 explains why).

---

## Part 3. Phase 0: make measurement trustworthy

> **In plain words:** before you try to improve a number, make sure the number is real, that you can measure it cheaply and repeatedly, and that you have a way to tell *which part* of the system improved.

### 3.1 Report on the strict split only

**What.** Run `scripts_evaluation/build_uncontaminated_split.py` to produce the strict TSV that excludes the 126 training and validation parents. Recompute the audited run on that split (you already have the per-run records; the analyzer just needs a filter). Put that number at the top of the dashboard.

**Why.** See Part 1.3. Any improvement you measure on the contaminated questions is partly memorisation. Everyone outside the team will ask this question first.

**How.**
1. Run the split builder with the v3 train and validation files as inputs.
2. Check the output directory `topics-qrels/splits/atom-electron-1.3-9b/` contains the strict query TSV and the excluded-ID list; it should have 704 rows.
3. Add a `--query_file` pointing at the strict TSV to the evaluator invocation for every future run.
4. Keep the full 830 run for the leaderboard submission format, but label it as containing training parents until you retrain on a clean set.

### 3.2 Freeze the judge

**What.** Keep the deterministic normalisation layer and the second adjudicator prompt that were added to `scripts_evaluation/evaluate_with_azure.py`, and never change the judge again mid-experiment. Version it. If you change it, re-judge every historical run you intend to compare.

**Why.** The audit found 21 clear false negatives out of 830 (2.5 points). Judge noise of that size can hide or fake a real change. The official BrowseComp-Plus leaderboard uses Qwen3-32B as judge, so before submitting, run the official `scripts_evaluation/evaluate_run.py` too and report both.

### 3.3 Build a development set that is not the benchmark

**What.** Once Phase 2 (the question factory) produces synthetic questions, hold out 300 of them as a **dev set** and 300 as a **synthetic test set**. Tune prompts, harness parameters, and training decisions on the dev set. Look at the synthetic test set only when you would otherwise look at the benchmark.

**Why.** Right now every prompt tweak and every threshold in the harness was chosen while looking at benchmark results. That is a slow form of overfitting to the test set. It also means you cannot run a benchmark every time you want to test an idea: 830 questions at 14 searches each at 4.5 seconds per search is about 15 hours of retrieval time. A 300-question dev set with a fast retrieval configuration (Part 7.10.3) gives you an answer in an hour.

### 3.4 Run the oracle-evidence test

**What.** For the 704 strict questions, take the qrel evidence documents from `topics-qrels/qrel_evidence.txt`, put their full text in the prompt (no tools), and ask the model for the answer in the usual contract. Score it. Do this for (a) the base Qwen3.5-9B, (b) the Atom Electron adapter, and (c) with thinking enabled and disabled.

**Why.** This is the single most informative experiment you can run this week. It measures the *conversion* ceiling with perfect surfacing. The paper's numbers (93.5% for gpt-4.1, 83.3% for Qwen3-32B) are the comparison. If the 9B scores, say, 75%, then you know that even perfect retrieval plus perfect persistence tops out at 75% with this model, and that model quality (Phases 3 to 5) is on the critical path. If it scores 85%, the model is fine and the harness is the problem. Either result tells you where to spend money.

**How.**
1. Write a small script that, for each strict qid, collects its evidence docids from the qrel file, fetches full text via the retrieval service's `/get_document`, concatenates them with docid headers, and truncates each document to a fixed budget (start with 6,000 tokens per document; the paper notes that 6% of Qwen3-32B oracle failures were context overflow, so log when you truncate).
2. Send one chat completion per question with the `QUERY_TEMPLATE` contract but no tools, `temperature 0`, `max_tokens` large enough for thinking (16,000 or more).
3. Judge with the frozen judge. Record the accuracy and the failure list.
4. Read 30 of the failures by hand. Classify each as: (i) answer present but model chose another candidate, (ii) answer present but model refused, (iii) answer requires a fact not in the evidence, (iv) format or truncation. This classification is what you will use later to design evidence notes (Part 4.1) and the reward (Part 7.7).

### 3.5 Keep the frozen 100-question failure set, and add a 100-question "pass" set

`topics-qrels/splits/atom_electron_failure_stratified_100.tsv` already exists. Add 100 questions the current system gets right, sampled across tool-call counts. Every change must be evaluated on both: failures tell you what you fixed, passes tell you what you broke. Note that both sets are benchmark questions, so they are for *diagnosis*, never for training, and never for reporting.

### 3.6 One dashboard number per component

Add these columns to the per-run metrics and the dashboard so you can see which half of the product in Part 1.2 moved:

- surfacing rate: fraction of questions where the gold string (or any gold docid) appeared in tool output;
- conversion rate: accuracy among surfaced questions;
- evidence recall at the end of the run (already there);
- mean productive tool calls, mean `get_document` calls;
- refusal rate, incomplete rate;
- accuracy on the strict split, the failure-100, the pass-100, and the synthetic dev set.

---

## Part 4. Phase 1: change what the model sees at test time (no training)

> **In plain words:** the model is failing to use evidence that is physically in its context. Before teaching it to reason better, make the evidence easier to use. This is called **context engineering**. Then, because a single research attempt is a noisy process, run several attempts and combine them.

Everything in this part uses the current model and adapter. It is also the harness you will later use as the RL environment, so build it cleanly.

### 4.1 Evidence notes instead of raw snippets

**The problem.** After each search the harness appends ten snippets of up to 256 tokens each. A run with 14 searches and one document fetch carries roughly 35,000 to 45,000 tokens of retrieved text, most of it irrelevant. The question typically has five to eight constraints. The model must keep all constraints in mind, notice that a snippet buried in search seven satisfies constraint four, and remember that when it finalises after search fourteen. A 9B model does this unreliably. This is the mechanism behind "the gold answer was in the tool output and the model still missed it".

**The fix.** Separate *reading* from *planning*. After every tool call, before the result is appended to the conversation, run a **side call** (a separate, short model request with no conversation history) that reads the raw result together with the question and produces a compact structured note. Only the note goes into the conversation. The raw text is kept in the run record for the audit and stays available through `get_document`.

**What the note contains.** Keep it rigid so the model learns its shape:

```
EVIDENCE NOTE for search #7 (query: "...")
Constraints in question: C1 poet lost two family members in a pandemic 2005-2023; C2 interview before 2015 says never read to as child; ...
Docs worth attention:
- [docid 48122] Title... — supports C1, C4 (quote: "..."); candidate entity: Jane Doe
- [docid 9031] Title... — mentions C2 for a different person (Mary Roe); conflicts with candidate
Docs irrelevant: 8 (not listed)
Candidates so far: Jane Doe (C1, C4 supported; C2, C3 unverified)
Suggested next action: get_document 48122 to check C2; or search "Jane Doe interview read to as a child"
```

**Why this works.** The side call has a short, clean context (question plus one result), which is where a 9B model is reliable. The main conversation shrinks by roughly ten times, so the planning turns operate on a few thousand tokens of structured facts. The constraint checklist is restated every turn, which is what the audit's "constraint ledger" prompt tried to make the model do internally; here the harness does it explicitly.

**How to build it.**
1. In `chat_client.py`, after `execute_tool` returns a search or document result, call a new function `summarize_result(question, tool_name, arguments, raw_result, ledger_so_far)`.
2. Implement it as a chat completion with thinking disabled, temperature 0, `max_tokens` 700, a fixed system prompt that contains the note template above, and no tools. Use the same model endpoint; it is a second request, not a second model.
3. Append the note as the tool message content. Store the raw result in the message under a private key such as `_raw_tool_output`, and strip it before sending to the API (the same pattern the compaction code uses with `_compacted_out`).
4. Maintain a running "candidate table" in `ResearchState`: for each candidate entity, which constraints are supported, contradicted, or unverified, with docids. Update it from each note (the note can emit a small JSON block for this). Re-inject the table as a short system-style message every three tool calls.
5. Keep the original raw-snippet mode behind a flag for A/B testing.
6. Cost: one extra short model call per tool call, roughly 1,000 input tokens and 400 output tokens. That is less than the tokens saved on subsequent turns.

**Variants to test after the basic version works.**
- Ask the summariser for the *quote* that supports each constraint. Quotes make the later verification step much more reliable than paraphrase.
- The "IterResearch" pattern from Tongyi DeepResearch: each turn the model sees only the question, the evolving report, and the last tool result; older results are dropped entirely. Your compaction controller already does a coarse version of this. Evidence notes make full per-turn reconstruction feasible.

### 4.2 The fresh-context final answerer

**The problem.** The final answer is currently produced by the same long conversation that did the searching. By then the context is at its noisiest and the model has been "primed" by its own earlier guesses.

**The fix.** When the research loop ends (voluntarily, by budget, or by the guard), do not accept the conversation's final answer directly. Instead, build a **fresh** prompt containing only: the question, the candidate table, all evidence notes, and the full text (or the relevant quotes) of every document the model opened, plus the top three snippets by constraint coverage. Ask for the answer in the usual contract with thinking enabled. Use *this* answer as the final answer.

**Why.** This is the oracle-evidence experiment from Part 3.4 applied to self-gathered evidence. The audit shows 46 wrong runs with 100% evidence recall and 130 wrong runs with the gold string on screen. A fresh reading of the gathered evidence, without the noise, should convert a good fraction of those.

**How.**
1. Add a `final_answer_stage(question, state, messages)` function called from `finish()` in the harness whenever the status is `completed` or the emergency finaliser is triggered.
2. Cap total prompt at, say, 24,000 tokens, prioritising opened documents and notes marked as supporting the leading candidate.
3. Record both answers (conversation final and fresh final) so you can measure agreement and which one is right more often. Start by using the fresh one only when the two disagree *and* the fresh one cites a document; then simplify once you have data.

### 4.3 Parallel rollouts with evidence pooling and voting

**The idea.** A research run is a random process (temperature 0.6, many decision points). Running it once gives you one sample. Running it N times gives you N samples, and there are two ways to combine them that are both known to work well on BrowseComp-style tasks:

- **Answer voting.** Normalise the N final answers and pick the most frequent. Ties are broken by the summed stated confidence, or by a verifier call. This is "self-consistency".
- **Evidence pooling.** Take the union of every document and note gathered by all N runs, and give it to the fresh-context answerer from Part 4.2. Because different runs search differently, the pooled evidence recall is much higher than any single run's. This moves you towards the oracle setting, where the paper shows 83 to 93%.

Use both: pool the evidence, run the fresh answerer on the pool, then vote among the N individual finals plus the pooled final, with the pooled final weighted double.

**Why.** Everything in Part 1.2 says you must raise surfacing and conversion at once. Pooling raises surfacing (union of N searches), voting raises conversion (averages out one-off reasoning slips). The original BrowseComp paper reports large gains from aggregating many samples for exactly this reason.

**Cost.** Linear in N. With N=4 you need four times the retrieval and model time you use today. This is why Part 7.10.3 (fast retrieval) is worth building now rather than later: it is needed for Phase 1, for the dev set, and for RL.

**How.**
1. Write `research_ensemble.py` that, for one question, launches N independent `run_conversation_with_tools` calls with different seeds and temperatures (for example three at 0.6 and one at 0.2), collects their run records, and performs pooling and voting.
2. Store all N records plus the ensemble decision in one JSON so the evaluator and dashboard treat the ensemble as a single run and report total tool calls honestly (the leaderboard asks for average search calls; report the sum across the N runs).
3. Measure on the dev set with N in {1, 2, 4, 8}. Expect steep gains from 1 to 4 and diminishing returns after.

### 4.4 Retrieval: raise the surfacing rate

Your retriever is already strong (hybrid BM25 + dense + RRF + Qwen3 cross-encoder cascade). The remaining losses are mostly in how the *agent* uses it. Four targeted changes, in order of expected value:

**4.4.1 Deep-pool scan for the whole question.** Table 2 in the paper shows that for the full question as the query, a Qwen3-Embedding-8B retriever has evidence Recall@5 of only 14.5% but Recall@100 of 47.7% and Recall@1000 of 76.7%. The evidence is often in the pool; it is just not in the top ten. Add a tool, or an automatic first step, that retrieves the top 100 for the full question and has the model scan them in batches of 20 titles plus first sentences, marking candidates. With evidence notes (Part 4.1), scanning 100 short entries costs about 4,000 tokens and directly attacks the 50 zero-recall and 232 low-recall questions.

**4.4.2 Multi-query search.** Let the `search` tool accept a list of two to four queries, run them all, fuse with RRF, and return ten novel documents. One tool call then explores several phrasings, which is what strong agents do with several sequential calls. Combine with the novelty filter you already have.

**4.4.3 Constraint-first query planning.** In the system prompt and in the evidence-note "suggested next action", require that the first three searches each target a *different* rare constraint (an unusual phrase, a number, a date range, a place), not the whole question. The audit noted the model repeatedly searched broad biographies. Distillation and RL will later reinforce this; the prompt is the cheap version now.

**4.4.4 Audit the 50 zero-recall questions offline.** For each, run the retriever directly with the full question and with hand-written constraint queries, and record at what rank the evidence appears. If evidence never appears within the top 1,000 for any reasonable query, that question is a retriever ceiling and no agent change will fix it; count those so you know your true maximum. If it appears at rank 30 to 300, the deep-pool scan will get it.

### 4.5 Decoding and budget settings

- **Thinking budget.** You run with `max_tokens 4096` per turn. A reasoning model on a hard multi-constraint question often needs more than 4,096 tokens of thinking before its first action; several of the `incomplete_length` cases are exactly this. Raise per-turn `max_tokens` to 12,000 to 16,000 with the evidence-note harness (the context is smaller so you can afford it), and keep the thinking-disabled retry as a fallback.
- **Temperature.** Use 0.2 to 0.3 for single runs; use a spread (0.2, 0.6, 0.6, 0.8) for ensembles, where diversity is a feature.
- **Snippet length.** With evidence notes the model never sees raw snippets, so raise the server-side snippet cap back to 512 tokens and k to 10 or 15; the summariser benefits from more text.
- **Tool budget.** With notes and compaction, budget is no longer limited by context. Set the ceiling to 32 productive calls but keep the stagnation controller; the audit's finding that 24-call runs score 14% was about looping, not about the ceiling.

### 4.6 What to expect from Phase 1

Rough, overlapping estimates on the strict split, starting from about 55%:

| Change | Expected gain |
|---|---|
| Evidence notes plus candidate table | +3 to +7 |
| Fresh-context final answerer | +2 to +5 |
| Deep-pool scan and multi-query search | +2 to +5 |
| 4-way ensemble with pooling and voting | +4 to +8 |

Combined, the low-to-mid 60s is likely and the low 70s is possible. Run each as a separate A/B on the dev set and the failure-100 before combining. Keep the winning configuration frozen; it becomes the trajectory format for Phase 3 and the environment for Phase 4.

---

## Part 5. Phase 2: the question factory

> **In plain words:** every training method that follows needs thousands of hard questions with known, verifiable answers, answerable from your corpus, and *not* taken from the benchmark. You have 114. You need 5,000 to 20,000. You will make them with a strong language model, from your own documents, and check each one automatically.

### 5.1 Why you cannot skip this

- **SFT** (Phase 3) needs thousands of teacher trajectories, and every trajectory starts from a question.
- **RL** (Phase 4) learns only from questions where the reward varies between attempts. A few hundred questions get "used up" within a couple of hundred training steps.
- **The benchmark is off limits.** Training on the 830 questions, or on questions written by looking at them, is contamination. Every published RL search agent that reported honest BrowseComp numbers trained on synthetic data (WebSailor's SailorFog-QA, WebShaper, ASearcher, DeepDive, WebExplorer, and Tongyi DeepResearch's pipeline are the main public examples; they are worth reading for recipes).
- **Search-R1 is the cautionary tale.** It was RL-trained on easy questions (Natural Questions, HotpotQA) and scores 4 to 10% on BrowseComp-Plus. RL transfers the *difficulty distribution* it was trained on. Your questions must look like BrowseComp: five to eight interlocking constraints, an obfuscated entity, two to four documents needed, a short unique answer.

### 5.2 What a BrowseComp-style question is, structurally

Look at any benchmark question. It is built by starting from an answer entity, walking outwards through facts about it (a person, a work, a place, a date), and then *obfuscating* each fact so that no single fact is searchable on its own but the conjunction identifies exactly one entity. Example structure: "A [role] who did [vague event] between [year range]... this person's [relation] wrote [vague description]... What is the name of [target attribute]?"

The factory reverses this: choose an answer, gather facts from documents, obfuscate, verify uniqueness.

### 5.3 The pipeline, step by step

You will need: the corpus (100,195 documents, loadable from Hugging Face as in the README), your retrieval service, and API access to one strong generator model (any frontier model works; an open model like Qwen3.5-397B, DeepSeek, or gpt-oss-120b on a rented GPU also works and is cheaper at scale).

**Step 1. Build the allowed document pool.**
Exclude every docid that appears in `qrel_evidence.txt` or `qrel_golds.txt` (about 5,000 documents). Exclude documents that mention any benchmark gold answer string (normalise both sides). What remains, roughly 90,000 documents, is your seed pool. These are the "hard negative" documents the benchmark authors mined; they are topically similar to real questions, which is exactly what you want, and they are not the evidence for any benchmark question.

**Step 2. Pick a seed document and an anchor entity.**
Sample a document. Ask the generator to list named entities in it that are specific (a person, an organisation, a work, an event) and to pick one that has at least three distinctive facts in the document. That is the anchor.

**Step 3. Grow a chain of two to four documents.**
Search your retrieval service for the anchor entity and for its related entities. Keep only results from the allowed pool. Ask the generator to pick one or two documents that add *new* facts about the anchor or about an entity linked to it (a co-author, an employer, a place). Record the docids and the exact sentences used. Two hops is enough for most questions; three or four hops for a harder tier.

**Step 4. Choose the target attribute and write the question.**
The generator is instructed to: (a) choose a target attribute with a short, unique answer (a name, a title, a year, a number, a place); (b) write a question that describes the anchor through four to eight obfuscated constraints drawn from the recorded sentences, each constraint on its own insufficient; (c) never use the anchor's name or any name that appears in the answer; (d) output the question, the answer, the list of supporting docids, and for each constraint the docid and quote it came from. Give it three real BrowseComp questions as style examples, chosen from the *training* parents you already used, not from the strict split.

**Step 5. Verify automatically (this is what makes the data usable).**
Run these checks in order and drop the question if any fails:

1. **Grounding check.** Every quoted sentence must appear verbatim in the cited document.
2. **Answerability check.** Give a strong model the question plus the supporting documents (oracle mode) and ask it to answer. Must match the intended answer. Run it twice with different models if you can afford it.
3. **Closed-book check.** Ask a strong model the question with *no* documents. If it answers correctly, the question is too easy or relies on famous facts; drop it or add constraints.
4. **Uniqueness check.** Ask the strong model, given the documents, whether any *other* entity in the documents also satisfies all constraints. Drop if yes.
5. **Retrievability check.** Run your retriever with the full question and with each constraint; at least one supporting document must appear in the top 100 for some query. Otherwise the question is unsolvable in your environment and will only produce zero rewards.
6. **Benchmark overlap check.** Drop if the answer string, the anchor entity, or any supporting docid overlaps with a benchmark gold answer, evidence docid, or gold docid. Also compute embedding similarity between the new question and every benchmark question and drop anything above a conservative threshold (for example cosine similarity above 0.85 with a Qwen3-Embedding model); log the near misses for a human to skim.

Expect to keep 30 to 50% of generated questions. Generate 30,000 to end up with about 10,000.

**Step 6. Label difficulty with the student.**
Run the current 9B system (Phase 1 harness, single rollout) eight times on each surviving question. Record the pass rate. Bucket: trivial (8/8), learnable (1/8 to 7/8), unsolved (0/8). This labelling costs real compute (10,000 questions × 8 runs × about 12 searches), so do it with the fast retrieval configuration from Part 7.10.3 and spread it over days. The buckets drive both Phase 3 (teacher trajectories are most valuable on learnable and unsolved questions) and Phase 4 (GRPO trains on learnable questions; see Part 7.8).

**Step 7. Split and freeze.**
Hold out 300 as dev and 300 as synthetic test (Part 3.3), stratified by difficulty and by answer type. Freeze them. The remainder is the training pool. Store everything with a schema like:

```json
{"qid": "syn-000123", "question": "...", "answer": "...", "answer_aliases": ["..."],
 "support_docids": ["48122", "9031"], "constraints": [{"text": "...", "docid": "48122", "quote": "..."}],
 "hops": 2, "answer_type": "person", "student_pass_rate": 0.375, "generator": "model-name", "version": 1}
```

### 5.4 Cost and time

Per question: about 10,000 to 20,000 tokens of generation and verification. For 30,000 candidates that is 300 to 600 million tokens. With a frontier API that is a few thousand dollars; with an open model on a rented 8×H100 node for a few days it is a few hundred dollars. The student difficulty labelling is the larger cost in wall time, not money.

### 5.5 Hygiene rules that must never be broken

- Nobody looks at strict-split benchmark questions while writing generator prompts.
- Every generated question carries its `support_docids` so that later, if a contamination question arises, you can prove where it came from.
- Re-run the overlap check whenever the benchmark files change.
- Keep the generator version in the record; you will regenerate tiers later.

---

## Part 6. Phase 3: distillation and offline preference training

> **In plain words:** show the 9B model thousands of examples of a much stronger model solving these questions in your exact environment, then fine-tune it to imitate them. This is called **distillation**. It is the most reliable way to move a small model's skill, and it is what RL will later build on.

### 6.1 Why distillation, and why now

Your adapter learned from 114 trajectories written by itself. A strong teacher writes trajectories that demonstrate the things you want and cannot currently get: constraint-first query planning, opening documents to verify, comparing candidates, not giving up after a low-novelty search, and stopping only when the constraints are covered. Thousands of such examples change the model's default behaviour in a way that 114 self-generated ones cannot.

Every successful open search agent used this recipe: synthetic questions, teacher trajectories, SFT "cold start", then RL. Doing RL directly on the current model is possible but slower and riskier, because RL only strengthens behaviours that already occur (Part 7.8).

### 6.2 Choosing the teacher

Requirements, in order of importance:

1. **Solves your questions in your environment.** Run the candidate teacher through `chat_client.py` (with the Phase 1 harness) on 200 dev questions. You want at least 65 to 75% accuracy, or the filtered dataset will be small and biased to easy questions.
2. **Exposes its reasoning.** The student is a thinking model; training it on trajectories with reasoning is far more effective than on tool calls alone. Open-weight thinking models expose full reasoning: the larger Qwen3.5 models, DeepSeek's reasoning models, gpt-oss-120b, GLM's reasoning models, Kimi K2 Thinking. Most proprietary APIs return only summaries of reasoning or none. Same-family teachers (a large Qwen3.5) are ideal because the chat template, tool format, and thinking style already match the student.
3. **Cost.** Open-weight teachers on a rented multi-GPU node are the cheapest at 5,000-plus trajectories. Proprietary teachers can fill gaps on the hardest questions.

A practical mix: a large open Qwen3.5 as the primary teacher; a frontier API model as a second attempt on the questions the primary teacher fails.

### 6.3 Generating and filtering trajectories

1. For each training-pool question (prioritise learnable and unsolved buckets), run the teacher through the Phase 1 harness two to four times at temperature 0.6 to 0.8.
2. Keep a trajectory only if: the final answer matches the intended answer (normalised, then judge); it contains at least one `get_document`; it has no rejected duplicate calls; it has at most the tool ceiling; it cites at least one supporting docid; and it ends with the complete answer contract.
3. Prefer the *shortest correct* trajectory per question, but keep one longer correct one for 20% of questions so the student sees persistence. Also keep trajectories where the teacher recovered from a wrong candidate; those are gold for teaching comparison.
4. Deduplicate near-identical trajectories.
5. Target: 5,000 to 10,000 trajectories, average 15,000 to 25,000 tokens each with evidence notes. If you use raw snippets instead (not recommended), lengths double.

Do **not** use `data/decrypted_run_files/` (the paper's o3 and GPT-5 trajectories). They are on the benchmark questions.

### 6.4 Training format and the assistant-only-loss problem

Your current trainer works around an Unsloth limitation by turning each trajectory into "prompt/completion views" with only the last assistant message supervised. That multiplies rows and supervises only one turn per row. For thousands of long multi-turn trajectories you want **one row per trajectory with the loss on every assistant turn and on nothing else** (no loss on system, user, or tool-result tokens). This is the standard "assistant-only" or "loss-mask" setup.

Ways to get it:
- TRL's `SFTTrainer` with `assistant_only_loss=True` requires the chat template to carry generation markers; the Qwen3.5 template may or may not, and Unsloth's vision-language classification blocked it. Check whether current versions fixed this.
- Write your own collator: render the conversation with the tokenizer's chat template, then build the label mask by rendering the conversation prefix by prefix and marking the token spans that belong to assistant messages (including the thinking and the tool-call JSON). This is 50 lines of code and removes the dependency on library support. Verify by decoding the supervised tokens of a few examples and reading them.
- LLaMA-Factory and Axolotl both implement multi-turn masking for Qwen-family models and may already support Qwen3.5.

Keep the thinking content inside the assistant turns. Keep tool results as `tool` messages, unmasked (they are inputs). Keep the evidence-note format identical to the harness.

Mix in the 240 identity and control rows at 5 to 10% of the batches, exactly as v3 does, so the identity does not drift.

### 6.5 Hyperparameters and evaluation

| Setting | Recommendation | Why |
|---|---|---|
| Method | LoRA rank 64, alpha 128, all linear projections | Enough capacity for behaviour change; still one GPU |
| Sequence length | 32,768 (raise to 49,152 if trajectories exceed) | Match harness |
| Epochs | 2 to 3 over 5k-10k trajectories | 1 epoch was right for 710 rows; more data supports more passes |
| Learning rate | 1e-4 for LoRA (2e-5 was for full-model style caution) | Standard for LoRA SFT |
| Effective batch | 16 to 32 sequences (batch 1, gradient accumulation) | Stability |
| Warmup | 3% of steps, cosine decay | Standard |
| Packing | Off (long multi-turn sequences) | Avoid cross-example attention bugs |
| Hardware | 1×H100/H200 80-141 GB with gradient checkpointing | Fits 9B LoRA at 32k |

Evaluate every 500 steps on the synthetic dev set (single rollout, fast retrieval). Stop when dev accuracy plateaus. Then evaluate the best checkpoint on the failure-100, pass-100, identity controls, and finally the strict split with the full harness.

Expected: single-rollout strict-split accuracy up by 5 to 12 points over the current adapter, and a large drop in refusals, duplicate loops, and no-document runs.

### 6.6 Rejection-sampling fine-tuning: RL without the machinery

Once the distilled student exists, you can run the simplest possible RL-like loop, sometimes called expert iteration or RFT:

1. Sample the *student* 8 times per training question (learnable bucket).
2. Keep the correct trajectories (judge), with the same filters as 6.3.
3. Fine-tune on them (mixed with the teacher data).
4. Repeat two or three rounds.

This is on-policy in spirit (the model learns from its own successes), needs no new infrastructure, and typically gives a few points. It is also the perfect dress rehearsal for GRPO because it exercises the exact rollout, reward, and data pipeline.

### 6.7 Step-level DPO for persistence: the cheap "don't give up" fix

**Direct Preference Optimisation (DPO)** trains on pairs: for the same context, a preferred response and a dispreferred one. It needs no reward model and no online sampling; it is an SFT-like job. Your audit already suggested the pairs that matter:

| Context (shared prefix) | Preferred continuation | Dispreferred continuation |
|---|---|---|
| Research state with an unverified identity-critical constraint and budget left | A discriminating search or a `get_document` on the candidate | An early final or a refusal |
| A candidate appears only in a truncated snippet | `get_document` on that docid | Final answer from the snippet |
| Two low-novelty searches in a row | A materially different query or a candidate comparison | The same broad query again |
| All constraints verified | The final answer | Another search |

**How to mine the pairs.** Take student and teacher rollouts on the same questions. Align at decision points. Where a correct trajectory took action A and an incorrect one took action B from a similar state, build a pair. You can also construct pairs directly from the harness's diagnostics (`_early_final_rejected`, duplicate rejections) by using the rejected response as the dispreferred one and the eventual successful continuation as preferred. Aim for 2,000 to 5,000 pairs. Train with TRL's `DPOTrainer` on the distilled adapter with a low learning rate (5e-6 to 1e-5 for LoRA) and beta 0.1, one epoch. Check that identity and the answer contract are unchanged.

DPO is weaker than online RL because it cannot discover new behaviours, but it directly targets your two named failure modes and costs a day.

---

## Part 7. Phase 4: reinforcement learning with GRPO, from zero

This part is the textbook. Sections 7.1 to 7.5 are concepts; 7.6 to 7.11 are the practical process; 7.12 is expectations. Read the concepts once even if a colleague will do the engineering, because the decisions in 7.7 (reward) and 7.8 (data) are the ones that determine success, and they are not engineering decisions.

### 7.1 What reinforcement learning is, in your vocabulary

Supervised fine-tuning (what you have done so far) shows the model examples and says "produce this". Reinforcement learning (RL) instead lets the model *try*, scores the attempt, and nudges the model to make high-scoring attempts more likely. Nobody writes the ideal answer; the model finds it.

The vocabulary, mapped onto your problem:

| RL term | In your problem |
|---|---|
| **Agent** or **policy** | The Qwen3.5-9B model plus adapter. "Policy" just means "the thing that decides what to output next"; mathematically it is the probability distribution over next tokens given the context. |
| **Environment** | Everything outside the model that reacts to it: your harness loop, the `search` and `get_document` tools, the retrieval service, the duplicate blocker, the budget. |
| **State** or **observation** | The conversation so far: system prompt, question, previous thinking, tool calls, tool results (or evidence notes). |
| **Action** | Strictly, each generated token. In practice we think of one assistant turn (thinking plus a tool call, or the final answer) as the action. |
| **Episode**, **trajectory**, or **rollout** | One complete research run from question to final answer. Exactly what one `run_qid_*.json` records. |
| **Reward** | A number given at the end of the episode: 1 if the final answer is correct, 0 otherwise, plus small adjustments. |
| **Return** | Total reward over the episode. With a single end-of-episode reward, return equals reward. |
| **On-policy** | The trajectories used for learning were generated by the *current* version of the model. GRPO is on-policy. |
| **Exploration** | Sampling at temperature above zero so that different attempts differ, which is how the model discovers a better behaviour. |
| **Advantage** | How much better a particular trajectory was than what you expected for that question. Positive: reinforce it. Negative: suppress it. |
| **KL penalty** | A leash that keeps the RL-trained model from drifting too far from the model it started as, to avoid forgetting and degeneration. |

> **In plain words:** RL is trial, score, adjust. The model tries a question several times; the tries that got it right are made more likely, the tries that got it wrong are made less likely; repeat over thousands of questions.

### 7.2 Why SFT cannot teach persistence but RL can

Your two named failures are *persistence* (giving up, looping) and *evidence use* (not converting a document it has). Consider what SFT sees: a correct trajectory where the teacher searched seven times and answered. The student learns "after roughly this many searches, answer". It does not learn *why* the teacher stopped there, and it never sees the counterfactual where stopping early would have been wrong.

RL sees the counterfactual directly. On one question, attempt 1 refuses after twelve searches (reward 0), attempt 2 keeps going, opens a document, and answers correctly (reward 1). The update moves probability from the refusing behaviour toward the persisting one, *in that kind of state*. Over thousands of questions the model internalises "in a state like this, another targeted search pays off" and "in a state like that, I already have enough". No human wrote either rule. This is precisely why every strong search agent uses RL after SFT: the stopping policy and the verification habit are outcome-driven skills, and RL optimises outcomes.

The flip side: RL only strengthens what it sees rewarded. If the model *never* opens a document, there is no rewarded example of opening a document, and RL cannot invent it. That is why Phase 3 comes first, and why Part 7.8's "learnable zone" matters.

### 7.3 From REINFORCE to PPO to GRPO

You do not need to derive these, but you need to know what the knobs do.

**REINFORCE (the basic policy gradient).** For a sampled trajectory with reward R, increase the log-probability of every token the model generated, scaled by R. Since R is 0 or 1, this means "make the correct trajectories more likely". Two problems: with 0/1 rewards it only ever pushes up, never down, so it learns slowly; and the update size varies wildly between samples, which makes training unstable. The fix for the first problem is a **baseline**: use R minus the expected reward, so below-average tries get pushed down. The result is the advantage from the table above.

**PPO (Proximal Policy Optimisation).** Adds two stabilisers. First, a **clipped ratio**: instead of scaling by the raw log-probability change, it uses the ratio of the new policy's probability to the old policy's probability and clips it to [1 minus epsilon, 1 plus epsilon], typically epsilon 0.2, so no single update can move a token's probability too far. Second, a **value model** (a critic) that predicts the expected reward from any state, used as the baseline. The value model is a second copy of the network that must be trained; for a 9B model that doubles memory and adds a whole extra thing that can go wrong.

**GRPO (Group Relative Policy Optimisation).** The insight from DeepSeek's reasoning work: you do not need a value model if you sample a *group* of attempts for the same question. Sample G attempts (G is typically 8 to 16), score each, and use the group's mean reward as the baseline for that question:

```
advantage_i = (reward_i - mean(rewards in the group)) / std(rewards in the group)
```

Then apply the PPO clipped update to every generated token in trajectory i, scaled by advantage_i, and subtract a small KL penalty (beta times the divergence from the starting model). That is the whole algorithm.

Why it fits you:
- No value model: one 9B model in memory plus the optimiser, which is why LoRA GRPO fits on one or two GPUs.
- The group baseline is exactly "how well does the model usually do on *this* question", which handles the fact that some questions are much harder than others.
- Rewards are verifiable (correct or not), which is where GRPO has been most successful ("RL with verifiable rewards").

**The formula, for the record** (you will see it in every framework's documentation):

```
J(θ) = E[ (1/G) Σ_i  (1/|o_i|) Σ_t  min( r_{i,t} · A_i ,  clip(r_{i,t}, 1-ε, 1+ε) · A_i )  −  β · KL(π_θ || π_ref) ]

where  r_{i,t} = π_θ(o_{i,t} | context) / π_old(o_{i,t} | context)   (probability ratio for token t of attempt i)
       A_i     = group-normalised advantage of attempt i
       π_ref   = the frozen starting model (after SFT)
```

> **In plain words:** for each question, run the model eight times. Attempts that scored better than the group average get every one of their tokens nudged up; worse-than-average attempts get nudged down; the nudge per token is capped; and the model is kept close to where it started.

### 7.4 The modern GRPO recipe (the fixes everyone now uses)

Several 2025 papers (DAPO, Dr. GRPO, and others) found and fixed problems in vanilla GRPO. Frameworks expose these as options; turn them on:

| Fix | What it does | Why you want it |
|---|---|---|
| **Token-level loss** (instead of averaging per trajectory first) | Long and short trajectories contribute proportionally to their tokens | Prevents the model from learning that shorter is better or longer is better for spurious reasons |
| **No standard-deviation normalisation** (Dr. GRPO) or careful handling of it | Avoids amplifying tiny differences on questions where nearly all attempts agree | Stability |
| **Clip-higher** (asymmetric epsilon, e.g. 0.2 low, 0.28 high) | Lets low-probability tokens grow faster | Preserves exploration; fights "entropy collapse", where the model becomes deterministic and stops discovering |
| **Dynamic sampling / group filtering** | Drop groups where all G attempts got the same reward (all 0 or all 1) before the update, and sample more prompts to fill the batch | Those groups have zero advantage and contribute nothing but noise; this is also why Part 7.8 filters questions by pass rate |
| **Overlong penalty / soft length limit** | Gradually penalise trajectories approaching the token or tool limit instead of truncating them silently | Truncated trajectories with reward 0 teach the wrong lesson ("thinking long is bad") |
| **Small or zero KL** (beta 0 to 0.001) | Weaker leash | Modern practice for verifiable-reward RL; keep a small value at first because you also care about identity retention |
| **Loss masking of environment tokens** | Never compute loss on tool results | See 7.5; without it the model is trained to "predict" search results, which is nonsense |

### 7.5 What is different about agentic (multi-turn, tool-using) RL

Most GRPO tutorials train single-turn maths: prompt in, answer out. Yours is different in four ways, and each has an engineering consequence.

1. **The trajectory is interleaved.** Model tokens (thinking, tool calls, final) alternate with environment tokens (tool results or evidence notes). Only the model tokens are actions. The trainer needs a **loss mask** that is 1 on model tokens and 0 on environment tokens. All agentic RL frameworks do this, but you must verify it on your data by decoding masked tokens once.

2. **Episodes are long and slow.** A trajectory is 10 to 30 model turns, each waiting on a retrieval call of several seconds. Generation is dominated by environment latency, not by the model. This changes the infrastructure calculus (Part 7.10.3): the retrieval service, not the GPU, is your bottleneck.

3. **The environment must be reproducible and stateless per episode.** Each attempt needs a fresh `ResearchState`, its own duplicate blocker, its own novelty set. Your harness already does this per qid; you will call it per attempt.

4. **The environment is part of what is learned.** The policy adapts to *your* retriever's behaviour. If you train with a cheaper retriever and evaluate with the full cascade, expect a small transfer loss. Keep them as similar as you can (Part 7.10.3).

Also relevant: if you use the evidence-note side calls from Part 4.1, those side calls are made by the same model. During RL, treat them as part of the environment (do not train on them, use the frozen SFT model or the current policy at temperature 0), so that the trained turns are only the planning turns. This keeps the credit assignment clean.

### 7.6 What an "RL environment" is for your problem: you already have most of it

People imagine an RL environment as a game simulator. For you it is a Python function:

```python
async def run_episode(question: str, answer: str, policy_endpoint) -> Episode:
    """One research attempt. Returns the message list, the loss mask, and the reward."""
```

and `run_conversation_with_tools` in `chat_client.py` is 90% of that function. It already: builds the prompt, calls an OpenAI-compatible endpoint, executes tools, blocks duplicates, tracks budget, forces a final, and returns the messages. What is missing:

- it must accept a `client` that points at the trainer's inference server rather than a production vLLM (usually the same API shape);
- it must return the messages in the exact form the trainer's tokenizer expects, plus a way to identify which messages are assistant turns (for the loss mask);
- it must return per-episode diagnostics (tool calls, duplicates, refusals, budget exhaustion) so the reward function can use them;
- it must be safe to run hundreds of times concurrently (thread or async pool; retrieval rate limiting; retries);
- it should support a **fast mode** (Part 7.10.3).

This is a refactor, not a rewrite. Isolate the loop into an `environment.py` module with no CLI parsing and no file writing; the benchmark runner and the RL rollout both call it.

### 7.7 Reward design

The reward is the only place where you tell the model what you want. Everything else is plumbing. Start simple, add carefully, and expect the model to exploit any loophole (this is called **reward hacking**).

**Base reward (start here and run your first experiment with only this):**

```
reward = 1.0 if final answer is correct else 0.0
```

"Correct" means: deterministic normalised match against `answer` and `answer_aliases` first (the same normaliser as the evaluator); if no match, an LLM judge with the frozen judge prompt. Use a *different* model as judge than the policy (a local Qwen3-32B, or the Azure judge); a policy that judges itself will learn to write answers that fool itself.

**Format requirements (necessary, small):**
- final message must contain `Explanation`, `Exact Answer`, and `Confidence` fields, parseable: otherwise reward 0 regardless of content;
- exactly one exact answer; if the answer field contains "or", a list, or multiple candidates, reward 0. Without this rule the model learns to hedge ("A or B") and the judge may accept it.

**Behavioural shaping (add only after the base reward works, each one tested on its own):**

| Term | Value | Purpose | Hacking risk |
|---|---|---|---|
| Duplicate tool call rejected by the harness | −0.05 each, capped at −0.3 | Discourage loops | Low |
| Budget exhausted without a final answer | reward 0 (already), plus −0.1 | Discourage running out | Low |
| Explicit refusal ("cannot determine") | 0 | The model must learn a best guess beats a refusal; do not penalise below 0 or it learns never to say it does not know even when correct to | Low |
| Correct answer that cites a supporting docid in the explanation | +0.1 | Encourage grounded answers | Medium: the model may cite randomly; only give it when the cited docid is in `support_docids` |
| Correct answer after opening at least one document | +0.05 | Encourage verification | Medium; remove once the behaviour is established |
| Confidence calibration: correct and confidence ≥ 70, or wrong and confidence ≤ 40 | +0.05 | Calibration is a benchmark metric | Low |

Things **never** to reward: number of searches (the model will spam), length of reasoning (it will pad), number of citations (it will cite everything), the judge's *explanation* (it is not a signal).

**Evidence-based partial credit** (optional, for harder curricula): because synthetic questions carry `support_docids`, you can give +0.2 if the trajectory retrieved all supporting documents even when the final answer was wrong. This densifies the signal on unsolved questions. Use it only for the unsolved bucket and remove it once pass rates rise, because it can teach "collect documents" without "answer".

Keep the reward function in one file, versioned, with unit tests on saved trajectories (a correct one, a hedged one, a looping one, a refusal, a truncated one).

### 7.8 The training questions and the learnable zone

GRPO learns from *variance within a group*. If all eight attempts on a question fail, every advantage is zero and the question teaches nothing; if all eight succeed, the same. The useful questions are those where the current model succeeds sometimes. This is the "learnable zone", roughly pass rates between 1/8 and 7/8.

This has direct consequences:

- Use the Phase 2 difficulty labels (Part 5.3 step 6) to select the training pool: mostly learnable questions, with 10 to 20% unsolved ones (some become learnable as the model improves) and no trivial ones.
- **Re-label periodically.** After every 100 or so steps, the model has changed; questions that were unsolved may now be learnable and learnable ones may be trivial. Run a cheap pass-rate estimate on a rotating sample and rebalance. This is a curriculum, and it is the single biggest lever on RL efficiency.
- Size: a group size of 8 on 64 questions per step is 512 rollouts per step. Over 300 steps that is 19,200 question-visits; with a pool of 5,000 learnable questions each is seen about four times, which is fine. Below 1,000 questions you will overfit within a hundred steps.
- Never include benchmark questions or their derivatives. The frozen failure-100 and pass-100 are *evaluation only*.

### 7.9 Infrastructure choices

An agentic RL setup has three moving parts: a **rollout engine** (an inference server such as vLLM that generates attempts fast), a **trainer** (which computes gradients on the collected trajectories and updates the weights), and a **weight sync** step (the updated weights are pushed to the rollout engine before the next round). Frameworks differ in how much of this they hide.

| Option | What it is | Strengths | Weaknesses | Fit for you |
|---|---|---|---|---|
| **ART (OpenPipe "Agent Reinforcement Trainer")** | Open-source GRPO for multi-step agents; LoRA training via Unsloth, rollouts via vLLM in the same process; you write the rollout as a plain Python function that calls an OpenAI-compatible endpoint and returns a trajectory with a reward | Closest to how your harness already works; single GPU possible; designed for beginners | Smaller community; verify Qwen3.5 (multimodal-classed) support in Unsloth and vLLM before committing | **Best first step.** |
| **verl (ByteDance)** | The most widely used open RL framework for LLMs; PPO/GRPO/DAPO; multi-turn tool calling through its agent loop; FSDP or Megatron training; vLLM or SGLang rollouts; LoRA support | Scales to 8 to 64 GPUs; Search-R1 and many search agents were built on it; every fix in 7.4 is a config flag | Steeper learning curve; YAML configuration with hundreds of options; multi-turn tool integration requires writing a tool class and a rollout config | **Best production step** once you have learned on ART. |
| **Tinker (Thinking Machines)** | A managed API: you write the sampling loop and reward in Python; their service runs LoRA training on their GPUs | No infrastructure at all; LoRA RL matches full fine-tuning for RL in their published results ("LoRA Without Regret") | Model list is fixed; check whether Qwen3.5-9B is available; per-token billing | Excellent if the model is supported. |
| **TRL `GRPOTrainer`** | Hugging Face's trainer; supports vLLM server mode and a custom rollout function | Familiar if you use TRL for SFT | Multi-turn tool rollouts are do-it-yourself; less battle-tested for agents | Acceptable fallback. |
| **SkyRL, rLLM, AReaL, slime, ROLL** | Other agentic RL frameworks | Some are async (rollouts and training overlap), which matters for slow environments | Smaller communities | Look at AReaL or SkyRL if retrieval latency dominates and you scale up. |

**Recommendation.** Learn on ART (or Tinker if the model is supported) with a LoRA on one or two GPUs, using a tiny run (Part 7.10.7). Move to verl with LoRA on 4 to 8 GPUs for the real run. The environment module, reward function, and question files are identical across all of them; only the glue changes.

**LoRA or full fine-tuning for RL?** LoRA. Thinking Machines' "LoRA Without Regret" results and common practice show that RL provides few bits of information per episode, so a rank-32 to 64 LoRA captures it as well as full fine-tuning, at a fraction of the memory. It also keeps the base weights untouched, which makes serving with your existing `--enable-lora` vLLM configuration trivial. Move to full fine-tuning only if a LoRA run visibly plateaus while reward still varies.

### 7.10 Step by step: setting up and running the RL job

This section assumes the Phase 1 harness is frozen, Phase 2 produced a labelled question pool, and Phase 3 produced a distilled adapter (call it `atom-electron-sft-v4`). If you want to try RL before those are done, you can, using the current adapter and a few hundred synthetic questions, but treat it as a learning exercise, not a production run.

#### 7.10.1 Hardware

| Stage | Minimum | Comfortable | Notes |
|---|---|---|---|
| Learning run (ART or TRL, LoRA) | 1×H100 80 GB or 1×H200 141 GB | 2×H100 (one for rollouts, one for training) | With a single GPU the framework alternates between generating and training and must fit both vLLM's KV cache and the trainer; ART/Unsloth handle this by putting vLLM to sleep during training. |
| Production run (verl, LoRA, 9B, 32k context) | 4×H100 | 8×H100 or 4×H200 | Rollout engines on half, training on half, or colocated with memory offload. |
| Retrieval | 1×RTX 4090 today | 2 to 4 replicas, or one A100/H100 with a lighter reranker | See 7.10.3; this is the actual bottleneck. |
| Judge | Azure or a 1×A100 running Qwen3-32B | | 512 judgments per step. |

Rent on Vast.ai as you do now, or a cloud with a multi-GPU node for the production run. Use a machine image with recent CUDA, PyTorch, vLLM, and the framework preinstalled where possible; you already learned that driver mismatches cost days.

#### 7.10.2 Software installation (learning run)

On a fresh instance, in `tmux` so it survives disconnects:

```bash
# 1. Sanity
nvidia-smi
python -c "import torch; print(torch.__version__, torch.cuda.get_device_name(0))"

# 2. A dedicated environment
python -m venv /workspace/rl && source /workspace/rl/bin/activate
pip install --upgrade pip

# 3. Framework (check the framework's README for the exact pinned versions that support Qwen3.5)
# Install the versions the framework pins. Qwen3.5 needs a recent vLLM (you used 0.28) and Transformers 5.
pip install vllm "transformers>=5" "trl>=0.29" unsloth unsloth_zoo   # ART / Unsloth path
pip install openpipe-art                                             # if using ART
# or, for verl:
# git clone https://github.com/volcengine/verl && cd verl && pip install -e . && follow their Qwen3 + tool-calling example

# 4. Your code
git clone <your repo> /workspace/bcp && pip install -e /workspace/bcp
export BCP_RETRIEVAL_URL=...  BCP_TOKEN=...   # the fast retrieval endpoint from 7.10.3
export HF_TOKEN=...                           # read-scoped; never in a file
huggingface-cli download Qwen/Qwen3.5-9B
huggingface-cli download CrowtherLabs/atom-electron-sft-v4   # your SFT adapter
```

Before anything else, run the same contract test you use in production: load the SFT adapter, send a PONG request, send a tool-call request, confirm structured tool calls come back. Everything downstream assumes this works.

#### 7.10.3 The fast retrieval environment (do this first; it gates everything)

The arithmetic, using your own measurements from `bcp-replication/retrieval_api.md`:

- one search takes 4.5 seconds solo and the service sustains about 0.24 searches per second;
- one RL step with 64 questions × 8 attempts = 512 episodes × about 12 searches = roughly 6,000 searches;
- at 0.24 per second that is **7 hours per step**. A 300-step run would take three months. This is not viable.

You need at least ten times the throughput, ideally thirty. Options, combinable:

1. **Cut the reranker cascade for training.** Ninety percent of the 4.5 seconds is the cross-encoder (stage 1 at 512 tokens over 200 candidates, then stage 2 at 8,192 tokens over 20). Keep stage 1 only (about 1.5 seconds), or rerank the top 50 at 512 tokens (under one second), or use a smaller reranker (Qwen3-Reranker-0.6B). Measure evidence Recall@10 on the strict split's qrels for each variant; if the light variant is within a few points of the full cascade, use it for RL and for the dev set. Keep the full cascade for the benchmark.
2. **Replicate the service.** It is stateless. Two to four replicas behind a simple round-robin in the client multiply throughput linearly. On Vast this is two to four more consumer GPUs.
3. **Cache.** Within a group, the eight attempts on the same question issue many identical or near-identical queries. A shared query→results cache (Redis or a dict with a lock) with normalised keys easily hits 20 to 40%. Also cache `get_document` (it is already cheap, but caching removes network round trips).
4. **Batch on the server.** The dense encoder and reranker are much faster per query when batched; if the service accepts a list of queries in one request, the client can batch across concurrent episodes.

Target: **at least 5 searches per second sustained**, which brings one step to about 20 minutes of environment time. Verify with `scripts_evaluation/bench_concurrency.py` before starting RL.

Model-side throughput is rarely the limit: a 9B model on one H100 under vLLM with 64 concurrent sequences produces several thousand tokens per second in aggregate; 512 episodes × about 8,000 generated tokens is about 4 million tokens, roughly 20 to 40 minutes. Rollouts and retrieval overlap, so one step is 30 to 60 minutes end to end on modest hardware; a 300-step run is one to two weeks.

#### 7.10.4 The rollout function

Refactor the harness loop into an importable environment and wrap it for the framework. The shape (pseudo-Python, framework-agnostic):

```python
# environment.py  (shared by benchmark runner and RL)
@dataclass
class Episode:
    messages: list[dict]          # full OpenAI-style conversation incl. tool calls & tool results
    assistant_turn_indices: list[int]   # which messages the policy generated (for the loss mask)
    final_answer: str | None
    diagnostics: dict             # productive calls, duplicates, refusals, opened docs, budget hit, tokens

async def run_episode(question: str, client, model_name: str, cfg: EnvConfig, seed: int) -> Episode:
    state = ResearchState()
    messages = build_initial_messages(question, cfg)
    assistant_turn_indices: list[int] = []
    while not done(state, cfg):
        resp = await client.chat.completions.create(model=model_name, messages=api_view(messages),
                                                    tools=cfg.tools, temperature=cfg.temperature,
                                                    max_tokens=cfg.max_tokens_per_turn, seed=seed)
        msg = resp.choices[0].message
        messages.append(msg); assistant_turn_indices.append(len(messages)-1)
        if msg.tool_calls:
            for call in msg.tool_calls:
                raw = await execute_tool(call, state, cfg)            # search / get_document via fast retrieval
                note = await summarize_if_enabled(question, call, raw, state, cfg)   # Phase 1 evidence note
                messages.append(tool_message(call.id, note, raw))
        else:
            break
    final = maybe_fresh_context_final(question, state, messages, cfg)  # Phase 1 answerer
    return Episode(messages, assistant_turn_indices, final, state.diagnostics())
```

```python
# rl_rollout.py  (framework glue; this is the part that changes between ART / verl / Tinker)
async def rollout(model, item):                     # item = one synthetic question record
    ep = await run_episode(item["question"], model.openai_client(), model.name, ENV_CFG, seed=random_seed())
    r = compute_reward(ep, item)                     # 7.7
    return framework_trajectory(ep.messages, ep.assistant_turn_indices, reward=r, metrics=ep.diagnostics)
```

Points to get right:
- **Tool results must be masked from the loss.** In ART you return the messages and it masks non-assistant roles; in verl you configure the multi-turn tool loop and it masks automatically; in TRL with a custom rollout you build the mask yourself. Verify by decoding one masked example and confirming only thinking, tool-call JSON, and final answers are supervised.
- **Thinking must be in the trajectory.** The reasoning parser on vLLM splits thinking into a separate field; when you rebuild the message for training, put it back into the assistant turn in the format the chat template expects, or the model is trained on tool calls without the reasoning that produced them.
- **Truncation policy.** If a turn hits `max_tokens` or the episode hits the context limit, end the episode with reward 0 but flag `truncated=True` so the overlong penalty from 7.4 applies softly instead of the trajectory looking like an ordinary failure.
- **Concurrency.** Wrap the retrieval client in a semaphore (concurrency 8 to 16 per replica) with retries and backoff; a retrieval error must fail the episode cleanly (reward 0, flagged `env_error=True`, and excluded from the batch), never crash the step.
- **Determinism for debugging.** Log every episode to disk in the same `run_qid_*.json` schema; you already have tooling to audit those files.

#### 7.10.5 The reward function

```python
# reward.py
def compute_reward(ep: Episode, item: dict) -> float:
    if ep.final_answer is None or not has_valid_contract(ep.final_answer):
        return 0.0
    exact = extract_exact_answer(ep.final_answer)
    if is_hedged(exact):                      # "A or B", lists, multiple names
        return 0.0
    correct = normalized_match(exact, item["answer"], item.get("answer_aliases", []))
    if not correct:
        correct = llm_judge(item["question"], exact, item["answer"])   # frozen judge, separate model, cached
    r = 1.0 if correct else 0.0
    r -= 0.05 * min(ep.diagnostics["rejected_duplicate_calls"], 6)
    if correct and cites_support_doc(ep.final_answer, item["support_docids"]):
        r += 0.1
    return max(r, -0.3)
```

Cache judge decisions by (question id, normalised answer). Unit-test with saved episodes. Log the reward components separately, so you can see whether a rise in mean reward comes from correctness or from shaping.

#### 7.10.6 Configuration and hyperparameters

| Parameter | Learning run | Production run | Rationale |
|---|---|---|---|
| Starting model | SFT adapter merged or loaded on Qwen3.5-9B | same | RL starts from the best SFT checkpoint; the reference model for KL is the same |
| Training method | LoRA rank 32, alpha 64, all projections | LoRA rank 64 (or full FT if plateau) | Memory; LoRA is adequate for RL |
| Questions per step | 16 | 64 to 128 | Gradient quality vs. step time |
| Group size G | 8 | 8 to 16 | Variance of the baseline; 16 helps on hard questions |
| Temperature (rollouts) | 1.0 | 1.0 (0.8 if degeneration appears) | Exploration; do not sample at 0.6 for RL |
| Max tokens per turn | 8,192 | 8,192 to 16,384 | Thinking headroom |
| Max context per episode | 32,768 | 32,768 to 49,152 | With evidence notes this covers 30 tool calls |
| Tool ceiling | 24 | 24 to 32 | Match harness |
| Learning rate | 1e-5 (LoRA) | 5e-6 to 1e-5 (LoRA); 1e-6 (full) | RL is sensitive; lower than SFT |
| Clip epsilon | 0.2 (0.28 high) | same | 7.4 |
| KL beta | 0.001 | 0.001, try 0 later | Identity retention; drop later |
| Advantage normalisation | mean only (no std) | same | 7.4 |
| Group filtering | on (drop all-0 / all-1 groups) | on | 7.4 |
| PPO epochs per batch | 1 | 1 to 2 | Stay near on-policy |
| Steps | 20 (smoke) | 200 to 500 | Watch curves; stop on dev plateau |
| Eval cadence | every 10 steps on 100 dev questions | every 20 steps on 300 dev + failure-100 + identity | Early detection of regressions |
| Checkpoint cadence | every 10 steps | every 20 steps, keep best-by-dev | Rollback |

#### 7.10.7 The smoke run (do not skip)

Purpose: prove that every piece works together before spending real money. Use 100 learnable questions, G=8, 16 questions per step, 20 steps, one GPU, fast retrieval.

Success criteria for the smoke run:
1. Episodes complete and are logged; you can open a `run_*.json` and read it.
2. The loss mask decodes to only assistant tokens (print it once).
3. Mean reward on the 100 questions is stable or rising over 20 steps (it may be noisy; a rise from 0.35 to 0.45 is a good sign).
4. No NaN loss, no explosion of response length, no drop in format-validity rate.
5. The checkpoint exports as a PEFT adapter, loads in vLLM with `--enable-lora`, passes the contract test, and scores at least the SFT model on 100 dev questions.
6. The identity controls still pass.

Expect to spend a week here. Most of it is plumbing: tokenizer template quirks, tool-call parsing under the framework's vLLM, mask alignment, and retrieval concurrency. That week is the "RL learning curve", and it is the same week whichever framework you use.

#### 7.10.8 The production run

1. Start from the best SFT adapter. Freeze a copy as the KL reference.
2. Load the training pool (learnable plus 15% unsolved), the dev set, and the frozen eval sets.
3. Launch with the production column of 7.10.6. Log to Weights & Biases or TensorBoard.
4. Every 20 steps: dev accuracy (single rollout), failure-100, pass-100, identity controls, format validity, mean tool calls, mean `get_document` calls, refusal rate, duplicate rate, mean response length, KL, entropy, clip fraction.
5. Every 100 steps: re-label a 500-question sample for pass rate and rebalance the pool (7.8).
6. Stop when dev accuracy has not improved for 60 steps, or when any regression check fails twice in a row.
7. Take the best-by-dev checkpoint, run the full harness on the strict split with the full reranker cascade, and compare against the SFT model under identical settings.

#### 7.10.9 Monitoring: what the curves should look like

| Curve | Healthy | Unhealthy, and what it means |
|---|---|---|
| Mean reward (train) | Rises steadily, then flattens | Flat from the start: no learnable signal (questions too hard or too easy, reward broken, mask wrong). Sudden jump to near 1.0: reward hacking; inspect episodes immediately. |
| Dev accuracy | Rises with train reward, with a lag | Train rises but dev does not: overfitting to the pool; add questions or stop. |
| Response length (tokens) | Gently rises or stable | Explodes: length is being rewarded indirectly (check truncation handling). Collapses: the model found a short hack. |
| Tool calls per episode | Moves toward the "right" number for the difficulty; `get_document` rises | Searches spike: something rewards searching. Drops to near zero: the model learned to guess. |
| Refusal rate | Falls | Rises: the reward for refusals is too high relative to wrong answers, or the questions are unsolvable. |
| Format validity | Stays above 98% | Falls: format reward missing or the KL is too weak. |
| Entropy | Slowly declining | Crashes to near zero early: entropy collapse; raise clip-high, temperature, or lower the learning rate. |
| KL to reference | Slowly rising, small | Spikes: learning rate too high or beta too low. |
| Clip fraction | 0.1 to 0.3 | Near 1.0: policy moved too far in one step; reduce learning rate. |

Read twenty random episodes every day. Curves tell you *that* something changed; episodes tell you *what*.

#### 7.10.10 Exporting, serving, and benchmarking

1. Save the LoRA adapter in PEFT format with `adapter_config.json` naming `Qwen/Qwen3.5-9B` as the base, exactly as your SFT pipeline does.
2. Serve with the same vLLM command in `AGENTS.md` section 5, swapping the adapter path.
3. Run `scripts_evaluation/smoke_test.py`, then the strict split with the frozen Phase 1 harness and the full reranker.
4. Judge with the frozen judge, then with the official Qwen3-32B judge for the leaderboard.
5. Record everything in `AGENTS.md`, as the project practice requires.

### 7.11 Failure modes and their fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| Reward flat at the initial level for 30+ steps | All-0 or all-1 groups dominate; or the loss mask is wrong; or learning rate too low | Check pass-rate histogram of the pool; decode the mask; raise learning rate 2× |
| Reward climbs but benchmark accuracy does not | Reward hacking (hedged answers, judge leniency) or distribution mismatch between synthetic questions and BrowseComp | Read episodes; tighten the hedging rule; compare question style; check retrieval mismatch (fast vs full) |
| Model starts answering without searching | Closed-book leakage in the synthetic set (strong model knew the answers) | Rerun the closed-book filter (5.3 step 5.3); penalise zero-search episodes lightly |
| Identity or chat behaviour degrades | KL too weak; no rehearsal | Raise beta to 0.01; mix a small SFT loss on identity rows (some frameworks support a mixed objective); or re-apply an identity LoRA after RL |
| Thinking becomes repetitive or contains loops | Entropy collapse or repetition penalty absent in rollouts | Clip-higher; temperature 1.0; a mild repetition penalty in rollout sampling only |
| Episodes truncated at context limit | Evidence notes disabled or too long; ceiling too high | Shorten notes; enable compaction; lower ceiling to 24 |
| Step time balloons | Retrieval queueing | Add replicas; cache; lower concurrency to the service's sweet spot |
| Training diverges (NaN, huge KL) | Learning rate; mixed precision; a bug in advantage computation | Halve learning rate; check that advantages are computed per group, not per batch |
| Wins on synthetic dev, loses on the failure-100 | Synthetic questions easier or differently shaped than BrowseComp | Raise hop count and constraint count in the generator; add a harder tier |

### 7.12 How much RL can give you, honestly

Published results for RL-trained open search agents in 2025 show BrowseComp (live web) moving from single digits to roughly 10 to 45% for 7B to 72B models after synthetic data plus RL, with the largest models and the most data at the top. On BrowseComp-Plus, with a fixed corpus and a good retriever, scores run higher than on the live web for the same model, and you are starting from a much higher SFT baseline than those papers. A realistic expectation for GRPO on top of a well-distilled 9B is **+3 to +10 points** in single-rollout accuracy, concentrated in exactly your two failure classes (refusals and unopened documents), plus a smaller improvement in query strategy. Ensembling (Part 4.3) stacks on top.

If after Phase 4 the 9B sits in the 70s on the strict split, you have built a state-of-the-art small research agent and the pipeline needed to scale it. That is the point at which Part 8 applies.

---

## Part 8. Phase 5: if you are still short, change the base model

> **In plain words:** every part of this plan is model-agnostic. If a 9B model plateaus below the target, the cheapest remaining lever is a larger model running the same pipeline. The identity adapter is a few hundred rows and retrains in an hour on any base.

### 8.1 Why size is the honest remaining lever

- The paper's oracle results show the *conversion* ceiling scales with model capability: 83% for a 32B open model, 93% for gpt-4.1. A 9B model's ceiling will be lower, and Part 3.4 will tell you exactly what it is.
- End-to-end, the paper's gap between the strongest reasoning model and everything else is not about knowledge; it is about sustaining a long interleaved search-and-reason process without losing the thread, which is capacity-bound.
- Your pipeline outputs (question factory, teacher trajectories, reward function, environment, harness, evaluation) are reusable as-is. Only the SFT and RL runs are repeated.

### 8.2 Candidates

Choose within the same family to keep the chat template, tool format, and thinking conventions identical to what you have already debugged:

| Model | Why consider it | Cost to run |
|---|---|---|
| Qwen3.5 mid-size dense (roughly 27B to 32B class) | Direct upgrade in reasoning; the paper's 32B oracle result is 83% | 1×H100/H200 for serving; 2 to 4 GPUs for LoRA SFT/RL at 32k |
| Qwen3.5 mixture-of-experts with a few billion active parameters (35B-A3B class) | Decodes about as fast as a 9B dense model; total capacity much larger | Memory of a 35B model, speed of a 3B |
| Qwen3.5 large MoE (100B-plus total, ~10B active) | Frontier-class reasoning at open-weight cost; can double as the teacher | Multi-GPU serving |

If you keep the "Atom Electron 1-9B" identity as a product name, note that the identity rows say "9B"; edit them when you change the base.

### 8.3 What carries over unchanged

Phase 0 measurement, the Phase 1 harness and ensemble, the Phase 2 question pool and difficulty labels (re-label for the new student), the Phase 3 teacher trajectories (re-run SFT), the Phase 4 environment, reward, and configs (re-run RL with adjusted memory settings). Expect the whole retrain cycle to take two to four weeks once the pipeline exists.

---

## Part 9. Budget and timeline

Assumptions: Vast.ai style rental at roughly $2 to $4 per GPU-hour for H100/H200 class, consumer GPUs cheaper; frontier API tokens at current list prices; one engineer full time plus one part-time reviewer.

| Phase | GPU time | API cost | Calendar |
|---|---|---|---|
| 0 Measurement | Oracle test: about 704 long prompts, a few GPU-hours | Judge calls: tens of dollars | Week 1 |
| 1 Harness | Reruns on dev/failure sets: 50 to 150 GPU-hours of model plus retrieval; a 4-way ensemble on the strict split: about 4× a normal run | Tens of dollars | Weeks 1 to 5 |
| 2 Question factory | Student labelling: 10k questions × 8 runs with fast retrieval: 200 to 400 GPU-hours spread over time | $300 to $3,000 depending on generator | Weeks 2 to 6 |
| 3 Distillation | Teacher rollouts (open model): 300 to 800 GPU-hours on a multi-GPU node; SFT: 20 to 60 GPU-hours; DPO: 10 GPU-hours | $0 to $10,000 if using a proprietary teacher for hard questions | Weeks 5 to 10 |
| 4 RL | Smoke: 50 GPU-hours. Production: 300 steps × 0.75 h × 4 to 8 GPUs = 900 to 1,800 GPU-hours, plus retrieval replicas | Judge calls: $500 to $1,500 for the run | Weeks 9 to 17 |
| 5 Scale | 2 to 4× the Phase 3 and 4 GPU time | | Weeks 15 to 22 |

Total for Phases 0 to 4 on the 9B: roughly $6,000 to $20,000 in compute and API, four to five months of calendar time with overlap. The largest uncertainties are teacher cost (choose open weights to control it) and RL step time (control it with the fast retrieval environment).

---

## Part 10. Decision rules between phases

Write these on the wall.

1. **After Phase 0.** If the oracle-evidence accuracy of the 9B is below 75%, schedule the Phase 5 decision now; the 9B cannot reach 90% even with perfect retrieval and persistence. Continue Phases 1 to 4 regardless, because they are needed for any model.
2. **After Phase 1.** If evidence notes plus the fresh answerer lift conversion (Part 3.6) by fewer than 5 points on the failure-100, the "answer in front of it" problem is model capacity, not context; expect Phase 3 to matter more. If a 4-way ensemble lifts strict-split accuracy by fewer than 3 points, single runs are already consistent and the bottleneck is surfacing; prioritise retrieval work.
3. **After Phase 2.** Do not start Phase 3 until the pool has at least 3,000 verified questions with at least 1,500 in the learnable bucket, and the dev and synthetic test sets are frozen.
4. **After Phase 3.** Do not start Phase 4 until the SFT model's single-rollout accuracy on the learnable pool is at least 40%, refusals are below 5%, and at least 80% of correct episodes opened a document. Below that, RL has too little signal; do another SFT or RFT round.
5. **During Phase 4.** Stop and inspect if train reward rises more than 0.15 in 10 steps, or if dev accuracy falls for two consecutive evaluations.
6. **After Phase 4.** If the strict split is below the intermediate target of 75% and the oracle test says the 9B ceiling is the reason, go to Phase 5. If the oracle test says the ceiling is fine, the gap is surfacing; invest in retrieval and multi-query search before scaling the model.

---

## Appendix A. Glossary

**Adapter / LoRA.** Low-Rank Adaptation: instead of changing all the model's weights, small extra matrices are trained and added on top. Cheap to train, easy to swap. Your Atom Electron adapter is one.

**Advantage.** How much better one attempt scored than the average attempt on the same question. Drives the direction of the RL update.

**Agentic RL.** Reinforcement learning where the model takes several actions (tool calls) per episode and the environment responds between actions.

**Base model.** The pretrained model before any of your fine-tuning (`Qwen/Qwen3.5-9B`).

**Calibration.** Whether stated confidence matches actual accuracy. Your model claims 91% on average when correct and 64% when wrong; well-calibrated would be much lower when wrong.

**Clipping (PPO).** Capping how much any one update can change a token's probability, for stability.

**Cold start.** The SFT step before RL that gives the model the basic behaviour so RL has something to reinforce.

**Contamination.** Training on the questions you evaluate on. Makes scores meaningless.

**Context engineering.** Designing what the model sees in its prompt at each step: which documents, in what form, how summarised.

**Conversion rate.** Among questions where the answer surfaced in tool output, the fraction answered correctly (Part 1.2).

**Cross-encoder / reranker.** A model that scores a query and a document together to reorder search results. The slow, accurate part of your retriever.

**Curriculum.** Choosing training questions by difficulty so the model always has something learnable.

**DAPO / Dr. GRPO.** Papers that fixed practical problems with GRPO; their fixes are listed in Part 7.4.

**Dev set.** Questions used for tuning decisions; never the test set.

**Distillation.** Training a smaller model on outputs of a stronger model.

**DPO.** Direct Preference Optimisation: training on pairs of a better and a worse response to the same context; no online sampling.

**Entropy.** How uncertain the model's next-token distribution is. Zero entropy means deterministic; RL can collapse it, which stops exploration.

**Environment.** Everything outside the model that reacts to its actions: tools, retriever, harness rules.

**Episode / trajectory / rollout.** One complete attempt at a question.

**Evidence recall.** Fraction of the human-labelled evidence documents that a run retrieved.

**Expert iteration / RFT.** Sample, keep correct attempts, fine-tune on them, repeat.

**Fresh-context answerer.** A final answering call with a clean prompt containing only gathered evidence (Part 4.2).

**GRPO.** Group Relative Policy Optimisation: RL that samples a group of attempts per question and uses the group average as the baseline; no value model.

**Group.** The G attempts sampled for one question in GRPO.

**Judge.** A model that decides whether a final answer matches the gold answer.

**KL divergence / KL penalty.** A measure of how far the trained model has moved from the reference model; penalised to prevent drift.

**Learnable zone.** Questions the current model solves sometimes but not always; the only questions GRPO learns from.

**Loss mask.** A marker for which tokens the training loss applies to; in agentic training, only the model's own tokens.

**On-policy.** Learning from attempts generated by the current model.

**Oracle retrieval.** Giving the model the labelled evidence documents directly; measures the reasoning ceiling.

**Policy.** The model, viewed as a rule for choosing outputs.

**Pooling (evidence).** Combining documents gathered by several attempts into one evidence set.

**qrels.** The relevance-label files listing which documents are evidence or gold for each question.

**Reward.** The score given to an attempt.

**Reward hacking.** The model finding a way to score well without doing what you meant.

**RRF.** Reciprocal Rank Fusion: a way to merge ranked lists from different retrievers.

**Rollout engine.** The inference server that generates attempts during RL (vLLM or SGLang).

**Self-consistency / voting.** Running several attempts and taking the most common answer.

**SFT.** Supervised fine-tuning: training on example outputs.

**Strict split.** The 704 benchmark questions never used in training.

**Surfacing rate.** Fraction of questions where the answer appeared in tool output at some point (Part 1.2).

**Test-time compute / scaling.** Spending more computation per question at inference (more thinking, more attempts) to improve accuracy.

**Value model / critic.** A model that predicts expected reward; used by PPO, not by GRPO.

**Weight sync.** Copying updated model weights from the trainer to the rollout engine between RL steps.

## Appendix B. Reading list

Read in this order; each is short or has a short summary.

1. BrowseComp-Plus (Chen et al., 2025), in this repository. Sections 4.6 to 4.8 for the retrieval, oracle, and get-document findings.
2. BrowseComp (Wei et al., 2025), for how the questions are built and for the aggregation-over-samples results.
3. DeepSeek-R1 (2025), for GRPO and RL with verifiable rewards.
4. DAPO (2025) and "Understanding R1-Zero-Like Training" (Dr. GRPO, 2025), for the fixes in Part 7.4.
5. Search-R1 (2025), for the first search-agent RL recipe and why it does not transfer to hard questions.
6. WebSailor, WebShaper, ASearcher, WebExplorer, DeepDive, and the Tongyi DeepResearch technical report (2025), for synthetic-question factories, SFT cold start, and agentic RL on search agents. Tongyi's IterResearch context management is the closest published analogue of Part 4.1.
7. "LoRA Without Regret" (Thinking Machines, 2025), for why LoRA suffices for RL.
8. The verl and OpenPipe ART documentation, multi-turn tool-calling examples.

## Appendix C. Checklists

**Before any experiment**
- [ ] Strict split TSV in use; contaminated IDs excluded.
- [ ] Judge version recorded; deterministic normaliser on.
- [ ] Failure-100, pass-100, dev-300 frozen and hashed.
- [ ] Retrieval configuration (full cascade vs fast) recorded in the run name.
- [ ] Model, adapter, harness flags, temperature, seeds recorded in the run record.

**Before training (SFT, DPO, or RL)**
- [ ] No training question overlaps a benchmark question, gold answer, evidence docid, or gold docid.
- [ ] Loss mask decoded and read for three examples.
- [ ] Identity/control rows mixed in (SFT) or identity evaluation scheduled (RL).
- [ ] Tokenizer preflight: no example exceeds the context.
- [ ] Baseline evaluation of the starting checkpoint on all frozen sets saved.

**Before an RL production run**
- [ ] Smoke run passed all six criteria in Part 7.10.7.
- [ ] Fast retrieval sustains at least 5 searches per second with the planned concurrency.
- [ ] Reward function unit tests pass; hedging rule tested.
- [ ] Judge is a separate model from the policy; judge cache in place.
- [ ] Pool has at least 3,000 questions; pass-rate histogram inspected.
- [ ] Monitoring dashboard shows every curve in Part 7.10.9.
- [ ] Checkpoint export to PEFT and vLLM load tested.

**Before reporting a number**
- [ ] Strict split, full reranker cascade, frozen harness, frozen judge.
- [ ] Ensemble size and total tool calls reported honestly.
- [ ] Official Qwen3-32B judge run for leaderboard comparability.
- [ ] `AGENTS.md` updated with run names, settings, and results.
