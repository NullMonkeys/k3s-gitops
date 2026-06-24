GitOps configuration for our internal k3s cluster, managed with ArgoCD.

> **Disclaimer:** This is an internal setup. ArgoCD is installed automatically by Ansible (see [k3s-iac](https://github.com/NullMonkeys/k3s-iac)), which also applies the root app after bootstrap. You probably shouldn't use it :)

## Prerequisites

- A running k3s cluster with ArgoCD installed (run `bootstrap/root-app.yaml` after)
- [Infisical](https://infisical.com) account with universal auth credentials
- Cloudflare API token (for cert-manager DNS-01) synced via Infisical
- DNS records pointing to your cluster for ingress hostnames (`argocd.internal.nullmonkeys.com`, `longhorn.internal.nullmonkeys.com`, etc.)

## Bootstrap

ArgoCD is installed automatically by Ansible (see [k3s-iac](https://github.com/NullMonkeys/k3s-iac)), which also applies `bootstrap/root-app.yaml` after the cluster is up. From there, ArgoCD reconciles everything in this repo.

All secrets are pulled from Infisical — nothing sensitive is committed to git.

## Adding new applications

1. Create a Kubernetes manifest in `applications/`
2. Add a corresponding ArgoCD `Application` resource in `bootstrap/applications/`
3. Push — ArgoCD reconciles automatically

Applications can use secrets from Infisical by creating `InfisicalStaticSecret` resources (see `infrastructure/cert-manager/secrets.yaml` for an example)

## License

Apache 2.0 — see [LICENSE](LICENSE)
