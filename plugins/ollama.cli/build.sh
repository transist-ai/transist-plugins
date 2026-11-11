#!/bin/sh
# Build a new payload and verify its macOS ad-hoc signature before publication.
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: sh build.sh <unused-output-path>\n' >&2
  exit 2
fi
case "$1" in
  /*) output=$1 ;;
  *) output="$(pwd)/$1" ;;
esac
if [ -e "$output" ] || [ -L "$output" ]; then
  printf 'Refusing to overwrite output: %s\n' "$output" >&2
  exit 1
fi
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d "$(dirname -- "$output")/.ollama-build.XXXXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
(cd "$root" && go build -o "$work/ollama.cli" ./cmd/plugin)
if [ "$(uname -s)" = Darwin ]; then
  if ! /usr/bin/codesign --verify --strict "$work/ollama.cli"; then
    /usr/bin/codesign --force --sign - "$work/ollama.cli"
  fi
  /usr/bin/codesign --verify --strict "$work/ollama.cli"
fi
# Hard-link creation fails if the destination appears concurrently; never replace it.
ln "$work/ollama.cli" "$output"
