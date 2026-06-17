output "instance_id" {
  value = aws_instance.hlbot.id
}

output "public_ip" {
  value = aws_instance.hlbot.public_ip
}

output "public_dns" {
  value = aws_instance.hlbot.public_dns
}

output "ssh" {
  description = "SSH in once cloud-init finishes (~2-3 min)."
  value       = "ssh ubuntu@${aws_instance.hlbot.public_dns}"
}

output "ssm" {
  description = "Connect via Session Manager (no SSH key required on the client)."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.hlbot.id}"
}
