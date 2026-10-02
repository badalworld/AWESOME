###############################################################################
# Network access
#
# Rules are standalone resources (never inline ingress/egress blocks), so that
# callers can attach their own rules to the exported security_group_id without
# fighting this module over the group's rule set.
###############################################################################

resource "aws_security_group" "this" {
  name_prefix = "${var.name}-"
  description = "Crypto Hunter engine"
  vpc_id      = data.aws_subnet.selected.vpc_id

  tags = merge(var.tags, { Name = var.name })

  # A group cannot be deleted while an instance still uses it, so build the
  # replacement first.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "app" {
  for_each = toset(var.app_cidrs)

  security_group_id = aws_security_group.this.id
  description       = "Dashboard and API"
  ip_protocol       = "tcp"
  from_port         = var.app_port
  to_port           = var.app_port
  cidr_ipv4         = strcontains(each.value, ":") ? null : each.value
  cidr_ipv6         = strcontains(each.value, ":") ? each.value : null

  tags = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each = toset(var.ssh_cidrs)

  security_group_id = aws_security_group.this.id
  description       = "SSH"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = strcontains(each.value, ":") ? null : each.value
  cidr_ipv6         = strcontains(each.value, ":") ? each.value : null

  tags = var.tags
}

# Creating a group with Terraform removes AWS's default allow-all egress rule,
# so state it explicitly: the host must reach the exchange APIs, GitHub, the
# package mirrors and the SSM endpoints.
resource "aws_vpc_security_group_egress_rule" "all_ipv4" {
  security_group_id = aws_security_group.this.id
  description       = "Exchange APIs, git, package mirrors, SSM"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"

  tags = var.tags
}
