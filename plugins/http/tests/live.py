#!/usr/bin/env python3
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    sys.stdout.write(
        "manual check: install ./http, validate ./http/workflow.yaml, then exercise "
        "a real allowlisted GET and a small checksum-verified download into run://...\n"
    )


if __name__ == "__main__":
    main()
