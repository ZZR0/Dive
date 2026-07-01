# DIVE

**D**ivergence-directed, **I**ntent-grounded patch **V**alidation through **E**xploration

DIVE is an automated technique for detecting unintended behavioral changes in pull requests (PRs). It compares the natural-language intent of a PR against the behavioral difference that the patch produces: an executable *patch oracle* is inferred from the PR description, and DIVE searches for inputs that reach the changed code and expose pre/post differences. Each exposed difference is then classified as intended or a regression.

This repository contains the DIVE implementation, evaluation artifacts, and the ICSE 2027 research-track paper draft.

## Motivation

Patches that fix one behavior can unintentionally change another. Existing intent-based detectors (e.g., [PatchGuru](PatchGuru/), [Testora](Testora/)) generate test inputs by querying a language model once for a small static set of inputs. That one-shot strategy often misses regressions—especially for APIs whose functions take structured domain objects (dataframes, tensors, schema instances), where the specific input that triggers a side effect is hard to guess.

DIVE separates **input discovery** from **intent judgment**:

| Task | Mechanism |
|------|-----------|
| Reach changed code and produce pre/post differences | Divergence-directed search over many candidate inputs (cheap containerized executions) |
| Decide whether a difference is intended | LLM reviewer using the PR description and patch oracle |

## Key Results

Evaluated on **703 merged PRs** from four Python libraries (pandas, scipy, keras, marshmallow), compared against PatchGuru and Testora:

| Tool | True bugs | Precision |
|------|-----------|-----------|
| **DIVE** | **51** | **79.7%** |
| PatchGuru | 35 | 68.6% |
| Testora | 7 | 41.2% |

DIVE achieves these results at roughly **half the LLM cost** of PatchGuru (~$0.033 vs. ~$0.070 per PR) and similar end-to-end time (~4.6 min/PR), because additional inputs are explored with executions rather than extra model queries.

## How DIVE Works

DIVE runs as **Phase 2** of the PatchGuru pipeline (after Phase 1 oracle inference). The pipeline has five steps:

1. **Seed extraction** — Collect candidate inputs from PR tests, docstrings, and the synthesized oracle.
2. **Structured input construction** — Build valid domain objects via type-driven strategies and LLM-generated generators; mutate at boundaries, types, and shapes.
3. **Divergence-directed search** — Score inputs by reaching changed lines and producing new pre/post differences; run differential execution in Docker.
4. **Triage** — Cluster raw differences, minimize representatives, and drop flaky ones.
5. **Intent classification** — LLM classifies each minimized difference as intended or a bug.

Core implementation:

| Module | Path |
|--------|------|
| Orchestrator | `PatchGuru/patchguru/analysis/DivergenceSearch.py` |
| Input constructor | `PatchGuru/patchguru/analysis/InputConstructor.py` |
| In-container search harness | `PatchGuru/patchguru/analysis/dive_harness.py` |
| Phase 2 switch | `PatchGuru/patchguru/Config.py` (`PHASE2_STRATEGY`) |

## Repository Layout

```
.
├── PatchGuru/          # DIVE implementation (extends PatchGuru Phase 2)
│   ├── patchguru/      # Core analysis pipeline
│   └── scripts/        # Batch runners, Docker clone setup, evaluation
├── Testora/            # Baseline comparison tool (vendored)
├── paper/              # ICSE 2027 LaTeX draft and experiment notes
│   ├── main.tex
│   └── experiment-status.md   # Source of truth for evaluation numbers
├── clones/             # Target-project git clones for Docker workers (generated)
└── clones_pgabl/       # Additional clone pool (generated)
```

## Getting Started

DIVE builds on [PatchGuru](PatchGuru/). See [PatchGuru/README.md](PatchGuru/README.md) for environment setup (Docker, API tokens, target projects).

### Prerequisites

- Docker
- Python 3.11+ with [uv](https://github.com/astral-sh/uv) (PatchGuru uses `uv run`)
- OpenAI API key (`.openai_token` in `PatchGuru/`)
- GitHub API token (`.github_token` in `PatchGuru/`)

### Run on a Single PR

```bash
cd PatchGuru

# Phase 1 + DIVE Phase 2
PATCHGURU_PHASE2_STRATEGY=dive \
  uv run python -m patchguru.SpecInfer --project marshmallow --pr_nb 707
```

### Batch Evaluation

```bash
cd PatchGuru

# 1. Set up target project environments (once per project)
bash scripts/setup_env.sh pandas

# 2. Expand to N parallel workers
NB_CLONES=10 bash scripts/expand_clones.sh pandas

# 3. Run batch with DIVE
NB_CLONES=10 WORKERS=10 bash scripts/watch_and_run.sh pandas scipy keras marshmallow
# Or directly:
uv run python scripts/run_all_specinfer.py \
  --projects pandas scipy keras marshmallow \
  --workers 10 --nb-clones 10 \
  --phase2-strategy dive \
  --cache-dir .cache_dive
```

### DIVE Configuration

Set via environment variables (see `PatchGuru/patchguru/Config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PATCHGURU_PHASE2_STRATEGY` | `baseline` | Set to `dive` to enable DIVE |
| `PATCHGURU_DIVE_EXEC_BUDGET` | `800` | Max executions per PR |
| `PATCHGURU_DIVE_TIME_BUDGET_SEC` | `300` | Wall-clock budget inside container |
| `PATCHGURU_DIVE_SEED_BASELINE_DIR` | — | Optional baseline cache for seed fusion |
| `PATCHGURU_DIVE_ABLATION` | `none` | Ablation: `no_constructor`, `no_guided`, `no_triage`, `search_only` |

Results are written to `<cache-dir>/oracles/<project>/<pr>/phase2/results.json`.

## Paper

The LaTeX draft lives in [`paper/`](paper/). Build with:

```bash
cd paper && make
```

Evaluation numbers and experiment progress are tracked in [`paper/experiment-status.md`](paper/experiment-status.md).

## Baselines

| Tool | Location | Role |
|------|----------|------|
| PatchGuru | [`PatchGuru/`](PatchGuru/) | Host pipeline + one-shot baseline (Phase 2) |
| Testora | [`Testora/`](Testora/) | External intent-based detector for comparison |

## Citation

If you use DIVE in your research, please cite our paper (BibTeX to be added upon publication).

## License

- DIVE extensions in `PatchGuru/` inherit the [Apache License 2.0](PatchGuru/LICENSE) from PatchGuru.
- `Testora/` is under the [MIT License](Testora/LICENSE).
