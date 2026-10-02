locals {
  # With an Elastic IP this is that address; otherwise whatever public address
  # the subnet handed the instance (empty in a private subnet).
  public_ip = var.create_eip ? aws_eip.this[0].public_ip : aws_instance.this.public_ip
}

output "instance_id" {
  description = "ID of the EC2 instance."
  value       = aws_instance.this.id
}

output "public_ip" {
  description = "Public IPv4 address (the Elastic IP when create_eip is true). Null when the instance has none."
  value       = local.public_ip != "" ? local.public_ip : null
}

output "private_ip" {
  description = "Private IPv4 address of the instance."
  value       = aws_instance.this.private_ip
}

output "engine_url" {
  description = "Base URL of the engine, for the ENGINE_URL variable of the Netlify dashboard proxy. Null when the instance has no public address."
  value       = local.public_ip != "" ? "http://${local.public_ip}:${var.app_port}" : null
}

output "ssm_session_command" {
  description = "Opens a shell on the instance through Session Manager (needs the AWS CLI and the Session Manager plugin). Logs: journalctl -u crypto-hunter -f"
  value       = "aws ssm start-session --region ${data.aws_region.current.region} --target ${aws_instance.this.id}"
}

output "ssm_port_forward_command" {
  description = "Forwards the dashboard to http://localhost:<app_port> through Session Manager, so no inbound port has to be open at all."
  value       = "aws ssm start-session --region ${data.aws_region.current.region} --target ${aws_instance.this.id} --document-name AWS-StartPortForwardingSession --parameters portNumber=${var.app_port},localPortNumber=${var.app_port}"
}

output "security_group_id" {
  description = "Security group of the instance. Attach extra aws_vpc_security_group_ingress_rule resources to it for any access this module does not model."
  value       = aws_security_group.this.id
}

output "iam_role_name" {
  description = "Name of the instance role, for attaching additional policies."
  value       = aws_iam_role.this.name
}

output "iam_role_arn" {
  description = "ARN of the instance role."
  value       = aws_iam_role.this.arn
}

output "data_volume_id" {
  description = "ID of the EBS volume that holds the application state (mounted at <app>/data)."
  value       = aws_ebs_volume.data.id
}

output "availability_zone" {
  description = "Availability Zone of the instance and of the data volume."
  value       = aws_ebs_volume.data.availability_zone
}

output "snapshot_policy_id" {
  description = "ID of the Data Lifecycle Manager policy that snapshots the data volume daily. Null when snapshot_retention_days is 0."
  value       = one(aws_dlm_lifecycle_policy.data[*].id)
}

output "region" {
  description = "AWS Region the deployment lives in."
  value       = data.aws_region.current.region
}
