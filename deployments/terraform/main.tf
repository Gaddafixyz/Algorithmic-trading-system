terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # Uncomment to use S3 backend for team / production use:
  # backend "s3" {
  #   bucket         = "my-terraform-state"
  #   key            = "trading-system/terraform.tfstate"
  #   region         = "us-east-1"
  #   encrypt        = true
  #   dynamodb_table = "terraform-locks"
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = var.tags
  }
}

# ─── Networking ──────────────────────────────────────────────────────────────

resource "aws_vpc" "trading" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "trading-vpc-${var.environment}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.trading.id
  cidr_block        = "10.0.${count.index}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "trading-private-${count.index}-${var.environment}" }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.trading.id
  cidr_block              = "10.0.${count.index + 10}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "trading-public-${count.index}-${var.environment}" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_internet_gateway" "trading" {
  vpc_id = aws_vpc.trading.id
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.trading.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.trading.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ─── NAT Gateway — private subnet egress ─────────────────────────────────────
# ECS tasks run in private subnets (no public IP) for security.  They still
# need outbound internet access to reach Binance WebSocket / REST APIs.
# A NAT gateway in the first public subnet provides that path without
# exposing the tasks directly to inbound internet traffic.

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "trading-nat-eip-${var.environment}" }
}

resource "aws_nat_gateway" "trading" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id   # place NAT GW in the first public subnet
  depends_on    = [aws_internet_gateway.trading]
  tags          = { Name = "trading-nat-${var.environment}" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.trading.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.trading.id
  }
  tags = { Name = "trading-private-rt-${var.environment}" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ─── Security groups ─────────────────────────────────────────────────────────

resource "aws_security_group" "app" {
  name        = "trading-app-${var.environment}"
  description = "Trading system ECS tasks"
  vpc_id      = aws_vpc.trading.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Dashboard"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }

  ingress {
    description = "Prometheus metrics"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }
}

resource "aws_security_group" "db" {
  name        = "trading-db-${var.environment}"
  description = "PostgreSQL RDS access"
  vpc_id      = aws_vpc.trading.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

resource "aws_security_group" "redis" {
  name        = "trading-redis-${var.environment}"
  description = "Redis ElastiCache access"
  vpc_id      = aws_vpc.trading.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

# ─── RDS PostgreSQL ───────────────────────────────────────────────────────────

resource "aws_db_subnet_group" "trading" {
  name       = "trading-db-subnet-${var.environment}"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "trading" {
  identifier             = "trading-${var.environment}"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = var.db_instance_class
  allocated_storage      = var.db_allocated_storage
  storage_encrypted      = true
  db_name                = "trading"
  username               = var.db_username
  password               = var.db_password
  db_subnet_group_name   = aws_db_subnet_group.trading.name
  vpc_security_group_ids = [aws_security_group.db.id]
  skip_final_snapshot    = var.environment != "production"
  deletion_protection    = var.environment == "production"
  backup_retention_period = var.environment == "production" ? 7 : 1
  multi_az               = var.environment == "production"
  tags                   = { Name = "trading-postgres-${var.environment}" }
}

# ─── ElastiCache Redis ────────────────────────────────────────────────────────

resource "aws_elasticache_subnet_group" "trading" {
  name       = "trading-redis-subnet-${var.environment}"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_cluster" "trading" {
  cluster_id           = "trading-${var.environment}"
  engine               = "redis"
  node_type            = var.redis_node_type
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  engine_version       = "7.0"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.trading.name
  security_group_ids   = [aws_security_group.redis.id]
}

# ─── ECS Fargate ─────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "trading" {
  name = "trading-${var.environment}"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name = "trading-ecs-execution-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "secrets_access" {
  name = "trading-secrets-access-${var.environment}"
  role = aws_iam_role.ecs_task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = [
        var.binance_api_key_secret_arn,
        var.binance_api_secret_secret_arn,
      ]
    }]
  })
}

resource "aws_cloudwatch_log_group" "trading" {
  name              = "/ecs/trading-${var.environment}"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "trading" {
  family                   = "trading-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.ecs_task_cpu
  memory                   = var.ecs_task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([{
    name  = "trading-system"
    image = var.trading_image
    command = ["python", "scripts/start.py"]

    portMappings = [
      { containerPort = 8080, protocol = "tcp" },
      { containerPort = 9090, protocol = "tcp" },
    ]

    environment = [
      { name = "TRADING_ENV", value = var.environment },
      { name = "DATABASE_URL", value = "postgresql+asyncpg://${var.db_username}:${var.db_password}@${aws_db_instance.trading.endpoint}/trading" },
      { name = "REDIS_URL", value = "redis://${aws_elasticache_cluster.trading.cache_nodes[0].address}:6379/0" },
    ]

    secrets = [
      { name = "BINANCE_API_KEY",    valueFrom = var.binance_api_key_secret_arn },
      { name = "BINANCE_API_SECRET", valueFrom = var.binance_api_secret_secret_arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.trading.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
  }])
}

resource "aws_ecs_service" "trading" {
  name            = "trading-${var.environment}"
  cluster         = aws_ecs_cluster.trading.id
  task_definition = aws_ecs_task_definition.trading.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_iam_role_policy_attachment.ecs_task_execution]
}

# ─── CloudWatch alarms ────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "ecs_cpu_high" {
  alarm_name          = "trading-${var.environment}-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "ECS task CPU utilization > 80%"
  dimensions = {
    ClusterName = aws_ecs_cluster.trading.name
    ServiceName = aws_ecs_service.trading.name
  }
}
