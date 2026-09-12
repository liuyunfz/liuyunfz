#!/usr/bin/env python3
"""Publish only allowlisted diagnostics from a private generator log."""

import argparse
from pathlib import Path
import re


ATTEMPT = re.compile(
    r"card-diag source=sub2api attempt=[1-3] "
    r"category=(?:success|timeout|dns|tls|network|http|content_type|json|response_size|internal|validation) "
    r"http=(?:0|[1-5][0-9]{2}) elapsed_ms=[0-9]{1,6} retry=[01]"
)
ERRORS = {
    "activity-card: snapshot response is invalid": "sub2api: schema or data validation failed",
    "activity-card: snapshot response is too large": "sub2api: response exceeded size limit",
    "activity-card: snapshot endpoint returned an invalid response": "sub2api: invalid response headers or status",
    "activity-card: could not render activity card": "sub2api: SVG rendering failed",
    "activity-card: could not write activity cards": "sub2api: output write failed",
    "activity-card: SUB2API_USER_ID is missing or invalid": "sub2api: user filter configuration invalid",
    "status-card: status data is stale": "homelab: online node data is stale",
    "status-card: status fetch failed": "homelab: network fetch failed",
    "status-card: status response is invalid": "homelab: response schema invalid",
    "status-card: status response is too large": "homelab: response exceeded size limit",
    "status-card: node directory is invalid": "homelab: node directory invalid",
    "status-card: status endpoint returned an invalid response": "homelab: invalid response headers or status",
    "status-card: could not render status card": "homelab: SVG rendering failed",
    "status-card: could not write status cards": "homelab: output write failed",
}
for filename in ("homelab-status-dark.svg", "homelab-status-light.svg",
                 "sub2api-activity-dark.svg", "sub2api-activity-light.svg"):
    for reason in ("expected a regular SVG file", "could not read generated SVG",
                   "unexpected SVG size", "forbidden XML content",
                   "private configuration found in SVG", "identifier or address found in SVG",
                   "private detail found in SVG", "malformed SVG", "root element is not SVG",
                   "disallowed SVG element", "disallowed SVG attribute", "unsafe SVG content",
                   "accessibility metadata is missing"):
        ERRORS[f"status-card validation: {filename}: {reason}"] = f"validation: {filename}: {reason}"
for reason in ("generated output directory is unavailable", "generated output does not contain exactly four cards"):
    ERRORS[f"status-card validation: {reason}"] = f"validation: {reason}"


def report(path: Path, *, failed: bool = False) -> None:
    # Read only a bounded tail. Never print arbitrary lines, traceback, URLs,
    # response bodies, IDs, credentials or exception messages.
    try:
        with path.open("rb") as source:
            source.seek(0, 2)
            source.seek(max(0, source.tell() - 65536))
            lines = source.read(65536).decode("utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    emitted = False
    for line in lines[-100:]:
        if ATTEMPT.fullmatch(line):
            print(line)
            emitted = True
        elif line in ERRORS:
            print("card-diag " + ERRORS[line])
            emitted = True
    if failed and not emitted:
        print("card-diag category=unclassified; raw details suppressed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--failed", action="store_true")
    args = parser.parse_args()
    report(args.log, failed=args.failed)
