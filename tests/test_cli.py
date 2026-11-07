from __future__ import annotations

import json

from inferplat import drill
from inferplat.cli import main
from inferplat.server import Settings, create_app


def test_load_and_sweep(models, tmp_path, capsys):
    with drill.serve(create_app(Settings(model_dir=models, model_version="v2")), drill.free_port()) as url:
        assert (
            main(
                [
                    "load",
                    "--url",
                    url,
                    "--rate",
                    "50",
                    "--seconds",
                    "1",
                    "--model-dir",
                    str(models),
                    "--max-error-rate",
                    "0",
                    "--max-p95-ms",
                    "500",
                ]
            )
            == 0
        )
        summary = json.loads(capsys.readouterr().out)
        assert summary["requests"] == 50 and summary["error_rate"] == 0
        assert main(["load", "--url", url, "--rate", "20", "--seconds", "0.5", "--model-dir", str(models), "--max-p95-ms", "0.001"]) == 1
        report = tmp_path / "capacity.md"
        assert (
            main(["sweep", "--url", url, "--levels", "1,4", "--seconds", "0.5", "--model-dir", str(models), "--report", str(report)]) == 0
        )
        assert "In flight" in report.read_text()


def test_drill_command(models, tmp_path):
    report = tmp_path / "drills.md"
    code = main(
        [
            "drill",
            "--model-dir",
            str(models),
            "--scenario",
            "errors",
            "--steps",
            "25,100",
            "--step-seconds",
            "2",
            "--min-requests",
            "60",
            "--report",
            str(report),
        ]
    )
    assert code == 0
    assert "rolled back" in report.read_text()


def test_rollout_with_a_file_store(models, tmp_path, capsys):
    import yaml

    from inferplat.gateway import GatewaySettings, create_gateway

    routing = tmp_path / "routing.yaml"
    with (
        drill.serve(create_app(Settings(model_dir=models, model_version="v1")), drill.free_port()) as s,
        drill.serve(create_app(Settings(model_dir=models, model_version="v2")), drill.free_port()) as c,
    ):
        routing.write_text(yaml.safe_dump({"stable": {"url": s}, "canary": {"url": c}, "canary_weight": 0}))
        with drill.serve(create_gateway(GatewaySettings(routing_file=routing, reload_interval=0.1)), drill.free_port()) as g:
            # No traffic at all: the controller cannot decide and rolls back.
            code = main(
                [
                    "rollout",
                    "--routing-file",
                    str(routing),
                    "--gateway",
                    g,
                    "--steps",
                    "50",
                    "--step-seconds",
                    "0.2",
                    "--min-requests",
                    "10",
                    "--max-extensions",
                    "1",
                    "--apply-timeout",
                    "5",
                ]
            )
    assert code == 2
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["outcome"] == "rolled_back"
