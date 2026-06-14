# Typed Prolog Failure Detection & Repair Strategy Mapping

## Executive Summary

Prolog's exception signals (type_error, instantiation_error, existence_error, unification failures) map to five structurally distinct failure modes in text-to-FOL pipelines. This document specifies a concrete typed-failure detector that routes failures to specialized repair strategies, drawing from ARGOS (iterative abductive augmentation), NLProlog (soft unification for lexical gaps), RECOVER (ontology-driven rule-based detection), and abductive logic programming literature.

---

## Part 1: Prolog Exception Signals to Failure Mode Mapping

### 1.1 Prolog Exception Hierarchy (ISO Standard)

SWI-Prolog distinguishes exceptions from silent goal failures using a formal error structure: `error(Formal, Context)`, where `Formal` specifies the error type [1]. Key exception signals:

- **type_error(ValidType, Culprit)**: Argument is not of expected type (e.g., atom when compound expected)
- **instantiation_error(Term)**: Variable is under-instantiated when a ground term is required
- **existence_error(ObjectType, Culprit)**: Resource (predicate, file) does not exist
- **unification_failure**: Two terms cannot unify syntactically (goal fails silently, no exception)

These exceptions are **catchable** and **leave proof traces** (partial unification attempts). Silent failures (goal exhaustion) are **not exceptions** [1].

### 1.2 Five Failure Modes in Text-to-FOL Pipelines

#### **Type 1: Lexical Mismatch**

**Definition**: Goal predicate name differs from loaded predicates; both predicates express the same semantic relationship but have different surface forms.

**Example**: FOL extractor produces `owns(X, Y)` but ontology only defines `has_possession(X, Y)`.

**Prolog Signal**: GOAL FAILURE (silent), no exception. Detected by predicate name mismatch + semantic similarity check.

**Heuristic Detection**:
```
IF goal_predicate(p, Arity) fails AND
   EXIST loaded_predicate(q, Arity) AND
   semantic_similarity(p, q) > threshold AND
   both_same_arity
THEN Type 1 failure detected
```

**Source**: Goal fails after exhausting all matching clauses; proof trace shows no matching head.

#### **Type 2: Argument-Structure Mismatch (Arity)**

**Definition**: FOL extractor produces predicates with wrong arity; e.g., extractor outputs `event(date, location, description)` but ontology defines `event(date, description)`.

**Prolog Signal**: 
- `existence_error(procedure, Culprit)` if no clause matches arity
- `type_error(compound_expected, Culprit)` if type constraint is violated
- Silent unification failure if arity mismatch prevents unification

**Heuristic Detection**:
```
IF Prolog throws [type_error | existence_error] during unification OR
   unification fails on compound structure mismatch
THEN Type 2 failure detected
```

**Source**: Unification fails during head-matching phase.

#### **Type 3: Missing Domain Fact**

**Definition**: Predicate is correctly defined (exists in ontology, arity matches), but no ground fact `p(a, b)` exists in KB for the specific values needed by proof.

**Example**: Proof requires `hasAge(alice, X)` but no fact states alice's age.

**Prolog Signal**: GOAL FAILURE (silent). Exhaustive search returns no matching fact. Differs from Type 1 because predicate **exists** but has no matching ground atoms.

**Heuristic Detection**:
```
IF goal predicate(p, Arity) fails AND
   predicate_definition_exists(p, Arity) AND
   proof_tree_shows_exhausted_clauses(p)
THEN Type 3 failure detected
```

**Source**: All clauses exhausted; proof-tree inspection shows which facts are missing.

#### **Type 4: Ontological Category Violation**

**Definition**: Predicate exists, arity matches, facts exist, BUT entity does not satisfy the required type constraints from the ontology (e.g., `hasAge(X)` requires X to be type `Person`, but X is typed as `Document`).

**Prolog Signal**: NO PROLOG EXCEPTION. Prolog's unification is agnostic to type semantics; type violations are caught only by external type-checker querying OpenCyc assertions (isa/genls predicates).

**Heuristic Detection**:
```
IF goal succeeds syntactically AND
   ontology_type_checker reveals: isa(entity, ActualType) AND
   NOT (ActualType in domain_types_for_predicate(p))
THEN Type 4 failure detected
```

**Source**: External ontology-based type checker (e.g., OpenCyc query: `isa(alice, Document)` but predicate expects `Person`).

#### **Type 5: Quantifier Scope Conflict**

**Definition**: Proof succeeds, but produces wrong result due to quantifier ambiguity. Example: extraction uses `∀X ∃Y loves(X,Y)` (everyone loves someone), but text intends `∃Y ∀X loves(X,Y)` (one person is loved by all).

**Prolog Signal**: NO EXCEPTION. Proof completes successfully but resolves to unexpected solution.

**Heuristic Detection**:
```
IF proof succeeds AND
   proof_result(Solution) != expected_result AND
   solution_trace shows quantifier-sensitive inference path
THEN Type 5 failure detected (via proof-tree analysis)
```

**Source**: Proof-tree inspection; requires user-provided scope annotations or semantic verification.

### 1.3 Exception Signal Mapping Table

| Failure Type | Prolog Signal | Detectability | Source |
|---|---|---|---|
| Type 1 (Lexical) | Goal failure | Predicate name mismatch + embedding sim | No matching clause head |
| Type 2 (Arity) | type_error / existence_error | Unification exception | Unification failure |
| Type 3 (Missing Fact) | Goal failure | Exhaustive clause search | All KB facts exhausted |
| Type 4 (Category) | None (silent) | Ontology type-check | External type-checker |
| Type 5 (Scope) | None (silent) | Proof-tree analysis | Proof matches wrong goal |

---

## Part 2: OpenCyc Type System for Type 4 Detection

### 2.1 OpenCyc Predicate Structure

OpenCyc uses two key predicates for type hierarchies [2]:

- **isa(Individual, Collection)**: "Individual is an instance of Collection"
  - Example: `isa(alice, Person)`, `isa(fido, Dog)`
  
- **genls(SubCollection, SuperCollection)**: "SubCollection is a generalization/subtype of SuperCollection"
  - Example: `genls(Doctor, Person)`, `genls(Lawyer, Person)`

Domain/range constraints can be expressed via rules:
```prolog
% If predicate hasSalary(X, Y) expects X to be type Employee
domain_constraint(hasSalary, 1, Employee).
% If it expects Y to be type CurrencyAmount
range_constraint(hasSalary, 2, MoneyAmount).
```

### 2.2 Type-Checking Heuristic for Type 4

For each extracted fact `p(e1, e2, ..., en)`:

1. Query ontology: `isa(e1, T1)`, `isa(e2, T2)`, etc.
2. For each argument position `i`, check if domain constraint is satisfied:
   ```
   domain_constraint(p, i, ExpectedType) ∧
   isa(ei, ActualType) ∧
   genls(ActualType, ExpectedType)  % Check subtype relationship
   ```
3. If constraint violated: Type 4 failure → route to entity re-typing repair.

### 2.3 OpenCyc Coverage Assessment

OpenCyc Lite contains ~6,000 base concepts [2]. For short professional documents (~3000 chars):
- **Sufficient for common entities**: Person, Organization, Place, Document, Date, Amount
- **Limited for domain-specific types**: e.g., "LegalTerm", "MedicalCondition" may not be in ontology
- **Recommended approach**: Use OpenCyc for upper-level categories; fall back to LLM-based entity typing for domain-specific edges

---

## Part 3: Repair Strategy Synthesis from Literature

### 3.1 ARGOS (Cotnareanu et al., ICLR 2026): Type 3 Repair

**Mechanism**: Iterative abductive augmentation using SAT solver feedback [3].

**Algorithm**:
1. Attempt to solve with logic solver (SAT-based)
2. If unsolvable, extract "backbone" (literals implied by premises)
3. Use LLM to generate commonsense propositions of form: `L1 ∧ L2 → L_new`
   - L1, L2 ∈ backbone (ensure relevance)
   - L_new is a new literal generated by LLM
4. Score each proposition for "commonsense" (would it be true without context?) and "relevance" (does it share entities with problem?)
5. Add highest-scoring proposition to problem; repeat until solvable

**Limitations**: ARGOS uses generic "generate commonsense fact" prompting; does **not** distinguish failure types. All failures routed to same augmentation loop.

**Repair Prompt (Type 3)**:
```
Given the logical problem below, the proof failed because the fact 
[MISSING_FACT] is not in the knowledge base. Generate the most 
specific, minimal fact that would make the proof succeed, grounded 
in the provided document. Output as a single Prolog clause.

Document: [CONTEXT]
Failed goal: [GOAL]
Proof chain: [TRACE]
```

### 3.2 NLProlog (Weber et al., ACL 2019): Type 1 Repair

**Mechanism**: Soft unification using learned embeddings [4].

**Algorithm**:
1. Replace hard unification with differentiable similarity function: `s1 ∼_θ s2 ∈ [0,1]`
2. Similarity based on cosine distance of pretrained sentence embeddings (SentVec)
3. Proof score = aggregation (min/product) of similarity scores along proof path
4. Fine-tune embeddings end-to-end on downstream task

**Limitations**: 
- **Non-auditable**: Learned embeddings are opaque; cannot inspect why `owns(X,Y)` matched `has_possession(X,Y)`
- **Requires training**: Cannot apply to new predicates without retraining
- **Handles only lexical gap**: Does not address Type 2, 3, 4, 5

**Repair Style (Type 1)**: NLProlog implicitly performs Type 1 repair by learning soft matches. Explicit bridge-axiom approach is more interpretable:
```
owns(X, Y) :- has_possession(X, Y).
% Cite text span: "The merchant owns the property" = "The merchant has possession of the property"
```

### 3.3 RECOVER (Cornelio & Diab, arXiv 2404.00756): Ontology-Driven Detection

**Mechanism**: Ontology + logical rules + LLM-based replanning [5].

**Framework**:
1. Represent environment as ontology (classes, instances, relations)
2. Define failure types via logical rules applied to scene-graphs (objects + relations)
3. Example failure detection rule (DroppingObjFailure):
   ```prolog
   Event(e) ∧ hasAction(e, a) ∧ ActionWithHeldObject(a) ∧
   hasPreconditions(e, pre_c) ∧ hasTriple(pre_c, trp1) ∧
   hasSubject(trp1, held_obj) ∧
   ¬(hasTriple(post_c, (held_obj, inside, hand))) ∧
   → Failure(DroppingObjFailure)
   ```
4. When failure detected, extract recovery strategy from ontology; pass to LLM for replanning

**Strengths**: 
- Ontology rules are **explicit** and **auditable**
- Type-specific recovery strategies (different repair for "dropped object" vs. "inaccessible path")
- **Generalizes to new failure types** by adding rules to ontology

**Limitations**: 
- Requires domain-specific ontology engineering (OntoThor example is for kitchen robotics)
- Rules must be manually authored for each failure type
- Not directly applicable to text-to-FOL without adaptation

**Repair Insight for Text-to-FOL**: RECOVER's ontology-rule approach is analogous to our typed-failure detector. Each failure type → explicit detection rule → specific LLM-based repair strategy.

### 3.4 Abductive Logic Programming (ALP) & Commonsense Reasoning

**PACS (Microsoft)**: Probabilistic Abductive Commonsense Sampling [6].

**Mechanism**:
1. Define set of "abducibles" (propositions that can be assumed)
2. For each ground atom, compute minimal set of abducibles needed to explain it
3. Use probabilistic reasoning to rank hypotheses
4. Generate abducibles via LLM; rank by commonsense probability

**Key Insight**: Abducibles should be **minimal** and **necessary** — only add facts required to reach proof goal, not all plausible commonsense facts.

**Repair Strategy for Type 3**: Use ALP principle to constrain LLM-generated facts:
```
FACT GENERATION PROMPT (Type 3 - Minimalist):
Given the unprovable goal [GOAL], identify the SINGLE, MOST SPECIFIC 
fact that, if added, would make the goal provable. Prefer facts 
already implicit in the document over broad commonsense statements.

Document: [CONTEXT]
Failed goal: [GOAL]
Required proof steps: [TRACE]

Output: One Prolog fact in the form predicate(arg1, arg2, ...).
```

---

## Part 4: Typed Failure Detector Design

### 4.1 Decision Tree & Detection Heuristics

```
START: Goal fails or exception raised

├─ Prolog exception? YES
│  ├─ type_error(...) or existence_error(...)?
│  │  └─ Type 2 (Arity Mismatch) → FOL_RESTRUCTURING
│  └─ [other exception] → Generic handler
│
├─ Goal fails silently (no exception)? YES
│  ├─ Predicate_signature matches any loaded predicate?
│  │  ├─ YES: exhaustive clause search shows facts available?
│  │  │  ├─ YES → Type 3 (Missing Fact) → ABDUCTIVE_GENERATION
│  │  │  └─ NO → Type 4 or 5, check further
│  │  └─ NO: semantic_similarity(attempted_pred, any_loaded_pred) > 0.8?
│  │     └─ YES → Type 1 (Lexical Mismatch) → BRIDGE_AXIOM
│  │
│  └─ Ontology type-check on extracted facts
│     └─ Type violation? → Type 4 (Category Violation) → ENTITY_RETYPING
│
└─ Proof succeeds but semantically wrong (requires proof-tree analysis)
   └─ Type 5 (Scope Conflict) → SCOPE_REANNOTATION
```

### 4.2 Type-Specific Repair Prompts

#### **Type 1: Lexical Mismatch**

```
REPAIR PROMPT - Lexical Bridging:

The extracted predicate name "{pred1}" does not exist in the ontology, 
but semantically it matches "{pred2}" which is defined. Generate a 
bridge axiom that equates them.

Text span for {pred1}: "{text_span1}"
Text span for {pred2}: "{text_span2}"

Verify that both predicates express the same relationship, then output 
a minimalist bridge axiom in Prolog:

{pred1}(X, Y) :- {pred2}(X, Y).

Bridge axiom:
```

**Evaluation**: Check that both sides have same arity and arguments align semantically.

---

#### **Type 2: Arity Mismatch**

```
REPAIR PROMPT - FOL Restructuring:

The extracted predicate "{pred}({extracted_arity})" does not match the 
ontology definition "{pred}({ontology_arity})". Re-extract the fact 
from the text using the correct arity.

Original text: "{text}"
Extracted (incorrect): {pred}({incorrect_args})
Expected arity: {ontology_arity}

Rewrite the fact with the correct arity, removing or consolidating 
arguments as needed. Only output the corrected Prolog fact.

Corrected fact:
```

**Evaluation**: Verify new arity matches ontology; check all arguments are from source text.

---

#### **Type 3: Missing Domain Fact**

```
REPAIR PROMPT - Abductive Fact Generation (Minimal):

The proof requires the fact [{missing_fact}] but it is not in the 
knowledge base. Based on the provided text, what is the implicit value 
for this fact? Be SPECIFIC and MINIMAL—do not add broad commonsense 
that is not grounded in the document.

Text context: "{text}"
Proof chain (what led to the missing fact): {proof_steps}
Required fact: {missing_predicate}

Answer with a single Prolog ground fact. Example format: hasAge(alice, 25).

Inferred fact:
```

**Evaluation**: 
- Fact is ground (no variables)
- Arguments are entities/values mentioned or strongly implied by text
- Fact is cited with specific text span

---

#### **Type 4: Ontological Category Violation**

```
REPAIR PROMPT - Entity Re-typing:

The entity "{entity}" was extracted as type "{extracted_type}", but the 
predicate "{pred}(X, Y)" requires X to be of type "{required_type}".

Options:
1. Re-identify the entity from the text with the correct type
2. Substitute a compatible entity mentioned in the text

Original extraction: {extracted_type}({entity}) based on: "{text_span}"
Ontology requirement: {required_type}

Which option is appropriate? If option 1, provide the corrected entity 
and type. If option 2, suggest a replacement entity from the text.

Corrected entity and type:
```

**Evaluation**: 
- New type is a subtype of required_type in OpenCyc
- New entity is explicitly mentioned in source text

---

#### **Type 5: Quantifier Scope Conflict**

```
REPAIR PROMPT - Scope Re-annotation:

The extracted formula uses a quantifier structure that produces the 
wrong logical meaning. Re-examine the quantifier scope.

Original extraction: {fol_formula}
Proof result: {proof_solution}
Expected result: {expected_solution}

Text: "{text}"

If a scope conflict is the issue, rewrite the formula with corrected 
quantifier nesting. Otherwise, explain why the extraction is correct.

Corrected formula (if needed):
```

**Evaluation**: 
- Rewritten formula, when solved, produces expected result
- Justification provided with reference to text

---

### 4.3 Fallback Strategy

**If failure does not match any of the five types**:
- Log the failure signal, goal, and proof trace
- Route to generic ARGOS-style abductive augmentation
- Prompt: "Provide any missing commonsense fact that would help prove [GOAL]"
- Mark as "untyped failure" for post-hoc analysis

---

## Part 5: Implementation Checklist

### Detection Layer
- [ ] Implement Prolog exception catcher (catch/3 wrapper)
- [ ] Extract proof-tree from SWI-Prolog via trace/debug interface
- [ ] Implement predicate name matching with semantic embedding similarity (e.g., SentBERT)
- [ ] Implement OpenCyc type-checker (SPARQL query or direct predicate loading)
- [ ] Implement proof-tree analyzer (identify entities with shared occurrence in backbone)

### Routing Layer
- [ ] Decision tree logic (exception → type mapping)
- [ ] Heuristic threshold tuning (semantic_similarity > 0.8?, commonsense_score > 0.3?)
- [ ] Fallback handler for untyped failures

### Repair Layer
- [ ] Type 1: Bridge axiom generator (verify semantic equivalence)
- [ ] Type 2: FOL restructurer (verify arity, argument alignment)
- [ ] Type 3: Abductive fact generator (verify minimality, grounding in text)
- [ ] Type 4: Entity re-typer (verify OpenCyc subtype relation)
- [ ] Type 5: Scope re-annotator (verify proof-tree resolution)

### Verification Layer
- [ ] Re-run Prolog proof after repair
- [ ] Log repair success/failure
- [ ] Collect statistics on repair type frequency and success rates

---

## References

1. SWI-Prolog Documentation: Exception Handling, https://eu.swi-prolog.org/pldoc/man?section=exception
2. OpenCyc: Lightweight OWL Ontology, https://www.qrg.northwestern.edu/nextkb/IntroOpenCycOnt.pdf
3. Cotnareanu et al. (2026): ARGOS – Balanced Neuro-Symbolic Approach for Commonsense Abductive Logic, ICLR 2026
4. Weber et al. (2019): NLProlog – Reasoning with Weak Unification for Question Answering in Natural Language, ACL 2019
5. Cornelio & Diab (2024): RECOVER – Neuro-Symbolic Framework for Failure Detection and Recovery, arXiv:2404.00756
6. Microsoft Research: Abductive Reasoning with Probabilistic Commonsense (PACS)
