variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "australia-southeast1"
}

variable "environment" {
  description = "Environment name — 'staging' or 'prod'. Used to name every resource."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be 'staging' or 'prod'."
  }
}

variable "domain" {
  description = "Fully-qualified domain for this environment, e.g. claustrum.finder.com or claustrum-staging.finder.com."
  type        = string
}

variable "vpc_self_link" {
  description = "Self-link of the VPC the Cloud SQL instance attaches to. Match what the gateway uses."
  type        = string
}

variable "container_image" {
  description = "Full container image path (e.g. gcr.io/PROJECT/claustrum-api:SHA). Updated on each deploy."
  type        = string
}

variable "auth_header_name" {
  description = "HTTP header the server reads to derive the authenticated user email. IAP rewrites X-Goog-Authenticated-User-Email upstream."
  type        = string
  default     = "X-Claustrum-User-Email"
}

variable "db_tier" {
  description = "Cloud SQL machine tier. Recommend db-f1-micro for staging, db-custom-2-7680 for prod."
  type        = string
}

variable "db_disk_size_gb" {
  description = "Cloud SQL disk size in GB. Autoresize is enabled."
  type        = number
  default     = 20
}

variable "db_ha" {
  description = "Cloud SQL high availability (REGIONAL vs ZONAL). false saves cost; PITR + backups cover data loss."
  type        = bool
  default     = false
}

variable "max_instances" {
  description = "Cloud Run max instances. Scale-to-zero is always on (min=0)."
  type        = number
  default     = 10
}

variable "iap_oauth_client_id" {
  description = "OAuth client ID for IAP. Reuse the one the gateway uses or create a new one per backend."
  type        = string
  sensitive   = true
}

variable "iap_oauth_client_secret" {
  description = "OAuth client secret for IAP."
  type        = string
  sensitive   = true
}

variable "iap_members" {
  description = "Principals allowed through IAP. e.g. ['domain:finder.com'] or ['group:engineers@finder.com']."
  type        = list(string)
  default     = ["domain:finder.com"]
}

variable "vpc_egress_subnetwork" {
  description = <<-EOT
    Self-link or short name of the subnetwork to use for Cloud Run Direct VPC egress.
    REQUIRED when Cloud SQL is private-IP only (ipv4_enabled = false) — without VPC
    egress, the embedded Cloud SQL Auth Proxy can't reach the private endpoint and
    startup probes fail. Subnetwork must be in the same region as Cloud Run.
    Leave null to skip VPC egress (only safe when Cloud SQL has a public IP).
  EOT
  type    = string
  default = null
}
