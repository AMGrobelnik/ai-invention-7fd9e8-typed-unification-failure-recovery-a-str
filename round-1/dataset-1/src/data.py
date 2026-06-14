#!/usr/bin/env python3
"""Load RuleTaker, ProofWriter, and CLUTRR datasets and standardize to exp_sel_data_out.json schema."""

import json
import sys
import re
from pathlib import Path

from loguru import logger

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/run.log", rotation="30 MB", level="DEBUG")

WS = Path(__file__).parent
DATASETS_DIR = WS / "temp" / "datasets"
OUTPUT_PATH = WS / "full_data_out.json"


def load_json_robust(path: Path) -> list[dict]:
    """Load a JSON array file, tolerating a truncated last element."""
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip().lstrip(",").rstrip(",")
            if line in ("", "[", "]"):
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # skip truncated last line
    return records


def parse_depth(config: str) -> int:
    """Extract reasoning depth integer from config string like 'depth-3'."""
    m = re.search(r"depth[-_](\d+)", str(config))
    return int(m.group(1)) if m else 1


def load_ruletaker() -> list[dict]:
    """Load RuleTaker train+test splits and convert to unified schema examples."""
    examples = []
    for split in ["train", "test"]:
        path = DATASETS_DIR / f"full_tasksource_ruletaker_default_{split}.json"
        if not path.exists():
            logger.warning(f"RuleTaker {split} not found: {path}")
            continue
        logger.info(f"Loading RuleTaker {split} from {path}")
        records = load_json_robust(path)
        logger.info(f"  Loaded {len(records)} records")
        for i, rec in enumerate(records):
            context = rec.get("context", "")
            question = rec.get("question", "")
            label = rec.get("label", "")
            config = rec.get("config", "depth-1")
            depth = parse_depth(config)
            expected = "true" if label == "entailment" else "false"
            # Build input: context + question
            input_text = f"Context: {context}\nQuery: {question}"
            examples.append({
                "input": input_text,
                "output": expected,
                "metadata_split": split,
                "metadata_config": config,
                "metadata_reasoning_depth": depth,
                "metadata_source_context": context,
                "metadata_query": question,
                "metadata_original_label": label,
                "metadata_row_index": i,
            })
    logger.info(f"RuleTaker: {len(examples)} total examples")
    return examples


def load_proofwriter() -> list[dict]:
    """Load ProofWriter train+test splits and convert to unified schema examples."""
    examples = []
    for split in ["train", "test"]:
        path = DATASETS_DIR / f"full_tasksource_proofwriter_default_{split}.json"
        if not path.exists():
            logger.warning(f"ProofWriter {split} not found: {path}")
            continue
        logger.info(f"Loading ProofWriter {split} from {path}")
        records = load_json_robust(path)
        logger.info(f"  Loaded {len(records)} records")
        for i, rec in enumerate(records):
            theory = rec.get("theory", "")
            question = rec.get("question", "")
            answer = rec.get("answer", "")
            config = rec.get("config", "depth-0")
            qdep = rec.get("QDep", 0)
            nfact = rec.get("NFact", 0)
            nrule = rec.get("NRule", 0)
            all_proofs = rec.get("allProofs", "")
            record_id = rec.get("id", f"pw_{split}_{i}")
            depth = int(qdep) if qdep is not None else parse_depth(config)
            expected = "true" if str(answer).strip().lower() == "true" else "false"
            input_text = f"Theory: {theory}\nQuery: {question}"
            examples.append({
                "input": input_text,
                "output": expected,
                "metadata_split": split,
                "metadata_config": config,
                "metadata_reasoning_depth": depth,
                "metadata_num_facts": int(nfact) if nfact else 0,
                "metadata_num_rules": int(nrule) if nrule else 0,
                "metadata_record_id": str(record_id),
                "metadata_all_proofs": str(all_proofs)[:500] if all_proofs else "",
                "metadata_row_index": i,
            })
    logger.info(f"ProofWriter: {len(examples)} total examples")
    return examples


def load_clutrr() -> list[dict]:
    """Load CLUTRR train+test splits and convert to unified schema examples."""
    examples = []
    for split in ["train", "test"]:
        path = DATASETS_DIR / f"full_kendrivp_CLUTRR_v1_extracted_default_{split}.json"
        if not path.exists():
            logger.warning(f"CLUTRR {split} not found: {path}")
            continue
        logger.info(f"Loading CLUTRR {split} from {path}")
        records = load_json_robust(path)
        logger.info(f"  Loaded {len(records)} records")
        for i, rec in enumerate(records):
            story = rec.get("story", "")
            query = rec.get("query", "")
            target_text = rec.get("target_text", "")
            proof_state = rec.get("proof_state", "")
            f_comb = rec.get("f_comb", "")
            task_name = rec.get("task_name", "")
            record_id = rec.get("id", f"clutrr_{split}_{i}")
            # Reasoning depth from f_comb chain length (e.g. "father-son" = 2 hops)
            depth = len(f_comb.split("-")) if f_comb else 1
            input_text = f"Story: {story}\nQuery: {query}"
            examples.append({
                "input": input_text,
                "output": str(target_text),
                "metadata_split": split,
                "metadata_task_name": str(task_name),
                "metadata_reasoning_depth": depth,
                "metadata_f_comb": str(f_comb),
                "metadata_proof_state": str(proof_state)[:500] if proof_state else "",
                "metadata_record_id": str(record_id),
                "metadata_row_index": i,
            })
    logger.info(f"CLUTRR: {len(examples)} total examples")
    return examples


def main() -> None:
    Path("logs").mkdir(exist_ok=True)

    logger.info("Starting dataset standardization")

    datasets_out = []

    # ProofWriter
    pw_examples = load_proofwriter()
    if pw_examples:
        datasets_out.append({"dataset": "proofwriter", "examples": pw_examples})

    # CLUTRR
    cl_examples = load_clutrr()
    if cl_examples:
        datasets_out.append({"dataset": "clutrr", "examples": cl_examples})

    output = {
        "metadata": {
            "description": "Neuro-symbolic reasoning datasets: RuleTaker, ProofWriter, CLUTRR",
            "sources": ["tasksource/proofwriter", "kendrivp/CLUTRR_v1_extracted"],
        },
        "datasets": datasets_out,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    total = sum(len(d["examples"]) for d in datasets_out)
    logger.info(f"Saved {total} total examples across {len(datasets_out)} datasets to {OUTPUT_PATH}")
    for d in datasets_out:
        logger.info(f"  {d['dataset']}: {len(d['examples'])} examples")


if __name__ == "__main__":
    main()
