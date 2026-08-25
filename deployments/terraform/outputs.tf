output "ecs_cluster_name" {
  description = "Name of the ECS cluster"
  value       = aws_ecs_cluster.trading.name
}

output "ecs_service_name" {
  description = "Name of the ECS service"
  value       = aws_ecs_service.trading.name
}

output "db_endpoint" {
  description = "RDS PostgreSQL endpoint (host:port)"
  value       = aws_db_instance.trading.endpoint
  sensitive   = true
}

output "redis_endpoint" {
  description = "ElastiCache Redis endpoint"
  value       = "${aws_elasticache_cluster.trading.cache_nodes[0].address}:6379"
  sensitive   = true
}

output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.trading.id
}

output "private_subnet_ids" {
  description = "Private subnet IDs"
  value       = aws_subnet.private[*].id
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for ECS tasks"
  value       = aws_cloudwatch_log_group.trading.name
}
