"""Build the exact command plan for a development deployment.

The plan is a pure function of the operator's choices and the pinned target. It
is data, not action: nothing here connects to a gateway or runs anything. That
is what makes the safety rules testable - "UI-only leaves the Agent untouched"
is a property of the returned list, provable without a gateway in the room.

Checkpoint and restore commands come from tools.gateway_recovery, the same
module the Legacy Updater uses. Reusing them is deliberate: recovery is the last
thing that should be reimplemented for a second program.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from tools.dev_updater.release_source import PinnedTarget
from tools.gateway_recovery import REMOTE_UI_PATH, checkpoint_commands, code_restore_commands


REMOTE_ARTIFACT_PATH = "/home/swadmin/edge-bacnet-ui-v2-update.tar.gz"
AGENT_SERVICE = "iot-cx-agent.service"
UI_SERVICE = "edge-bacnet-ui.service"

# Touching any of these is out of scope for every component selection. The plan
# is scanned against this list before it is returned, so a future edit that adds
# a forbidden command fails loudly rather than reaching a gateway.
FORBIDDEN_PATTERNS = (
    (r"\bbacrpm?\b", "BACnet read command"),
    (r"\bbacwp\b", "BACnet write command"),
    (r"\bcloud_url\b\s*=", "cloud_url assignment"),
    (r"iot-cx-mstp-router", "MSTP router service"),
    (r"\brm\s+-rf\s+/home/swadmin/edge-bacnet-ui-v2/data", "trend data deletion"),
    (r"\bgit\s+push\b", "git push"),
    (r"BACNET_IP_PORTS?\s*=", "BACnet port assignment"),
)


class Component(str, Enum):
    """What the operator chose to update. There is no default on purpose."""

    NONE = "none"
    UI_ONLY = "ui"
    AGENT_ONLY = "agent"
    UI_AND_AGENT = "ui+agent"

    @property
    def touches_ui(self) -> bool:
        return self in (Component.UI_ONLY, Component.UI_AND_AGENT)

    @property
    def touches_agent(self) -> bool:
        return self in (Component.AGENT_ONLY, Component.UI_AND_AGENT)

    @property
    def label(self) -> str:
        return {
            Component.NONE: "Nothing selected",
            Component.UI_ONLY: "Edge UI only",
            Component.AGENT_ONLY: "Edge Agent only",
            Component.UI_AND_AGENT: "Edge UI + Edge Agent",
        }[self]


class PlanError(RuntimeError):
    """Raised when a plan cannot be built safely."""


@dataclass(frozen=True)
class Step:
    stage: str
    description: str
    command: str
    needs_sudo: bool = False
    # A read-only step may run during preflight; everything else needs the
    # operator's explicit confirmation first.
    read_only: bool = False


@dataclass(frozen=True)
class DeploymentPlan:
    component: Component
    target: PinnedTarget
    steps: tuple[Step, ...]
    rollback_steps: tuple[Step, ...]
    rollback_location: str
    warnings: tuple[str, ...] = field(default=())

    @property
    def stages(self) -> tuple[str, ...]:
        seen: list[str] = []
        for step in self.steps:
            if step.stage not in seen:
                seen.append(step.stage)
        return tuple(seen)


# --- read-only preflight -----------------------------------------------------

def preflight_steps() -> tuple[Step, ...]:
    """Look, and change nothing.

    Every command here either reads a file, queries systemd, or prints a
    version. None writes, restarts, or configures. cloud_url is read so it can
    be displayed and compared afterwards - never to modify it.
    """
    def look(description: str, command: str) -> Step:
        return Step("preflight", description, command, read_only=True)

    return (
        look("gateway hostname", "hostname"),
        look("gateway ID", "cat /etc/iot-cx-agent/gateway-id 2>/dev/null || hostname"),
        look("current Edge UI commit", f"git -C {REMOTE_UI_PATH} rev-parse HEAD 2>/dev/null || echo unknown"),
        look("current Edge UI version", f"grep -o 'Edge Release [0-9.]*' {REMOTE_UI_PATH}/templates/base.html 2>/dev/null | head -1 || echo unknown"),
        look(
            "current Agent commit",
            "git -C /home/swadmin/iot-cloud-commissioning rev-parse HEAD 2>/dev/null || echo unknown",
        ),
        look(
            "current Agent version",
            "python3 -c 'import iot_cx_agent; print(getattr(iot_cx_agent, \"__version__\", \"unknown\"))' 2>/dev/null || echo unknown",
        ),
        look(
            "cloud_url (read only)",
            "grep -oP '(?<=^cloud_url:\\s).*' /etc/iot-cx-agent/agent.yaml 2>/dev/null "
            "|| grep -oP '(?<=^CLOUD_URL=).*' /etc/iot-cx-agent/agent.env 2>/dev/null || echo unknown",
        ),
        look("Edge UI service state", f"systemctl is-active {UI_SERVICE} 2>/dev/null || true"),
        look("Agent service state", f"systemctl is-active {AGENT_SERVICE} 2>/dev/null || true"),
        look("trend sample count", "sqlite3 /home/swadmin/edge-bacnet-ui-v2/data/edge-trends.db 'SELECT COUNT(*) FROM trend_samples' 2>/dev/null || echo unavailable"),
        look("free disk space", "df -h /home/swadmin | tail -1"),
        look("sudo available", "sudo -n true 2>/dev/null && echo yes || echo password-required"),
    )


# --- checkpoint --------------------------------------------------------------

def checkpoint_steps(target: PinnedTarget) -> tuple[Step, ...]:
    """Take a code-only checkpoint and prove it is readable before going on.

    The rollback release is the checkpoint's name: restoring means putting the
    gateway back on what it was running, so the checkpoint is filed under the
    release it came from.
    """
    steps = [
        Step("checkpoint", f"checkpoint step {index + 1}", command)
        for index, command in enumerate(checkpoint_commands(target.rollback_release))
    ]
    folder = f"/home/swadmin/gw-recovery/{target.rollback_release}"
    steps.extend([
        # Written, then read back and verified. An unverified checkpoint is not
        # a checkpoint.
        Step("checkpoint", "verify checkpoint archive integrity", f"cd {folder} && sha256sum -c pre-update-code.sha256"),
        Step("checkpoint", "verify checkpoint archive is readable", f"tar -tzf {folder}/pre-update-code.tar.gz > /dev/null && echo CHECKPOINT_READABLE=yes"),
        Step("checkpoint", "record checkpoint inventory", f"cat {folder}/manifest.txt"),
        # The three recordings below only read. Flagged as such so the scope
        # gate can tell "noted the Agent's state" from "acted on the Agent".
        Step("checkpoint", "record trend counts at checkpoint", "sqlite3 /home/swadmin/edge-bacnet-ui-v2/data/edge-trends.db 'SELECT COUNT(*) FROM trend_samples' 2>/dev/null || echo unavailable", read_only=True),
        Step("checkpoint", "record cloud_url at checkpoint", "grep -oP '(?<=^cloud_url:\\s).*' /etc/iot-cx-agent/agent.yaml 2>/dev/null || echo unknown", read_only=True),
        Step("checkpoint", "record service states at checkpoint", f"systemctl is-active {UI_SERVICE} {AGENT_SERVICE} 2>/dev/null || true", read_only=True),
    ])
    return tuple(steps)


# --- component stages --------------------------------------------------------

def ui_steps(target: PinnedTarget) -> tuple[Step, ...]:
    """Replace Edge UI code only.

    Nothing here names the Agent service, the Agent's configuration, or
    anything under data/. start.sh and .env are left exactly where they are.
    """
    return (
        Step("edge-ui", "verify uploaded artifact hash on the gateway",
             f"test \"$(sha256sum {REMOTE_ARTIFACT_PATH} | cut -d' ' -f1)\" = \"{target.artifact_sha256}\" && echo ARTIFACT_HASH_OK"),
        Step("edge-ui", "prepare extraction folder",
             "rm -rf /tmp/edge-dev-ui-update && mkdir -p /tmp/edge-dev-ui-update"),
        Step("edge-ui", "extract artifact",
             f"tar -xzf {REMOTE_ARTIFACT_PATH} -C /tmp/edge-dev-ui-update"),
        Step("edge-ui", "verify extracted payload carries no site data",
             "test ! -e /tmp/edge-dev-ui-update/data && test ! -e /tmp/edge-dev-ui-update/.env "
             "&& test ! -e /tmp/edge-dev-ui-update/start.sh && echo PAYLOAD_IS_CODE_ONLY"),
        Step("edge-ui", "stop Edge UI", f"sudo -S -p '' systemctl stop {UI_SERVICE}", needs_sudo=True),
        Step("edge-ui", "apply code files",
             "cd /tmp/edge-dev-ui-update && for item in app.py edge_program_engine.py edge_trend_store.py "
             "timed_override_store.py router_config.py requirements.txt templates static; do "
             f"[ -e \"$item\" ] && cp -a \"$item\" {REMOTE_UI_PATH}/; done; true"),
        Step("edge-ui", "restore ownership",
             f"sudo -S -p '' chown -R swadmin:swadmin {REMOTE_UI_PATH}", needs_sudo=True),
        Step("edge-ui", "keep start.sh executable", f"chmod +x {REMOTE_UI_PATH}/start.sh"),
        Step("edge-ui", "start Edge UI",
             f"sudo -S -p '' systemctl start --no-block {UI_SERVICE}", needs_sudo=True),
        Step("edge-ui", "confirm Edge UI answers",
             f"sleep 5 && systemctl is-active {UI_SERVICE} && curl -sS -o /dev/null -w 'HTTP %{{http_code}}\\n' http://127.0.0.1:5000/"),
    )


def agent_steps(target: PinnedTarget) -> tuple[Step, ...]:
    """Update the Edge Agent to an exact commit, leaving its cloud_url alone.

    The Agent's configuration is read to confirm compatibility and then left
    untouched. The gateway stays pointed at whichever cloud it was already
    pointed at.
    """
    repo = "/home/swadmin/iot-cloud-commissioning"
    return (
        Step("edge-agent", "record cloud_url before Agent work",
             "grep -oP '(?<=^cloud_url:\\s).*' /etc/iot-cx-agent/agent.yaml 2>/dev/null || echo unknown",
             read_only=True),
        Step("edge-agent", "fetch approved commit only", f"git -C {repo} fetch --no-tags origin {target.agent_commit}"),
        Step("edge-agent", "verify the commit arrived",
             f"git -C {repo} cat-file -e {target.agent_commit}^{{commit}} && echo AGENT_COMMIT_PRESENT"),
        Step("edge-agent", "check out the exact commit", f"git -C {repo} checkout --detach {target.agent_commit}"),
        Step("edge-agent", "confirm checked-out commit matches the manifest",
             f"test \"$(git -C {repo} rev-parse HEAD)\" = \"{target.agent_commit}\" && echo AGENT_COMMIT_OK"),
        Step("edge-agent", "install Agent dependencies",
             f"{repo}/edge-agent/.venv/bin/pip install -q -r {repo}/edge-agent/requirements.txt"),
        Step("edge-agent", "restart Agent", f"sudo -S -p '' systemctl restart {AGENT_SERVICE}", needs_sudo=True),
        Step("edge-agent", "confirm Agent is running",
             f"sleep 5 && systemctl is-active {AGENT_SERVICE}"),
        Step("edge-agent", "confirm cloud_url is unchanged",
             "grep -oP '(?<=^cloud_url:\\s).*' /etc/iot-cx-agent/agent.yaml 2>/dev/null || echo unknown",
             read_only=True),
    )


def postflight_steps(component: Component) -> tuple[Step, ...]:
    steps = [
        Step("postflight", "gateway hostname", "hostname", read_only=True),
        Step("postflight", "cloud_url after update",
             "grep -oP '(?<=^cloud_url:\\s).*' /etc/iot-cx-agent/agent.yaml 2>/dev/null || echo unknown", read_only=True),
        Step("postflight", "trend sample count after update",
             "sqlite3 /home/swadmin/edge-bacnet-ui-v2/data/edge-trends.db 'SELECT COUNT(*) FROM trend_samples' 2>/dev/null || echo unavailable",
             read_only=True),
    ]
    if component.touches_ui:
        steps.append(Step("postflight", "Edge UI commit after update",
                          f"git -C {REMOTE_UI_PATH} rev-parse HEAD 2>/dev/null || echo unknown", read_only=True))
        steps.append(Step("postflight", "Edge UI service state",
                          f"systemctl is-active {UI_SERVICE} 2>/dev/null || true", read_only=True))
    if component.touches_agent:
        steps.append(Step("postflight", "Agent commit after update",
                          "git -C /home/swadmin/iot-cloud-commissioning rev-parse HEAD 2>/dev/null || echo unknown", read_only=True))
        steps.append(Step("postflight", "Agent service state",
                          f"systemctl is-active {AGENT_SERVICE} 2>/dev/null || true", read_only=True))
    return tuple(steps)


def rollback_plan(target: PinnedTarget) -> tuple[Step, ...]:
    """Code-only restore. Site data is never in the archive, so it cannot be lost."""
    return tuple(
        Step("rollback", f"rollback step {index + 1}", command)
        for index, command in enumerate(code_restore_commands(target.rollback_release))
    )


# --- assembly ----------------------------------------------------------------

def build_plan(component: Component, target: PinnedTarget, *, confirmed: bool, agent_confirmed: bool = False) -> DeploymentPlan:
    """Assemble the plan, refusing anything the safety model forbids.

    confirmed is the operator's explicit deployment confirmation. Without it
    there is no plan to run - not an empty one, an error. An Agent update needs
    a second, separate confirmation on top.
    """
    if component is Component.NONE:
        raise PlanError("Select a component to update. There is no default.")
    if not confirmed:
        raise PlanError("Deployment has not been confirmed by the operator. Nothing will run.")
    if component.touches_agent and not agent_confirmed:
        raise PlanError(
            "Updating the Edge Agent needs a second explicit confirmation. "
            "The Agent talks to the Cloud; confirm you intend to change it on this gateway."
        )

    warnings: list[str] = []
    steps: list[Step] = list(preflight_steps())
    steps.extend(checkpoint_steps(target))
    if component.touches_ui:
        steps.extend(ui_steps(target))
    if component.touches_agent:
        steps.extend(agent_steps(target))
        warnings.append(
            "Edge Agent update selected. The Agent will restart and reconnect to its EXISTING cloud_url, "
            "which this application never changes. Confirm the gateway's cloud is the one you intend to test against."
        )
    steps.extend(postflight_steps(component))

    plan = DeploymentPlan(
        component=component,
        target=target,
        steps=tuple(steps),
        rollback_steps=rollback_plan(target),
        rollback_location=f"/home/swadmin/gw-recovery/{target.rollback_release}/pre-update-code.tar.gz",
        warnings=tuple(warnings),
    )
    _assert_plan_is_in_scope(plan)
    return plan


def _assert_plan_is_in_scope(plan: DeploymentPlan) -> None:
    """A last gate between a built plan and a gateway.

    Scope is checked on the assembled commands rather than trusted from the
    builders above, so a careless future edit is caught here instead of in the
    field.
    """
    for step in plan.steps:
        for pattern, what in FORBIDDEN_PATTERNS:
            if re.search(pattern, step.command):
                raise PlanError(f"Plan step '{step.description}' contains a forbidden {what}: {step.command}")

    if not plan.component.touches_agent:
        for step in plan.steps:
            # A UI-only plan may *read* the Agent's state; it may not act on it.
            if step.read_only:
                continue
            if AGENT_SERVICE in step.command or "/etc/iot-cx-agent" in step.command:
                raise PlanError(
                    f"UI-only plan step '{step.description}' would act on the Edge Agent: {step.command}"
                )

    if not plan.component.touches_ui:
        for step in plan.steps:
            if step.read_only:
                continue
            if REMOTE_ARTIFACT_PATH in step.command or f"cp -a \"$item\" {REMOTE_UI_PATH}" in step.command:
                raise PlanError(
                    f"Agent-only plan step '{step.description}' would replace Edge UI files: {step.command}"
                )

    stages = plan.stages
    if "checkpoint" not in stages:
        raise PlanError("Every plan must take a checkpoint.")
    work_stages = [stage for stage in stages if stage in ("edge-ui", "edge-agent")]
    if work_stages and stages.index("checkpoint") > min(stages.index(stage) for stage in work_stages):
        raise PlanError("The checkpoint must be taken and verified before any change is applied.")
