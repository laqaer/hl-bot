variable "region" {
  description = "AWS region. Tokyo (ap-northeast-1) is low-latency to Hyperliquid."
  type        = string
  default     = "ap-northeast-1"
}

variable "instance_type" {
  description = "EC2 type. t4g.small (2 vCPU ARM / 2GB) comfortably runs the bot + WS + loop."
  type        = string
  default     = "t4g.small"
}

variable "ami_id" {
  description = "Override AMI. Empty = latest Ubuntu 24.04 arm64 (Canonical)."
  type        = string
  default     = ""
}

variable "key_name" {
  description = "Existing EC2 key pair name for SSH fallback access. Optional if using Session Manager."
  type        = string
  default     = ""
}

variable "ssh_cidr" {
  description = "CIDR allowed to SSH. Set to your IP/32 for SSH fallback; leave empty to disable SSH ingress (Session Manager is recommended)."
  type        = string
  default     = ""
}

variable "root_gb" {
  description = "Root EBS size (GB). The SQLite DB / track record live here."
  type        = number
  default     = 20
}

variable "repo_url" {
  description = "Git URL of the hl-bot repo to deploy."
  type        = string
}

variable "branch" {
  description = "Branch to deploy."
  type        = string
  default     = "main"
}

variable "hl_address" {
  description = "Your funded Hyperliquid account (read + trade)."
  type        = string
}

variable "hl_trader_address" {
  description = "Account the bot trades on. Empty = same as hl_address."
  type        = string
  default     = ""
}

variable "backup_bucket" {
  description = "S3 bucket for Litestream DB backups. Empty = no backups/IAM S3."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = { Project = "hl-bot" }
}
