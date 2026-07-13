# PyInstaller spec for code-tracer.
#
# Build (onefile):
#   .venv/bin/pyinstaller build.spec --noconfirm
# Produces dist/code-tracer (single executable) or dist/code-tracer/ (onedir).
#
# Notes:
#  * static/index.html is bundled as data and located at runtime via a
#    frozen-aware path (see code_tracer.app.STATIC_DIR handling).
#  * tree_sitter_language_pack ships compiled parser binaries that must be
#    collected as data + binaries.
#  * llama-cpp-python is OPTIONAL. It carries a large native lib; by default
#    it is EXCLUDED from the frozen binary, so a frozen build runs the FTS5 +
#    fake-embedder path. To bundle it, remove it from `excludes` and add
#    collect_all("llama_cpp"). The real embedder also works when running from
#    a normal venv install (pip install .[embed]).

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
hiddenimports = []

for pkg in ("tree_sitter_language_pack", "tree_sitter", "pathspec"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# bundle the UI
datas += [("static/index.html", "static")]

ONEFILE = True  # set False for onedir if onefile is flaky on the target

block_cipher = None

a = Analysis(
    ["src/code_tracer/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + [
        "code_tracer.cli", "code_tracer.app", "code_tracer.indexer",
        "code_tracer.parsing", "code_tracer.search", "code_tracer.graph",
        "code_tracer.stack", "code_tracer.embedder", "code_tracer.llm",
        "tomli_w",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["llama_cpp"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="code-tracer",
        console=True,
        upx=False,
    )
else:
    exe = EXE(pyz, a.scripts, [], name="code-tracer", console=True, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="code-tracer")
