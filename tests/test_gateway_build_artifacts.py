import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_release_worker_builds_disable_vcs_stamping(tmp_path):
    source = Path(__file__).resolve().parents[1]
    script = tmp_path / "gateway/scripts/build_release_artifacts.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(source / "gateway/scripts/build_release_artifacts.sh", script)
    mediator = tmp_path / "gateway/mediator"
    mediator.mkdir()
    for name in ("launch.py", "hardening.py", "sbx_gateway_mediator.py", "sbx_gateway_web.py"):
        (mediator / name).write_text("# fixture\n")
    calls = tmp_path / "calls.jsonl"
    go = tmp_path / "fake-go"
    go.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['GO_CALLS'], 'a') as output:\n"
        "    output.write(json.dumps({'args': args, 'env': {name: os.environ[name] "
        "for name in ('CGO_ENABLED', 'GOOS', 'GOARCH', 'GOTOOLCHAIN')}}) + '\\n')\n"
        "pathlib.Path(args[args.index('-o') + 1]).write_bytes(b'fixture worker')\n"
    )
    go.chmod(0o755)
    subprocess.run(
        ["bash", str(script)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GO": str(go), "GO_CALLS": str(calls), "GOTOOLCHAIN": "auto"},
    )
    builds = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [(build["env"]["GOOS"], build["env"]["GOARCH"]) for build in builds] == [
        ("linux", "amd64"),
        ("darwin", "amd64"),
        ("darwin", "arm64"),
    ]
    for build in builds:
        args, env = build["args"], build["env"]
        assert "-buildvcs=false" in args
        assert "-trimpath" in args and "-ldflags=-s -w" in args
        assert env["CGO_ENABLED"] == "0" and env["GOTOOLCHAIN"] == "local"
        artifact = Path(args[args.index("-o") + 1])
        assert artifact.name == f"sbx-gateway-{env['GOOS']}-{env['GOARCH']}"
        assert artifact.read_bytes() == b"fixture worker"
        assert artifact.stat().st_mode & 0o777 == 0o555
