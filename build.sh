#!/bin/sh
# Build a single-file executable of the headless CLI.
#
#     ./build.sh                  # -> dist/jsonl-inspect
#     ./build.sh ~/.local/bin     # build straight onto your PATH
#
# The output is a zipapp: one ~125 KB executable file with a shebang, no
# virtualenv, no install step, nothing to unpack at runtime. It works because
# the headless CLI imports no third-party packages -- cli.py, discovery.py and
# parser.py are pure standard library -- so any system python3 can run it.
#
# server.py is deliberately excluded. It needs Flask, which would defeat the
# point; the zipapp is the "clean this session" tool, not the web UI. Use the
# repo (or `uv tool install`) when you want a browser.
#
# Not a fully static binary: it still needs *a* python3 on the machine. That
# trade is deliberate. Embedding the interpreter (PyInstaller, Nuitka) costs
# 10-25 MB and a much slower cold start to remove a dependency that macOS and
# every mainstream Linux already ship.

set -e

REPO=$(cd "$(dirname "$0")" && pwd)
OUT_DIR=${1:-"$REPO/dist"}
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/jsonl_inspect" "$OUT_DIR"
for mod in __init__ parser discovery cli; do
    cp "$REPO/jsonl_inspect/$mod.py" "$STAGE/jsonl_inspect/"
done

python3 -m zipapp "$STAGE" \
    --main "jsonl_inspect.cli:main" \
    --python "/usr/bin/env python3" \
    --output "$OUT_DIR/jsonl-inspect"
chmod +x "$OUT_DIR/jsonl-inspect"

size=$(wc -c < "$OUT_DIR/jsonl-inspect" | tr -d ' ')
echo "built $OUT_DIR/jsonl-inspect (${size} bytes)"
"$OUT_DIR/jsonl-inspect" --help > /dev/null && echo "smoke test: ok"
