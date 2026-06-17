terraform {
  required_version = ">= 1.3"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# Latest Ubuntu 24.04 (Noble) arm64 from Canonical, unless an AMI is pinned.
data "aws_ami" "ubuntu" {
  count       = var.ami_id == "" ? 1 : 0
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  ami         = var.ami_id != "" ? var.ami_id : data.aws_ami.ubuntu[0].id
  use_backups = var.backup_bucket != ""
  trader      = var.hl_trader_address != "" ? var.hl_trader_address : var.hl_address
}

resource "aws_security_group" "hlbot" {
  name_prefix = "hlbot-"
  description = "hl-bot: SSH in, all out"

  dynamic "ingress" {
    for_each = var.ssh_cidr != "" ? [1] : []
    content {
      description = "SSH"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [var.ssh_cidr]
    }
  }
  egress {
    description = "all outbound (HL API/WSS, Telegram, S3)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = var.tags
}

# --- IAM: instance role so Litestream backs up to S3 with NO static keys ---
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "hlbot" {
  name_prefix        = "hlbot-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "s3" {
  count = local.use_backups ? 1 : 0
  statement {
    actions = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [
      "arn:aws:s3:::${var.backup_bucket}",
      "arn:aws:s3:::${var.backup_bucket}/*",
    ]
  }
}

resource "aws_iam_role_policy" "s3" {
  count  = local.use_backups ? 1 : 0
  name   = "hlbot-litestream-s3"
  role   = aws_iam_role.hlbot.id
  policy = data.aws_iam_policy_document.s3[0].json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.hlbot.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "hlbot" {
  name_prefix = "hlbot-"
  role        = aws_iam_role.hlbot.name
}

resource "aws_instance" "hlbot" {
  ami                    = local.ami
  instance_type          = var.instance_type
  key_name               = var.key_name != "" ? var.key_name : null
  vpc_security_group_ids = [aws_security_group.hlbot.id]
  iam_instance_profile   = aws_iam_instance_profile.hlbot.name

  root_block_device {
    volume_size = var.root_gb
    volume_type = "gp3"
    encrypted   = true
  }

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    repo_url          = var.repo_url
    branch            = var.branch
    hl_address        = var.hl_address
    hl_trader_address = local.trader
    backup_bucket     = var.backup_bucket
    region            = var.region
  })

  tags = merge(var.tags, { Name = "hl-bot" })
}
