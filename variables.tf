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
  description = "Root volume size in GB (operating system, Python virtualenv, logs). The application state is on the separate data volume, see data_volume_size_gb."
  type        = number
  default     = 20
}

variable "data_volume_size_gb" {
  description = "Size in GB of the separate EBS volume mounted at data/ (SQLite DBs + machine key + dashboard settings). It survives instance replacement."
  type        = number
  default     = 10
}

variable "snapshot_retention_days" {
  description = "Days of daily snapshots of the data volume to keep. 0 disables the schedule."
  type        = number
  default     = 14
}

variable "repo_url" {
  description = "Git URL the instance clones the engine from. It must be readable without credentials."
  type        = string
  default     = "https://github.com/badalworld/AWESOME.git"
}

variable "repo_ref" {
  description = "Branch, tag or commit to check out. Pin a tag or commit for reproducible deployments; changing it replaces the instance (the data volume is kept)."
  type        = string
  default     = "main"
}

variable "app_port" {
  description = "Port the dashboard / API listens on (run.py --port)."
  type        = number
  default     = 8080
}

variable "subnet_id" {
  description = "Subnet to launch in. Null = a default subnet of the default VPC. Pin it for production: the data volume lives in this subnet's Availability Zone."
  type        = string
  default     = null
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
  description = "CIDR blocks allowed to reach the dashboard/API port. Empty = closed (use the ssm_port_forward_command output). Never use 0.0.0.0/0 without web.api_token (see api_token_ssm_parameter_name) and, ideally, a reverse proxy with TLS (docs/DEPLOYMENT.md)."
  type        = list(string)
  default     = []
}

variable "api_token_ssm_parameter_name" {
  description = "Optional name of an SSM Parameter Store parameter (SecureString) holding the dashboard API token. When set, the host reads it at every service start and applies it as web.api_token; the token never enters Terraform state."
  type        = string
  default     = null
}
