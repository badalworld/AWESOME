###############################################################################
# Crypto Hunter on a single EC2 host.
#
# The engine is a single process that must never run twice against the same
# account, and it keeps its state (SQLite databases, the credential key) on
# local disk. So this module deliberately builds one instance, not an Auto
# Scaling group or a container service: see README.md for the reasoning.
###############################################################################

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

###############################################################################
# Where to launch
###############################################################################

# Default-VPC lookup, used only when subnet_id is not given.
data "aws_vpc" "default" {
  count   = var.subnet_id == null ? 1 : 0
  default = true
}

# The Availability Zones that actually offer the instance type (not every zone
# of a region does, e.g. us-east-1e for the t3 family).
data "aws_ec2_instance_type_offerings" "zones" {
  count         = var.subnet_id == null ? 1 : 0
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

data "aws_subnets" "default" {
  count = var.subnet_id == null ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }

  filter {
    name   = "availability-zone"
    values = coalescelist(data.aws_ec2_instance_type_offerings.zones[0].locations, ["unavailable"])
  }

  lifecycle {
    postcondition {
      condition     = length(self.ids) > 0
      error_message = "No default subnet in an Availability Zone that offers ${var.instance_type}. Set subnet_id explicitly."
    }
  }
}

locals {
  subnet_id = var.subnet_id != null ? var.subnet_id : sort(data.aws_subnets.default[0].ids)[0]
}

data "aws_subnet" "selected" {
  id = local.subnet_id
}

###############################################################################
# Which image
###############################################################################

# Canonical publishes the current Ubuntu 24.04 LTS AMI id in a public SSM
# parameter. insecure_value is used on purpose: the AMI id is not a secret, and
# `value` would be marked sensitive and hidden from every plan.
data "aws_ssm_parameter" "ubuntu_ami" {
  count = var.ami_id == null ? 1 : 0
  name  = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

locals {
  ami_id = var.ami_id != null ? var.ami_id : data.aws_ssm_parameter.ubuntu_ami[0].insecure_value
}

# `ami` is ignored on the instance (see lifecycle below) so that a new Ubuntu
# release never replaces a trading host unannounced. This resource turns an
# *explicit* change of var.ami_id back into a replacement.
resource "terraform_data" "ami_pin" {
  input = var.ami_id
}

###############################################################################
# First-boot script
###############################################################################

locals {
  api_token_enabled = var.api_token_ssm_parameter_name != null

  apt_packages = join(" ", concat(
    ["curl", "git", "python3-pip", "python3-venv"],
    local.api_token_enabled ? ["python3-boto3"] : [],
  ))

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repo_url        = var.repo_url
    repo_ref        = var.repo_ref
    port            = var.app_port
    data_volume_id  = aws_ebs_volume.data.id
    apt_packages    = local.apt_packages
    token_parameter = local.api_token_enabled ? var.api_token_ssm_parameter_name : ""
    aws_region      = data.aws_region.current.region
    sync_token_py   = chomp(file("${path.module}/files/sync_api_token.py"))
  })
}

###############################################################################
# The instance
###############################################################################

resource "aws_instance" "this" {
  #checkov:skip=CKV_AWS_126:Detailed (1-minute) monitoring costs extra and one small host does not need it.
  #checkov:skip=CKV_AWS_135:EBS optimization is always on for Nitro types and an error on older ones, so it is left to the instance type.
  #checkov:skip=CKV_AWS_88:The host must reach exchange APIs, GitHub and SSM, which in a default VPC needs a public address. There is no inbound rule unless app_cidrs or ssh_cidrs is set; use create_eip = false in a private subnet with NAT to avoid it.

  ami                    = local.ami_id
  instance_type          = var.instance_type
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.this.id]
  iam_instance_profile   = aws_iam_instance_profile.this.name
  key_name               = var.key_name

  # With an Elastic IP the host needs a public address from the first second,
  # or the bootstrap (apt, git, pip) would race the EIP association in subnets
  # that do not auto-assign one. Without one, follow the subnet's own default.
  associate_public_ip_address = var.create_eip ? true : null

  # The bootstrap runs once per instance, so any change to it (new repo_ref, new
  # module version) must produce a fresh instance. That is safe because all state
  # lives on the separate data volume.
  user_data                   = local.user_data
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    encrypted             = true
    kms_key_id            = var.kms_key_arn
    delete_on_termination = true
    tags                  = merge(var.tags, { Name = "${var.name}-root" })
  }

  tags = merge(var.tags, { Name = var.name })

  lifecycle {
    ignore_changes       = [ami]
    replace_triggered_by = [terraform_data.ami_pin]

    precondition {
      condition     = length(local.user_data) <= 16384
      error_message = "The rendered user_data is larger than the 16 KB EC2 limit."
    }
  }
}

resource "aws_eip" "this" {
  count = var.create_eip ? 1 : 0

  domain   = "vpc"
  instance = aws_instance.this.id

  tags = merge(var.tags, { Name = var.name })
}

###############################################################################
# Misconfiguration warnings (shown by plan/apply, they never block)
###############################################################################

check "dashboard_exposure" {
  assert {
    condition = (
      var.api_token_ssm_parameter_name != null ||
      !anytrue([for cidr in var.app_cidrs : endswith(cidr, "/0")])
    )
    error_message = "app_cidrs opens the dashboard to the whole internet but api_token_ssm_parameter_name is not set, so anyone who finds the address can use it. Set a token, or narrow app_cidrs."
  }
}

check "ssh_exposure" {
  assert {
    condition     = !anytrue([for cidr in var.ssh_cidrs : endswith(cidr, "/0")])
    error_message = "ssh_cidrs opens SSH to the whole internet. Prefer SSM Session Manager (no inbound rule needed) or a narrow range."
  }
}
