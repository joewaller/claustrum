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

locals {
  name = "claustrum-${var.environment}"
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
}

resource "random_password" "db_password" {
  length  = 32
  special = true
  # Cloud SQL rejects some specials; restrict to safe set.
  override_special = "-_=+"
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

# Cloud Run + IAP pattern: grant allUsers `roles/run.invoker`. Security comes
# from two stacked layers above this IAM binding:
#   1. Cloud Run ingress = INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER restricts
#      network reachability to the load balancer only.
#   2. IAP on the LB backend enforces principal-level auth.
# So `allUsers` here just means "anyone the LB allows through" — which IAP gates.
# Per https://cloud.google.com/iap/docs/enabling-cloud-run
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  project  = google_cloud_run_v2_service.claustrum.project
  location = google_cloud_run_v2_service.claustrum.location
  name     = google_cloud_run_v2_service.claustrum.name
  role     = "roles/run.invoker"
  member   = "allUsers"
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
  port_name             = "https"
  timeout_sec           = 30
  load_balancing_scheme = "EXTERNAL_MANAGED"

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

resource "google_compute_managed_ssl_certificate" "claustrum" {
  provider = google-beta
  name     = "${local.name}-ssl-cert"

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
