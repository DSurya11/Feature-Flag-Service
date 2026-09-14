terraform {
  required_providers {
    neon = {
      source  = "kislerdm/neon"
      version = "0.18.0"
    }
  }
}

provider "neon" {
  api_key = var.neon_api_key
}

resource "neon_project" "this" {
  name                      = "Feature flag service-tf-managed"
  history_retention_seconds = 21600
}
