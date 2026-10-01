output "public_ip" {
  description = "Elastic IP of the engine host. Use http://<ip>:<port> as ENGINE_URL for the Netlify edge proxy."
  value       = aws_eip.engine.public_ip
}

output "engine_url" {
  description = "Value for the ENGINE_URL Netlify environment variable."
  value       = "http://${aws_eip.engine.public_ip}:${var.app_port}"
}

output "instance_id" {
  description = "Instance ID (connect with: aws ssm start-session --target <id>)."
  value       = aws_instance.engine.id
}
