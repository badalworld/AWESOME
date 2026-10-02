###############################################################################
# Instance role
#
# No access keys ever live on the host. The role grants Session Manager (shell
# and port forwarding without any inbound port) and, optionally, read access to
# exactly one SSM parameter.
###############################################################################

resource "aws_iam_role" "this" {
  name_prefix          = "${var.name}-"
  description          = "Crypto Hunter engine host"
  permissions_boundary = var.iam_permissions_boundary_arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.${data.aws_partition.current.dns_suffix}" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "additional" {
  for_each = toset(var.additional_iam_policy_arns)

  role       = aws_iam_role.this.name
  policy_arn = each.value
}

# Read access to the one parameter that holds the dashboard token. For a
# SecureString encrypted with a customer-managed key, also pass a kms:Decrypt
# policy through additional_iam_policy_arns.
resource "aws_iam_role_policy" "api_token" {
  count = local.api_token_enabled ? 1 : 0

  name = "read-api-token"
  role = aws_iam_role.this.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:GetParameter"
      Resource = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.api_token_ssm_parameter_name, "/")}"
    }]
  })
}

resource "aws_iam_instance_profile" "this" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.this.name

  tags = var.tags
}
