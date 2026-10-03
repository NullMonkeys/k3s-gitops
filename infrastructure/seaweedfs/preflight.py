"""Read-only ArgoCD sync gate: credentials, image architecture, and disk capacity."""

import base64
import json
from pathlib import Path
import socket
import ssl
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

GIB = 1024**3
CLAIMS = {
    "mount0-seaweedfs-volume-0": 64 * GIB,
    "seaweedfs-filer-seaweedfs-filer-0": 2 * GIB,
    "seaweedfs-master-seaweedfs-master-0": GIB,
}
# Verified against the SeaweedFS 4.48 and operator 1.0.40 manifest lists.
SUPPORTED_ARCHITECTURES = {"amd64", "arm64"}


def condition_true(conditions, name):
    return any(c["type"] == name and c["status"] == "True" for c in conditions)


def validate_identities(config):
    identities = config.get("identities", [])
    if not identities:
        raise ValueError("S3 identities configuration must not be empty")
    for identity in identities:
        if not identity.get("name") or identity["name"].lower() == "anonymous":
            raise ValueError("S3 identities must be named and must not allow anonymous access")
        credentials = identity.get("credentials", [])
        if not credentials or not identity.get("actions"):
            raise ValueError("Each S3 identity needs credentials and actions")
        for credential in credentials:
            if not credential.get("accessKey") or not credential.get("secretKey"):
                raise ValueError("S3 access and secret keys must not be empty")
    if not any("Admin" in identity["actions"] and not identity.get("disabled") for identity in identities):
        raise ValueError("An administrative S3 identity is required for initial bucket provisioning")


def storage_budgets(kubernetes_nodes, longhorn_nodes):
    ready_nodes = {}
    for node in kubernetes_nodes["items"]:
        if node["spec"].get("unschedulable") or not condition_true(
            node["status"].get("conditions", []), "Ready"
        ):
            continue
        info = node["status"]["nodeInfo"]
        if info["operatingSystem"] != "linux" or info["architecture"] not in SUPPORTED_ARCHITECTURES:
            raise ValueError("Schedulable nodes must run Linux on amd64 or arm64")
        ready_nodes[node["metadata"]["name"]] = info["architecture"]
    if not ready_nodes:
        raise ValueError("No ready supported Kubernetes nodes")

    budgets = []
    for node in longhorn_nodes["items"]:
        spec, status = node["spec"], node["status"]
        if node["metadata"]["name"] not in ready_nodes or not spec.get("allowScheduling"):
            continue
        if spec.get("evictionRequested") or not condition_true(status.get("conditions", []), "Ready"):
            continue
        for disk_name, disk in spec.get("disks", {}).items():
            disk_status = status.get("diskStatus", {}).get(disk_name, {})
            conditions = disk_status.get("conditions", [])
            if not disk.get("allowScheduling") or disk.get("evictionRequested"):
                continue
            if disk.get("diskType", "filesystem") != "filesystem":
                continue
            if not all(condition_true(conditions, c) for c in ("Ready", "Schedulable")):
                continue
            maximum = disk_status.get("storageMaximum", 0)
            reserve = max((maximum + 4) // 5, disk.get("storageReserved", 0))
            # Count both actual free space and existing full-size reservations;
            # do not rely on sparse volumes or Longhorn overprovisioning.
            budget = min(
                disk_status.get("storageAvailable", 0) - reserve,
                maximum - reserve - disk_status.get("storageScheduled", 0),
            )
            budgets.append({
                "node": node["metadata"]["name"],
                "disk": disk_name,
                "bytes": max(0, budget),
            })
    return budgets


def allocate(budgets, requests):
    remaining = [dict(disk) for disk in budgets]
    placements = []
    for claim, size in sorted(requests.items(), key=lambda item: item[1], reverse=True):
        candidates = [disk for disk in remaining if disk["bytes"] >= size]
        if not candidates:
            largest = max((disk["bytes"] for disk in remaining), default=0)
            raise ValueError(
                f"Insufficient capacity for {claim}: needs {size / GIB:.1f} GiB on one disk; "
                f"largest remaining budget is {largest / GIB:.1f} GiB after 20% reserve"
            )
        disk = max(candidates, key=lambda item: item["bytes"])
        disk["bytes"] -= size
        placements.append((claim, disk["node"], disk["disk"]))
    return placements


class KubernetesAPI:
    def __init__(self):
        directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.token = (directory / "token").read_text().strip()
        self.namespace = (directory / "namespace").read_text().strip()
        self.context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        self.server = "https://kubernetes.default.svc"

    def get(self, path):
        request = Request(self.server + path, headers={"Authorization": "Bearer " + self.token})
        with urlopen(request, context=self.context, timeout=10) as response:
            return json.load(response)


def main():
    api = KubernetesAPI()
    namespace = api.namespace
    deadline = time.monotonic() + 180
    while True:
        try:
            secret = api.get(f"/api/v1/namespaces/{namespace}/secrets/seaweedfs-secrets")
            content = secret.get("data", {}).get("seaweedfs_s3_config.json")
            if not content:
                raise ValueError("Missing seaweedfs_s3_config.json in seaweedfs-secrets")
            validate_identities(json.loads(base64.b64decode(content, validate=True)))
            break
        except HTTPError as error:
            if error.code != 404 or time.monotonic() >= deadline:
                raise
            print("Waiting for Infisical to provision the S3 identities Secret", flush=True)
            time.sleep(5)

    storage_class = api.get("/apis/storage.k8s.io/v1/storageclasses/seaweedfs-longhorn")
    if (
        storage_class.get("provisioner") != "driver.longhorn.io"
        or storage_class.get("parameters", {}).get("numberOfReplicas") != "1"
        or storage_class.get("reclaimPolicy") != "Retain"
        or not storage_class.get("allowVolumeExpansion")
    ):
        raise ValueError("seaweedfs-longhorn must have one replica, Retain, and expansion enabled")

    nodes = api.get("/api/v1/nodes")
    longhorn_nodes = api.get("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes")
    budgets = storage_budgets(nodes, longhorn_nodes)
    existing = api.get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims")
    claims = {claim["metadata"]["name"]: claim for claim in existing["items"]}
    requests = {}
    for name, size in CLAIMS.items():
        if name in claims:
            if claims[name]["spec"].get("storageClassName") != "seaweedfs-longhorn":
                raise ValueError(f"Existing PVC {name} uses an unexpected StorageClass")
            if claims[name].get("status", {}).get("phase") != "Bound":
                raise ValueError(f"Existing PVC {name} is not Bound; inspect Longhorn before syncing")
        else:
            requests[name] = size
    for claim, node, disk in allocate(budgets, requests):
        print(f"Capacity available for {claim} on {node}/{disk}", flush=True)

    # Readiness /readyz is a manager ping; additionally verify the webhook TLS
    # listener before allowing the first custom resource through admission.
    certificate = api.get(f"/api/v1/namespaces/{namespace}/secrets/seaweedfs-operator-webhook-server-cert")
    context = ssl.create_default_context(
        cadata=base64.b64decode(certificate["data"]["tls.crt"]).decode()
    )
    host = "seaweedfs-operator-webhook." + namespace + ".svc"
    with socket.create_connection((host, 443), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname=host):
            pass
    print("Preflight passed: credentials, images, capacity, and webhook readiness", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never include API payloads or the identities JSON in log output.
        print(f"SeaweedFS preflight failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
