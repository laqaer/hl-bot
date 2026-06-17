# Deploy hl-bot on AWS

`terraform apply` brings up an EC2 instance that boots **already running the bot
in paper** — systemd timers, WebSocket feed, health/heartbeat, and (optional)
IAM-role Litestream backups to S3. Going live stays the gated step in
[`../../docs/GO_LIVE.md`](../../docs/GO_LIVE.md) / [`../../docs/HOST_QUICKSTART.md`](../../docs/HOST_QUICKSTART.md).

Defaults chosen for you: **t4g.small (ARM, ~$12/mo)** in **ap-northeast-1 (Tokyo)**
— low latency to Hyperliquid — Ubuntu 24.04, encrypted gp3 root, **IAM instance
role** for backups (no static AWS keys on the box).

## Prerequisites
- Terraform ≥ 1.3 and AWS credentials (`aws configure` / `AWS_PROFILE`).
- The repo reachable by the instance (public, or provide a token/deploy key).
- (Optional) An EC2 key pair if you want SSH fallback access. Session Manager is enabled by default and needs no key pair.

## Deploy

Create a `terraform.tfvars` file (see example below) or pass vars directly.

```bash
cd deploy/aws
terraform init
terraform plan
terraform apply
```

Example `terraform.tfvars`:

```hcl
key_name    = "hl-bot"                        # optional if using Session Manager
ssh_cidr    = "YOUR.IP.ADDR/32"               # lock SSH to your IP; set to "" to disable SSH ingress
repo_url    = "https://github.com/laqaer/hl-bot.git"
branch      = "main"
hl_address  = "0xYOURFUNDEDACCOUNT"
# hl_trader_address = "0x..."                 # only if different from hl_address
# backup_bucket = "your-existing-s3-bucket"   # optional; omit to skip backups
```

With the default settings the instance is reachable via **AWS Systems Manager Session Manager** (no SSH key required on the client). SSH is retained as a fallback if `key_name` is provided.

Terraform prints connection commands after apply. Cloud-init takes ~2–3 min; watch `/var/log/hl-bot-bootstrap.log` on the box.

## Verify (on the instance)

Via Session Manager:
```bash
aws ssm start-session --region ap-northeast-1 --target INSTANCE_ID
systemctl list-timers 'hlbot-*'
systemctl status hlbot-ws.service --no-pager
sudo -u hlbot bash -lc 'cd /opt/hl-bot && uv run hlbot doctor && uv run hlbot health'
```

Or via SSH fallback:
```bash
ssh ubuntu@<public_dns>
systemctl list-timers 'hlbot-*'
systemctl status hlbot-ws.service --no-pager
sudo -u hlbot bash -lc 'cd /opt/hl-bot && uv run hlbot doctor && uv run hlbot health'
```

## Confirm edge, then go live
Follow [`../../docs/HOST_QUICKSTART.md`](../../docs/HOST_QUICKSTART.md) §4–§6:
run `hlbot confirm` on real history; if a strategy passes, add the API wallet,
enable it to `live_small`, set `HLBOT_TICK_ARGS`, restart. **No real money moves
until you do that.**

## Notes / decisions
- **Backups use the instance IAM role** (Terraform attaches an S3 policy scoped to
  `backup_bucket`) — no `AWS_ACCESS_KEY_ID` on the box. Region from `AWS_REGION`.
- **Cost:** t4g.small ~$12/mo + EBS ~$2. Drop to `t4g.micro` (free tier yr 1) via
  `-var 'instance_type=t4g.micro'` if you don't run the loop on this box.
- **Security:** set `ssh_cidr` to your IP/32. Root volume is encrypted. The
  funded key never touches the box — only an approved API/agent wallet, added by
  hand post-confirm.
- **Teardown:** `terraform destroy` (back up the DB first if you want the history).
- HCL here is written carefully but not validated in this repo's CI; run
  `terraform validate` / `plan` before `apply`.
