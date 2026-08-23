"""Functional provider registry with one explicit write owner per step."""

from automation_fleet import provider_gitops, provider_kubernetes


def execute_operation(root, step):
    provider = step.get("provider")
    if provider == "kubernetes_helm":
        return provider_kubernetes.execute_operation(root, step)
    if provider in {"gitops_argocd", "gitops_flux"}:
        return provider_gitops.execute_operation(root, step)
    raise ValueError("provider: unsupported provider " + str(provider))


def rollback_operation(step, rollback_ref):
    provider = step.get("provider")
    if provider == "kubernetes_helm":
        return provider_kubernetes.rollback(step, rollback_ref)
    if provider in {"gitops_argocd", "gitops_flux"}:
        return provider_gitops.rollback(step, rollback_ref)
    raise ValueError("provider: unsupported rollback provider " + str(provider))


def rollback_reference(step, provider_result):
    provider = step.get("provider")
    if provider == "kubernetes_helm":
        return provider_result.get("previous_revision")
    if provider in {"gitops_argocd", "gitops_flux"}:
        return provider_result.get("observed_commit")
    return None
