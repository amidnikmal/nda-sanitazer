# code-tracer

Local **code-insight tracer**: index a project directory and answer *"where do I
look?"*. The code map is built **statically** with tree-sitter — not by an LLM.
An LLM is only ever an optional add-on (annotations / re-ranking) via an
OpenAI-compatible `llama-server` on `127.0.0.1:8080`. When that server is
absent, every command degrades silently to a fast, fully-local mode. **The
runtime never makes external network calls.**

## What it does

- **`index`** — build/refresh a SQLite index (files / symbols / edges / chunks),
  incrementally by file-content hash. FTS5 full-text over symbol names,
  identifiers, docstrings and comments; a per-symbol embedding vector.
- **`where "question"`** — hybrid retrieval: FTS5 (BM25) + vector top-50,
  fused with Reciprocal Rank Fusion, then (if `:8080` is up) an LLM re-ranks the
  top-20. Prints `file:line — symbol — why`. `--fast` skips the LLM.
- **`trace <file:line | name> [--up N] [--down N]`** — BFS the call graph
  (default depth 3, fan-out cap 8). `rich` tree grouped by file, edge
  `confidence` shown; `--mermaid` emits a mermaid graph; `--explain` adds a
  one-line "what it does" per node via the LLM (cached in SQLite).
- **`stack`** — read a runtime stack trace from **stdin** (python / PHP /
  Go panic / Node.js), bind frames to indexed symbols, flag frames outside the
  index.
- **`outline <file>`** — list the symbols defined in a file.
- **`app`** — an ephemeral browser UI (see below).

## Install (target machine: Ubuntu 24.04)

```bash
cd tracer
python3 -m venv .venv
.venv/bin/pip install -e .            # core (FTS5 + fake-embedder fallback)
.venv/bin/pip install -e '.[embed]'   # optional: real GGUF embeddings (llama-cpp-python)
```

### Embedding model (optional but recommended)

The real embedder uses `llama-cpp-python` with a GGUF model placed in
`~/.local/share/code-tracer/models/`. Preferred order:

1. `Qwen3-Embedding-0.6B-Q8_0.gguf` (repo `Qwen/Qwen3-Embedding-0.6B-GGUF`)
2. `bge-m3-Q8_0.gguf`
3. `embeddinggemma-300M-Q8_0.gguf`

Download once from Hugging Face (needs outbound access on the target box):

```bash
mkdir -p ~/.local/share/code-tracer/models
curl -L -o ~/.local/share/code-tracer/models/Qwen3-Embedding-0.6B-Q8_0.gguf \
  https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf
```

If no model is present, code-tracer automatically falls back to a deterministic
hash-based embedder; `where` still works, carried by FTS5.

## Usage

```bash
code-tracer index /path/to/project
code-tracer where "where is request authentication handled?"
code-tracer where "auth" --fast          # never touch the LLM
code-tracer trace handle_login --down 3
code-tracer trace src/api.py:42 --up 2 --explain
code-tracer trace process --mermaid
code-tracer outline src/api.py
cat crash.txt | code-tracer stack
code-tracer app                          # browser UI
```

`where` / `trace` / `stack` / `outline` operate on the index for the **current
directory** by default; pass `--dir <path>` to target another indexed project.

## Languages

First-class: **python, javascript, typescript, go, php, java** (plus `.tsx`,
`.jsx`, `.mjs`, `.cjs`). Definitions captured: functions, methods, classes
(and interfaces/enums/traits/types where the grammar has them), with name,
line range, signature, and the docstring/leading comment. Calls inside bodies
become graph edges.

## Configuration

`~/.config/code-tracer.toml` is created with defaults on first run:

| key | default | meaning |
|-----|---------|---------|
| `editor_command` | `code -g {path}:{line}` | opened when you click a location in the UI |
| `sanitizer_path` | `~/nda-sanitizer/.venv/bin/nda-sanitizer` | CLI used by the copy-sanitize toggle |
| `llm_url` | `http://127.0.0.1:8080/v1` | OpenAI-compatible llama-server |
| `llm_model` | `local` | model name sent to the server |
| `embed_model_path` | `""` | override GGUF path (empty ⇒ auto-resolve) |
| `embed_disable_thinking` | `true` | disable Qwen3 "thinking" for annotations |
| `no_think_mode` | `kwargs` | `kwargs` (`enable_thinking=false`), `suffix` (`/no_think`), or `both` |
| `heartbeat_timeout_s` | `15` | UI dies after this long without a tab heartbeat |

## Browser UI (`code-tracer app`)

- Binds to **127.0.0.1** on a **random port** with a **one-time token** in the
  URL. Any request without the token gets **403**.
- The open tab sends heartbeats; if they stop for `heartbeat_timeout_s`, the
  process exits. `Ctrl+C` also stops it. No daemon, no fixed port, no systemd.
- In a headless container the browser won't open — the URL is just printed.
- Screens: **Where**, **Trace** (collapsible tree, file filter, "copy file
  list"), **Stack** (textarea → frame table), **Index** (path, run, progress,
  counts). Clicking `file:line` runs the configured editor command.
- **Copy** copies the output as-is. The **"Sanitize on copy (for cloud)"**
  toggle is **OFF by default**; when ON, output is piped through the sanitizer
  CLI and only the masked text (plus `vault_id`) reaches the clipboard. If the
  sanitizer is unavailable while the toggle is ON, **copying is blocked** and
  the raw text is never released.

## Standalone binary (PyInstaller)

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller build.spec --noconfirm
./dist/code-tracer version
```

`build.spec` produces a onefile executable (`dist/code-tracer`) bundling the
tree-sitter parsers and the UI. `llama-cpp-python` is **excluded** by default
(large native lib); a frozen binary therefore runs the FTS5 + fake-embedder
path. To bundle real embeddings, edit `build.spec` (remove `llama_cpp` from
`excludes`, add `collect_all("llama_cpp")`). Set `ONEFILE = False` in the spec
for a onedir build if onefile misbehaves on the target.

## Limitations — read honestly

- **The call graph is approximate by design.** Edges are resolved by a
  name-scope heuristic (same file > same directory > globally unique name).
  Ambiguous names get a `low`-confidence edge with a candidate list. Expect:
  - **false edges** where different symbols share a common name (`run`, `get`,
    `handle`, `process`);
  - **missing edges** on dependency injection, reflection/metaprogramming,
    dynamic dispatch, event buses, and cross-language calls.
  Treat the output as "likely paths to look at", not a proof.
- **The tracer's output is NDA-sensitive** (real symbol names, paths, code
  structure). Only send it to a cloud LLM with the **sanitize toggle ON**.
- While `app` is open there is an **ephemeral HTTP listener on 127.0.0.1**
  guarded by a one-time token; it disappears when you close the tab.
