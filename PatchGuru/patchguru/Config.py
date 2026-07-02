import os

DATA_SYNTHESIS_PROMPT = "v1"

LOG_LEVEL = "DEBUG"
LOG_DIR = os.environ.get("PATCHGURU_LOG_DIR", "logs/final_logs/marshmallow")
# LOG_DIR = "logs/debug"

LLM_MODEL = "gpt-5-mini"  # Default model for LLM queries

USE_REFERENCE = True
USE_REFERENCE_SUMMARY = True

USE_PHASE2 = True  # Whether to use phase 2 in the analysis pipeline

# Phase 2 strategy switch (logic isolation for experiments):
#   "baseline" -> original PatchGuru one-shot LLM generalization (spec_generalization)
#   "dive"     -> DIVE divergence-directed feedback search (dive_search)
PHASE2_STRATEGY = os.environ.get("PATCHGURU_PHASE2_STRATEGY", "baseline").strip().lower()

# DIVE search budgets / knobs (only used when PHASE2_STRATEGY == "dive")
DIVE_EXEC_BUDGET = int(os.environ.get("PATCHGURU_DIVE_EXEC_BUDGET", "800"))  # max call_impl executions per PR
DIVE_TIME_BUDGET_SEC = int(os.environ.get("PATCHGURU_DIVE_TIME_BUDGET_SEC", "300"))  # wall-clock budget inside container
DIVE_DEFLAKE_K = int(os.environ.get("PATCHGURU_DIVE_DEFLAKE_K", "3"))  # repeats to drop flaky divergences
DIVE_MAX_CLUSTERS = int(os.environ.get("PATCHGURU_DIVE_MAX_CLUSTERS", "8"))  # max divergence clusters to classify
DIVE_NUM_REVIEW = int(os.environ.get("PATCHGURU_DIVE_NUM_REVIEW", "3"))  # self-consistency votes for classifier (majority vote stabilizes BUG/MISMATCH)
DIVE_HARVEST_TIME_SEC = int(os.environ.get("PATCHGURU_DIVE_HARVEST_TIME_SEC", "45"))  # hard cap on baseline seed-harvest exec time
DIVE_INSTALL_DEPS = os.environ.get("PATCHGURU_DIVE_INSTALL_DEPS", "1") == "1"  # try pip install hypothesis/coverage in container
# Seed fusion: root cache dir of a *baseline* run (the dir that CONTAINS "oracles",
# e.g. ".cache_rerun"). When set, DIVE harvests every concrete input the baseline
# phase-2 driver actually exercised (asserts neutralized, real pre/post monkeypatched)
# and adds them as highest-priority search seeds, so DIVE's recall is >= baseline's.
DIVE_SEED_BASELINE_DIR = os.environ.get("PATCHGURU_DIVE_SEED_BASELINE_DIR", "").strip()

# RQ2 ablation switch (only used when PHASE2_STRATEGY == "dive"):
#   "none"           -> full DIVE (default)
#   "no_constructor" -> w/o (B) structured input construction (Hypothesis + LLM gen_inputs)
#   "no_guided"      -> w/o (C) divergence-guided fitness (uniform search)
#   "no_triage"      -> w/o (D) triage (cluster minimize + deflake)
#   "search_only"    -> w/o (E) TestDriverReview (Mokav-style: divergence => BUG)
DIVE_ABLATION = os.environ.get("PATCHGURU_DIVE_ABLATION", "none").strip().lower()


def dive_ablation_config() -> dict:
    """Return harness/orchestrator flags for the active DIVE ablation variant."""
    ab = DIVE_ABLATION
    if not ab or ab == "none":
        return {
            "ablation": None,
            "use_constructor": True,
            "use_guided_search": True,
            "use_triage": True,
            "use_review": True,
        }
    if ab == "search_only":
        return {
            "ablation": ab,
            "use_constructor": True,
            "use_guided_search": True,
            "use_triage": True,
            "use_review": False,
        }
    return {
        "ablation": ab,
        "use_constructor": ab != "no_constructor",
        "use_guided_search": ab != "no_guided",
        "use_triage": ab != "no_triage",
        "use_review": True,
    }


def clones_pool_dir() -> str:
    """Git clone pool root (relative to PatchGuru cwd or absolute)."""
    return os.environ.get("PATCHGURU_CLONES_DIR", "../clones")


def container_base_name(project: str) -> str:
    """Docker container name prefix, e.g. pandas-dev or pgabl-pandas-dev."""
    tag = os.environ.get("PATCHGURU_CONTAINER_TAG", "").strip()
    if tag:
        return f"{tag}-{project}-dev"
    return f"{project}-dev"


INTENT_ANALYSIS_PROMPT = "v1"  # Default prompt version for intent analysis
RUNTIME_ERROR_REPAIR_PROMPT = "v1"  # Default prompt version for runtime error repair
SYNTAX_ERROR_REPAIR_PROMPT = "v1"  # Default prompt version for syntax error repair
ASSERTION_ERROR_REPAIR_PROMPT = "v1"  # Default prompt version for assertion error repair
SELF_REVIEW_PROMPT = "v1"  # Default prompt version for self review
BUG_TRIGGER_PROMPT = "v1"  # v2 not in public repo; use v1

REPAIR_ATTEMPTS = 8  # Number of attempts to repair errors in code
ANALYSIS_ATTEMPTS = 5  # Number of attempts to re-run the analysis if output is invalid
GENERALIZED_ATTEMPTS = 3  # Number of attempts to generalize specifications
REVIEW_ATTEMPTS = 3  # Number of attempts to re-run the review if output is invalid

MAX_LLM_QUERIES = 30  # Maximum number of LLM queries to ask during analysis

PL = "python"  # Default programming language for analysis

CACHE_DIR = os.environ.get("PATCHGURU_CACHE_DIR", ".cache")

# PR_CUT_OFF = {
#     "pandas": 59900,
#     "scipy": 21652,
#     "keras": 20264,
#     "marshmallow": 0
# }
PR_CUT_OFF = {
    "pandas": 0,
    "scipy": 0,
    "keras": 0,
    "marshmallow": 0
}