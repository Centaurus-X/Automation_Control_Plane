# -*- coding: utf-8 -*-

"""Operational prompts that guide models through safe read and change flows."""

import copy

from mcp_controller_server.errors import make_error, make_success


PROMPTS = (
    {
        "name": "assess_automation_health",
        "title": "Assess automation system health",
        "description": "Build an evidence-first health assessment from MQTT-backed read tools.",
        "arguments": [{
            "name": "focus",
            "description": "Optional node, controller or symptom to emphasize.",
            "required": False,
        }],
    },
    {
        "name": "plan_safe_automation_change",
        "title": "Plan a safe automation change",
        "description": "Create a non-mutating plan that respects authority, revisions and approval.",
        "arguments": [
            {
                "name": "goal",
                "description": "The desired operational outcome.",
                "required": True,
            },
            {
                "name": "scope",
                "description": "Optional node:<id> or cross:<id> authority scope.",
                "required": False,
            },
        ],
    },
)


def list_prompts():
    return [copy.deepcopy(prompt) for prompt in PROMPTS]


def get_prompt(name, arguments):
    values = arguments if isinstance(arguments, dict) else {}
    if name == "assess_automation_health":
        unknown = sorted(set(values) - {"focus"})
        if unknown:
            return make_error("invalid_arguments", "unknown prompt arguments", fields=unknown)
        focus = str(values.get("focus") or "the complete site")
        text = (
            "Assess automation health for {}. First inspect automation_connection_status, "
            "then automation_runtime_status and automation_nodes_list. Use "
            "automation_events_recent and automation_sensors_latest only as bounded "
            "post-connect evidence. Separate confirmed faults from missing evidence. "
            "Do not prepare or commit any change."
        ).format(focus)
        return make_success(
            description="Evidence-first automation health assessment",
            messages=[{"role": "user", "content": {"type": "text", "text": text}}],
        )
    if name == "plan_safe_automation_change":
        unknown = sorted(set(values) - {"goal", "scope"})
        if unknown or not isinstance(values.get("goal"), str) or not values["goal"]:
            return make_error("invalid_arguments", "goal is required and must be text")
        scope = str(values.get("scope") or "the relevant node authority scope")
        text = (
            "Plan this automation outcome without applying it: {}. Scope: {}. Read current "
            "runtime, nodes and authoritative configuration first. Preserve node-local "
            "authority, base epoch, scope revision and fingerprint. If a valid canonical "
            "operation can be proposed, call automation_change_prepare only. Report its "
            "digest and wait for external operator approval; never call commit without that "
            "approval."
        ).format(values["goal"], scope)
        return make_success(
            description="Non-mutating automation change plan",
            messages=[{"role": "user", "content": {"type": "text", "text": text}}],
        )
    return make_error("prompt_not_found", "prompt is unknown")
