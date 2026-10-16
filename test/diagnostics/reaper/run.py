"""Manual Docker diagnostic: uv run python test/diagnostics/reaper/run.py.

Runs dash, BusyBox ash, and Bash as PID 1, without Docker init. Each case checks
the old tail baseline, the chart's loop, and signal resilience. No image builds
or PID exhaustion are needed. Reports are saved under /tmp. Containers are
stopped, but retained for inspection; their names are printed in the report.
"""

import argparse
import subprocess
import time
from pathlib import Path

import yaml

CASES = {
    "dash": ("python:3.12-bookworm", "sh"),
    "ash": ("python:3.12-alpine", "sh"),
    "bash": ("python:3.12-bookworm", "/bin/bash"),
}


def main() -> None:
    """Run only when invoked manually by someone with Docker access."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", choices=CASES, action="append")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    root = here.parents[2]
    values = root / "src/k8s_sandbox/resources/helm/agent-env/values.yaml"
    command = yaml.safe_load(values.read_text())["services"]["default"]["command"]
    stamp = time.time_ns()
    report = Path(f"/tmp/reaper-{stamp}.log")
    print(f"Report: {report}", flush=True)
    with report.open("w") as log:

        def run(*argv: str, timeout: int = 60) -> str:
            print("$ " + " ".join(argv), file=log, flush=True)
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout
            )
            output = result.stdout + result.stderr
            print(output, end="", flush=True)
            print(output, file=log, end="", flush=True)
            result.check_returncode()
            return result.stdout.strip()

        for case in args.shell or CASES:
            image, shell = CASES[case]
            run("docker", "pull", image, timeout=300)
            run("docker", "image", "inspect", "--format", "{{.Id}}", image)
            for mode in ("baseline", "loop"):
                name = f"reaper-{stamp}-{case}-{mode}"
                entrypoint, *arguments = (
                    ["tail", "-f", "/dev/null"]
                    if mode == "baseline"
                    else [shell, *command[1:]]
                )
                print(f"\n{case}: {mode} ({name})", flush=True)
                run(
                    "docker",
                    "run",
                    "--detach",
                    "--init=false",
                    "--network=none",
                    "--pids-limit=256",
                    "--name",
                    name,
                    "--mount",
                    f"type=bind,src={here},dst=/diagnostic,readonly",
                    "--entrypoint",
                    entrypoint,
                    image,
                    *arguments,
                )
                try:
                    run("docker", "exec", name, "python", "/diagnostic/probe.py", mode)
                    if mode == "loop":
                        run(
                            "docker",
                            "exec",
                            name,
                            "python",
                            "/diagnostic/probe.py",
                            "signals",
                        )
                    run("docker", "exec", name, "python", "-c", "print('exec OK')")
                finally:
                    run("docker", "stop", "--time=0", name)
        print("PASS: baseline leaks; all tested loops reap and survive signals.")
        print("PASS", file=log)


if __name__ == "__main__":
    main()
