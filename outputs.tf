output "public_ip" {
  description = "Elastic IP of the engine host. Use http://<ip>:<port> as ENGINE_URL for the Vercel dashboard proxy."
  value       = module.engine.public_ip
}

output "engine_url" {
  description = "Value for the ENGINE_URL Vercel environment variable."
  value       = module.engine.engine_url
}

output "instance_id" {
  description = "Instance ID (connect with: aws ssm start-session --target <id>)."
  value       = module.engine.instance_id
}

output "ssm_session_command" {
  description = "Open a shell on the host through Session Manager. Logs: journalctl -u crypto-hunter -f"
  value       = module.engine.ssm_session_command
}

output "ssm_port_forward_command" {
  description = "Forward the dashboard to http://localhost:<app_port> without opening any inbound port."
  value       = module.engine.ssm_port_forward_command
}

output "data_volume_id" {
  description = "EBS volume that holds the application state."
  value       = module.engine.data_volume_id
}
