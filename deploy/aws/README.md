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
- An EC2 key pair in the target region (for SSH).
- The repo reachable by the instance (public, or provide a token/deploy key).

## Deploy
```bash
cd deploy/aws
terraform init
terraform apply \
  -var 'key_name=YOUR_EC2_KEYPAIR' \
  -var 'ssh_cidr=YOUR.IP.ADDR.0/32' \
  -var 'repo_url=https://github.com/laqaer/hl-bot.git' \
  -var 'branch=main' \
  -var 'hl_address=0xYOURFUNDEDACCOUNT' \
  -var 'backup_bucket=your-existing-s3-bucket'      # optional; omit to skip backups
```
Terraform prints the public DNS + an `ssh` command. Cloud-init takes ~2–3 min;
watch `/var/log/hl-bot-bootstrap.log` on the box.

## Verify (on the instance)
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
- **Access is IP-independent via SSM:** the instance role includes
  `AmazonSSMManagedInstanceCore`, so EC2 → Connect → **Session Manager** works from
  any network with no inbound SSH. If your IP changes, SSH via `ssh_cidr` breaks
  but SSM still works (and the bot keeps trading either way — `ssh_cidr` only gates
  inbound SSH, never the bot's outbound connections). You can close port 22
  entirely and rely on SSM.
- **Teardown:** `terraform destroy` (back up the DB first if you want the history).
- HCL here is written carefully but not validated in this repo's CI; run
  `terraform validate` / `plan` before `apply`.
