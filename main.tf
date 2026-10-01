# Infrastructure for the self-hosted trading engine described in docs/DEPLOYMENT.md:
# one small EC2 host running `python run.py` under systemd.
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

data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_security_group" "engine" {
  name_prefix = "${var.name}-"
  description = "Crypto Hunter engine"
  vpc_id      = data.aws_vpc.default.id

  dynamic "ingress" {
    for_each = length(var.app_cidrs) > 0 ? [1] : []
    content {
      description = "Dashboard / API"
      from_port   = var.app_port
      to_port     = var.app_port
      protocol    = "tcp"
      cidr_blocks = var.app_cidrs
    }
  }

  dynamic "ingress" {
    for_each = length(var.ssh_cidrs) > 0 ? [1] : []
    content {
      description = "SSH"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.ssh_cidrs
    }
  }

  egress {
    description = "Exchange APIs, package mirrors, git"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# SSM Session Manager access, so SSH can stay closed.
resource "aws_iam_role" "engine" {
  name_prefix = "${var.name}-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.engine.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "engine" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.engine.name
}

resource "aws_instance" "engine" {
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = var.instance_type
  key_name               = var.key_name
  iam_instance_profile   = aws_iam_instance_profile.engine.name
  vpc_security_group_ids = [aws_security_group.engine.id]

  root_block_device {
    volume_type = "gp3"
    volume_size = var.volume_size_gb
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required" # IMDSv2 only
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repo_url = var.repo_url
    repo_ref = var.repo_ref
    port     = var.app_port
  })
  user_data_replace_on_change = true

  tags = {
    Name = var.name
  }
}

resource "aws_eip" "engine" {
  instance = aws_instance.engine.id
  domain   = "vpc"
}
