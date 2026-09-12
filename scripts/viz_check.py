#!/usr/bin/env python3
"""Headless protocol check for LLM generate-fix loops.

Runs the runtime-parameter parser and `opentrons.cli analyze`, then writes
a machine-readable report for the fixing agent. Read-only: never modifies
the protocol file.

Usage:
    python3 python/viz_check.py <protocol.py> [--report report.json]
        [--rtp-values '{"var": value}'] [--python python3]

Exit codes: 0 clean, 1 dirty (errors found), 2 usage/environment failure.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runtime_parameters_parser import parse_protocol_file  # noqa: E402

LINE_RE = re.compile(r"\[line (\d+)\]")
TRACE_RE = re.compile(r'File "[^"]*\.py", line (\d+)')


def hint_for(stage, error_type, detail):
    text = f"{error_type} {detail}"
    if "No module named 'opentrons'" in text or "No module named opentrons" in text:
        return "Environment problem, not protocol code: run with a Python that has the opentrons package installed."
    if "not a valid deck slot" in text or "DeckSlotName" in text:
        return "Fix the deck slot: Flex slots are A1-A4, B1-B4, C1-C4, D1-D4."
    if "definition not found" in text or "LabwareDefinitionNotFound" in text:
        return "Fix the load name: use a standard definition or drop the custom .json next to the protocol."
    if "apiLevel" in text or "APIVersionError" in text:
        return "Fix requirements/metadata apiLevel to one the installed robot software supports."
    if "robotType" in text:
        return "Fix requirements robotType to 'Flex' or 'OT-2'."
    if stage == "parse":
        return "Fix the Python syntax or the add_parameters definition first."
    if "ExceptionInProtocolError" in text:
        return "Fix the run() logic around the reported line."
    return "Fix the reported error, then re-run viz_check."


def extract_line(detail, fallback=None):
    match = LINE_RE.search(detail or "")
    if match:
        return int(match.group(1))
    match = TRACE_RE.search(detail or "")
    if match:
        return int(match.group(1))
    return fallback


def run_analyze(python, protocol, rtp_values):
    tmp = tempfile.NamedTemporaryFile(
        prefix="opentrons-analysis-", suffix=".json", delete=False
    )
    tmp.close()
    args = [
        python, "-m", "opentrons.cli", "analyze",
        "--json-output", tmp.name,
        "--log-output", "stderr",
    ]
    if rtp_values:
        args += ["--rtp-values", rtp_values]
    args.append(protocol)
    proc = subprocess.run(args, capture_output=True, text=True)
    try:
        with open(tmp.name, encoding="utf-8") as f:
            analysis = json.load(f)
    except (OSError, ValueError):
        analysis = None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    stderr_tail = "\n".join(
        [line for line in proc.stderr.splitlines() if line.strip()][-5:]
    )
    return proc.returncode, analysis, stderr_tail


def main():
    parser = argparse.ArgumentParser(description="Headless Opentrons protocol check.")
    parser.add_argument("protocol", help="Protocol .py file to check")
    parser.add_argument("--report", default=None, help="Write report JSON here (default: stdout)")
    parser.add_argument("--rtp-values", default=None, help="Primitive RTP values as JSON string")
    parser.add_argument("--python", default="python3", help="Python with opentrons installed")
    args = parser.parse_args()

    if not os.path.exists(args.protocol):
        print(f"Error: file not found: {args.protocol}", file=sys.stderr)
        return 2

    errors = []

    parameters = parse_protocol_file(args.protocol)
    if parameters and any("error" in p for p in parameters):
        first = next(p for p in parameters if "error" in p)
        errors.append({
            "stage": "parse",
            "errorType": "SyntaxError",
            "line": first.get("line"),
            "detail": first.get("error", "syntax error"),
            "hint": hint_for("parse", "SyntaxError", first.get("error", "")),
        })
        params = []
    else:
        params = parameters

    if not errors:
        code, analysis, stderr_tail = run_analyze(
            args.python, args.protocol, args.rtp_values
        )
        if analysis is None:
            errors.append({
                "stage": "analyze",
                "errorType": "AnalysisFailed",
                "line": None,
                "detail": stderr_tail or "analyze produced no output",
                "hint": hint_for("analyze", "AnalysisFailed", stderr_tail),
            })
            command_count = 0
        else:
            command_count = len(analysis.get("commands", []))
            for err in analysis.get("errors", []):
                detail = err.get("detail", "")
                errors.append({
                    "stage": "analyze",
                    "errorType": err.get("errorType", "Unknown"),
                    "line": extract_line(detail),
                    "detail": detail,
                    "hint": hint_for("analyze", err.get("errorType", ""), detail),
                })
    else:
        command_count = 0

    report = {
        "status": "clean" if not errors else "dirty",
        "protocol": os.path.abspath(args.protocol),
        "errors": errors,
        "stats": {"commands": command_count},
        "params": params,
    }
    output = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(output + "\n")
    else:
        print(output)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
