"""Run steps against a gateway over SSH.

Kept apart from the plan so that everything which decides *what* to do is pure
and testable, and this file only decides *how* to do it. paramiko is imported
lazily: the plan, the identity and the whole test suite work without it.

Only read_only steps can be run by run_preflight(). Running a plan requires an
explicitly confirmed DeploymentPlan, which build_plan() will not produce
without the operator's consent.
"""
from __future__ import annotations

from dataclasses import dataclass

from tools.dev_updater.plan import DeploymentPlan, Step


class ExecutorError(RuntimeError):
    """Raised when a gateway cannot be reached or a required step fails."""


@dataclass(frozen=True)
class StepResult:
    step: Step
    exit_status: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


def _client(host: str, user: str, password: str):
    try:
        import paramiko
    except ImportError as error:  # pragma: no cover - environment dependent
        raise ExecutorError(
            "paramiko is not installed. Run the installed launcher, which provisions it, "
            "or 'pip install paramiko' in this environment."
        ) from error

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, username=user, password=password, timeout=20,
                       allow_agent=False, look_for_keys=False)
    except Exception as error:
        raise ExecutorError(f"Could not connect to {user}@{host}: {error}") from error
    return client


def _run(client, command: str, *, sudo_password: str | None = None, timeout: int = 300) -> tuple[int, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if sudo_password and "sudo -S" in command:
        stdin.write(sudo_password + "\n")
        stdin.flush()
    output = stdout.read().decode("utf-8", "replace") + stderr.read().decode("utf-8", "replace")
    return stdout.channel.recv_exit_status(), output.strip()


def run_preflight(host: str, user: str, password: str, steps: tuple[Step, ...]) -> dict[str, str]:
    """Read the gateway and return what it said. Writes nothing.

    Every step is checked for read_only before it is sent, so a future edit that
    slips a mutating command into the preflight list cannot reach the gateway.
    """
    for step in steps:
        if not step.read_only:
            raise ExecutorError(f"Preflight step '{step.description}' is not read-only. Refused.")

    client = _client(host, user, password)
    try:
        readings: dict[str, str] = {}
        for step in steps:
            _, output = _run(client, step.command, timeout=60)
            readings[step.description] = output or "(no output)"
        return readings
    finally:
        client.close()


def run_plan(plan: DeploymentPlan, host: str, user: str, password: str, *,
             sudo_password: str | None = None) -> list[StepResult]:
    """Execute a confirmed plan, stopping at the first failure.

    Stopping matters: a half-applied update with a verified checkpoint behind it
    is recoverable, and continuing past a failed step is how that stops being
    true.
    """
    client = _client(host, user, password)
    results: list[StepResult] = []
    try:
        for step in plan.steps:
            status, output = _run(client, step.command,
                                  sudo_password=sudo_password if step.needs_sudo else None)
            results.append(StepResult(step, status, output))
            if status != 0 and not step.read_only:
                raise ExecutorError(
                    f"Step '{step.description}' failed with exit status {status}. "
                    f"Stopped before any further change. Roll back from {plan.rollback_location}.\n{output}"
                )
        return results
    finally:
        client.close()
