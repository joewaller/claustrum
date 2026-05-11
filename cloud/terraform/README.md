# Claustrum Cloud — terraform

Stands up a single environment (staging or prod) of the cross-machine
coordination server on GCP behind IAP.

## What it builds

| Resource | Purpose |
|---|---|
| Cloud Run service | Stateless FastAPI server; scales to zero |
| Cloud SQL Postgres 16 | Private-IP via VPC peering; 7-day PITR backups |
| Serverless NEG | LB target for Cloud Run |
| External HTTPS LB + managed SSL | Custom domain with Google-managed cert |
| Identity-Aware Proxy | Enforces `@finder.com` (or your principals) at the LB |
| Secret Manager (×2) | DB password + composed DB URL for Cloud Run |
| Service account + IAM | Cloud SQL Client, Secret Accessor, log/metric writer |
| HTTP→HTTPS redirect | Belt & braces |

Cloud Run ingress is `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` — only the LB
can reach the service. Cloud Run's own `*.run.app` URL won't respond to
external callers.

## Per-environment apply

Two suggested terraform workspaces or two `-var-file` invocations. The
example below uses var files:

```bash
# Initial setup (once per environment)
terraform init

# Staging
terraform apply -var environment=staging -var-file=staging.tfvars

# Prod
terraform apply -var environment=prod -var-file=prod.tfvars
```

`staging.tfvars` and `prod.tfvars` are environment-specific — keep them
out of OSS git (or use sane defaults that operators override). The
example shape:

```hcl
project_id              = "your-gcp-project"
region                  = "australia-southeast1"
vpc_self_link           = "projects/your-project/global/networks/default"
container_image         = "gcr.io/your-project/claustrum-api:abc1234"
domain                  = "claustrum.example.com"
db_tier                 = "db-f1-micro"            # or db-custom-2-7680 for prod
db_ha                   = false                     # set true for prod after stabilising
max_instances           = 10
iap_oauth_client_id     = "<from GCP Console>"
iap_oauth_client_secret = "<from GCP Console>"
iap_members             = ["domain:example.com"]
```

## Pre-requisites the terraform does NOT create

1. **GCP project** must exist and you must be authenticated (`gcloud auth
   application-default login`).
2. **VPC** must exist with at least one subnet in the same region. Pass
   its self-link as `vpc_self_link`.
3. **IAP OAuth brand + client** — create manually in
   `APIs & Services → Credentials`. Set the authorized redirect URI to
   `https://iap.googleapis.com/v1/oauth/clientIds/<CLIENT_ID>:handleRedirect`.
   Pass the ID/secret as terraform variables.
4. **DNS A record** for `var.domain` pointing at the `lb_ip` output.
   Cloud-managed SSL provisioning takes 10–60 minutes after DNS resolves.
5. **Container image** must be built and pushed before `terraform apply`
   (or apply once with a placeholder, build, then re-apply).

## After applying

```bash
# 1. Apply migration to Cloud SQL via the Auth Proxy
cloud_sql_proxy -instances=$(terraform output -raw cloud_sql_connection_name)=tcp:5432 &
PGPASSWORD=$(gcloud secrets versions access latest \
  --secret=$(terraform output -raw db_password_secret)) \
  psql -h localhost -U claustrum -d claustrum \
  -f ../server/migrations/0001_init.sql

# 2. Update DNS (see lb_ip output) and wait for SSL cert to provision

# 3. Verify the service
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  https://<your-domain>/healthz
```

## Adapting for non-IAP auth

The server only reads one header — `X-Claustrum-User-Email` by default. If
your authenticated proxy emits a different header:

- Set `auth_header_name = "Cf-Access-Authenticated-User-Email"` (for
  Cloudflare Access) or similar.
- Remove the `iap { ... }` block from `google_compute_backend_service` and
  the IAP IAM bindings, and front the LB with whatever proxy you run.
