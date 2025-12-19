variable "project" {
  description = "ID do projeto GCP"
  type        = string
  default     = "ID_PROJECT"
}

variable "region" {
  description = "Região da GCP"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zona da GCP"
  type        = string
  default     = "us-central1-c"
}
