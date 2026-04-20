"""
Kubernetes MCP Server – Prompts

Pre-built prompt templates that an LLM client can use to kick off common
Kubernetes operational workflows (RCA, self-healing, health review, etc.).
"""

from __future__ import annotations

CLUSTER_HEALTH_CHECK = (
    "You are a Kubernetes operations expert.  Perform a health check on "
    "cluster '{cluster}'.  Use the `cluster_rca` tool to gather issues, then "
    "summarise the overall health, highlight critical problems and "
    "recommend remediation steps."
)

POD_TROUBLESHOOT = (
    "You are a Kubernetes debugging expert.  A user reports problems with pod "
    "'{pod}' in namespace '{namespace}' on cluster '{cluster}'.  "
    "Use the `pod_rca` tool to analyse the pod, fetch its logs with "
    "`get_pod_logs`, and inspect related events.  Provide a root cause "
    "analysis and suggest fixes."
)

SELF_HEAL_WORKFLOW = (
    "You are a Kubernetes SRE.  An RCA report has identified issues on "
    "cluster '{cluster}'.  Review the report, generate a healing plan using "
    "`generate_heal_plan`, present the plan to the user for approval, and "
    "upon approval execute it with `execute_heal_plan`.  Always prefer "
    "dry-run first."
)

NAMESPACE_REVIEW = (
    "You are a Kubernetes platform engineer.  Review all workloads in "
    "namespace '{namespace}' on cluster '{cluster}'.  List pods, "
    "deployments, and services.  Check for any under-replicated deployments, "
    "failing pods, or warning events.  Provide a structured summary."
)

MULTI_CLUSTER_OVERVIEW = (
    "You are a multi-cluster Kubernetes administrator.  List all registered "
    "clusters using `list_clusters`, run a health check on each, and produce "
    "a consolidated dashboard showing cluster name, health status, node "
    "count, and top issues."
)

INCIDENT_RESPONSE = (
    "You are an incident responder for Kubernetes infrastructure.  An alert "
    "has been triggered: '{alert_message}'.  Identify the affected cluster "
    "and resources, run RCA, propose a remediation plan, and if approved "
    "execute self-healing actions.  Document each step taken."
)


def get_prompt(name: str, **kwargs: str) -> str:
    """Retrieve a prompt template by name and fill in variables."""
    templates = {
        "cluster_health_check": CLUSTER_HEALTH_CHECK,
        "pod_troubleshoot": POD_TROUBLESHOOT,
        "self_heal_workflow": SELF_HEAL_WORKFLOW,
        "namespace_review": NAMESPACE_REVIEW,
        "multi_cluster_overview": MULTI_CLUSTER_OVERVIEW,
        "incident_response": INCIDENT_RESPONSE,
    }
    tmpl = templates.get(name)
    if tmpl is None:
        return f"Unknown prompt: {name}. Available: {', '.join(templates.keys())}"
    try:
        return tmpl.format(**kwargs)
    except KeyError as e:
        return f"Missing variable {e} for prompt '{name}'."
