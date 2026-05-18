output "lb_ip" {
  description = "Static IP for the HTTPS load balancer. Create an A record for var.domain pointing here."
  value       = google_compute_global_address.claustrum.address
}

output "cloud_run_service_name" {
  description = "Cloud Run service name (also used by the deploy script)."
  value       = google_cloud_run_v2_service.claustrum.name
}

output "cloud_run_uri" {
  description = "Cloud Run *.run.app URL (locked down to LB ingress; nothing should hit this directly)."
  value       = google_cloud_run_v2_service.claustrum.uri
}

output "cloud_sql_connection_name" {
  description = "Cloud SQL connection name (PROJECT:REGION:INSTANCE)."
  value       = google_sql_database_instance.claustrum.connection_name
}

output "migrate_job_name" {
  description = "Cloud Run Job that applies pending SQL migrations. Trigger with `gcloud run jobs execute --wait`."
  value       = google_cloud_run_v2_job.claustrum_migrate.name
}

output "db_password_secret" {
  description = "Secret Manager secret ID holding the Cloud SQL password."
  value       = google_secret_manager_secret.db_password.secret_id
}

output "db_url_secret" {
  description = "Secret Manager secret ID holding the Cloud Run-mountable DB URL."
  value       = google_secret_manager_secret.db_url.secret_id
}

output "service_account_email" {
  description = "Cloud Run service account email."
  value       = google_service_account.claustrum.email
}

output "ssl_cert_id" {
  description = "Managed SSL certificate ID. Provisioning takes 10-60 minutes after DNS resolves to lb_ip."
  value       = google_compute_managed_ssl_certificate.claustrum.id
}
