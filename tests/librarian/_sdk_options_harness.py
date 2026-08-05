#!/usr/bin/env python3
"""Subprocess harness for `test_agent_sdk_options.py`: the ONE place in this suite that imports
the Claude Agent SDK.

**Out of process on purpose.** `test_agent_pure.py` asserts that building a `double`-backend agent
leaves `claude_agent_sdk` out of `sys.modules`, and an import is permanent for the rest of a pytest
session — so importing the SDK anywhere in-process would make that invariant depend on collection
order, and would quietly make the offline suite load the agent framework it promises never to load.
Same reasoning as `_worker_harness.py`: not a test module, no `test_` prefix, wiring from argv.

Prints ONE JSON object on stdout: `{"argv": [...]}`, the command line the SDK's own transport would
exec from `agent.build_options_kwargs`. The argv is the artifact worth asserting because the argv is
where the defect was VISIBLE — `--setting-sources=project` in a real process tree, with the
knowledge repo's `.mcp.json` servers hanging off it.

Nothing here connects, authenticates or spends a token: the transport is constructed and asked to
build its command, never started. `_build_command`/`_find_cli` are SDK-internal, which is
acceptable for exactly one reason — `pyproject.toml` pins `claude-agent-sdk` EXACTLY, so a version
bump is a deliberate, reviewed act and this test failing is the review noticing.
"""
import argparse
import json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="a checkout carrying .claude/skills/librarian/")
    args = ap.parse_args()

    from stigmergy.librarian import agent, config

    settings = config.Settings(repo=args.repo, backend="sdk")
    kwargs = agent.build_options_kwargs(
        settings=settings, worktree_root=args.repo,
        skill_text=agent.read_skill(args.repo),
        # An explicit, minimal environ: the harness must not smuggle this machine's ambient
        # credentials into an assertion about what the agent's subprocess is handed.
        environ={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home"})

    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    # Constructing this at all is half the test: a typo'd or removed option key in
    # `build_options_kwargs` is a `TypeError` here, which is the only thing standing between a dict
    # of kwargs and the real options object it has to become.
    options = ClaudeAgentOptions(**kwargs)
    transport = SubprocessCLITransport(prompt="(never sent)", options=options)
    transport._cli_path = transport._find_cli()
    print(json.dumps({"argv": transport._build_command()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
