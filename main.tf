# Quick-start deployment of the self-hosted trading engine described in docs/DEPLOYMENT.md:
# one small EC2 host running `python run.py` under systemd, with its state (SQLite databases,
# credential key, dashboard settings) on a separate, snapshotted EBS volume.
#
# All of the infrastructure lives in ./modules/crypto-hunter-ec2, which is a reusable module for
# AWS provider v6 (see its README). This file only configures the provider and calls it, so
# `terraform apply` from the repository root keeps working:
#
#   terraform init
#   terraform apply -var 'app_cidrs=["203.0.113.7/32"]'

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "terraform"
    }
  }
}

module "engine" {
  source = "./modules/crypto-hunter-ec2"

  name                         = var.name
  instance_type                = var.instance_type
  root_volume_size_gb          = var.volume_size_gb
  repo_url                     = var.repo_url
  repo_ref                     = var.repo_ref
  app_port                     = var.app_port
  key_name                     = var.key_name
  ssh_cidrs                    = var.ssh_cidrs
  app_cidrs                    = var.app_cidrs
  subnet_id                    = var.subnet_id
  data_volume_size_gb          = var.data_volume_size_gb
  snapshot_retention_days      = var.snapshot_retention_days
  api_token_ssm_parameter_name = var.api_token_ssm_parameter_name
}

# Upgrading a deployment that was created before this module existed: keep its identity
# (above all the Elastic IP, which may already be allow-listed on an exchange or set as
# ENGINE_URL on Netlify) instead of destroying and recreating it. Delete these blocks after
# the first apply. They do nothing for new deployments.
#
# The old security group is deliberately not moved: it used inline rules, which cannot be
# combined with the standalone rule resources the module uses, so it is replaced.
#
# The instance is replaced either way, because its bootstrap script changed. The old
# data/ directory lived on its root disk and is lost with it: back it up first (see
# "Upgrading from the pre-module root configuration" in modules/crypto-hunter-ec2/README.md).
moved {
  from = aws_iam_role.engine
  to   = module.engine.aws_iam_role.this
}

moved {
  from = aws_iam_role_policy_attachment.ssm
  to   = module.engine.aws_iam_role_policy_attachment.ssm
}

moved {
  from = aws_iam_instance_profile.engine
  to   = module.engine.aws_iam_instance_profile.this
}

moved {
  from = aws_instance.engine
  to   = module.engine.aws_instance.this
}

moved {
  from = aws_eip.engine
  to   = module.engine.aws_eip.this[0]
}
