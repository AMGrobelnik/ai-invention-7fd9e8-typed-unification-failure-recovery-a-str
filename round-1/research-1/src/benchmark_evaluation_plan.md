# Benchmark Evaluation Plan: Typed Failure Detection & Repair

## Overview

This document specifies a rigorous evaluation methodology for measuring the effectiveness of typed-failure detection and repair in text-to-FOL pipelines. The evaluation compares the typed-repair approach against baselines (RAG, chain-of-thought, ARGOS-style generic fallback) on standard benchmarks (RuleTaker, CLUTRR) and custom annotated datasets.

---

## Part 1: Benchmark Selection & Adaptation

### 1.1 RuleTaker Benchmark

**Source**: Emmons et al., "RuleTaker: A Large-Scale Dataset for Interpretable Reasoning"

**Structure** [1]:
- **Dataset Composition**: 5 difficulty levels (D0–D5)
- **D0**: Direct lookups (no reasoning required)
- **D1–D5**: Progressive multi-hop inference depths (up to 8+ reasoning steps)
- **Dataset Size**: ~100K examples per difficulty level
- **Format**: Natural language context + question → True/False classification
- **Example**: 
  ```
  Context: "If something is kind, then it is small. All cats are kind."
  Question: "Are cats small?"
  Answer: True (requires 1-hop inference: cat → kind → small)
  ```

**Strengths for Typed-Failure Evaluation**:
- Fully formal/synthetic → clean FOL extraction ground truth
- Explicit rule definitions → easy to identify Type 2 (arity) and Type 3 (missing fact) failures
- Varying complexity → test repair scalability across difficulty levels
- Deterministic reasoning → Type 5 (scope) conflicts easily detectable

**Adaptation for Typed-Failure Study**:
1. Use **D1–D3 subset** (100K–300K examples) to keep evaluation tractable
2. **Intentionally corrupt** selected examples to introduce each failure type:
   - Type 1: Rename a predicate (cat → feline) without bridge axiom
   - Type 2: Remove an argument from predicate definition (keep(X,Y) → keep(X))
   - Type 3: Remove specific ground facts from context
   - Type 4: (Limited applicability to RuleTaker; use synthetic type constraints)
   - Type 5: Intentionally introduce quantifier ambiguity in context

3. **Measure per-type accuracy**: Success rate on Type 1–5 corruptions after repair
4. **Aggregate**: Overall accuracy across all types as percentage improvement over baseline

### 1.2 CLUTRR Benchmark

**Source**: Sinha et al., "CLUTRR: A Diagnostic Benchmark for Inductive Reasoning from Text" [2]

**Structure**:
- **Task**: Infer kinship relations between family members in semi-synthetic narratives
- **Example**:
  ```
  Context: "Alice is Bob's mother. Jim is Alice's father."
  Query: "What is Jim's relationship to Bob?"
  Answer: grandfather
  ```
- **Dataset Size**: Multiple split sizes (1K, 10K, 20K+)
- **Inductive vs. Transductive**: 
  - Transductive: Test on same entities as training
  - Inductive: Test on unseen entity combinations
- **Noise Variants**: Clean, with "distractor facts" (irrelevant information)

**Strengths for Typed-Failure Evaluation**:
- **Implicit commonsense**: Requires background knowledge (e.g., "grandfather = father of parent")
- **Multi-hop reasoning**: Up to 4+ reasoning steps
- **Systematic generalization**: Held-out rule combinations test Type 5 (scope) robustness
- **Semi-synthetic**: Ground-truth relations are known; can measure extraction precision

**Adaptation for Typed-Failure Study**:
1. Use **clean 10K version** + manually create **"corrupted" variants**:
   - **Type 1 variants**: Use synonyms for relations (mother → parent, sibling → brother)
   - **Type 3 variants**: Remove specific facts from context (remove one parent link)
   - **Type 5 variants**: Use stories requiring scope re-annotation (e.g., "Everyone has a parent" vs. "Some person is everyone's parent")

2. **Evaluation**:
   - Accuracy on original CLUTRR + accuracy on corrupted variants
   - Measure recall of extracted kinship facts directly from text
   - Measure multi-hop deduction accuracy (can model infer grandfather from parent + parent links?)

3. **Commonsense evaluation**:
   - Use CLUTRR's built-in rules as ground truth
   - When repair generates missing facts (Type 3), check if generated facts match rule definitions

---

## Part 2: Evaluation Metrics

### 2.1 Atomic Fact Extraction Metrics

For each document, measure direct extraction accuracy (Type 1, 2 detection):

```
Atomic Fact Extraction:
  Precision = TP / (TP + FP)
    where TP = correctly extracted facts (predicate name + arity + args match ontology)
          FP = extracted facts with wrong name, arity, or argument assignment
  
  Recall = TP / (TP + FN)
    where FN = facts in source document not extracted
  
  F1 = 2 * (Precision * Recall) / (Precision + Recall)
```

**Measurement Protocol**:
- Gold standard: Manual annotation of facts in each test document
- Per-document: Track which facts required Type 1 repair (name mismatch) vs. Type 2 (arity)
- Aggregate: Report F1 per repair type

**Threshold Justification**: ≥85% extraction F1 required before multi-hop reasoning evaluation (garbage in → garbage out)

### 2.2 Multi-Hop Deduction Accuracy

Measure end-to-end accuracy on queries requiring 2+ proof steps:

```
Multi-Hop Accuracy = (Correct Answers on k-hop queries) / (Total k-hop queries)
  for k = 2, 3, 4, ...
```

**Example (CLUTRR)**:
- Query: "Alice's paternal grandfather?" requires 2 hops (father → father)
- Success: Model extracts facts, applies rules, infers correctly
- Failure: Logical error (scope mismatch) or missing fact (Type 3)

**Measurement**:
- Report accuracy vs. hop depth (2-hop, 3-hop, 4-hop)
- Separate accuracy for "with repair" vs. "without repair"
- Identify failure modes post-hoc: which queries failed due to Type 3 vs. Type 5?

### 2.3 Hallucination Rate

Measure fraction of proof steps NOT grounded in source text or explicitly cited bridge axioms:

```
Hallucination Rate = (Unjustified proof steps) / (Total proof steps)
  where "Unjustified" = no source text span or bridge axiom citation
```

**Measurement**:
- For each proof, trace back each conclusion to source text
- Count steps using only abducted facts (Type 3 repairs) without source grounding
- For bridge axioms (Type 1 repairs), verify both predicates cited in text

**Target**: <10% hallucination rate on corrupted benchmarks (vs. raw LLM baseline ~40%)

### 2.4 Bridge Axiom Reusability

Track how many Type 1 repairs (bridge axioms) are discovered once and reused across documents:

```
Reuse Rate = (Unique bridge axioms reused) / (Total unique bridge axioms discovered)
```

**Measurement**:
- Maintain global index of bridge axioms by source + target predicate
- When Type 1 failure detected on document D, check if bridge axiom already in index
- If yes: reuse; if no: generate and add to index
- Report cumulative reuse rate as more documents processed

**Significance**: Higher reuse suggests that lexical variation is limited in domain; repair effort amortizes over corpus

---

## Part 3: Custom Annotation Schema

### 3.1 Annotation Task Definition

For subset of 50–100 real-world documents (legal texts, news articles, narratives), annotate:

**Per-document annotation**:
- Extract all facts explicitly stated
- For each unprovable multi-hop query, identify failure type

**Annotation format** (one row per failure):

| Document ID | Query | Query Predicate | Failure Signal | Failure Type | Expected Repair | Text Span (Citation) |
|---|---|---|---|---|---|---|
| doc_001 | grandfather(alice, bob)? | grandfather | goal_fails | Type 5 (scope) | Re-annotate quantifiers | "Alice's mother's father" (implies ∃ relationship) |
| doc_001 | hasAge(alice, X) | hasAge | goal_fails | Type 3 (missing) | Abduct fact | "Alice is middle-aged" (implies 40–60) |
| doc_002 | owns(merchant, goods) | owns | goal_fails | Type 1 (lexical) | Bridge axiom: owns ← has_possession | "merchant has possession of goods" |

**Annotation guidelines**:
1. Read document; extract all explicit facts using ontology predicates
2. For each potential query (relation between two entities), check if it can be proven
3. If unprovable AND multi-hop required: classify failure type using decision tree from typed_failure_detector_design.md
4. Cite specific text span supporting repair classification

### 3.2 Inter-Annotator Agreement

**Setup**: 2 annotators independently annotate 20 documents (50–100 failures total)

**Metrics**:
- Cohen's κ for failure type classification (5 categories)
- Pairwise F1 for text span agreement (union overlap ≥80%)

**Threshold**: κ ≥ 0.75 required for high-confidence evaluation set

**Effort Estimate**: 2 annotators, 50–100 documents, 3–5 failures per doc = ~200–300 hours total (5–10 hours per 10 documents)

---

## Part 4: Baseline Comparisons

### 4.1 Baseline 1: RAG (Retrieval-Augmented Generation)

**Method**: Retrieve relevant facts from KB; pass to LLM for reasoning.

**Implementation**:
- Embed query and all KB facts in semantic space (SentBERT)
- Retrieve top-K facts (K=10) by cosine similarity
- Concatenate retrieved facts + original query → LLM → answer

**Hyper-parameter**: K ∈ {5, 10, 20}

**Expected Performance**: 
- Good on Type 1 (retrieves synonymous predicates)
- Poor on Type 3 (missing facts not in KB)
- Poor on Type 5 (LLM prone to scope errors without symbolic grounding)

### 4.2 Baseline 2: Chain-of-Thought Prompting (CoT)

**Method**: LLM generates reasoning steps in natural language; extract answer.

**Implementation**:
```
Prompt: "Reason through this step-by-step:
Document: [TEXT]
Question: [QUERY]
Answer: [Let me think...]"
```

**Variant**: Self-consistency (generate 5 reasoning paths; take majority answer)

**Expected Performance**:
- Good on simple queries
- Breaks on deep multi-hop (token limit)
- Hallucination rate ~30–40%

### 4.3 Baseline 3: ARGOS-style Generic Fallback

**Method**: Identical to our approach, but route all failures to single augmentation strategy (no typed detection).

**Implementation**: 
- Any failure → generate missing commonsense fact via LLM
- No Type 1 detection (so bridge axioms not generated)
- No Type 4 detection (so entity typing not corrected)
- No Type 5 detection (so quantifier scopes not fixed)

**Expected Performance**:
- Lower precision (generates irrelevant commonsense facts)
- Higher cost (more LLM calls to find right fact)
- Better than baselines 1–2 on Type 3, worse on Type 1/4/5

### 4.4 Ablation: Typed Detection Without Repair

**Method**: Detect failure type but do not apply repair (just log type).

**Purpose**: Measure how much improvement comes from "knowing the type" vs. "applying right repair"

**Expected Performance**: Between ARGOS-style and typed-repair; validates that repair specialization is key

---

## Part 5: Statistical Analysis

### 5.1 Significance Testing

For each metric (e.g., F1 accuracy), compare:
- Typed-Repair vs. each baseline using paired t-test (per document)
- Bonferroni correction for multiple comparisons (5 failure types)
- Report 95% confidence intervals

### 5.2 Error Analysis

**Per-failure-type breakdown**:
- Report accuracy separately for Type 1, 2, 3, 4, 5
- Identify which types are reliably detected vs. missed
- For missed detections: why did heuristics fail? (e.g., semantic_similarity below threshold)

**Failure mode patterns**:
- Which document types (legal, narrative, technical) have highest Type 3/5 rates?
- Which LLMs (GPT-4, Claude, Llama) generate different failure distributions?

---

## Part 6: Experimental Setup & Timeline

### 6.1 Datasets

| Benchmark | Split | Size | Purpose |
|---|---|---|---|
| RuleTaker D1–D3 | Unperturbed | ~300K | Baseline accuracy |
| RuleTaker D1–D3 | Type-1 corrupted | 10K | Lexical mismatch detection |
| RuleTaker D1–D3 | Type-2 corrupted | 10K | Arity mismatch detection |
| RuleTaker D1–D3 | Type-3 corrupted | 10K | Missing fact detection |
| CLUTRR clean 10K | Unperturbed | 10K | Baseline accuracy |
| CLUTRR custom | Type-1 variants | 2K | Lexical mismatch (synonyms) |
| CLUTRR custom | Type-3 variants | 2K | Missing fact (removed relations) |
| Custom legal/news | Annotated | 50–100 | Ground truth failure distribution |

**Total evaluation data**: ~340K–360K examples

### 6.2 Evaluation Phases

**Phase 1 (Weeks 1–2): Benchmark Setup**
- Download RuleTaker, CLUTRR
- Implement perturbation scripts (Type 1–3 corruption)
- Implement evaluation metrics (F1, accuracy, hallucination rate)

**Phase 2 (Weeks 3–4): Baseline Implementation**
- Implement RAG, CoT, ARGOS baselines
- Run on unperturbed benchmarks
- Document baseline accuracy

**Phase 3 (Weeks 5–7): Typed-Repair Implementation**
- Implement typed-failure detector (decision tree)
- Implement Type 1–5 repair strategies
- Integrate with Prolog reasoner

**Phase 4 (Weeks 8–9): Annotation**
- Recruit annotators
- Annotate 50–100 custom documents
- Measure inter-annotator agreement

**Phase 5 (Weeks 10–12): Evaluation**
- Run typed-repair on all benchmarks (RuleTaker, CLUTRR, custom)
- Run baselines on same data
- Perform statistical analysis

**Phase 6 (Weeks 13–14): Analysis & Writing**
- Error analysis per failure type
- Write up results
- Prepare figures/tables for paper

---

## Part 7: Success Criteria

**Minimal Success**:
- Typed-repair achieves ≥10% absolute improvement on multi-hop accuracy vs. ARGOS-style fallback
- Typed detection accuracy (F1 on failure-type classification) ≥80% on custom-annotated data

**Strong Success**:
- ≥15% improvement on corrupted RuleTaker/CLUTRR
- ≥20% improvement on multi-hop accuracy (CLUTRR 3-hop+)
- Hallucination rate <5% with typed-repair vs. >30% with baselines
- Type-1 repair achieves >70% reuse rate by end of corpus

**Publication-Quality Success**:
- All above + statistically significant (p<0.05) improvements
- Clear per-type analysis showing which failure types most impactful
- Generalizes to real-world documents (50+ legal/news texts)
- Bridge axiom reusability enables transfer to new domains

---

## References

[1] Emmons et al., "RuleTaker: A Large-Scale Dataset for Interpretable Reasoning", arXiv:2009.13592 (https://arxiv.org/abs/2009.13592)

[2] Sinha et al., "CLUTRR: A Diagnostic Benchmark for Inductive Reasoning from Text", EMNLP 2019 (https://aclanthology.org/D19-1458.pdf)
