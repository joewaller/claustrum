# Claustrum Cloud — GCP infrastructure
#
# Components per environment:
#   - Cloud Run service (private; only the LB reaches it)
#   - Cloud SQL Postgres 16 (private IP, no public ingress)
#   - HTTPS Load Balancer with managed SSL
#   - Identity-Aware Proxy (IAP) on the LB backend
#   - Service account, Secret Manager DB password
#
# Run per environment (staging, prod):
#   terraform apply -var environment=staging -var-file=staging.tfvars
#   terraform apply -var environment=prod    -var-file=prod.tfvars

terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
  }

  # GCS remote backend so state survives anything that wipes the local
  # checkout (e.g. companion deploy.sh runs `git reset --hard` between
  # deploys, which previously destroyed terraform.tfstate.d/). Initialize
  # with `-backend-config="bucket=<your-bucket>"` per environment, or
  # add a backend.tfvars equivalent. Workspaces (staging / prod) map to
  # separate state files under the prefix below.
  backend "gcs" {
    prefix = "claustrum"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# Resolves the project number so we can construct the IAP service-agent
# email statically (`service-<number>@gcp-sa-iap.iam.gserviceaccount.com`).
# Lets the IAP IAM binding reference a value known at plan time instead of
# the computed `google_project_service_identity.iap.email`, which can't be
# imported and therefore looks "unknown" to any state that pre-dates a
# fresh apply — causing the binding to recreate on every full apply.
data "google_project" "this" {
  project_id = var.project_id
}

locals {
  name = "claustrum-${var.environment}"

  # Housekeeping jobs (Phase 3) and their cadences. Cloud Scheduler hits each
  # /jobs/* endpoint through the IAP-protected LB on an OIDC token (see the
  # scheduler block at the bottom of this file). Cron in Etc/UTC.
  scheduler_jobs = {
    "validate-proposals" = {
      schedule = "0 * * * *" # hourly — promote topics at >=2 distinct proposers
      path     = "/jobs/validate-proposals"
    }
    "state-transitions" = {
      schedule = "*/5 * * * *" # every 5 min — active->paused, expire claims
      path     = "/jobs/state-transitions"
    }
    "topic-concentration" = {
      schedule = "30 * * * *" # hourly (offset) — alert >=3 active on one topic
      path     = "/jobs/topic-concentration"
    }
    "archive-cold" = {
      schedule = "20 3 * * *" # daily 03:20 UTC — move cold rows to sessions_archive
      path     = "/jobs/archive-cold"
    }
  }
}

# =============================================================================
# Required services (project-wide; safe to re-enable)
# =============================================================================

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "compute.googleapis.com",
    "iap.googleapis.com",
    "servicenetworking.googleapis.com",
    "cloudscheduler.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# =============================================================================
# Service account
# =============================================================================

resource "google_service_account" "claustrum" {
  account_id   = local.name
  display_name = "Claustrum Cloud (${var.environment})"
  description  = "Service account for ${local.name} Cloud Run service"
}

resource "google_project_iam_member" "claustrum_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.claustrum.email}"
}

resource "google_project_iam_member" "claustrum_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.claustrum.email}"
}

resource "google_project_iam_member" "claustrum_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.claustrum.email}"
}

resource "google_project_iam_member" "claustrum_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.claustrum.email}"
}

# =============================================================================
# Cloud SQL Postgres
# =============================================================================

# Reserved range + VPC peering for private services (Cloud SQL private IP).
# Safe to re-apply across environments — only one peering per VPC is needed.
resource "google_compute_global_address" "private_service_range" {
  name          = "claustrum-private-service-range-${var.environment}"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = var.vpc_self_link
}

resource "google_service_networking_connection" "private_vpc_connection" {
  network                 = var.vpc_self_link
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_range.name]

  depends_on = [google_project_service.required]

  # Only one servicenetworking peering exists per (VPC, service). If another
  # workload already owns it on a shared VPC, operators add claustrum's range
  # to the existing peering via `gcloud services vpc-peerings update` and
  # `terraform import` this resource. Ignoring drift on reserved_peering_ranges
  # prevents terraform from later removing the co-tenant's range.
  lifecycle {
    ignore_changes = [reserved_peering_ranges]
  }
}

resource "random_password" "db_password" {
  length  = 32
  special = true
  # Cloud SQL rejects some specials; restrict to safe set.
  override_special = "-_=+"

  # Avoid forced replacement after a state import where override_special
  # isn't reconstructible from the imported value. Replacement would
  # cascade through the secret + secret_version + (effectively) the
  # SQL user's password, which is a recipe for an outage in prod.
  lifecycle {
    ignore_changes = [override_special]
  }
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "${local.name}-db-password"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db_password.result
}

# Registrar secret for POST /v1/topics/register — only created when a value is
# provided (var.registrar_secret != ""), so the registrar stays disabled by
# default. The Cloud Run SA already holds project-level secretAccessor.
resource "google_secret_manager_secret" "registrar_secret" {
  count     = var.registrar_secret != "" ? 1 : 0
  secret_id = "${local.name}-registrar-secret"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "registrar_secret" {
  count       = var.registrar_secret != "" ? 1 : 0
  secret      = google_secret_manager_secret.registrar_secret[0].id
  secret_data = var.registrar_secret
}

resource "google_sql_database_instance" "claustrum" {
  name             = "${local.name}-pg"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    tier              = var.db_tier
    availability_type = var.db_ha ? "REGIONAL" : "ZONAL"
    disk_size         = var.db_disk_size_gb
    disk_type         = "PD_SSD"
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = var.vpc_self_link
    }

    database_flags {
      name  = "max_connections"
      value = "100"
    }

    insights_config {
      query_insights_enabled = true
    }
  }

  deletion_protection = var.environment == "prod"

  depends_on = [google_service_networking_connection.private_vpc_connection]
}

resource "google_sql_database" "claustrum" {
  name     = "claustrum"
  instance = google_sql_database_instance.claustrum.name
}

resource "google_sql_user" "claustrum" {
  name     = "claustrum"
  instance = google_sql_database_instance.claustrum.name
  password = random_password.db_password.result
}

# DB URL secret consumed by Cloud Run (composed from password + instance info).
resource "google_secret_manager_secret" "db_url" {
  secret_id = "${local.name}-db-url"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_url" {
  secret = google_secret_manager_secret.db_url.id
  # Cloud Run mounts the Cloud SQL Auth Proxy at /cloudsql/<connection_name>.
  secret_data = "postgresql://${google_sql_user.claustrum.name}:${random_password.db_password.result}@/${google_sql_database.claustrum.name}?host=/cloudsql/${google_sql_database_instance.claustrum.connection_name}"
}

# =============================================================================
# Cloud Run service
# =============================================================================

resource "google_cloud_run_v2_service" "claustrum" {
  name     = local.name
  location = var.region

  template {
    service_account = google_service_account.claustrum.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    # Direct VPC egress so the embedded Cloud SQL Auth Proxy can reach a
    # private-IP-only Cloud SQL instance over the VPC. Omitted (null) leaves
    # Cloud Run with the default egress path, which works only for Cloud SQL
    # with a public IP enabled.
    dynamic "vpc_access" {
      for_each = var.vpc_egress_subnetwork == null ? [] : [1]
      content {
        network_interfaces {
          network    = var.vpc_self_link
          subnetwork = var.vpc_egress_subnetwork
        }
        egress = "PRIVATE_RANGES_ONLY"
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.claustrum.connection_name]
      }
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name = "CLAUSTRUM_DB_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_url.secret_id
            version = "latest"
          }
        }
      }

      env {
        name  = "CLAUSTRUM_AUTH_HEADER"
        value = var.auth_header_name
      }

      env {
        name  = "CLAUSTRUM_VERSION"
        value = var.container_image
      }

      # Only injected when var.registrar_secret is set; otherwise the register
      # endpoint stays disabled (403) — the topics registrar is opt-in.
      dynamic "env" {
        for_each = var.registrar_secret != "" ? [1] : []
        content {
          name = "CLAUSTRUM_REGISTRAR_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.registrar_secret[0].secret_id
              version = "latest"
            }
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 2
        period_seconds        = 5
        failure_threshold     = 6
      }

      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 30
        failure_threshold = 3
      }
    }
  }

  # Only the LB can reach us — nobody hits the *.run.app URL directly.
  ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  traffic {
    percent = 100
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
  }

  depends_on = [google_secret_manager_secret_version.db_url]
}

# Cloud Run + IAP pattern (per https://cloud.google.com/iap/docs/enabling-cloud-run):
# IAP invokes Cloud Run on the authenticated user's behalf using its own service
# agent. So the IAP service agent — not allUsers — needs roles/run.invoker.
#
# 1. Provision the IAP service agent in this project (idempotent).
# 2. Grant it the invoker role on the Cloud Run service.
# 3. Combined with ingress=INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER, this means
#    the only path to Cloud Run is: LB → IAP (auth) → IAP service agent → Run.
#
# An earlier version of this file used `allUsers` as the invoker. That bypassed
# the IAP service agent's role and caused IAP to return:
#   "The IAP service account is not provisioned"
# because the service agent wasn't created in the project.
resource "google_project_service_identity" "iap" {
  provider = google-beta
  project  = google_cloud_run_v2_service.claustrum.project
  service  = "iap.googleapis.com"
}

# google_project_service_identity returns success before the underlying
# service account is queryable from IAM, so the immediate iam_member
# binding below fails with: `Service account service-PROJECT@gcp-sa-iap...
# does not exist`. Waiting 30s lets the SA propagate.
resource "time_sleep" "wait_for_iap_identity" {
  depends_on      = [google_project_service_identity.iap]
  create_duration = "30s"
}

resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  project  = google_cloud_run_v2_service.claustrum.project
  location = google_cloud_run_v2_service.claustrum.location
  name     = google_cloud_run_v2_service.claustrum.name
  role     = "roles/run.invoker"
  # Static reference to the IAP service agent — see data.google_project.this
  # above for the why. `google_project_service_identity.iap` still runs to
  # provision the agent on fresh projects; we just don't read its computed
  # `.email` because that re-evaluates as unknown on every full apply.
  member = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com"

  depends_on = [time_sleep.wait_for_iap_identity]
}

# =============================================================================
# Cloud Run Job — schema migrations
# =============================================================================
#
# Applies any pending `cloud/server/migrations/*.sql` against the same
# Cloud SQL instance the API uses. Reuses the API image (migrations are
# COPY'd in), service account, VPC egress, cloudsql volume, and DB URL
# secret. The migration runner (`app.migrate`) is idempotent — already-
# applied versions are skipped via _schema_migrations.
#
# Triggered manually by the deploy script after `terraform apply`:
#
#   gcloud run jobs execute <migrate_job_name> --region=<region> --wait
#
# Cloud Run Jobs work for private-IP Cloud SQL because they run inside the
# VPC (via the same Direct VPC egress the API uses), unlike a local
# cloud-sql-proxy which has no route to the private endpoint.
resource "google_cloud_run_v2_job" "claustrum_migrate" {
  name     = "${local.name}-migrate"
  location = var.region

  template {
    template {
      service_account = google_service_account.claustrum.email
      timeout         = "300s"
      max_retries     = 1

      dynamic "vpc_access" {
        for_each = var.vpc_egress_subnetwork == null ? [] : [1]
        content {
          network_interfaces {
            network    = var.vpc_self_link
            subnetwork = var.vpc_egress_subnetwork
          }
          egress = "PRIVATE_RANGES_ONLY"
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.claustrum.connection_name]
        }
      }

      containers {
        image   = var.container_image
        command = ["python", "-m", "app.migrate"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name = "CLAUSTRUM_DB_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.db_url.secret_id
              version = "latest"
            }
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.db_url,
    google_sql_database.claustrum,
  ]
}

# =============================================================================
# Serverless NEG + HTTPS LB + IAP
# =============================================================================

resource "google_compute_region_network_endpoint_group" "claustrum" {
  name                  = "${local.name}-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = google_cloud_run_v2_service.claustrum.name
  }
}

resource "google_compute_backend_service" "claustrum" {
  name                  = "${local.name}-backend"
  protocol              = "HTTPS"
  timeout_sec           = 30
  load_balancing_scheme = "EXTERNAL_MANAGED"
  # port_name intentionally omitted — Serverless NEGs (Cloud Run) reject it
  # with "Port name is not supported for a backend service with Serverless
  # network endpoint groups".

  backend {
    group = google_compute_region_network_endpoint_group.claustrum.id
  }

  iap {
    oauth2_client_id     = var.iap_oauth_client_id
    oauth2_client_secret = var.iap_oauth_client_secret
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

resource "google_compute_global_address" "claustrum" {
  name = "${local.name}-ip"
}

resource "google_compute_url_map" "claustrum" {
  name            = "${local.name}-url-map"
  default_service = google_compute_backend_service.claustrum.id
}

resource "random_id" "ssl_cert_suffix" {
  byte_length = 4
  keepers = {
    domain = var.domain
  }

  # `keepers` isn't reconstructible from an import, so after-import plans
  # see it as drift and want to replace the suffix — which would force
  # the managed SSL cert to be recreated (10-60 min HTTPS outage). Pin it.
  lifecycle {
    ignore_changes = [keepers]
  }
}

resource "google_compute_managed_ssl_certificate" "claustrum" {
  provider = google-beta
  # Unique suffix so `create_before_destroy` can land a new cert before the
  # old one is deleted — google_compute_managed_ssl_certificate doesn't
  # support name_prefix and collides on a fixed name (`Error 409: ... already
  # exists`). This is also what makes cert rotation work after a
  # FAILED_NOT_VISIBLE state — `terraform taint` produces a fresh cert with
  # a new suffix on the next apply.
  name = "${local.name}-ssl-cert-${random_id.ssl_cert_suffix.hex}"

  managed {
    domains = [var.domain]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_target_https_proxy" "claustrum" {
  name             = "${local.name}-https-proxy"
  url_map          = google_compute_url_map.claustrum.id
  ssl_certificates = [google_compute_managed_ssl_certificate.claustrum.id]
}

resource "google_compute_global_forwarding_rule" "claustrum_https" {
  name                  = "${local.name}-https-forwarding"
  target                = google_compute_target_https_proxy.claustrum.id
  port_range            = "443"
  ip_address            = google_compute_global_address.claustrum.address
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# HTTP → HTTPS redirect
resource "google_compute_url_map" "claustrum_redirect" {
  name = "${local.name}-http-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "claustrum_redirect" {
  name    = "${local.name}-http-proxy"
  url_map = google_compute_url_map.claustrum_redirect.id
}

resource "google_compute_global_forwarding_rule" "claustrum_redirect" {
  name                  = "${local.name}-http-forwarding"
  target                = google_compute_target_http_proxy.claustrum_redirect.id
  port_range            = "80"
  ip_address            = google_compute_global_address.claustrum.address
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# IAP access binding — only allowed principals can reach the LB.
resource "google_iap_web_backend_service_iam_member" "members" {
  for_each            = toset(var.iap_members)
  project             = var.project_id
  web_backend_service = google_compute_backend_service.claustrum.name
  role                = "roles/iap.httpsResourceAccessor"
  member              = each.value
}

# =============================================================================
# Cloud Scheduler — Phase 3 housekeeping jobs
# =============================================================================
#
# Auth model — we rely on Cloud Run ingress + IAP + IAM, NOT on app-side OIDC
# validation. The /jobs/* routes skip the current_user dependency (they take no
# caller-supplied identity, so there's nothing to spoof), but the whole service
# is ingress=INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER behind the IAP-protected LB.
# So the only path to /jobs/* is: LB -> IAP (authenticate) -> Cloud Run. An
# unauthenticated request never gets past IAP; /jobs/* are unreachable without a
# valid IAP token.
#
# Cloud Scheduler authenticates the same way a human does: it sends an OIDC
# token whose audience is the IAP OAuth client id, signed for a dedicated
# scheduler service account that is granted roles/iap.httpsResourceAccessor on
# the backend (below). IAP injects that SA's email in the auth header; the
# routes ignore it. Any IAP-authorised principal may also POST these by hand —
# intentional, that's how they're smoke-tested, and the jobs are idempotent.

resource "google_service_account" "scheduler" {
  account_id   = "${local.name}-scheduler"
  display_name = "Claustrum Scheduler (${var.environment})"
  description  = "Mints OIDC tokens for Cloud Scheduler -> /jobs/* through IAP."
}

# The scheduler SA may pass IAP (scoped to just this SA, alongside var.iap_members).
resource "google_iap_web_backend_service_iam_member" "scheduler" {
  project             = var.project_id
  web_backend_service = google_compute_backend_service.claustrum.name
  role                = "roles/iap.httpsResourceAccessor"
  member              = "serviceAccount:${google_service_account.scheduler.email}"
}

# Cloud Scheduler's own service agent must be able to mint OIDC tokens as the
# scheduler SA. The agent is provisioned when cloudscheduler.googleapis.com is
# enabled (see google_project_service.required); its email is static.
resource "google_service_account_iam_member" "scheduler_token_creator" {
  service_account_id = google_service_account.scheduler.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"

  depends_on = [google_project_service.required]
}

resource "google_cloud_scheduler_job" "jobs" {
  for_each = local.scheduler_jobs

  name      = "${local.name}-${each.key}"
  region    = var.region
  schedule  = each.value.schedule
  time_zone = "Etc/UTC"

  # The LB backend caps requests at timeout_sec = 30; give Scheduler a little
  # more headroom and let it retry once on a transient 5xx.
  attempt_deadline = "60s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.domain}${each.value.path}"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      # IAP requires the OIDC token's audience to be the IAP OAuth client id.
      audience = var.iap_oauth_client_id
    }
  }

  depends_on = [
    google_project_service.required,
    google_iap_web_backend_service_iam_member.scheduler,
    google_service_account_iam_member.scheduler_token_creator,
  ]
}
