# DIVE

**D**ivergence-directed, **I**ntent-grounded patch **V**alidation through **E**xploration

DIVE detects unintended behavioral changes in pull requests (PRs). It builds on the [PatchGuru](PatchGuru/) pipeline: Phase 1 infers an executable *patch oracle* from the PR description; Phase 2 (DIVE) searches for inputs that reach the changed code, expose pre/post differences, and classifies each difference as intended or a regression.

Supported target projects: **pandas**, **scipy**, **keras**, **marshmallow**.

> **Scope:** Each PR must modify a **single source function** (test-only changes are not supported).

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker | Runs target projects in isolated containers |
| Python 3.11+ | PatchGuru uses [uv](https://github.com/astral-sh/uv) (`uv run`) |
| OpenAI API key | File `PatchGuru/.openai_token` (one line, the key) |
| GitHub API token | File `PatchGuru/.github_token` (for fetching PR diffs) |
| Network / proxy | Optional; see [Proxy](#proxy) below |

Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Quick Start

### 1. Configure API tokens

```bash
cd PatchGuru
echo "sk-..." > .openai_token
echo "ghp_..." > .github_token
chmod 600 .openai_token .github_token
```

Testora reads the same tokens (via symlinks or copies in `Testora/`).

### 2. Build target-project environments

Clone directories are created next to this repo at `../clones/` (i.e. sibling of `PatchGuru/`). Run once per project:

```bash
cd PatchGuru
bash scripts/setup_env.sh pandas     # repeat for scipy / keras / marshmallow
```

This clones the project, installs dependencies inside Docker, and commits a reusable image `patchguru-<project>-dev`.

### 3. Scale parallel workers

```bash
NB_CLONES=10 bash scripts/expand_clones.sh pandas
```

Repeat for each project you plan to analyze. Each clone gets its own long-running container (`patchguru-<project>-dev-<id>`).

### 4. Run DIVE on one PR

```bash
cd PatchGuru

PATCHGURU_PHASE2_STRATEGY=dive \
  uv run python -m patchguru.SpecInfer --project marshmallow --pr_nb 707
```

Re-run from scratch (ignore cache):

```bash
PATCHGURU_PHASE2_STRATEGY=dive \
  uv run python -m patchguru.SpecInfer --project marshmallow --pr_nb 707 --force
```

## Batch Runs

### DIVE (recommended entry point)

After containers are ready:

```bash
cd PatchGuru

NB_CLONES=10 WORKERS=10 \
  PATCHGURU_PHASE2_STRATEGY=dive \
  bash scripts/watch_and_run.sh pandas scipy keras marshmallow
```

Or call the scheduler directly:

```bash
cd PatchGuru

uv run python scripts/run_all_specinfer.py \
  --projects pandas scipy keras marshmallow \
  --workers 10 --nb-clones 10 \
  --timeout 2400 \
  --phase2-strategy dive \
  --cache-dir .cache_dive
```

Run a custom PR list (one line per PR: `<project> <pr_nb>`):

```bash
uv run python scripts/run_all_specinfer.py \
  --pr-file scripts/pr_batch_300/new200.txt \
  --phase2-strategy dive \
  --workers 10 --nb-clones 10 \
  --cache-dir .cache_dive
```

Resume without re-running finished PRs: omit `--force`. Completed PRs (`stage: completed`) are skipped automatically.

Convenience wrapper (prepares PR list from a baseline cache, then runs DIVE Phase 2):

```bash
cd PatchGuru
PR_FILE=scripts/pr_batch_300/new200.txt \
  BASELINE_CACHE=.cache_baseline \
  DIVE_CACHE=.cache_dive \
  bash scripts/run_dive.sh
```

### PatchGuru baseline (one-shot Phase 2)

Same setup; omit `--phase2-strategy dive` (default is `baseline`):

```bash
cd PatchGuru
uv run python scripts/run_all_specinfer.py \
  --projects pandas scipy keras marshmallow \
  --workers 10 --nb-clones 10 \
  --cache-dir .cache_baseline
```

Or use the helper script:

```bash
bash scripts/run_baseline.sh
```

### Testora baseline

Testora lives in [`Testora/`](Testora/). Set up a venv and reuse the clone pool under `../clones/`:

```bash
cd Testora
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# Single PR
.venv/bin/python -m testora.RegressionFinder --project scipy --pr 21768

# Batch on the shared new200 benchmark (703 PRs)
bash scripts/run_testora.sh
```

Inspect results in the Web UI:

```bash
.venv/bin/python -m testora.webui.WebUI --files .results_new200/logs/*.json
# open http://localhost:4000/
```

## Pipeline Overview

Each PR goes through two phases in PatchGuru/DIVE:

| Phase | What it does |
|-------|----------------|
| **Phase 1** | Intent analysis → oracle synthesis → self-review |
| **Phase 2** | **Baseline:** LLM generates a few static inputs. **DIVE:** seed extraction → input construction → divergence-directed search → triage → intent classification |

Phase 2 runs only when Phase 1 concludes `NORMAL`. Final labels:

| `review_conclusion` | Meaning |
|---------------------|---------|
| `BUG` | Behavioral change inconsistent with PR intent |
| `NORMAL` | Consistent with intent |
| `MISMATCH` | Generated test/oracle is invalid |

DIVE Phase 2 steps (implementation in `PatchGuru/patchguru/analysis/`):

1. **Seed extraction** — PR tests, docstrings, Phase 1 oracle calls
2. **Input construction** — `InputConstructor.py`: type-driven strategies + LLM generators
3. **Divergence-directed search** — `dive_harness.py`: fitness-guided input exploration in Docker
4. **Triage** — cluster, minimize, deflake divergences
5. **Intent classification** — LLM reviews each minimized difference

## Configuration

Environment variables (defaults in `PatchGuru/patchguru/Config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PATCHGURU_PHASE2_STRATEGY` | `baseline` | Set to `dive` to enable DIVE |
| `PATCHGURU_CACHE_DIR` | `.cache_rerun` | Output directory (overridden by `--cache-dir`) |
| `PATCHGURU_DIVE_EXEC_BUDGET` | `800` | Max function executions per PR |
| `PATCHGURU_DIVE_TIME_BUDGET_SEC` | `300` | Wall-clock search budget inside container |
| `PATCHGURU_DIVE_DEFLAKE_K` | `3` | Repeats to drop flaky divergences |
| `PATCHGURU_DIVE_MAX_CLUSTERS` | `8` | Max divergence clusters to classify |
| `PATCHGURU_DIVE_SEED_BASELINE_DIR` | — | Baseline cache for seed fusion |
| `PATCHGURU_DIVE_ABLATION` | `none` | `no_constructor`, `no_guided`, `no_triage`, `search_only` |
| `PATCHGURU_NB_CLONES` | `3` | Clone pool size (scripts usually set `10`) |
| `PATCHGURU_CLONE_ID` | — | Pin a single clone (debugging) |

Ablation experiments:

```bash
PATCHGURU_DIVE_ABLATION=no_constructor bash scripts/run_ablation.sh
```

### Proxy

If you need a proxy for `git clone` or GitHub API access:

| Variable | Purpose | Typical value |
|----------|---------|---------------|
| `GIT_PROXY` | Host-side git fetch | `socks5h://127.0.0.1:10808` |
| `PATCHGURU_HOST_PROXY` | PyGithub / HTTP requests | same as `GIT_PROXY` |
| `CONTAINER_PROXY` | pip/build inside containers | `http://host.docker.internal:10810` |

Batch scripts inject `http_proxy` / `PATCHGURU_HOST_PROXY` into child processes when set.

## Outputs

After a run, results live under `<cache-dir>/oracles/<project>/<pr>/`:

| File | Content |
|------|---------|
| `results.json` | Phase 1 result (`review_conclusion`, LLM usage, stage) |
| `phase2/results.json` | Phase 2 result (DIVE search stats + conclusion) |
| `specification.py` | Generated oracle test code |
| `phase2/specification.py` | Phase 2 test driver |

Batch progress:

| Path | Content |
|------|---------|
| `scripts/run_all_progress.jsonl` | One JSON line per PR |
| `scripts/logs/*.log` | Batch stdout |
| `logs/<project>/<clone_id>/<session>/events.log` | Per-PR event trace |

Summarize a cache directory:

```bash
cd PatchGuru
uv run python scripts/summarize_results.py --cache-dir .cache_dive
bash scripts/summarize_results.sh .cache_dive    # shell wrapper
```

Compare DIVE vs baseline:

```bash
uv run python scripts/compare_baseline_dive.py \
  --baseline-cache .cache_baseline --dive-cache .cache_dive
```

## Repository Layout

```
.
├── PatchGuru/              # DIVE + PatchGuru pipeline
│   ├── patchguru/          # Core Python package
│   │   └── analysis/       # DivergenceSearch, InputConstructor, dive_harness
│   ├── scripts/            # setup, batch runners, evaluation helpers
│   └── .devcontainer/      # Optional VS Code Dev Container setup
├── Testora/                # Testora baseline (comparison)
├── clones/                 # Target-project clones (generated by setup_env.sh)
└── clones_pgabl/           # Additional clone pool (generated)
```

PR benchmark lists: `PatchGuru/scripts/pr_batch_300/` (e.g. `new200.txt` — 703 PRs across four libraries).

Further details: [PatchGuru/README.md](PatchGuru/README.md), [PatchGuru/scripts/README.md](PatchGuru/scripts/README.md), [Testora/README.md](Testora/README.md).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `git clean` permission denied after container runs | `bash PatchGuru/scripts/fix_clone_permissions.sh` |
| keras checkout: `reference is not a tree` | `bash PatchGuru/scripts/fix_keras_clones.sh` |
| marshmallow: duplicate `marshmallow/` paths | `bash PatchGuru/scripts/fix_marshmallow_clones.sh` |
| scipy needs full clone rebuild | `FORCE_REFRESH=1 NB_CLONES=10 bash scripts/expand_clones.sh scipy` |
| Batch shows `✓ done` but no `results.json` | Analysis exited early; check `events.log` |
| Stale logs in terminal | Set `PYTHONUNBUFFERED=1` |

## Alternative: Dev Container

Both PatchGuru and Testora support VS Code Dev Containers (`.devcontainer/`). Open the subfolder in VS Code → **Dev Containers: Rebuild and Reopen in Container**. This builds PatchGuru/Testora plus in-container target-project instances. Host-side `scripts/setup_env.sh` is the recommended path for large batch runs.

## License

- DIVE extensions in `PatchGuru/`: [Apache License 2.0](PatchGuru/LICENSE)
- `Testora/`: [MIT License](Testora/LICENSE)
