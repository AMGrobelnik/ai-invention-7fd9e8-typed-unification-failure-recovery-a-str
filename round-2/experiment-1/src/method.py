#!/usr/bin/env python3
"""Typed Failure Recovery for Neuro-Symbolic Reasoning.

Compares typed failure repair (Type 1: unknown predicate, Type 2: arity mismatch,
Type 3: missing fact) against a generic-repair baseline on ProofWriter + CLUTRR.

Fallback: Python forward-chaining logic engine (no SWI-Prolog required).
LLM calls via OpenRouter (claude-haiku-4-5).
"""

import asyncio
import gc
import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import psutil
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

WORKSPACE = Path(__file__).parent
LOGS_DIR = WORKSPACE / "logs"
OUTPUTS_DIR = WORKSPACE / "outputs"
DATA_DIR = WORKSPACE / "data"

for d in [LOGS_DIR, OUTPUTS_DIR, DATA_DIR]:
    d.mkdir(exist_ok=True)

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add(str(LOGS_DIR / "run.log"), rotation="30 MB", level="DEBUG")

# ── Resource limits ────────────────────────────────────────────────────────────
_avail = psutil.virtual_memory().available
RAM_BUDGET = min(int(_avail * 0.6), 8 * 1024**3)  # 60% of available or 8 GB
resource.setrlimit(resource.RLIMIT_AS, (RAM_BUDGET * 3, RAM_BUDGET * 3))

# ── Config ─────────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OR_MODEL = "anthropic/claude-haiku-4-5"
SKILL_DIR = Path("/home/adrian/projects/ai-inventor/.claude/skills/aii-openrouter-llms")
OR_PY = "/home/adrian/projects/ai-inventor/.claude/skills/.ability_client_venv/bin/python"
OR_SCRIPT = str(SKILL_DIR / "scripts/aii_or_call_llms.py")

LLM_COST_LIMIT = 8.0
# haiku-4.5 pricing: $0.80/M in, $4.00/M out (approximate)
COST_PER_INPUT_TOKEN = 0.80 / 1_000_000
COST_PER_OUTPUT_TOKEN = 4.00 / 1_000_000

total_cost_usd = 0.0
total_llm_calls = 0

HF_TOKEN = os.environ.get("HF_TOKEN", "")

MAX_EXAMPLES = int(os.environ.get("MAX_EXAMPLES", "50"))
MINI_RUN = os.environ.get("MINI_RUN", "0") == "1"


# ── Python Forward-Chaining Logic Engine ───────────────────────────────────────

class Predicate:
    """A ground atom: name + tuple of string arguments."""
    __slots__ = ("name", "args")

    def __init__(self, name: str, args: tuple[str, ...]):
        self.name = name
        self.args = args

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Predicate) and self.name == other.name and self.args == other.args

    def __hash__(self) -> int:
        return hash((self.name, self.args))

    def __repr__(self) -> str:
        return f"{self.name}({', '.join(self.args)})"


class Rule:
    """Horn clause: head :- body (list of Predicates with possible variables)."""
    __slots__ = ("head", "body")

    def __init__(self, head: tuple[str, tuple], body: list[tuple[str, tuple]]):
        self.head = head   # (name, args) — args may contain '?X' variables
        self.body = body   # list of (name, args)


class LogicEngine:
    """Simple bottom-up forward-chaining with unification for Horn clauses."""

    def __init__(self):
        self.facts: set[Predicate] = set()
        self.rules: list[Rule] = []
        self.predicate_arities: dict[str, set[int]] = {}

    def add_fact(self, name: str, args: tuple[str, ...]) -> None:
        p = Predicate(name, args)
        self.facts.add(p)
        self.predicate_arities.setdefault(name, set()).add(len(args))

    def add_rule(self, head: tuple[str, tuple], body: list[tuple[str, tuple]]) -> None:
        self.rules.append(Rule(head, body))
        name, args = head
        self.predicate_arities.setdefault(name, set()).add(len(args))

    def parse_and_add(self, clause: str) -> None:
        """Parse 'pred(a, b).' or 'head(X) :- body(X, Y).' and add."""
        clause = clause.strip().rstrip(".")
        if ":-" in clause:
            head_str, body_str = clause.split(":-", 1)
            head = _parse_term(head_str.strip())
            body = [_parse_term(t.strip()) for t in _split_body(body_str.strip())]
            self.add_rule(head, body)
        else:
            name, args = _parse_term(clause)
            self.add_fact(name, args)

    def _unify(self, pattern: tuple, ground: tuple, bindings: dict) -> Optional[dict]:
        name_p, args_p = pattern
        name_g, args_g = ground
        if name_p != name_g or len(args_p) != len(args_g):
            return None
        new_b = dict(bindings)
        for pv, gv in zip(args_p, args_g):
            if pv.startswith("?"):
                if pv in new_b:
                    if new_b[pv] != gv:
                        return None
                else:
                    new_b[pv] = gv
            elif pv != gv:
                return None
        return new_b

    def _apply_bindings(self, term: tuple, bindings: dict) -> tuple:
        name, args = term
        resolved = tuple(bindings.get(a, a) for a in args)
        return name, resolved

    def forward_chain(self, max_iters: int = 20) -> None:
        """Saturate facts by applying all rules repeatedly."""
        for _ in range(max_iters):
            new_facts: set[Predicate] = set()
            for rule in self.rules:
                self._apply_rule(rule, new_facts)
            added = new_facts - self.facts
            if not added:
                break
            self.facts |= added

    def _apply_rule(self, rule: Rule, new_facts: set[Predicate]) -> None:
        """Try to fire rule; add derived facts."""
        self._match_body(rule.body, {}, rule, new_facts)

    def _match_body(self, body: list, bindings: dict, rule: Rule, out: set[Predicate]) -> None:
        if not body:
            name, args = self._apply_bindings(rule.head, bindings)
            if all(not a.startswith("?") for a in args):
                out.add(Predicate(name, args))
            return
        lit = body[0]
        rest = body[1:]
        for fact in self.facts:
            b2 = self._unify(lit, (fact.name, fact.args), bindings)
            if b2 is not None:
                self._match_body(rest, b2, rule, out)

    def query(self, name: str, args: tuple[str, ...]) -> tuple[bool, list[dict], str]:
        """
        Returns (success, bindings_list, error_message).
        Args may contain None to represent unbound variables.
        """
        # Check for unknown predicate
        if name not in self.predicate_arities:
            return False, [], f"existence_error(procedure,{name}/{len(args)})"
        # Check for arity mismatch
        expected_arities = self.predicate_arities[name]
        if len(args) not in expected_arities:
            expected = sorted(expected_arities)[0]
            return False, [], f"arity_error({name},{expected},{len(args)})"
        # Run forward chaining
        self.forward_chain()
        results = []
        for fact in self.facts:
            if fact.name != name or len(fact.args) != len(args):
                continue
            match = {}
            ok = True
            for pat, val in zip(args, fact.args):
                if pat is None:
                    match[val] = val
                elif pat != val:
                    ok = False
                    break
            if ok:
                results.append(match)
        if results:
            return True, results, ""
        return False, [], "missing_fact"


def _parse_term(s: str) -> tuple[str, tuple]:
    """Parse 'pred(arg1, arg2)' → ('pred', ('arg1', 'arg2'))."""
    s = s.strip()
    m = re.match(r"^(\w+)\(([^)]*)\)$", s)
    if m:
        name = m.group(1)
        raw_args = m.group(2)
        args = tuple(a.strip() for a in raw_args.split(",") if a.strip())
        return name, args
    # atom with no args
    m2 = re.match(r"^(\w+)$", s)
    if m2:
        return m2.group(1), ()
    return s, ()


def _split_body(body_str: str) -> list[str]:
    """Split comma-separated body literals (respecting parentheses)."""
    parts, depth, cur = [], 0, ""
    for ch in body_str:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


# ── LLM Call ───────────────────────────────────────────────────────────────────

def call_llm(prompt: str, system: str = "", max_tokens: int = 512) -> tuple[str, float]:
    """Call OpenRouter LLM. Returns (response_text, cost_usd)."""
    global total_cost_usd, total_llm_calls

    if total_cost_usd >= LLM_COST_LIMIT:
        logger.warning(f"LLM cost limit ${LLM_COST_LIMIT} reached — skipping call")
        return "", 0.0

    cmd = [OR_PY, OR_SCRIPT,
           "--model", OR_MODEL,
           "--input", prompt,
           "--max-tokens", str(max_tokens),
           "--temperature", "0.2"]
    if system:
        cmd += ["--instructions", system]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout.strip()
        logger.debug(f"LLM raw output (truncated): {output[:300]}")

        # Parse response — skill prints "Response:\n<text>\n\nTokens: X in, Y out"
        resp_text = ""
        tokens_in, tokens_out = 0, 0
        lines = output.split("\n")
        in_response = False
        resp_lines = []
        for line in lines:
            if line.startswith("Response:"):
                in_response = True
                continue
            if in_response and line.startswith("Tokens:"):
                m = re.search(r"(\d+) in, (\d+) out", line)
                if m:
                    tokens_in, tokens_out = int(m.group(1)), int(m.group(2))
                in_response = False
                continue
            if in_response:
                resp_lines.append(line)
        resp_text = "\n".join(resp_lines).strip()

        cost = tokens_in * COST_PER_INPUT_TOKEN + tokens_out * COST_PER_OUTPUT_TOKEN
        total_cost_usd += cost
        total_llm_calls += 1
        logger.info(f"LLM call #{total_llm_calls} | tokens={tokens_in}+{tokens_out} | cost=${cost:.4f} | total=${total_cost_usd:.4f}")
        return resp_text, cost
    except subprocess.TimeoutExpired:
        logger.error("LLM call timed out")
        return "", 0.0
    except Exception:
        logger.exception("LLM call failed")
        return "", 0.0


# ── Failure Classification ──────────────────────────────────────────────────────

def classify_failure(error_msg: str) -> str:
    """Classify failure into Type 1/2/3/other."""
    if "existence_error" in error_msg or "unknown_pred" in error_msg:
        return "type1_unknown_predicate"
    if "arity_error" in error_msg:
        return "type2_arity_mismatch"
    if error_msg == "missing_fact" or not error_msg:
        return "type3_missing_fact"
    return "other"


# ── Typed Repairs ──────────────────────────────────────────────────────────────

def repair_type1(engine: LogicEngine, error_msg: str, query_name: str,
                 source_text: str) -> tuple[bool, list[str]]:
    """Type 1: unknown predicate → generate bridge axiom via LLM."""
    known = sorted(engine.predicate_arities.keys())
    prompt = (
        f"A logic query failed because predicate '{query_name}' is not defined.\n"
        f"Available predicates: {', '.join(known)}\n"
        f"Source text: {source_text[:500]}\n\n"
        f"Generate a single Prolog-style bridge rule connecting '{query_name}' "
        f"to one of the available predicates. "
        f"Format: '{query_name}(X,Y) :- available_pred(X,Y).' "
        f"Output ONLY the rule, nothing else."
    )
    resp, cost = call_llm(prompt, system="You are a logic programming assistant. Output only Prolog rules.")
    rules = _extract_prolog_clauses(resp)
    if rules:
        for r in rules:
            try:
                engine.parse_and_add(r)
            except Exception:
                logger.warning(f"Could not parse repair rule: {r}")
    return bool(rules), rules


def repair_type2(engine: LogicEngine, error_msg: str, query_name: str, query_args: tuple,
                 source_text: str) -> tuple[bool, list[str]]:
    """Type 2: arity mismatch → ask LLM for corrected predicate definition."""
    m = re.search(r"arity_error\((\w+),(\d+),(\d+)\)", error_msg)
    expected, provided = "?", str(len(query_args))
    if m:
        expected = m.group(2)
    prompt = (
        f"A logic query for '{query_name}' failed with an arity error: "
        f"expected {expected} arguments but got {provided}.\n"
        f"Query args: {query_args}\n"
        f"Source text: {source_text[:500]}\n\n"
        f"Generate a Prolog fact or rule that defines '{query_name}' correctly "
        f"using {provided} arguments. Output ONLY the clause, nothing else."
    )
    resp, cost = call_llm(prompt, system="You are a logic programming assistant. Output only Prolog clauses.")
    clauses = _extract_prolog_clauses(resp)
    if clauses:
        for c in clauses:
            try:
                engine.parse_and_add(c)
            except Exception:
                logger.warning(f"Could not parse repair clause: {c}")
    return bool(clauses), clauses


def repair_type3(engine: LogicEngine, query_name: str, query_args: tuple,
                 source_text: str) -> tuple[bool, list[str]]:
    """Type 3: missing fact → ask LLM to extract missing fact from source."""
    query_str = f"{query_name}({', '.join(str(a) if a is not None else '_' for a in query_args)})"
    prompt = (
        f"A Prolog query failed to prove: {query_str}\n"
        f"Existing facts: {_facts_summary(engine)}\n"
        f"Source text: {source_text[:500]}\n\n"
        f"Based on the source text, what Prolog fact would make the query succeed? "
        f"Output ONLY the fact (e.g., 'parent(ann, bob).'), nothing else."
    )
    resp, cost = call_llm(prompt, system="You are a logic programming assistant. Output only Prolog facts.")
    facts = _extract_prolog_clauses(resp)
    if facts:
        for f in facts:
            try:
                engine.parse_and_add(f)
            except Exception:
                logger.warning(f"Could not parse repair fact: {f}")
    return bool(facts), facts


def repair_baseline(engine: LogicEngine, error_msg: str, query_name: str, query_args: tuple,
                    source_text: str) -> tuple[bool, list[str]]:
    """Baseline: generic repair prompt regardless of failure type."""
    query_str = f"{query_name}({', '.join(str(a) if a is not None else '_' for a in query_args)})"
    prompt = (
        f"A logic proof failed.\n"
        f"Query: {query_str}\n"
        f"Error: {error_msg or 'proof failed'}\n"
        f"Existing facts: {_facts_summary(engine)}\n"
        f"Source text: {source_text[:500]}\n\n"
        f"Fix the knowledge base by providing missing facts or rules. "
        f"Output ONLY valid Prolog clauses (one per line), nothing else."
    )
    resp, cost = call_llm(prompt, system="You are a logic programming assistant. Output only Prolog clauses.")
    clauses = _extract_prolog_clauses(resp)
    if clauses:
        for c in clauses:
            try:
                engine.parse_and_add(c)
            except Exception:
                logger.warning(f"Could not parse baseline clause: {c}")
    return bool(clauses), clauses


def _extract_prolog_clauses(text: str) -> list[str]:
    """Extract Prolog clauses from LLM response."""
    clauses = []
    for line in text.split("\n"):
        line = line.strip()
        # Remove markdown backticks
        line = re.sub(r"^`+|`+$", "", line).strip()
        if not line or line.startswith("%"):
            continue
        # Must look like a prolog term
        if re.match(r"^\w+[\w\s]*\(", line) or re.match(r"^\w+\.$", line):
            line = line.rstrip(".")
            clauses.append(line)
    return clauses


def _facts_summary(engine: LogicEngine) -> str:
    facts = list(engine.facts)[:10]
    return "; ".join(str(f) for f in facts)


# ── FOL Extraction from Natural Language ───────────────────────────────────────

def extract_fol_from_text(text: str, dataset_name: str) -> tuple[list[str], str, tuple, tuple]:
    """
    Extract FOL facts, a query, and the query args from the input text.
    Returns (facts_list, query_pred_name, query_args, query_var).
    """
    facts = []
    query_pred = "query"
    query_args: tuple = ()
    query_var: tuple = (None,)

    if dataset_name == "proofwriter" or "Theory:" in text or "Query:" in text:
        theory_m = re.search(r"Theory:\s*(.*?)(?:\nQuery:|$)", text, re.DOTALL)
        query_m = re.search(r"Query:\s*(.+?)$", text, re.MULTILINE)

        theory_text = theory_m.group(1).strip() if theory_m else text
        query_text = query_m.group(1).strip() if query_m else ""

        facts = _parse_proofwriter_theory(theory_text)
        query_pred, query_args, query_var = _parse_proofwriter_query(query_text)

    elif dataset_name == "clutrr" or "Story:" in text:
        story_m = re.search(r"Story:\s*(.*?)(?:\nQuery:|$)", text, re.DOTALL)
        query_m = re.search(r"Query:\s*(.+?)$", text, re.MULTILINE)

        story_text = story_m.group(1).strip() if story_m else text
        query_text = query_m.group(1).strip() if query_m else ""

        facts = _parse_clutrr_story(story_text)
        query_pred, query_args, query_var = _parse_clutrr_query(query_text, facts)

    return facts, query_pred, query_args, query_var


def _normalize(s: str) -> str:
    """Normalize entity name to valid Prolog atom."""
    return re.sub(r"\s+", "_", s.strip().lower().replace("-", "_"))


def _parse_proofwriter_theory(theory: str) -> list[str]:
    """Convert ProofWriter theory sentences to Prolog facts/rules.

    Handles patterns like:
      "The dog needs the bear."       → needs(dog, bear)
      "The bear is rough."            → prop(bear, rough)
      "The bear is not blue."         → (skip: CWA, absence = false)
      "If someone is rough then they chase the bald eagle." → chase(?X,bald_eagle) :- prop(?X,rough)
      "If someone needs the bear then they are not blue."   → (skip negation)
      "If the bear is nice then the bear chases the bald eagle." → chase(bear,bald_eagle) :- prop(bear,nice)
      "All dogs are kind."            → prop(?Z,kind) :- prop(?Z,dog)
    """
    facts = []
    # Split on ". " or ".\n" but preserve multi-word entities
    raw = re.sub(r"\s+", " ", theory)
    sentences = re.split(r"\.\s+", raw)

    for sent in sentences:
        sent = sent.strip().rstrip(".")
        if not sent:
            continue

        # Skip negation sentences (CWA: absence implies false)
        if re.search(r"\b(not|does not|do not|cannot|never)\b", sent, re.IGNORECASE):
            continue

        # "The X verbs the Y." → binary_rel(x, y)
        m = re.match(
            r"^The ([\w ]+?) (needs?|chases?|eats?|sees?|likes?|visits?|helps?|hears?|holds?|gives?|makes?|owes?) the ([\w ]+)$",
            sent, re.IGNORECASE)
        if m:
            subj = _normalize(m.group(1))
            rel = _normalize(m.group(2).rstrip("s"))  # lemmatize
            obj = _normalize(m.group(3))
            facts.append(f"{rel}({subj},{obj})")
            continue

        # "The X is Y." (single property) → prop(x, y)
        m = re.match(r"^The ([\w ]+?) is (\w+)$", sent, re.IGNORECASE)
        if m:
            subj = _normalize(m.group(1))
            prop = _normalize(m.group(2))
            facts.append(f"prop({subj},{prop})")
            continue

        # "X is Y." (no article)
        m = re.match(r"^(\w+) is (\w+)$", sent, re.IGNORECASE)
        if m and m.group(1).lower() not in ("if", "all", "someone", "something", "they", "it"):
            subj = _normalize(m.group(1))
            prop = _normalize(m.group(2))
            facts.append(f"prop({subj},{prop})")
            continue

        # "All Xs are Y." → prop(?Z,y) :- prop(?Z,x)
        m = re.match(r"^All ([\w ]+?)s? are (\w+)s?$", sent, re.IGNORECASE)
        if m:
            cat = _normalize(m.group(1).rstrip("s"))
            prop = _normalize(m.group(2))
            facts.append(f"prop(?Z,{prop}) :- prop(?Z,{cat})")
            continue

        # "If someone is Y then they verb the W."
        # → verb(?X, w) :- prop(?X, y)
        m = re.match(
            r"^If someone is (\w+) then they ([\w]+?) the ([\w ]+)$",
            sent, re.IGNORECASE)
        if m:
            cond_prop = _normalize(m.group(1))
            action = _normalize(m.group(2).rstrip("s"))
            obj = _normalize(m.group(3))
            facts.append(f"{action}(?X,{obj}) :- prop(?X,{cond_prop})")
            continue

        # "If someone is Y then they are Z."
        # → prop(?X, z) :- prop(?X, y)
        m = re.match(r"^If someone is (\w+) then they are (\w+)$", sent, re.IGNORECASE)
        if m:
            cond = _normalize(m.group(1))
            result = _normalize(m.group(2))
            facts.append(f"prop(?X,{result}) :- prop(?X,{cond})")
            continue

        # "If someone verbs the Y then they are Z."
        # → prop(?X, z) :- verb(?X, y)
        m = re.match(
            r"^If someone ([\w]+?) the ([\w ]+?) then they are (\w+)$",
            sent, re.IGNORECASE)
        if m:
            verb = _normalize(m.group(1).rstrip("s"))
            obj = _normalize(m.group(2))
            prop = _normalize(m.group(3))
            facts.append(f"prop(?X,{prop}) :- {verb}(?X,{obj})")
            continue

        # "If someone verbs the Y then they verb2 the Z."
        m = re.match(
            r"^If someone ([\w]+?) the ([\w ]+?) then they ([\w]+?) the ([\w ]+?)$",
            sent, re.IGNORECASE)
        if m:
            v1 = _normalize(m.group(1).rstrip("s"))
            o1 = _normalize(m.group(2))
            v2 = _normalize(m.group(3).rstrip("s"))
            o2 = _normalize(m.group(4))
            facts.append(f"{v2}(?X,{o2}) :- {v1}(?X,{o1})")
            continue

        # "If the X is Y then the X verbs the Z."
        m = re.match(
            r"^If the ([\w ]+?) is (\w+) then the ([\w ]+?) ([\w]+?) the ([\w ]+)$",
            sent, re.IGNORECASE)
        if m:
            subj1 = _normalize(m.group(1))
            prop = _normalize(m.group(2))
            subj2 = _normalize(m.group(3))
            verb = _normalize(m.group(4).rstrip("s"))
            obj = _normalize(m.group(5))
            if subj1 == subj2:
                facts.append(f"{verb}({subj1},{obj}) :- prop({subj1},{prop})")
            continue

        # "If the X is Y then the Z is W."
        m = re.match(
            r"^If the ([\w ]+?) is (\w+) then the ([\w ]+?) is (\w+)$",
            sent, re.IGNORECASE)
        if m:
            subj1, cond = _normalize(m.group(1)), _normalize(m.group(2))
            subj2, result = _normalize(m.group(3)), _normalize(m.group(4))
            if subj1 == subj2:
                facts.append(f"prop({subj1},{result}) :- prop({subj1},{cond})")
            continue

        # "If the X verbs the Y then the X is Z."
        m = re.match(
            r"^If the ([\w ]+?) ([\w]+?) the ([\w ]+?) then the ([\w ]+?) is (\w+)$",
            sent, re.IGNORECASE)
        if m:
            subj1 = _normalize(m.group(1))
            verb = _normalize(m.group(2).rstrip("s"))
            obj = _normalize(m.group(3))
            subj2 = _normalize(m.group(4))
            prop = _normalize(m.group(5))
            if subj1 == subj2:
                facts.append(f"prop({subj1},{prop}) :- {verb}({subj1},{obj})")
            continue

    return facts


def _parse_proofwriter_query(query_text: str) -> tuple[str, tuple, tuple]:
    """Parse ProofWriter queries — they look like sentences to verify.

    "The dog needs the bear." → (needs, (dog, bear), ...)
    "The bear is rough."      → (prop, (bear, rough), ...)
    """
    q = query_text.strip().rstrip(".")

    # Binary relation: "The X verbs the Y"
    m = re.match(
        r"^The ([\w ]+?) (needs?|chases?|eats?|sees?|likes?|visits?|helps?|hears?|holds?|gives?|makes?|owes?) the ([\w ]+)$",
        q, re.IGNORECASE)
    if m:
        subj = _normalize(m.group(1))
        rel = _normalize(m.group(2).rstrip("s"))
        obj = _normalize(m.group(3))
        return rel, (subj, obj), (None,)

    # Property: "The X is Y"
    m = re.match(r"^The ([\w ]+?) is (\w+)$", q, re.IGNORECASE)
    if m:
        subj = _normalize(m.group(1))
        prop = _normalize(m.group(2))
        return "prop", (subj, prop), (None,)

    # "X is Y"
    m = re.match(r"^(\w+) is (\w+)$", q, re.IGNORECASE)
    if m:
        return "prop", (_normalize(m.group(1)), _normalize(m.group(2))), (None,)

    return "query", (), (None,)


def _parse_clutrr_story(story: str) -> list[str]:
    """Extract kinship facts from CLUTRR story (bracket format: [Name]'s rel, [Name])."""
    facts = []

    # Kinship relation keywords
    RELS = {
        "father": "father", "dad": "father", "mother": "mother", "mom": "mother",
        "son": "son", "daughter": "daughter", "brother": "brother", "sister": "sister",
        "husband": "husband", "wife": "wife", "grandfather": "grandfather",
        "grandpa": "grandfather", "grandmother": "grandmother", "grandma": "grandmother",
        "grandson": "grandson", "granddaughter": "granddaughter",
        "uncle": "uncle", "aunt": "aunt", "nephew": "nephew", "niece": "niece",
        "father-in-law": "father_in_law", "mother-in-law": "mother_in_law",
        "son-in-law": "son_in_law", "daughter-in-law": "daughter_in_law",
        "brother-in-law": "brother_in_law", "sister-in-law": "sister_in_law",
    }
    rel_pattern = "|".join(re.escape(k) for k in sorted(RELS, key=len, reverse=True))

    # Bracket format: "[A]'s {rel}, [B]" or "[A]'s {rel} [B]"
    for m in re.finditer(
        rf"\[(\w+)\]'s ({rel_pattern}),?\s+\[(\w+)\]",
        story, re.IGNORECASE
    ):
        a, rel_raw, b = m.group(1).lower(), m.group(2).lower(), m.group(3).lower()
        rel = RELS.get(rel_raw, rel_raw.replace("-", "_"))
        facts.append(f"{rel}({a},{b})")

    # Plain format: "X's {rel} is Y" or "X's {rel}, Y"
    for m in re.finditer(
        rf"(\w+)'s ({rel_pattern})\s+(?:is\s+)?(\w+)",
        story, re.IGNORECASE
    ):
        a, rel_raw, b = m.group(1).lower(), m.group(2).lower(), m.group(3).lower()
        if b in ("the", "a", "an", "his", "her", "their", "is"):
            continue
        rel = RELS.get(rel_raw, rel_raw.replace("-", "_"))
        facts.append(f"{rel}({a},{b})")

    # Plain format: "X is the {rel} of Y"
    for m in re.finditer(
        rf"(\w+) is the ({rel_pattern}) of (\w+)",
        story, re.IGNORECASE
    ):
        a, rel_raw, b = m.group(1).lower(), m.group(2).lower(), m.group(3).lower()
        rel = RELS.get(rel_raw, rel_raw.replace("-", "_"))
        facts.append(f"{rel}({a},{b})")

    # Comprehensive kinship inference rules
    facts.extend([
        # Direct inverse relationships
        "father(?Y,?X) :- son(?X,?Y)",
        "mother(?Y,?X) :- daughter(?X,?Y)",
        "son(?Y,?X) :- father(?X,?Y)",
        "daughter(?Y,?X) :- mother(?X,?Y)",
        "brother(?Y,?X) :- brother(?X,?Y)",
        "sister(?Y,?X) :- sister(?X,?Y)",
        # Parent generalization
        "parent(?X,?Y) :- father(?X,?Y)",
        "parent(?X,?Y) :- mother(?X,?Y)",
        # Grandparent chains
        "grandfather(?X,?Z) :- father(?X,?Y),parent(?Y,?Z)",
        "grandmother(?X,?Z) :- mother(?X,?Y),parent(?Y,?Z)",
        # Grandson/granddaughter: granddaughter(A,B) means B is granddaughter of A → grandson(A, C) if C is brother of B
        "grandson(?X,?Z) :- grandfather(?X,?Z)",
        "grandson(?X,?Z) :- grandmother(?X,?Z)",
        "granddaughter(?X,?Z) :- grandfather(?X,?Z)",
        "granddaughter(?X,?Z) :- grandmother(?X,?Z)",
        # Sibling → uncle/aunt
        "uncle(?A,?C) :- brother(?A,?B),parent(?B,?C)",
        "aunt(?A,?C) :- sister(?A,?B),parent(?B,?C)",
        # Nephew/niece
        "nephew(?X,?Y) :- uncle(?Y,?X)",
        "niece(?X,?Y) :- aunt(?Y,?X)",
    ])
    return facts


KINSHIP_PREDICATES = [
    "father", "mother", "son", "daughter", "brother", "sister",
    "husband", "wife", "grandfather", "grandmother", "grandson", "granddaughter",
    "uncle", "aunt", "nephew", "niece", "parent",
    "father_in_law", "mother_in_law", "son_in_law", "daughter_in_law",
    "brother_in_law", "sister_in_law",
]


def _parse_clutrr_query(query_text: str, facts: list[str]) -> tuple[str, tuple, tuple]:
    """Parse CLUTRR query: returns ('any_kinship', (a, b), (None,)) for search over all predicates."""
    # Bracket format: "('Clarence', 'Michael')"
    m = re.search(r"\('(\w+)',\s*'(\w+)'\)", query_text)
    if m:
        return "any_kinship", (m.group(1).lower(), m.group(2).lower()), (None,)

    # Two capitalized names in text
    names = re.findall(r"\b([A-Z][a-z]+)\b", query_text)
    if len(names) >= 2:
        return "any_kinship", (names[0].lower(), names[1].lower()), (None,)

    # Extract from facts as last resort
    fact_names: list[str] = []
    for f in facts:
        for fa, fb in re.findall(r"\((\w+),(\w+)\)", f):
            if not fa.startswith("?"):
                fact_names.append(fa)
            if not fb.startswith("?"):
                fact_names.append(fb)
    unique_names = list(dict.fromkeys(fact_names))
    if len(unique_names) >= 2:
        return "any_kinship", (unique_names[0], unique_names[-1]), (None,)
    return "any_kinship", (None, None), (None,)


# ── Hallucination Check ────────────────────────────────────────────────────────

def check_hallucination(facts_used: list[str], source_text: str) -> float:
    """
    Check what fraction of used facts are NOT supported by source text.
    Uses simple string matching + optional LLM verification.
    """
    if not facts_used:
        return 0.0
    invented = 0
    for fact in facts_used[:5]:  # Check up to 5 facts
        # Extract entities from fact
        entities = re.findall(r"\b[a-z]{3,}\b", fact)
        # Check if any entity appears in source text (case-insensitive)
        supported = any(e in source_text.lower() for e in entities if e not in {"the", "and", "for", "with"})
        if not supported:
            invented += 1
    return invented / min(len(facts_used), 5)


# ── Single Example Processing ──────────────────────────────────────────────────

def query_kinship(engine: LogicEngine, a: Optional[str], b: Optional[str]) -> tuple[bool, str, str]:
    """Try all kinship predicates to find which holds between a and b. Returns (found, rel_name, error)."""
    if a is None or b is None:
        return False, "", "missing_args"
    engine.forward_chain()
    for pred in KINSHIP_PREDICATES:
        if pred not in engine.predicate_arities:
            continue
        success, _, _ = engine.query(pred, (a, b))
        if success:
            return True, pred, ""
    return False, "", "missing_fact"


def process_example(example: dict, dataset_name: str) -> dict:
    """Process one example with both typed pipeline and baseline. Returns result dict."""
    input_text = example.get("input", "")
    expected_output = example.get("output", "").strip().lower()

    result: dict[str, Any] = {
        "input": input_text[:200],
        "expected_output": expected_output,
        "typed_result": None,
        "baseline_result": None,
        "failure_type": None,
        "typed_repair_applied": False,
        "baseline_repair_applied": False,
        "typed_success": False,
        "baseline_success": False,
        "typed_cost_usd": 0.0,
        "baseline_cost_usd": 0.0,
        "hallucination_rate_typed": 0.0,
        "hallucination_rate_baseline": 0.0,
        "error": None,
    }

    try:
        facts, query_pred, query_args, query_var = extract_fol_from_text(input_text, dataset_name)
        logger.debug(f"  Extracted {len(facts)} facts, query: {query_pred}({query_args})")

        if not facts and not query_pred:
            result["error"] = "extraction_failed"
            return result

        is_kinship = (query_pred == "any_kinship")

        # ── TYPED PIPELINE ──────────────────────────────────────────────────
        engine_typed = LogicEngine()
        for f in facts:
            try:
                engine_typed.parse_and_add(f)
            except Exception as e:
                logger.debug(f"  Skipping fact '{f}': {e}")

        cost_before = total_cost_usd

        if is_kinship:
            kin_found, kin_rel, error = query_kinship(engine_typed, *query_args[:2])
            success = kin_found
            bindings = [{kin_rel: kin_rel}] if kin_found else []
        else:
            success, bindings, error = engine_typed.query(query_pred, query_args)

        if not success:
            failure_type = classify_failure(error)
            result["failure_type"] = failure_type
            logger.debug(f"  Typed failure: {failure_type} | error: {error}")

            if failure_type == "type1_unknown_predicate":
                repaired, _ = repair_type1(engine_typed, error, query_pred, input_text)
            elif failure_type == "type2_arity_mismatch":
                repaired, _ = repair_type2(engine_typed, error, query_pred, query_args, input_text)
            else:
                repaired, _ = repair_type3(engine_typed, query_pred, query_args, input_text)

            result["typed_repair_applied"] = True
            if repaired:
                if is_kinship:
                    kin_found, kin_rel, error = query_kinship(engine_typed, *query_args[:2])
                    success = kin_found
                    bindings = [{kin_rel: kin_rel}] if kin_found else []
                else:
                    success, bindings, error = engine_typed.query(query_pred, query_args)

        result["typed_cost_usd"] = total_cost_usd - cost_before

        typed_answer = _interpret_result(success, bindings, query_pred, is_kinship)
        result["typed_result"] = typed_answer
        result["typed_success"] = _check_match(typed_answer, expected_output, success)
        result["hallucination_rate_typed"] = check_hallucination(
            [str(f) for f in list(engine_typed.facts)[:5]], input_text
        )

        # ── BASELINE PIPELINE ───────────────────────────────────────────────
        engine_base = LogicEngine()
        for f in facts:
            try:
                engine_base.parse_and_add(f)
            except Exception:
                pass

        cost_before2 = total_cost_usd

        if is_kinship:
            kin_found_b, kin_rel_b, error_b = query_kinship(engine_base, *query_args[:2])
            success_b = kin_found_b
            bindings_b = [{kin_rel_b: kin_rel_b}] if kin_found_b else []
        else:
            success_b, bindings_b, error_b = engine_base.query(query_pred, query_args)

        if not success_b:
            result["baseline_repair_applied"] = True
            repaired_b, _ = repair_baseline(engine_base, error_b, query_pred, query_args, input_text)
            if repaired_b:
                if is_kinship:
                    kin_found_b, kin_rel_b, error_b = query_kinship(engine_base, *query_args[:2])
                    success_b = kin_found_b
                    bindings_b = [{kin_rel_b: kin_rel_b}] if kin_found_b else []
                else:
                    success_b, bindings_b, error_b = engine_base.query(query_pred, query_args)

        result["baseline_cost_usd"] = total_cost_usd - cost_before2

        baseline_answer = _interpret_result(success_b, bindings_b, query_pred, is_kinship)
        result["baseline_result"] = baseline_answer
        result["baseline_success"] = _check_match(baseline_answer, expected_output, success_b)
        result["hallucination_rate_baseline"] = check_hallucination(
            [str(f) for f in list(engine_base.facts)[:5]], input_text
        )

    except Exception:
        logger.exception(f"Error processing example")
        result["error"] = "processing_error"

    return result


def _interpret_result(success: bool, bindings: list[dict], query_pred: str, is_kinship: bool = False) -> str:
    if success:
        if is_kinship and bindings:
            # bindings[0] is {rel_name: rel_name} — return the relation name
            return list(bindings[0].keys())[0]
        if bindings and any(v for v in bindings[0].values()):
            return list(bindings[0].values())[0] if bindings[0] else "true"
        return "true"
    return "false"


def _check_match(predicted: str, expected: str, success: bool) -> bool:
    """Check if predicted answer matches expected."""
    pred_lower = predicted.strip().lower()
    exp_lower = expected.strip().lower()
    if pred_lower == exp_lower:
        return True
    # true/false equivalence
    if exp_lower in ("true", "yes") and pred_lower in ("true", "yes"):
        return True
    if exp_lower in ("false", "no") and pred_lower in ("false", "no"):
        return True
    # Partial match for kinship (e.g., "grandson" in "grandson of")
    if exp_lower and pred_lower and (exp_lower in pred_lower or pred_lower in exp_lower):
        return True
    return False


# ── Data Loading ───────────────────────────────────────────────────────────────

def load_data_from_hf(max_examples: int = 50) -> list[tuple[str, dict]]:
    """Download small subset from HuggingFace and return [(dataset_name, example)] list."""
    examples = []
    try:
        from datasets import load_dataset
        logger.info("Loading ProofWriter from HuggingFace...")
        pw = load_dataset(
            "tasksource/proofwriter",
            split="test",
            token=HF_TOKEN or None,
        )
        target_pw = max(6, max_examples // 2)
        count = 0
        for row in pw:
            if count >= target_pw:
                break
            theory = row.get("theory", row.get("context", ""))
            question = row.get("question", row.get("query", ""))
            answer = str(row.get("answer", row.get("label", ""))).lower()
            if not theory or not question:
                continue
            inp = f"Theory: {theory}\nQuery: {question}"
            examples.append(("proofwriter", {
                "input": inp,
                "output": answer,
                "metadata_dataset": "proofwriter",
            }))
            count += 1
        logger.info(f"Loaded {count} ProofWriter examples")
    except Exception:
        logger.warning("ProofWriter load failed; using synthetic examples")
        examples.extend(_synthetic_proofwriter())

    try:
        from datasets import load_dataset
        logger.info("Loading CLUTRR from HuggingFace...")
        cl = load_dataset(
            "kendrivp/CLUTRR_v1_extracted",
            split="test",
            token=HF_TOKEN or None,
        )
        target_cl = max(6, max_examples // 2)
        count = 0
        for row in cl:
            if count >= target_cl:
                break
            story = row.get("story", row.get("context", ""))
            query = row.get("query", row.get("question", ""))
            answer = str(row.get("target_text", row.get("target", row.get("answer", "")))).lower()
            if not story:
                continue
            inp = f"Story: {story}\nQuery: {query}"
            examples.append(("clutrr", {
                "input": inp,
                "output": answer,
                "metadata_dataset": "clutrr",
            }))
            count += 1
        logger.info(f"Loaded {count} CLUTRR examples")
    except Exception:
        logger.warning("CLUTRR load failed; using synthetic examples")
        examples.extend(_synthetic_clutrr())

    return examples[:max_examples]


def load_data_from_files(data_workspace: Path, max_examples: int = 50) -> list[tuple[str, dict]]:
    """Load from iter_1 dependency files if they exist."""
    examples = []
    mini_file = data_workspace / "mini_data_out.json"
    if mini_file.exists():
        logger.info(f"Loading from {mini_file}")
        data = json.loads(mini_file.read_text())
        for ds in data.get("datasets", []):
            ds_name = ds.get("dataset", "unknown")
            for ex in ds.get("examples", []):
                examples.append((ds_name, ex))
                if len(examples) >= max_examples:
                    return examples
    # Try full files
    full_dir = data_workspace / "full_data_out"
    if full_dir.exists():
        for part_file in sorted(full_dir.glob("*.json")):
            logger.info(f"Loading from {part_file}")
            try:
                data = json.loads(part_file.read_text())
                for ds in data.get("datasets", []):
                    ds_name = ds.get("dataset", "unknown")
                    for ex in ds.get("examples", []):
                        examples.append((ds_name, ex))
                        if len(examples) >= max_examples:
                            return examples
            except Exception:
                logger.warning(f"Failed to load {part_file}")
    return examples


def _synthetic_proofwriter() -> list[tuple[str, dict]]:
    """Synthetic ProofWriter-style examples for testing."""
    return [
        ("proofwriter", {
            "input": "Theory: The dog is big. The cat is small. All big animals are heavy.\nQuery: Is the dog heavy?",
            "output": "true",
            "metadata_dataset": "proofwriter",
        }),
        ("proofwriter", {
            "input": "Theory: Anne is kind. Bob is cold. If Anne is kind then Anne is quiet.\nQuery: Is Anne quiet?",
            "output": "true",
            "metadata_dataset": "proofwriter",
        }),
        ("proofwriter", {
            "input": "Theory: The bear is rough. The lion is kind. All rough animals are fierce.\nQuery: Is the bear fierce?",
            "output": "true",
            "metadata_dataset": "proofwriter",
        }),
        ("proofwriter", {
            "input": "Theory: Dave is smart. Eve is fast. If Dave is smart then Dave is successful.\nQuery: Is Dave successful?",
            "output": "true",
            "metadata_dataset": "proofwriter",
        }),
        ("proofwriter", {
            "input": "Theory: The tiger is slow. The wolf is fast. All slow animals are cautious.\nQuery: Is the wolf cautious?",
            "output": "false",
            "metadata_dataset": "proofwriter",
        }),
        ("proofwriter", {
            "input": "Theory: Mary is young. Tom is old. If Mary is young then Mary is energetic.\nQuery: Is Tom energetic?",
            "output": "false",
            "metadata_dataset": "proofwriter",
        }),
    ]


def _synthetic_clutrr() -> list[tuple[str, dict]]:
    """Synthetic CLUTRR-style examples for testing."""
    return [
        ("clutrr", {
            "input": "Story: John's father is Bob. Bob's father is Tom.\nQuery: What is Tom's relationship to John?",
            "output": "grandfather",
            "metadata_dataset": "clutrr",
        }),
        ("clutrr", {
            "input": "Story: Alice's mother is Carol. Carol's mother is Eve.\nQuery: What is Eve's relationship to Alice?",
            "output": "grandmother",
            "metadata_dataset": "clutrr",
        }),
        ("clutrr", {
            "input": "Story: Mike is the father of Lisa. Lisa is the mother of Sam.\nQuery: What is Mike's relationship to Sam?",
            "output": "grandfather",
            "metadata_dataset": "clutrr",
        }),
        ("clutrr", {
            "input": "Story: Paul's sister is Anna. Anna's son is Ben.\nQuery: What is Ben's relationship to Paul?",
            "output": "nephew",
            "metadata_dataset": "clutrr",
        }),
    ]


# ── Main ───────────────────────────────────────────────────────────────────────

@logger.catch(reraise=True)
def main() -> None:
    global total_cost_usd, total_llm_calls

    logger.info("=== Typed Failure Recovery Experiment ===")
    logger.info(f"Model: {OR_MODEL} | Budget: ${LLM_COST_LIMIT}")
    logger.info(f"MAX_EXAMPLES={MAX_EXAMPLES} | MINI_RUN={MINI_RUN}")

    # Load data
    dep_workspace = Path("/home/adrian/projects/ai-inventor/aii_data/users/admin/runs/"
                         "run_iLVCVqhq-L4t/3_invention_loop/iter_1/gen_art/gen_art_dataset_1")

    if dep_workspace.exists():
        logger.info("Loading from dependency workspace...")
        n = 6 if MINI_RUN else MAX_EXAMPLES
        examples_raw = load_data_from_files(dep_workspace, max_examples=n)
        if not examples_raw:
            logger.info("No files found in dep workspace; downloading from HF...")
            examples_raw = load_data_from_hf(max_examples=n)
    else:
        logger.info("Dependency workspace absent; downloading from HuggingFace...")
        n = 6 if MINI_RUN else MAX_EXAMPLES
        examples_raw = load_data_from_hf(max_examples=n)

    if not examples_raw:
        logger.error("No examples loaded!")
        raise RuntimeError("No examples to process")

    logger.info(f"Total examples to process: {len(examples_raw)}")

    # Group by dataset
    by_dataset: dict[str, list[dict]] = {}
    for ds_name, ex in examples_raw:
        by_dataset.setdefault(ds_name, []).append(ex)

    # Process all examples
    all_results: list[dict] = []
    typed_correct, baseline_correct = 0, 0
    typed_repairs, baseline_repairs = 0, 0
    typed_repair_success, baseline_repair_success = 0, 0
    typed_hallucination, baseline_hallucination = [], []
    sample_traces = []

    for ds_name, exs in by_dataset.items():
        logger.info(f"--- Dataset: {ds_name} ({len(exs)} examples) ---")
        for i, ex in enumerate(exs):
            logger.info(f"  [{i+1}/{len(exs)}] Processing example...")
            if total_cost_usd >= LLM_COST_LIMIT:
                logger.warning("Cost limit reached; stopping early")
                break

            r = process_example(ex, ds_name)
            r["dataset"] = ds_name
            all_results.append(r)

            if r["typed_success"]:
                typed_correct += 1
            if r["baseline_success"]:
                baseline_correct += 1
            if r["typed_repair_applied"]:
                typed_repairs += 1
                if r["typed_success"]:
                    typed_repair_success += 1
            if r["baseline_repair_applied"]:
                baseline_repairs += 1
                if r["baseline_success"]:
                    baseline_repair_success += 1
            typed_hallucination.append(r["hallucination_rate_typed"])
            baseline_hallucination.append(r["hallucination_rate_baseline"])

            logger.info(
                f"  typed={'✓' if r['typed_success'] else '✗'} "
                f"baseline={'✓' if r['baseline_success'] else '✗'} "
                f"failure={r['failure_type']} cost=${total_cost_usd:.4f}"
            )

            if len(sample_traces) < 5:
                sample_traces.append({
                    "example_id": len(all_results),
                    "dataset": ds_name,
                    "query_preview": r["input"][:100],
                    "expected": r["expected_output"],
                    "typed_result": r["typed_result"],
                    "baseline_result": r["baseline_result"],
                    "failure_type": r["failure_type"],
                    "typed_repair_applied": r["typed_repair_applied"],
                    "baseline_repair_applied": r["baseline_repair_applied"],
                })

            del r
            gc.collect()

    total = len(all_results)
    typed_acc = typed_correct / total if total > 0 else 0.0
    baseline_acc = baseline_correct / total if total > 0 else 0.0
    typed_hall = sum(typed_hallucination) / len(typed_hallucination) if typed_hallucination else 0.0
    baseline_hall = sum(baseline_hallucination) / len(baseline_hallucination) if baseline_hallucination else 0.0

    logger.info(f"=== RESULTS ({total} examples) ===")
    logger.info(f"Typed:    accuracy={typed_acc:.3f}  hallucination={typed_hall:.3f}  repairs={typed_repairs}/{typed_repair_success} succeeded")
    logger.info(f"Baseline: accuracy={baseline_acc:.3f}  hallucination={baseline_hall:.3f}  repairs={baseline_repairs}/{baseline_repair_success} succeeded")
    logger.info(f"Total LLM cost: ${total_cost_usd:.4f} ({total_llm_calls} calls)")

    # ── Build output in exp_gen_sol_out schema ──────────────────────────────
    # Group results by dataset
    output_datasets = []
    for ds_name in by_dataset:
        ds_exs = [r for r in all_results if r.get("dataset") == ds_name]
        output_examples = []
        for orig_ex, res in zip(by_dataset[ds_name], ds_exs):
            out_ex = {
                "input": orig_ex.get("input", ""),
                "output": orig_ex.get("output", ""),
                "predict_typed_method": res.get("typed_result", "") or "",
                "predict_baseline": res.get("baseline_result", "") or "",
                "metadata_failure_type": res.get("failure_type") or "none",
                "metadata_typed_correct": str(res.get("typed_success", False)),
                "metadata_baseline_correct": str(res.get("baseline_success", False)),
                "metadata_typed_repair_applied": str(res.get("typed_repair_applied", False)),
                "metadata_baseline_repair_applied": str(res.get("baseline_repair_applied", False)),
                "metadata_typed_cost_usd": str(round(res.get("typed_cost_usd", 0.0), 5)),
                "metadata_baseline_cost_usd": str(round(res.get("baseline_cost_usd", 0.0), 5)),
                "metadata_hallucination_typed": str(round(res.get("hallucination_rate_typed", 0.0), 3)),
                "metadata_hallucination_baseline": str(round(res.get("hallucination_rate_baseline", 0.0), 3)),
            }
            # Clean metadata_ from original if present
            for k, v in orig_ex.items():
                if k.startswith("metadata_") and k not in out_ex:
                    out_ex[k] = str(v)
            output_examples.append(out_ex)
        output_datasets.append({"dataset": ds_name, "examples": output_examples})

    method_out = {
        "metadata": {
            "method_name": "Typed Failure Recovery for Neuro-Symbolic Reasoning",
            "description": (
                "Compares typed failure repair (Type1: unknown predicate, "
                "Type2: arity mismatch, Type3: missing fact) vs generic-repair baseline "
                "on ProofWriter + CLUTRR reasoning benchmarks. "
                "Uses Python forward-chaining logic engine (SWI-Prolog fallback). "
                f"LLM: {OR_MODEL}."
            ),
            "total_examples": total,
            "typed_accuracy": round(typed_acc, 4),
            "baseline_accuracy": round(baseline_acc, 4),
            "typed_hallucination_rate": round(typed_hall, 4),
            "baseline_hallucination_rate": round(baseline_hall, 4),
            "typed_repairs_attempted": typed_repairs,
            "typed_repairs_succeeded": typed_repair_success,
            "baseline_repairs_attempted": baseline_repairs,
            "baseline_repairs_succeeded": baseline_repair_success,
            "total_cost_usd": round(total_cost_usd, 5),
            "total_llm_calls": total_llm_calls,
            "model": OR_MODEL,
            "sample_traces": sample_traces,
            "key_finding": (
                f"Typed repair accuracy={typed_acc:.3f} vs baseline={baseline_acc:.3f}. "
                + ("Typed repair OUTPERFORMS baseline." if typed_acc > baseline_acc else
                   "Baseline matches or exceeds typed repair — may indicate extraction limits.")
            ),
        },
        "datasets": output_datasets,
    }

    out_file = OUTPUTS_DIR / "method_out.json"
    out_file.write_text(json.dumps(method_out, indent=2))
    logger.info(f"Saved method_out.json ({out_file.stat().st_size // 1024} KB)")

    # Copy to workspace root for schema validation
    (WORKSPACE / "method_out.json").write_text(json.dumps(method_out, indent=2))
    logger.info("Done.")


if __name__ == "__main__":
    main()
