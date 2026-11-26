#!/usr/bin/env bash
# Build the Rust audit hook (hooks/guard-rs) into hooks/bin/, where guard.sh
# looks for it. Needs a Rust toolchain (https://rustup.rs). Run it again after
# changing irreversible.py, guard_irreversible.py or ladder_state.py: until then
# the binary notices it is stale and hands every call to the Python guard.
#   bash hooks/scripts/build_guard.sh          build and install
#   bash hooks/scripts/build_guard.sh --clean  remove the binary (back to Python)
set -euo pipefail
here=$(cd "${0%/*}" && pwd)
hooks=$(dirname "$here")
bin_dir="$hooks/bin"
if [ "${1:-}" = "--clean" ]; then
  rm -f "$bin_dir/guard_irreversible" "$bin_dir/guard_irreversible.exe"
  echo "removed the Rust audit hook; guard.sh runs guard_irreversible.py again"
  exit 0
fi
command -v cargo >/dev/null 2>&1 || { echo "build_guard.sh: cargo not found (install Rust from https://rustup.rs)" >&2; exit 1; }
cargo build --release --locked --manifest-path "$hooks/guard-rs/Cargo.toml"
target="${CARGO_TARGET_DIR:-$hooks/guard-rs/target}/release"
ext=""
[ -f "$target/guard_irreversible.exe" ] && ext=".exe"
mkdir -p "$bin_dir"
# Copy then rename: a hook may be running the old binary right now.
cp "$target/guard_irreversible$ext" "$bin_dir/guard_irreversible$ext.new"
mv -f "$bin_dir/guard_irreversible$ext.new" "$bin_dir/guard_irreversible$ext"
"$bin_dir/guard_irreversible$ext" --check
