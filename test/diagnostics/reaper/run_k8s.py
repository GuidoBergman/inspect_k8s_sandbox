"""Manually compare tail and the default loop through Kubernetes sandbox.exec.

Creates one Helm release with two sandbox pods, then uninstalls that release.
Requires an existing test cluster. No Docker commands or model calls are made.
The values and report are retained in the printed /tmp directory.
"""

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
from typing import cast

import yaml

from k8s_sandbox._sandbox_environment import (
    K8sSandboxEnvironment,
    K8sSandboxEnvironmentConfig,
)


async def run(args: argparse.Namespace) -> None:
    """Install two sandboxes and reuse the Docker diagnostic's probe."""
    os.environ["INSPECT_K8S_DEFAULT_NAMESPACE"] = args.namespace
    directory = Path(tempfile.mkdtemp(prefix="reaper-k8s-"))
    print(f"Values and report: {directory}", flush=True)
    service = {
        "image": "python:3.12-bookworm",
        "runtimeClassName": args.runtime,
    }
    values = directory / "values.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "networkPolicy": {"enabled": not args.no_network_policy},
                "services": {
                    # Deliberately inherit the chart's command for the fixed case.
                    "default": service,
                    "baseline": {
                        **service,
                        "command": ["tail", "-f", "/dev/null"],
                    },
                },
            }
        )
    )
    config = K8sSandboxEnvironmentConfig(
        values=values, context=args.context, restarted_container_behavior="raise"
    )
    probe = Path(__file__).with_name("probe.py").read_text()
    with (directory / "report.log").open("w") as report:

        def record(message: str) -> None:
            print(message, flush=True)
            print(message, file=report, flush=True)

        record(
            f"context={args.context} namespace={args.namespace} runtime={args.runtime}"
        )
        envs = cast(
            dict[str, K8sSandboxEnvironment],
            await K8sSandboxEnvironment.sample_init("reaper-diagnostic", config, {}),
        )
        release = envs["default"].release
        record(f"release={release.release_name}")
        try:
            for name, modes in (
                ("baseline", ("baseline",)),
                ("default", ("loop", "signals")),
            ):
                sandbox = envs[name]
                await sandbox.write_file("/tmp/reaper-probe.py", probe)
                for mode in modes:
                    record(f"service={name} mode={mode}")
                    result = await sandbox.exec(
                        ["python", "/tmp/reaper-probe.py", mode], timeout=60
                    )
                    record(result.stdout + result.stderr)
                    if not result.success:
                        raise RuntimeError(f"{name}/{mode} failed: {result.returncode}")
                result = await sandbox.exec(
                    ["python", "-c", "print('exec OK')"], timeout=10
                )
                record(result.stdout + result.stderr)
                if not result.success:
                    raise RuntimeError(f"{name}: subsequent exec failed")
            record("PASS: baseline leaks; chart default reaps and survives signals.")
        except Exception as exc:
            record(f"FAILED: {type(exc).__name__}: {exc}")
            raise
        finally:
            record(f"Uninstalling {release.release_name}")
            await release.uninstall(quiet=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--runtime", required=True, help="e.g. gvisor or runc")
    parser.add_argument(
        "--no-network-policy", action="store_true", help="For clusters without Cilium"
    )
    asyncio.run(run(parser.parse_args()))
