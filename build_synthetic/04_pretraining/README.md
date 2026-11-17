# OpenMythos mixed pre-training scripts

These four scripts are designed to sit beside your existing `mix_core.py`.
They do not replace or patch `llm_backends.py`, `build_synthetic.py`, or
`diversity.py`.

## Files

- `tokenizer.py`: train a ByteLevel BPE tokenizer, inspect it, or encode text.
- `budget.py`: count tokens in local corpora and estimate steps/optimizer/checkpoint budgets.
- `train_mix.py`: use `mix_core.MixedChunker` for token-exact public/synthetic mixing and train an OpenMythos model.
- `chat.py`: load the final training checkpoint and run an interactive prompt loop.
- `sources.example.json`: editable source registry.

## Suggested setup

Use the same Python environment in which `open-mythos` is installed:

```powershell
py -m pip install tokenizers transformers datasets
# Install OpenMythos using the project's documented install command.
```

Train tokenizer from validated synthetic data plus local public-data samples:

```powershell
py tokenizer.py train --jsonl ..\03_synthetic\validated.jsonl --text-key training_text --out tokenizer --vocab-size 16000
```

Inspect:

```powershell
py tokenizer.py inspect --tokenizer tokenizer --text "Where is the monthly invoice report?"
```

Copy `sources.example.json` to `sources.json`, edit dataset paths/configs and confirm dataset licences/terms. The Stack v2 adapter may fetch content from Software Heritage S3; verify its dataset card, access terms and bucket layout before a long run.

Estimate budget:

```powershell
py budget.py --tokenizer tokenizer --source synthetic=..\03_synthetic\validated.jsonl:training_text --seq-len 512 --micro-batch 1 --grad-accum 16 --steps 1000
```

First run only the model/API check:

```powershell
py train_mix.py --tokenizer tokenizer --sources sources.json --mix "fineweb=0.55,squad=0.20,php=0.20,synthetic=0.05" --preset s --seq-len 512 --dry-run
```

Then train:

```powershell
py train_mix.py --tokenizer tokenizer --sources sources.json --mix "fineweb=0.55,squad=0.20,php=0.20,synthetic=0.05" --preset s --seq-len 512 --micro-batch 1 --grad-accum 16 --steps 1000 --out checkpoints\run1
```

Chat:

```powershell
py chat.py --tokenizer tokenizer --preset s --seq-len 512 --checkpoint checkpoints\run1\final
```

## Important implementation notes

1. `mix_core.JsonlSource` defaults to `text`; this pipeline explicitly supports
   `text_key: training_text` for the validator's output schema.
2. The OpenMythos model constructor has varied across releases. The adapter
   first calls `open_mythos.variants.mythos_1b()` (the base factory referenced
   by the supplied `mix_core.PRESETS`), applies `MC.COMMON` and the selected
   preset as config overrides, then resolves `OpenMythos` from common module
   locations. If your installed package exposes another constructor signature,
   the dry-run error prints the signature needed to add that exact adapter.
3. GQA overrides are `attn_type=gqa` plus the selected `n_heads`/`n_kv_heads`.
   Confirm the model's printed config reflects these values. A config factory
   may use immutable dataclasses or nested config objects; the adapter does not
   silently claim success if the model constructor cannot be resolved.
4. Your RTX 2060 has 6 GB VRAM. The `s` preset is the smallest of the supplied
   presets, but full AdamW training can still exceed 6 GB due to gradients,
   optimizer states and activations. Start with `--seq-len 256` or `512`,
   micro-batch 1, and a dry run followed by a one-step test. This script does
   not claim CPU/NVMe optimizer offload; ordinary `.to(cuda)` places model
   parameters on the GPU.
5. The starter implementation is single-process, single-GPU. It saves optimizer
   and model state in `training_state.pt`. Resume with `--resume path\training_state.pt`.
6. `--resume` restores the model/optimizer and token counter, but data-stream
   position is approximated by `MixedChunker.start_tokens`; for perfectly
   reproducible resume, save per-source iterator state as a later enhancement.
7. Public dataset names/configurations are examples only. Check licensing,
   dataset terms, filtering, and schema before large-scale training.
