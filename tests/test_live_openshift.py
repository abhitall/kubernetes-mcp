"""
End-to-end live test for the Kubernetes MCP Server against OpenShift CRC.

Run with:
    python tests/test_live_openshift.py
"""

# ruff: noqa: E402

import json
import os
import subprocess
import sys
import warnings

import urllib3

warnings.filterwarnings("ignore")
urllib3.disable_warnings()

# Get fresh token
TOKEN = subprocess.check_output(["oc", "whoami", "-t"]).decode().strip()

os.environ["CLUSTER_REGISTRY"] = json.dumps([{
    "name": "openshift-crc",
    "flavor": "openshift",
    "api_server": "https://api.crc.testing:6443",
    "openshift_oauth_token": TOKEN,
    "skip_tls_verify": True,
    "namespace": "default",
}])

from src.models import ClusterConfig, K8sFlavor
from src.connectors.cluster import ClusterConnector

cfg = ClusterConfig(
    name="openshift-crc",
    flavor=K8sFlavor.OPENSHIFT,
    api_server="https://api.crc.testing:6443",
    openshift_oauth_token=TOKEN,
    skip_tls_verify=True,
)

conn = ClusterConnector()
conn.register(cfg)

# Override the module-level singleton so k8s_ops uses our connector
import src.connectors.cluster as cmod

cmod.connector = conn

from src.tools import k8s_ops
from src.tools.rca import run_cluster_rca, run_namespace_rca, run_pod_rca
from src.tools.self_heal import generate_heal_plan
from src.resources.cluster_resources import get_cluster_list, get_cluster_health_resource
from src.prompts.k8s_prompts import get_prompt

CLUSTER = "openshift-crc"
OK = 0
FAIL = 0
RESULTS = []


def test(name, fn):
    global OK, FAIL
    try:
        result = fn()
        short = str(result)[:200]
        print(f"  PASS: {name} -> {short}")
        RESULTS.append(("PASS", name))
        OK += 1
        return result
    except Exception as e:
        print(f"  FAIL: {name} -> {type(e).__name__}: {e}")
        RESULTS.append(("FAIL", name))
        FAIL += 1
        return None


# ============================================================
print("=" * 60)
print("KUBERNETES MCP SERVER - OPENSHIFT LIVE TEST")
print("=" * 60)

# -- CLUSTER MANAGEMENT --
print("\n== CLUSTER MANAGEMENT ==")
test("get_cluster_list", lambda: get_cluster_list())
test("get_cluster_health_resource", lambda: get_cluster_health_resource(CLUSTER))

# -- NAMESPACES --
print("\n== NAMESPACES ==")
namespaces = test("list_namespaces", lambda: k8s_ops.list_namespaces(CLUSTER))
assert namespaces is not None and len(namespaces) > 0, "No namespaces found"

# -- PODS --
print("\n== PODS ==")
test("list_pods(default)", lambda: k8s_ops.list_pods(CLUSTER, "default"))
pods = test("list_pods(openshift-console)", lambda: k8s_ops.list_pods(CLUSTER, "openshift-console"))

if pods:
    pod = pods[0]
    pn = pod["name"]
    ns = pod["namespace"]
    test(f"get_pod({pn})", lambda: k8s_ops.get_pod(CLUSTER, ns, pn))
    test(f"get_pod_logs({pn})", lambda: k8s_ops.get_pod_logs(CLUSTER, ns, pn, tail_lines=10))
    # previous=True may fail if pod has never been restarted (expected)
    try:
        result = k8s_ops.get_pod_logs(CLUSTER, ns, pn, tail_lines=5, previous=True)
        print(f"  PASS: get_pod_logs({pn},previous=True) -> {str(result)[:100]}")
        RESULTS.append(("PASS", f"get_pod_logs({pn},previous=True)"))
        OK += 1
    except Exception as e:
        if "previous terminated container" in str(e) or "Bad Request" in str(e):
            print(f"  SKIP: get_pod_logs({pn},previous=True) -> No previous container (expected)")
            RESULTS.append(("SKIP", f"get_pod_logs({pn},previous=True)"))
            OK += 1  # Not a real failure
        else:
            print(f"  FAIL: get_pod_logs({pn},previous=True) -> {e}")
            RESULTS.append(("FAIL", f"get_pod_logs({pn},previous=True)"))
            FAIL += 1

# -- DEPLOYMENTS --
print("\n== DEPLOYMENTS ==")
test("list_deployments(default)", lambda: k8s_ops.list_deployments(CLUSTER, "default"))
deps = test("list_deployments(openshift-console)", lambda: k8s_ops.list_deployments(CLUSTER, "openshift-console"))
if deps:
    dn = deps[0]["name"]
    test(f"get_deployment({dn})", lambda: k8s_ops.get_deployment(CLUSTER, "openshift-console", dn))

# -- NODES --
print("\n== NODES ==")
nodes = test("list_nodes", lambda: k8s_ops.list_nodes(CLUSTER))

# -- EVENTS --
print("\n== EVENTS ==")
test("list_events(default)", lambda: k8s_ops.list_events(CLUSTER, "default"))
test("list_events(openshift-console)", lambda: k8s_ops.list_events(CLUSTER, "openshift-console"))

# -- SERVICES --
print("\n== SERVICES ==")
test("list_services(default)", lambda: k8s_ops.list_services(CLUSTER, "default"))
test("list_services(openshift-console)", lambda: k8s_ops.list_services(CLUSTER, "openshift-console"))

# -- CONFIGMAPS --
print("\n== CONFIGMAPS ==")
test("list_configmaps(default)", lambda: k8s_ops.list_configmaps(CLUSTER, "default"))
cms = k8s_ops.list_configmaps(CLUSTER, "default")
if cms:
    cm_name = cms[0]["name"]
    test(f"get_configmap({cm_name})", lambda: k8s_ops.get_configmap(CLUSTER, "default", cm_name))

# -- SECRETS --
print("\n== SECRETS ==")
test("list_secrets(default)", lambda: k8s_ops.list_secrets(CLUSTER, "default"))

# -- GENERIC RESOURCES --
print("\n== GENERIC RESOURCES ==")
test("list_resources(routes)", lambda: k8s_ops.list_resources(CLUSTER, "route.openshift.io/v1", "Route", "openshift-console"))
test("get_resource(route)", lambda: k8s_ops.get_resource(CLUSTER, "route.openshift.io/v1", "Route", "console", "openshift-console"))

# -- RCA --
print("\n== ROOT CAUSE ANALYSIS ==")
rca_report = test("run_cluster_rca", lambda: run_cluster_rca(CLUSTER))
test("run_namespace_rca(default)", lambda: run_namespace_rca(CLUSTER, "default"))
if pods:
    test(f"run_pod_rca({pn})", lambda: run_pod_rca(CLUSTER, ns, pn))

# -- SELF HEAL (dry run) --
print("\n== SELF HEAL (dry run) ==")
if rca_report:
    plan = test("generate_heal_plan", lambda: generate_heal_plan(rca_report, dry_run=True))
    if plan:
        print(f"    Plan has {len(plan.actions)} proposed actions")

# -- PROMPTS --
print("\n== PROMPTS ==")
test("cluster_health_check", lambda: get_prompt("cluster_health_check", cluster=CLUSTER))
test("pod_troubleshoot", lambda: get_prompt("pod_troubleshoot", cluster=CLUSTER, namespace="default", pod="test"))
test("self_heal_workflow", lambda: get_prompt("self_heal_workflow", cluster=CLUSTER))
test("namespace_review", lambda: get_prompt("namespace_review", cluster=CLUSTER, namespace="default"))
test("multi_cluster_overview", lambda: get_prompt("multi_cluster_overview"))
test("incident_response", lambda: get_prompt("incident_response", alert_message="CPU high on node crc"))

# -- SUMMARY --
print("\n" + "=" * 60)
print(f"TOTAL: {OK} PASSED, {FAIL} FAILED out of {OK + FAIL}")
print("=" * 60)

if FAIL > 0:
    print("\nFailed tests:")
    for status, name in RESULTS:
        if status == "FAIL":
            print(f"  - {name}")
    sys.exit(1)
else:
    print("\nAll tests passed!")
    sys.exit(0)
