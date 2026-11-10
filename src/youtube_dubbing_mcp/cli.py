"""`youtube-dubbing-mcp` runs the stdio MCP server.

Extras:
  youtube-dubbing-mcp --check                 verify ffmpeg, yt-dlp, the API key and the voice engine (read-only)
  youtube-dubbing-mcp --print-config cursor   print a ready-to-paste client config
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__

CLIENTS = ("claude-desktop", "cursor", "claude-code", "codex", "vscode", "windsurf")
KEY_PLACEHOLDER = "<your-miragea-api-key>"
PACKAGE = "youtube-dubbing-mcp"
SERVER_KEY = "youtube-dubbing"
UVX_ARGS = [PACKAGE]


def config_snippet(client: str) -> str:
    server = {"command": "uvx", "args": list(UVX_ARGS), "env": {"MIRAGEA_API_KEY": KEY_PLACEHOLDER}}
    if client in ("claude-desktop", "cursor", "windsurf"):
        return json.dumps({"mcpServers": {SERVER_KEY: server}}, indent=2)
    if client == "vscode":
        return json.dumps({"servers": {SERVER_KEY: {"type": "stdio", **server}}}, indent=2)
    if client == "claude-code":
        return f"claude mcp add {SERVER_KEY} -s user -e MIRAGEA_API_KEY={KEY_PLACEHOLDER} -- uvx {PACKAGE}"
    if client == "codex":
        return (
            f"[mcp_servers.{SERVER_KEY}]\n"
            'command = "uvx"\n'
            f"args = {json.dumps(UVX_ARGS)}\n"
            "tool_timeout_sec = 1800\n"
            f'env = {{ MIRAGEA_API_KEY = "{KEY_PLACEHOLDER}" }}'
        )
    raise ValueError(f"Unknown client {client!r}; choose from {', '.join(CLIENTS)}.")


async def _check() -> int:
    from .server import dubbing_check_setup

    try:
        report = await dubbing_check_setup()
    except Exception as exc:  # noqa: BLE001
        print(f"Check failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2), file=sys.stderr)
    return 0 if report.get("ready") else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PACKAGE,
        description="YouTube dubbing MCP server: AI dubbing with one cloned voice per speaker, synced SRT and "
        "YouTube multi-language audio tracks (stdio).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--check", action="store_true", help="verify ffmpeg, yt-dlp and MIRAGEA_API_KEY (creates no job)")
    parser.add_argument("--print-config", choices=CLIENTS, metavar="CLIENT", help=f"print config for: {', '.join(CLIENTS)}")
    args = parser.parse_args(argv)
    if args.print_config:
        print(config_snippet(args.print_config))
        return 0
    if args.check:
        return asyncio.run(_check())
    from .server import run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
