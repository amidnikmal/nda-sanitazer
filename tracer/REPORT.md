# code-tracer — build report

Built autonomously in a CPU-only cloud container (Ubuntu, no GPU, **no
llama-server**). This report records versions, the decisions taken at each fork,
gate results, and what could not be verified in the container (and how to verify
it on the target machine).

## Environment

- Python 3.11.15, 4 CPU cores, ~15 GiB RAM, no GPU.
- Outbound HTTPS only through the agent egress proxy. **`huggingface.co` is
  policy-blocked** (proxy returns `403 CONNECT tunnel failed`, `connect_rejected`)
  — not retryable. PyPI is reachable.
- No `llama-server` on `127.0.0.1:8080` in this container (by design).

## Versions (key deps)

| package | version |
|---------|---------|
| tree-sitter | 0.23.2 |
| tree-sitter-language-pack | **0.8.0** (pinned) |
| llama-cpp-python | 0.3.34 (built from source, ~5 min) |
| pathspec | 1.1.1 |
| rich | 14.x |
| typer | 0.26.8 |
| pyinstaller | latest on PyPI |

## Decisions at forks

1. **tree-sitter-language-pack pinned to 0.8.0.** The current release (1.12.5)
   **downloads parser binaries at runtime** from GitHub releases, which the
   egress proxy blocks (403) — `get_parser()` raised `DownloadError`. 0.8.0
   **bundles** compiled parsers as wheels, so it works fully offline. All six
   target languages parse (python, javascript, typescript, go, php, java).

2. **Embedder: fixed fallback to a deterministic fake embedder.** The GGUF
   download is impossible here (`huggingface.co` blocked, see above). Per the
   spec this is the sanctioned fallback:
   - `llama-cpp-python` **is** in the `[embed]` optional extra and **builds and
     imports successfully** — so the real embedder path is available; it simply
     has no model file to load in this container.
   - `get_embedder()` auto-selects: real `LlamaEmbedder` when both `llama_cpp`
     and a GGUF in `~/.local/share/code-tracer/models/` are present, else the
     deterministic hash-based `FakeEmbedder` (256-dim, L2-normalized). Any
     load failure also falls back rather than crashing.
   - Tests use the fake embedder; core + `where` (FTS5-carried) work fully.
   - **On the target machine**, drop a GGUF in the models dir (see README) and
     the real embedder activates automatically — no code change.

3. **UI server: stdlib `http.server` (ThreadingHTTPServer).** Chosen over
   starlette+uvicorn to minimize dependencies (smaller/simpler PyInstaller
   bundle). Random port on 127.0.0.1, one-time URL token (403 without it),
   heartbeat watchdog thread that shuts the process down after
   `heartbeat_timeout_s` (default 15) of silence.

4. **.gitignore handling: `pathspec` (`gitignore` factory).** Plus a builtin
   skip list (`node_modules`, `.venv`, `build`, `dist`, `__pycache__`, …) and a
   binary-file guard (NUL byte in first 8 KiB).

5. **FTS5 table is a standard (writable) fts5 table**, not `content=''`
   contentless — contentless tables cannot `DELETE` by rowid, which incremental
   re-indexing needs when a file changes.

6. **Edge resolution heuristic** (as specified): same file > same directory >
   globally unique name; ambiguous ⇒ `low` confidence with a candidate id list;
   unknown callee ⇒ edge with `callee_id = NULL` (kept as an approximate edge).
   False edges on shared common names are accepted by design.

7. **Qwen3 no-think** is configurable via `no_think_mode`
   (`kwargs` ⇒ `chat_template_kwargs.enable_thinking=false`, `suffix` ⇒
   `/no_think`, or `both`); default `kwargs`. Leaked `<think>…</think>` is
   stripped from responses defensively.

## Gate results

| # | gate | result |
|---|------|--------|
| 1 | venv + deps import; FTS5 available | **PASS** |
| 2 | embedder real-or-fixed-fallback | **PASS** — fake-hash fallback (no GGUF; HF blocked); real path available via `[embed]` |
| 3 | index real repo, counts non-zero, 2nd run faster | **PASS** — 1st ~0.09 s, 2nd ~0.01 s; 17 files / 182 symbols / 989 edges |
| 4 | CLI where/trace/stack/outline on fixtures; fast mode w/o :8080 | **PASS** — all 4 stack formats, up/down/mermaid, where returns the expected file top-5 |
| 5 | headless `app` over HTTP; 403 without token | **PASS** — live curl: 403 no-token, 200 with, where/trace/stack APIs OK, sanitize-block OK, self-terminated on heartbeat timeout |
| 6 | PyInstaller onefile → onedir | **PASS (onefile)** — `dist/code-tracer` 30 MB; `version`/`index`/`trace`/`app` all run from the frozen binary |
| 7 | pytest green twice | **PASS** — 26 passed, 2 skipped, both runs |

### Test summary

`26 passed, 2 skipped`. The 2 skips are the LLM-requiring integration tests
(marker `integration`, auto-skip when `:8080` is down — always skipped here).
The "LLM absent degrades silently" test **runs and passes** (asserts
`chat()` returns `None`, never raises, when the server is unreachable).

## Indexing speed (this container, CPU, fake embedder)

- Whole `nda-sanitazer` repo (28 source files): **~237 files/s**,
  ~1975 symbols/s embedded — but with the **fake** embedder, so the symbols/s
  figure is not representative of real GGUF inference.
- **Not measured:** real embedder throughput (chunks/s) — no model available.
  Expect it to be dominated by llama.cpp CPU inference of Qwen3-Embedding-0.6B
  (order of tens-to-low-hundreds of short chunks/s on a few cores); measure on
  the target machine after placing the GGUF.

## Not verified in this container (verify on target)

1. **LLM layer** (`where` rerank, `trace --explain`): no `:8080` here. The
   client code degrades silently (probe + try/except → `None`). On the target
   (sanitizer's llama-server on `:8080`), run `code-tracer where "…"` without
   `--fast` and `code-tracer trace <fn> --explain`; the integration tests will
   then un-skip.
2. **Real GGUF embedder**: place a model per README and re-run `index`; confirm
   `embedder: llama-gguf` in the index banner. The `where` vector arm then
   carries semantic (not just lexical) matches.
3. **PyInstaller binary bundling llama_cpp**: the default spec **excludes**
   `llama_cpp`. Bundling the native lib was not attempted here (kept the binary
   lean; real embeddings are better run from a venv install). To bundle, edit
   `build.spec` and rebuild on the target.
4. **Browser / editor launch**: headless — the UI URL is printed but no browser
   opens, and the `code -g {path}:{line}` editor command was not launched.
   Verify interactively on the desktop.
5. **Sanitizer integration**: the sanitizer CLI is absent here, so the
   sanitize-on-copy toggle was verified only in its **blocked** path (copy
   refused, raw text withheld). Verify the success path on the machine that has
   `~/nda-sanitizer/.venv/bin/nda-sanitizer`.

## Layout

```
tracer/
  pyproject.toml     build.spec      .gitignore
  README.md          REPORT.md
  src/code_tracer/   config db embedder languages parsing indexer
                     search graph stack llm cli app __main__
  static/index.html  (self-contained UI, no CDN)
  tests/             indexer / where_trace / stack / app / llm_integration
                     fixtures/mini_repo (known-graph python + js)
```
