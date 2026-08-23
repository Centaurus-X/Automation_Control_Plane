"""Live multi-cluster snapshot and Prometheus exposition."""

import time

from automation_fleet.provider_kubernetes import observe_target
from automation_fleet.validation import validate_inventory


def _agent_observation(target, agent_states):
    state = agent_states.get(target["target_id"])
    if state is None or state.get("status") != "ONLINE":
        return {
            "target_id": target["target_id"],
            "cluster_id": target["cluster_id"],
            "namespace": target["namespace"],
            "status": "UNREACHABLE",
            "error": "outbound cluster agent is offline",
            "resources": [],
            "agent_status": "OFFLINE",
        }
    observation = state.get("observation")
    if not isinstance(observation, dict) or not observation:
        return {
            "target_id": target["target_id"],
            "cluster_id": target["cluster_id"],
            "namespace": target["namespace"],
            "status": "DEGRADED",
            "error": "outbound cluster agent has no observation",
            "resources": [],
            "agent_status": "ONLINE",
        }
    result = dict(observation)
    result["agent_status"] = "ONLINE"
    result["agent_holder_id"] = state.get("holder_id")
    result["agent_lease_epoch"] = state.get("lease_epoch")
    return result


def collect_snapshot(inventory_value, timeout_s=30, agent_states=None):
    inventory = validate_inventory(inventory_value)
    states = agent_states or {}
    targets = []
    for target in sorted(inventory["targets"], key=_target_key):
        if target.get("execution_mode") == "agent":
            targets.append(_agent_observation(target, states))
        else:
            targets.append(observe_target(target, timeout_s=timeout_s))
    healthy = sum(1 for target in targets if target["status"] == "HEALTHY")
    degraded = sum(1 for target in targets if target["status"] == "DEGRADED")
    unreachable = len(targets) - healthy - degraded
    resources = sum(int(target.get("resource_count", 0)) for target in targets)
    online_agents = sum(1 for state in states.values() if state.get("status") == "ONLINE")
    return {
        "schema_version": 1,
        "observed_at_unix": int(time.time()),
        "environment_id": inventory["environment_id"],
        "summary": {
            "targets": len(targets),
            "healthy": healthy,
            "degraded": degraded,
            "unreachable": unreachable,
            "resources": resources,
            "agents_online": online_agents,
            "agents_registered": len(states),
        },
        "targets": targets,
    }


def _target_key(target):
    return (target.get("wave", 0), target["site_id"], target["target_id"])


def metrics_text(snapshot, plans, audit_valid=True):
    summary = snapshot["summary"]
    lines = [
        "# HELP automation_fleet_targets Number of registered targets.",
        "# TYPE automation_fleet_targets gauge",
        "automation_fleet_targets " + str(summary["targets"]),
        "# HELP automation_fleet_targets_healthy Number of healthy targets.",
        "# TYPE automation_fleet_targets_healthy gauge",
        "automation_fleet_targets_healthy " + str(summary["healthy"]),
        "# HELP automation_fleet_targets_degraded Number of degraded targets.",
        "# TYPE automation_fleet_targets_degraded gauge",
        "automation_fleet_targets_degraded " + str(summary["degraded"]),
        "# HELP automation_fleet_targets_unreachable Number of unreachable targets.",
        "# TYPE automation_fleet_targets_unreachable gauge",
        "automation_fleet_targets_unreachable " + str(summary["unreachable"]),
        "# HELP automation_fleet_observed_resources Number of observed resources.",
        "# TYPE automation_fleet_observed_resources gauge",
        "automation_fleet_observed_resources " + str(summary["resources"]),
        "# HELP automation_fleet_agents_online Number of online outbound cluster agents.",
        "# TYPE automation_fleet_agents_online gauge",
        "automation_fleet_agents_online " + str(summary.get("agents_online", 0)),
        "# HELP automation_fleet_agents_registered Number of registered outbound cluster agents.",
        "# TYPE automation_fleet_agents_registered gauge",
        "automation_fleet_agents_registered " + str(summary.get("agents_registered", 0)),
        "# HELP automation_fleet_audit_chain_valid Whether the local audit hash chain verifies.",
        "# TYPE automation_fleet_audit_chain_valid gauge",
        "automation_fleet_audit_chain_valid " + ("1" if audit_valid else "0"),
    ]
    status_counts = {}
    for plan in plans:
        status = plan["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    lines.extend([
        "# HELP automation_fleet_plans Number of journaled plans by status.",
        "# TYPE automation_fleet_plans gauge",
    ])
    for status in sorted(status_counts):
        safe_status = status.replace('"', "")
        lines.append(
            'automation_fleet_plans{status="' + safe_status + '"} '
            + str(status_counts[status]))
    return "\n".join(lines) + "\n"
