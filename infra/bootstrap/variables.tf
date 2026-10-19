variable "project_id" {
  description = "An existing GCP project with billing enabled."
  type        = string
}

variable "region" {
  description = "Region for the state bucket. us-central1, us-east1 and us-west1 are in the free tier."
  type        = string
  default     = "us-central1"
}

variable "billing_account" {
  description = "Billing account ID the project is linked to (gcloud billing projects describe <project>)."
  type        = string
}

variable "budget_amount" {
  description = "Monthly budget in the billing account's currency. Alerts at 10%, 50%, 100% and forecast 100%."
  type        = number
}

variable "github_repository" {
  description = "owner/name of the GitHub repository whose Actions may deploy."
  type        = string
}
