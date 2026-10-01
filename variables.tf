variable "region" {
  description = "AWS region. Pick the one closest to your exchange's matching engine (see docs/DEPLOYMENT.md)."
  type        = string
  default     = "ap-southeast-1" # Singapore
}

variable "name" {
  description = "Name prefix for all resources."
  type        = string
  default     = "crypto-hunter"
}

variable "instance_type" {
  description = "EC2 instance type. 1 vCPU / 1 GB RAM is enough for all three venues."
  type        = string
  default     = "t3.micro"
}

variable "volume_size_gb" {
  description = "Root volume size in GB. The data/ directory (SQLite DBs + machine key) lives here."
  type        = number
  default     = 20
}

variable "repo_url" {
  description = "Git URL the instance clones the engine from."
  type        = string
  default     = "https://github.com/badalworld/AWESOME.git"
}

variable "repo_ref" {
  description = "Branch, tag or commit to check out."
  type        = string
  default     = "main"
}

variable "app_port" {
  description = "Port the dashboard / API listens on (run.py --port)."
  type        = number
  default     = 8080
}

variable "key_name" {
  description = "Optional name of an existing EC2 key pair for SSH access. Leave null to use SSM Session Manager only."
  type        = string
  default     = null
}

variable "ssh_cidrs" {
  description = "CIDR blocks allowed to SSH (port 22). Empty = SSH closed."
  type        = list(string)
  default     = []
}

variable "app_cidrs" {
  description = "CIDR blocks allowed to reach the dashboard/API port. Empty = closed. Never use 0.0.0.0/0 without a reverse proxy, TLS and web.api_token (docs/DEPLOYMENT.md)."
  type        = list(string)
  default     = []
}
