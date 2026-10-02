###############################################################################
# Identity
###############################################################################

variable "name" {
  description = "Name prefix for every resource and the value of the Name tag. Use a different name for each deployment in the same account and region."
  type        = string
  default     = "crypto-hunter"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$", var.name))
    error_message = "name must be 2-32 characters: lowercase letters, digits and hyphens, starting and ending with a letter or digit."
  }
}

variable "tags" {
  description = "Extra tags applied to every resource. They are merged with (and win over) any default_tags configured on the AWS provider."
  type        = map(string)
  default     = {}
}

###############################################################################
# Placement and compute
###############################################################################

variable "subnet_id" {
  description = "Subnet to launch in. Null picks a default subnet of the default VPC, in an Availability Zone that offers instance_type. Pin it for production: the data volume is created in this subnet's Availability Zone and cannot follow the instance to another one."
  type        = string
  default     = null

  validation {
    condition     = var.subnet_id == null || can(regex("^subnet-[0-9a-f]+$", var.subnet_id))
    error_message = "subnet_id must look like subnet-0123456789abcdef0."
  }
}

variable "create_eip" {
  description = "Allocate an Elastic IP and attach it to the instance. This gives the dashboard / Netlify ENGINE_URL / exchange API-key IP allow-list a stable address that survives instance replacement. Set false for an instance in a private subnet that you reach through SSM Session Manager."
  type        = bool
  default     = true
}

variable "instance_type" {
  description = "EC2 instance type. The default AMI is x86_64; for a Graviton (arm64) type also pass an arm64 ami_id."
  type        = string
  default     = "t3.micro"

  validation {
    condition     = can(regex("^[a-z0-9-]+\\.[a-z0-9]+$", var.instance_type))
    error_message = "instance_type must look like t3.micro."
  }
}

variable "ami_id" {
  description = "AMI to launch instead of the latest Canonical Ubuntu 24.04 LTS (amd64), which is resolved from Canonical's public SSM parameter. It must be Ubuntu 24.04 or newer (the bootstrap uses apt and needs Python 3.11+). Changing this value replaces the instance; newer releases of the default image do not."
  type        = string
  default     = null

  validation {
    condition     = var.ami_id == null || can(regex("^ami-[0-9a-f]+$", var.ami_id))
    error_message = "ami_id must look like ami-0123456789abcdef0."
  }
}

variable "key_name" {
  description = "Name of an existing EC2 key pair for SSH. Null (the default) means no SSH key at all: use SSM Session Manager."
  type        = string
  default     = null
}

###############################################################################
# Storage
###############################################################################

variable "root_volume_size_gb" {
  description = "Size in GB of the root volume (operating system, Python virtualenv, logs). Application state is not stored here."
  type        = number
  default     = 20

  validation {
    condition     = var.root_volume_size_gb == floor(var.root_volume_size_gb) && var.root_volume_size_gb >= 8 && var.root_volume_size_gb <= 16384
    error_message = "root_volume_size_gb must be a whole number between 8 and 16384."
  }
}

variable "data_volume_size_gb" {
  description = "Size in GB of the separate EBS volume mounted at <app>/data: the per-venue SQLite databases, the credential encryption key and the dashboard settings overrides. It outlives the instance."
  type        = number
  default     = 10

  validation {
    condition     = var.data_volume_size_gb == floor(var.data_volume_size_gb) && var.data_volume_size_gb >= 1 && var.data_volume_size_gb <= 16384
    error_message = "data_volume_size_gb must be a whole number between 1 and 16384."
  }
}

variable "data_volume_snapshot_id" {
  description = "Create the data volume from this snapshot, to restore a backup. Null creates a new, empty volume. Changing it replaces the data volume and therefore the instance."
  type        = string
  default     = null

  validation {
    condition     = var.data_volume_snapshot_id == null || can(regex("^snap-[0-9a-f]+$", var.data_volume_snapshot_id))
    error_message = "data_volume_snapshot_id must look like snap-0123456789abcdef0."
  }
}

variable "kms_key_arn" {
  description = "ARN of a customer-managed KMS key for the root and data volumes. Null uses the AWS-managed aws/ebs key. Both volumes are always encrypted."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:[^:]+:kms:[^:]+:[0-9]{12}:key/[0-9A-Za-z-]+$", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws:kms:<region>:<account>:key/<id>), not an alias."
  }
}

variable "snapshot_retention_days" {
  description = "Days of daily EBS snapshots of the data volume to keep (Amazon Data Lifecycle Manager). 0 disables the schedule. Independently of this, a final snapshot is always taken if Terraform deletes the volume."
  type        = number
  default     = 14

  validation {
    condition     = var.snapshot_retention_days == floor(var.snapshot_retention_days) && var.snapshot_retention_days >= 0 && var.snapshot_retention_days <= 1000
    error_message = "snapshot_retention_days must be a whole number between 0 and 1000."
  }
}

###############################################################################
# Application
###############################################################################

variable "repo_url" {
  description = "HTTPS Git URL the instance clones the application from. It must be readable without credentials, because user_data is stored in clear text by AWS provider v6."
  type        = string
  default     = "https://github.com/badalworld/AWESOME.git"

  validation {
    condition     = can(regex("^https://[A-Za-z0-9._~:/%+-]+$", var.repo_url))
    error_message = "repo_url must be a plain https:// URL made of letters, digits and . _ ~ : / % + - (no credentials, query string or spaces)."
  }
}

variable "repo_ref" {
  description = "Branch, tag or commit to check out. Pin a tag or commit SHA for reproducible deployments. Changing it replaces the instance (the data volume is kept and re-attached)."
  type        = string
  default     = "main"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._/-]*$", var.repo_ref))
    error_message = "repo_ref must be a branch, tag or commit made of letters, digits and . _ / - (and must not start with a dash)."
  }
}

variable "app_port" {
  description = "TCP port the dashboard and API listen on (run.py --port)."
  type        = number
  default     = 8080

  validation {
    condition     = var.app_port == floor(var.app_port) && var.app_port >= 1024 && var.app_port <= 65535
    error_message = "app_port must be a whole number between 1024 and 65535 (the service runs unprivileged)."
  }
}

variable "api_token_ssm_parameter_name" {
  description = "Name of an existing SSM Parameter Store parameter (type SecureString recommended) that holds the dashboard API token. When set, the instance may read only that parameter, and every start of the service copies its value into data/settings.json as web.api_token, so the token never appears in user_data or the Terraform state. Null leaves the dashboard unauthenticated."
  type        = string
  default     = null

  validation {
    condition     = var.api_token_ssm_parameter_name == null || can(regex("^/?[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$", var.api_token_ssm_parameter_name))
    error_message = "api_token_ssm_parameter_name must be an SSM parameter name such as /crypto-hunter/api-token."
  }
}

###############################################################################
# Network access (the security group has no other inbound rules)
###############################################################################

variable "app_cidrs" {
  description = "IPv4 or IPv6 CIDR blocks allowed to reach app_port. Empty keeps the port closed: use the ssm_port_forward_command output instead. Netlify has no fixed egress IPs, so proxying through it needs a wide range, in which case also set api_token_ssm_parameter_name."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.app_cidrs : can(cidrhost(cidr, 0))])
    error_message = "app_cidrs must contain valid CIDR blocks such as 203.0.113.7/32."
  }
}

variable "ssh_cidrs" {
  description = "IPv4 or IPv6 CIDR blocks allowed to reach SSH (port 22). Only useful together with key_name; SSM Session Manager needs no inbound rule at all."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.ssh_cidrs : can(cidrhost(cidr, 0))])
    error_message = "ssh_cidrs must contain valid CIDR blocks such as 203.0.113.7/32."
  }
}

###############################################################################
# IAM
###############################################################################

variable "additional_iam_policy_arns" {
  description = "Extra managed policies to attach to the instance role, for example one granting kms:Decrypt on the customer-managed key that protects the SSM token parameter."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.additional_iam_policy_arns : can(regex("^arn:[^:]+:iam::", arn))])
    error_message = "additional_iam_policy_arns must contain IAM policy ARNs (arn:aws:iam::...)."
  }
}

variable "iam_permissions_boundary_arn" {
  description = "ARN of a permissions boundary to put on the IAM roles this module creates, for accounts that require one."
  type        = string
  default     = null

  validation {
    condition     = var.iam_permissions_boundary_arn == null || can(regex("^arn:[^:]+:iam::", var.iam_permissions_boundary_arn))
    error_message = "iam_permissions_boundary_arn must be an IAM policy ARN (arn:aws:iam::...)."
  }
}
