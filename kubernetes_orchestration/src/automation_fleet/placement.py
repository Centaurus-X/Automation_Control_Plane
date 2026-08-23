"""Deterministic target placement with explicit exclusion evidence."""


def _target_key(target):
    return (
        int(target.get("wave", 0)),
        target["site_id"],
        target.get("failure_domain", target["cluster_id"]),
        target["cluster_id"],
        target["target_id"],
    )


def _selector_reasons(target, selector, required_capabilities):
    reasons = []
    target_kind = selector.get("target_kind")
    if target_kind and target["target_kind"] != target_kind:
        reasons.append("target_kind_mismatch")
    site_ids = selector.get("site_ids", [])
    if site_ids and target["site_id"] not in site_ids:
        reasons.append("site_mismatch")
    cluster_ids = selector.get("cluster_ids", [])
    if cluster_ids and target["cluster_id"] not in cluster_ids:
        reasons.append("cluster_mismatch")
    failure_domains = selector.get("failure_domains", [])
    if failure_domains and target["failure_domain"] not in failure_domains:
        reasons.append("failure_domain_mismatch")
    execution_modes = selector.get("execution_modes", [])
    if execution_modes and target["execution_mode"] not in execution_modes:
        reasons.append("execution_mode_mismatch")
    write_owners = selector.get("write_owners", [])
    if write_owners and target["write_owner"] not in write_owners:
        reasons.append("write_owner_mismatch")
    labels = target.get("labels", {})
    for key in sorted(selector.get("match_labels", {})):
        if labels.get(key) != selector["match_labels"][key]:
            reasons.append("label_mismatch:" + key)
    target_capabilities = set(target.get("capabilities", []))
    requested = set(selector.get("required_capabilities", []))
    requested.update(required_capabilities)
    for capability in sorted(requested - target_capabilities):
        reasons.append("capability_missing:" + capability)
    return reasons


def select_targets(inventory, placement, required_capabilities):
    """Return stable selected targets and a complete placement decision trail."""
    targets = sorted(inventory["targets"], key=_target_key)
    requested_ids = placement.get("target_ids")
    selector = placement.get("selector")
    decisions = []
    selected = []
    if requested_ids is not None:
        requested = set(requested_ids)
        found = set()
        for rank, target in enumerate(targets, start=1):
            is_selected = target["target_id"] in requested
            if is_selected:
                found.add(target["target_id"])
                capability_reasons = _selector_reasons(
                    target, {}, required_capabilities)
                if capability_reasons:
                    is_selected = False
                    reasons = capability_reasons
                else:
                    reasons = ["explicit_target_id"]
                    selected.append(target)
            else:
                reasons = ["not_explicitly_selected"]
            decisions.append({
                "target_id": target["target_id"],
                "selected": is_selected,
                "rank": rank,
                "reasons": reasons,
            })
        missing = sorted(requested - found)
        if missing:
            raise ValueError("unknown_target: unknown targets: " + ",".join(missing))
        if not selected:
            raise ValueError("placement_empty: explicit targets lack required capabilities")
        return selected, decisions

    candidates = []
    for rank, target in enumerate(targets, start=1):
        reasons = _selector_reasons(target, selector, required_capabilities)
        is_selected = not reasons
        if is_selected:
            candidates.append(target)
            reasons = ["selector_match"]
        decisions.append({
            "target_id": target["target_id"],
            "selected": is_selected,
            "rank": rank,
            "reasons": reasons,
        })
    maximum = selector.get("max_targets")
    if maximum is not None and len(candidates) > maximum:
        retained = {target["target_id"] for target in candidates[:maximum]}
        candidates = candidates[:maximum]
        for decision in decisions:
            if decision["selected"] and decision["target_id"] not in retained:
                decision["selected"] = False
                decision["reasons"] = ["selector_max_targets"]
    if not candidates:
        raise ValueError("placement_empty: no target satisfies the placement contract")
    return candidates, decisions


def rollout_waves(targets):
    """Build stable wave and failure-domain batches for plan evidence."""
    grouped = {}
    for target in targets:
        wave = int(target.get("wave", 0))
        grouped.setdefault(wave, []).append(target)
    result = []
    for wave in sorted(grouped):
        wave_targets = sorted(grouped[wave], key=_target_key)
        result.append({
            "wave": wave,
            "target_ids": [target["target_id"] for target in wave_targets],
            "failure_domains": sorted({
                target["failure_domain"] for target in wave_targets}),
            "sites": sorted({target["site_id"] for target in wave_targets}),
            "canary_target_ids": [
                next(
                    target["target_id"] for target in wave_targets
                    if target["failure_domain"] == failure_domain
                )
                for failure_domain in sorted({
                    target["failure_domain"] for target in wave_targets})
            ],
        })
    return result
