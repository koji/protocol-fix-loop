---
name: protocol-fix-loop
description: Generate or edit an Opentrons protocol, then verify it headlessly and fix until clean. Reach for it when asked to create a protocol or when a protocol check reports dirty.
---

# Protocol Fix Loop

You own the loop. Generate, check, fix, re-check. Max 5 rounds.

## Generate

Draft the protocol with the OpentronsAI MCP server when the host has it.
It complements the checker below. It does not replace it.

```json
{
  "mcpServers": {
    "gradio": {
      "url": "https://opentrons-opentronsai-mcp-server.hf.space/gradio_api/mcp/"
    }
  }
}
```

Without MCP access, write the protocol directly and let the checker
carry the verification.

## Check

Run the headless checker against the protocol. It is read-only.

```bash
python3 <skill-dir>/scripts/viz_check.py <protocol.py> --report /tmp/report.json
```

`<skill-dir>` is this skill's installed directory. The checker and its
parser travel with the skill. The Python must have the `opentrons`
package installed.

Exit codes: 0 is clean, 1 is dirty, 2 is a usage or environment failure.
Fix the environment first on exit 2. Never edit the protocol to satisfy
the environment.

## Fix

Read `/tmp/report.json`. `status: dirty` means fix every entry in `errors`
using its `line` and `hint`, then re-run the check. One round is one
check plus its fixes. Stop after 5 rounds and report what remains.

Never auto-accept pipette mounts or volumes. A value the checker cannot
judge stays human-decided.

## Done

`status: clean` means stop and ask the human for a visual check in the
Protocol Visualizer. In VS Code a new `.py` protocol opens the panel
automatically. Otherwise run `Visualizer: Open Protocol Visualizer`.
