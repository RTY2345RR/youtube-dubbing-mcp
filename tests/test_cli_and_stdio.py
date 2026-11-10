import json
import os
import subprocess
import sys

import pytest

from youtube_dubbing_mcp.cli import CLIENTS, config_snippet, main

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

EXPECTED_TOOLS = {
    "dubbing_check_setup",
    "dubbing_list_voices",
    "dubbing_start_project",
    "dubbing_get_segments",
    "dubbing_assign_speakers",
    "dubbing_extract_voice_samples",
    "dubbing_set_voice",
    "dubbing_set_translation",
    "dubbing_render",
    "dubbing_export_youtube_package",
    "dubbing_project_status",
    "dubbing_list_projects",
}


@pytest.mark.parametrize("client", CLIENTS)
def test_config_snippets(client):
    snippet = config_snippet(client)
    assert "youtube-dubbing-mcp" in snippet and "MIRAGEA_API_KEY" in snippet
    if client in ("claude-desktop", "cursor", "windsurf", "vscode"):
        json.loads(snippet)
    if client == "codex" and tomllib:
        assert tomllib.loads(snippet)["mcp_servers"]["youtube-dubbing"]["command"] == "uvx"


def test_print_config(capsys):
    assert main(["--print-config", "claude-desktop"]) == 0
    assert "youtube-dubbing" in json.loads(capsys.readouterr().out)["mcpServers"]


def test_stdio_lists_tools_without_key(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "MIRAGEA_API_KEY"}
    env["DUBBING_OUTPUT_DIR"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "youtube_dubbing_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
    )

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def read(want):
        while True:
            line = proc.stdout.readline()
            assert line, "server closed stdout"
            data = json.loads(line)
            if data.get("id") == want:
                return data

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        init = read(1)
        assert init["result"]["serverInfo"]["name"] == "youtube-dubbing-mcp"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in read(2)["result"]["tools"]}
        assert set(tools) == EXPECTED_TOOLS
        assert tools["dubbing_get_segments"]["annotations"]["readOnlyHint"] is True
        text = json.dumps(tools).lower()
        for banned in ("price", "pricing", "credit", "billing", "quips"):
            assert banned not in text
    finally:
        proc.kill()
        proc.wait()
