# SeaweedFS

Internal S3 storage managed entirely through GitHub and ArgoCD. There is one
master, volume server, filer, S3 gateway, and operator. Each PVC has **one**
Longhorn replica; there are no backups or redundant data copies.

| PVC | Size | Purpose |
| --- | --- | --- |
| `mount0-seaweedfs-volume-0` | 64 GiB | Objects; initial usable target 50 GiB |
| `seaweedfs-filer-seaweedfs-filer-0` | 2 GiB | LevelDB bucket/path/object metadata |
| `seaweedfs-master-seaweedfs-master-0` | 1 GiB | Master Raft state |

The 67 GiB total is provisioned capacity, not immediate disk consumption. The
50 GiB target is not a hard bucket quota. Logical volume files are capped at
1 GiB each, with 60 allocation slots and no preallocation. Buckets can consume
multiple partly filled volumes, so watch slot usage as well as bytes. Deleting
objects does not immediately shrink append-only volume files; SeaweedFS vacuum
and compaction reclaim deleted space.

## First rollout

1. In Infisical project `acbf89d5-1845-4cbe-9e2d-e623806693e8`, environment
   `prod`, path `/k3s/seaweedfs`, create a key named
   `seaweedfs_s3_config.json` whose value is the JSON identities configuration:

   ```json
   {
     "identities": [
       {
         "name": "seaweedfs-admin",
         "credentials": [
           {"accessKey": "GENERATE_AN_ACCESS_KEY", "secretKey": "GENERATE_A_SECRET_KEY"}
         ],
         "actions": ["Admin", "Read", "Write", "List", "Tagging"]
       }
     ]
   }
   ```

   Generate real random credentials in your secret manager; never commit them.
   Do not add an anonymous identity. The sync gate rejects an empty configuration,
   identities without credentials, and configurations without an administrator.
2. Merge the reviewed GitHub changes. The root application discovers the
   SeaweedFS Application in `bootstrap/infrastructure`; do not apply it manually.
3. Observe ArgoCD. Wave -2 provisions storage, secret synchronization, monitoring,
   policies, and preflight RBAC/configuration. Wave 0 installs CRDs, the webhook
   certificate, and the operator. Operator readiness gates subsequent waves.
   Wave 1 is a read-only preflight Job; wave 2 creates the Seaweed cluster.
4. The preflight verifies supported Linux architectures (amd64/arm64), the S3
   Secret, one-replica storage settings, webhook TLS readiness, and room for all
   new PVCs. It reserves at least 20% of every candidate disk and counts existing
   full-size Longhorn reservations, without overprovisioning. Each new PVC must
   fit on a single disk. This is a capacity feasibility check; Longhorn chooses
   the actual placement. Verify disk headroom and actual placement after sync.
5. Missing credentials cause the hook to wait up to three minutes; other failures
   stop sync with an explanation. Inspect `kubectl -n seaweedfs logs
   job/seaweedfs-preflight`, correct the configuration through GitHub/Infisical,
   and retry the ArgoCD sync. Failed hooks are retained for diagnosis.

Retained, Bound PVCs are not counted again on subsequent syncs. If an existing
claim is Pending, investigate Longhorn before retrying. ArgoCD selective syncs
skip hooks: use a full application sync for rollout and capacity checks.

## Clients and validation

In-cluster endpoint: `http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333`.
Configure clients for path-style buckets and region `us-east-1`. S3 credentials
protect the gateway. NetworkPolicies allow S3 from cluster namespaces and block
direct filer/master/volume access except from this cluster's components and
operator; the monitoring namespace can reach component metric ports.

For local administration, port-forward the S3 Service:

```sh
kubectl -n seaweedfs port-forward svc/seaweedfs-s3 8333:8333
```

Configure a local AWS CLI profile named `seaweedfs-admin` with the Infisical
credentials, using `aws configure --profile seaweedfs-admin`. Do not print or
commit the profile. In another terminal:

```sh
aws --profile seaweedfs-admin configure set s3.addressing_style path
aws --profile seaweedfs-admin --endpoint-url http://localhost:8333 s3api create-bucket --bucket seaweedfs-smoke
aws --profile seaweedfs-admin --endpoint-url http://localhost:8333 s3 cp /tmp/seaweedfs-smoke.bin s3://seaweedfs-smoke/payload.bin
aws --profile seaweedfs-admin --endpoint-url http://localhost:8333 s3 ls s3://seaweedfs-smoke/
aws --profile seaweedfs-admin --endpoint-url http://localhost:8333 s3 cp s3://seaweedfs-smoke/payload.bin /tmp/seaweedfs-smoke-downloaded.bin
cmp /tmp/seaweedfs-smoke.bin /tmp/seaweedfs-smoke-downloaded.bin
aws --no-sign-request --endpoint-url http://localhost:8333 s3api get-object --bucket seaweedfs-smoke --key payload.bin /tmp/seaweedfs-anonymous.bin
```

Use a generated test file of at least 20 MiB so `aws s3 cp` exercises multipart
upload. Anonymous GET must fail with AccessDenied; also verify anonymous bucket
listing fails. Keep the test object while testing restart persistence below,
then remove only this disposable bucket with authenticated `s3 rb ... --force`.
Test aborted multipart uploads with `create-multipart-upload`, `upload-part`,
and `abort-multipart-upload`, confirming no unfinished upload remains.

When onboarding an application, provision its bucket with the administrator,
then add a separate identity with actions such as `Read:app-bucket`,
`Write:app-bucket`, `List:app-bucket`, and `Tagging:app-bucket`. Do not share the
administrative identity with workloads. Bucket-wide Read/Write/List tests and
denial against another bucket should accompany application onboarding.

## Grafana and alerts

The operator chart's legacy dashboard is disabled. `dashboard.json` provisions
the **SeaweedFS** dashboard via a ConfigMap labeled `grafana_dashboard: "1"`;
the existing Grafana sidecar discovers it across namespaces. Select the existing
Prometheus datasource. Changes to the dashboard belong in GitHub.

The chart's ServiceMonitor scrapes only the operator. The Seaweed resource's
`metricsPort` settings enable separate monitors for master (9324), volume
(9325), filer (9326), and S3 (9327). Prometheus already selects all monitors.
Rules carry `release: kube-prometheus-stack` to match the live rule selector.
No Pushgateway or additional monitoring stack is deployed.

Verify all five targets are UP and that the dashboard shows all three PVCs,
Longhorn health, slot allocation, CPU/memory, and S3 activity after the smoke
test. Idle request counters/histograms may have no series until exercised.
Alerts cover unavailable/missing targets, unhealthy Longhorn volumes, and
80%/90% filesystem and slot utilization. They appear in Prometheus/Alertmanager;
this change does not add external notification routing.

## Maintenance through GitOps

- **Credential rotation:** update the JSON in Infisical, wait for the Secret
  refresh (up to one hour), then commit a changed timestamp annotation under
  `spec.s3.annotations` in `cluster.yaml`. SeaweedFS reads the config on startup;
  the annotation change restarts the gateway through the operator. Coordinate
  client updates and test authentication before removing old credentials.
- **Restart/persistence checks:** change a timestamp annotation under
  `spec.master.annotations`, then filer, then volume, in separate GitHub changes.
  Wait for each component to become Ready and verify the retained test object's
  download/checksum and bucket listing after each restart. Expect downtime.
- **PVC expansion:** first check physical capacity with the same 20% headroom
  calculation. Expansion needs a reviewed GitOps maintenance change that grows
  the named PVC; changing the Seaweed resource alone does not reliably resize
  an existing claim. Update its configured size, `preflight.py` claim budget,
  and logical-volume limit together. If immutable StatefulSet claim templates
  need replacement, use a temporary, scoped ArgoCD maintenance Job to recreate
  that StatefulSet with orphaned pods/PVCs. Never delete the PVC or enable PV
  reclamation. Validate filesystem size with kubelet volume metrics after sync.
- **Node recovery:** inspect nodes, Longhorn volume health, and component events.
  With one replica, restoring the original storage node/disk may be necessary;
  reattaching a sole replica is not failover to a second copy. Permanent disk
  loss is unrecoverable without a backup. Filer metadata is essential: intact
  object chunks alone do not restore the S3 namespace.
- **Adding redundancy later:** increase Longhorn replicas through a reviewed
  GitOps maintenance Job for existing volumes as well as changing the dedicated
  StorageClass for new PVCs. A StorageClass edit alone does not update existing
  volumes. Increase masters to three with per-node anti-affinity when capacity
  permits. Multiple filers require a deliberate shared/replicated metadata-store
  design; do not simply scale independent LevelDB instances. Keep SeaweedFS
  replication `000` unless intentionally accepting an additional storage
  multiplier. Add off-cluster backups as a separate change.

The StorageClass and Seaweed resource require ArgoCD prune/delete confirmation,
the operator retains PVCs, and PVs have `Retain` policy. This guards accidental
removal; it does not replace a backup.

## Local checks

Render in a temporary copy to keep Helm downloads out of the repository:

```sh
task_render_dir=$(mktemp -d)
cp -R infrastructure/seaweedfs/. "$task_render_dir/"
kustomize build --enable-helm "$task_render_dir" > /tmp/seaweedfs-rendered.yaml
python3 -m unittest discover -s infrastructure/seaweedfs/tests -p 'test_*.py'
```

For alert evaluation, extract the PrometheusRule `spec` into a temporary
`rules.yaml` with PyYAML, copy `tests/alerts.test.yaml` alongside it, and run
`promtool check rules rules.yaml` followed by `promtool test rules
alerts.test.yaml`. Validate the rendered `Seaweed` resource against the v1
schema in the rendered operator CRD. Validate Kubernetes built-in kinds with
kubeconform; custom kinds need their corresponding CRD schemas.
