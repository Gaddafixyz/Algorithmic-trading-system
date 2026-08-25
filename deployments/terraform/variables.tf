variable "region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (paper | testnet | production)"
  type        = string
  default     = "paper"
  validation {
    condition     = contains(["paper", "testnet", "production"], var.environment)
    error_message = "environment must be one of: paper, testnet, production."
  }
}

variable "trading_image" {
  description = "Docker image URI for the trading system (e.g. 123456789.dkr.ecr.us-east-1.amazonaws.com/trading-system:latest)"
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.medium"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GiB"
  type        = number
  default     = 20
}

variable "db_username" {
  description = "PostgreSQL superuser name"
  type        = string
  default     = "trader"
}

variable "db_password" {
  description = "PostgreSQL password (stored in Secrets Manager; do not commit)"
  type        = string
  sensitive   = true
}

variable "redis_node_type" {
  description = "ElastiCache node type"
  type        = string
  default     = "cache.t3.micro"
}

variable "ecs_task_cpu" {
  description = "Fargate vCPU units (1024 = 1 vCPU)"
  type        = number
  default     = 1024
}

variable "ecs_task_memory" {
  description = "Fargate memory in MiB"
  type        = number
  default     = 2048
}

variable "binance_api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Binance API key"
  type        = string
}

variable "binance_api_secret_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Binance API secret"
  type        = string
}

variable "alert_email" {
  description = "Email address for CloudWatch alarm notifications"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default = {
    Project   = "trading-system"
    ManagedBy = "terraform"
  }
}
