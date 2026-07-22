GitOps configuration for an internal, highly available k3s cluster, managed with ArgoCD

> [!NOTE]
> **Disclaimer:** This is an internal setup. ArgoCD is installed automatically by Ansible (see [k3s-iac](https://github.com/NullMonkeys/k3s-iac)), which also applies the root app after bootstrap

## Architecture Highlights
*   **GitOps Workflow:** Fully automated reconciliation via ArgoCD using the "App of Apps" pattern
*   **High Availability:** Multi-node k3s cluster deployed on Oracle Cloud
*   **Tiered Disaster Recovery:** CloudNativePG (Postgres) configured with WAL streaming for Point-In-Time Recovery (PITR) to Cloudflare R2, selectively applied to critical services (Authentik IAM, AFFiNE) to optimize storage costs
*   **Centralized Identity:** Authentik deployed as core infrastructure for SSO (OIDC/OAuth2) across all applications
*   **Secrets & PKI Management:** 100% of sensitive data is synced securely via Infisical. SSL certificates are centrally managed via Infisical's ACME server using External Account Binding (EAB) with Cert-Manager
*   **Storage:** Distributed block storage via Longhorn

## Prerequisites

- A running k3s cluster with ArgoCD installed (run `bootstrap/root-app.yaml` after)
- [Infisical](https://infisical.com) account with universal auth credentials
- Cloudflare API token (for cert-manager DNS-01) synced via Infisical
- DNS records pointing to your cluster for ingress hostnames (`argocd.internal.nullmonkeys.com`, `longhorn.internal.nullmonkeys.com`, etc.)

## Bootstrap

ArgoCD is installed automatically by Ansible (see [k3s-iac](https://github.com/NullMonkeys/k3s-iac)), which also applies `bootstrap/root-app.yaml` after the cluster is up. From there, ArgoCD reconciles everything in this repo

All secrets and EAB credentials for certificate management are pulled from Infisical - nothing sensitive is committed to git

## Adding new applications

1. Create a Kubernetes manifest in `applications/`
2. Add a corresponding ArgoCD `Application` resource in `bootstrap/applications/`
3. Push - ArgoCD reconciles automatically

Applications can use secrets from Infisical by creating `InfisicalStaticSecret` resources (see `infrastructure/cert-manager/secrets.yaml` for an example)

## License

MIT - see [LICENSE](LICENSE)
