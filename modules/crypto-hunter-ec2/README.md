# crypto-hunter-ec2

Terraform module that runs the Crypto Hunter trading engine (this repository) on AWS, written for
**AWS provider v6** (`hashicorp/aws ~> 6.0`).

It builds one small Ubuntu 24.04 EC2 host that clones the app, installs it into a virtualenv and runs
`python run.py` under systemd, with all state on a separate encrypted, snapshotted EBS volume.
Venues stay in paper mode: the module changes nothing about the engine's own live-trading gates.

```
  you ── SSM Session Manager ───────────►  no inbound port needed
  you / Vercel ── HTTP :8080 ───────────►  only from app_cidrs (optional)

  ┌─ subnet (one Availability Zone) ─────────────────────────────────────────────────────┐
  │ EC2 host: Ubuntu 24.04, IMDSv2, no SSH key by default                                │
  │   systemd unit crypto-hunter  ->  python run.py --port 8080                          │
  │                                                                                      │
  │   root volume  /                          OS, virtualenv, code                       │
  │   data volume  /home/trader/awesome/data  SQLite DBs, credential key, settings       │
  │                                           + daily snapshots (Data Lifecycle Manager) │
  └──────────────────────────────────────────────────────────────────────────────────────┘
```

Both volumes are encrypted gp3.

## Why one instance, and why EBS

The engine is a single process that must never run twice against the same exchange account, and it keeps
per-venue SQLite databases (WAL mode) plus the AES key that encrypts stored API credentials on local disk.
So the module deliberately does **not** build an Auto Scaling group or a container service (two copies would
trade the same account), and uses EBS rather than EFS (SQLite WAL is not safe on NFS).

What makes replacing the instance safe is that `data/` lives on its own volume. A new `repo_ref`, a new
module version or a new OS image replaces the *instance*; the replacement attaches the same volume and the
engine resumes from its persisted state, including the credential key, so stored exchange keys stay decryptable.

## What it creates

| Resource | Purpose |
|---|---|
| `aws_instance` | The host. IMDSv2 required, encrypted gp3 root volume, bootstrap in `user_data`. |
| `aws_eip` | Stable public address (optional, `create_eip`). |
| `aws_ebs_volume` + `aws_volume_attachment` | The state volume, encrypted gp3, with a final snapshot if it is ever deleted. |
| `aws_dlm_lifecycle_policy` + IAM role | Daily snapshots of the state volume, `snapshot_retention_days` kept (optional). |
| `aws_security_group` + rule resources | No inbound access unless `app_cidrs` / `ssh_cidrs` are set; all outbound allowed. |
| `aws_iam_role`, instance profile | Session Manager access, plus read access to one SSM parameter if a token is configured. |
| `terraform_data` | Turns an explicit change of `ami_id` into a replacement (see [Updating](#updating-and-replacing-the-host)). |

## Requirements

* Terraform `>= 1.5` and AWS provider `~> 6.0`.
* Credentials that may create the resources above (EC2/EBS, IAM roles and instance profiles, Data Lifecycle
  Manager, security groups, Elastic IPs) and read the public Canonical AMI parameter in SSM.
* A subnet with outbound internet access. The host reaches GitHub, PyPI, the Ubuntu mirrors, the exchange
  APIs and the SSM endpoints. In a default VPC that means a public address, which is why `create_eip` is on.
* To use the shell and port-forwarding outputs: the AWS CLI and the
  [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).

## Usage

The repository root is a ready-to-run configuration that calls this module:

```sh
terraform init
terraform apply
```

To use the module from your own configuration, configure the provider yourself (the module deliberately has no
`provider` block) and pin a tag or commit:

```hcl
provider "aws" {
  region = "ap-southeast-1"
}

module "engine" {
  source = "github.com/badalworld/AWESOME//modules/crypto-hunter-ec2?ref=<tag-or-commit>"

  repo_ref  = "<tag-or-commit>"          # the code the host runs; pin it like the module itself
  subnet_id = "subnet-0123456789abcdef0" # recommended for production, see below

  # app_cidrs stays empty: reach the dashboard through SSM port forwarding.
}

output "open_dashboard" {
  value = module.engine.ssm_port_forward_command
}
```

Use `providers = { aws = aws.other }` on the module call if you work with several provider configurations.

`subnet_id` matters because the state volume is created in that subnet's Availability Zone and cannot follow
the instance to another one. Left null, the module picks a default subnet of the default VPC, in a zone that
offers `instance_type`; pin it so the choice can never drift.

## Reaching the dashboard

**Recommended: Session Manager port forwarding.** No inbound port is opened at all:

```sh
$(terraform output -raw ssm_port_forward_command)   # then browse http://localhost:8080
$(terraform output -raw ssm_session_command)        # a shell on the host
```

**Direct access.** Put your address in `app_cidrs` (IPv4 or IPv6, e.g. `["203.0.113.7/32"]`) and open
`http://<public_ip>:<app_port>`. The dashboard speaks plain HTTP, so keep this to addresses you trust.

**Through the Vercel proxy.** Set `ENGINE_URL` on Vercel to the `engine_url` output. Vercel has no fixed
egress addresses, so the proxy needs a wide `app_cidrs` range; do that only together with
`api_token_ssm_parameter_name` below (the module warns at plan time if you do not).

## Protecting the dashboard with an API token

The dashboard has no user accounts; its only protection is the `web.api_token` setting. To have the host
apply a token on every start without the secret ever entering Terraform state or `user_data`:

```sh
aws ssm put-parameter --name /crypto-hunter/api-token --type SecureString --value "$(openssl rand -hex 32)"
```

```hcl
module "engine" {
  # ...
  api_token_ssm_parameter_name = "/crypto-hunter/api-token"
}
```

* The instance role may read that one parameter and nothing else. If the parameter is encrypted with a
  customer-managed KMS key, also pass a `kms:Decrypt` policy through `additional_iam_policy_arns`.
* Before every start, `crypto-hunter-sync-token` (an `ExecStartPre` of the unit) copies the value into
  `data/settings.json` as `web.api_token`, keeping every other saved setting. **If the parameter cannot be
  read, the service does not start**: an unauthenticated dashboard is never the fallback.
* Rotate by updating the parameter and running `sudo systemctl restart crypto-hunter` on the host.
* SSM is the source of truth: a `web.api_token` saved from the dashboard's Settings page is overwritten at the
  next start.
* The module cannot create the parameter for you, because its value would then be stored in Terraform state.

## State, backups and restores

| What | Where | Protection |
|---|---|---|
| Per-venue SQLite databases, `.secrets/machine.key`, `settings.json` | data volume, mounted at `/home/trader/awesome/data` | encrypted; survives instance replacement |
| Daily snapshots | Data Lifecycle Manager, 03:00 UTC | the last `snapshot_retention_days` (default 14) are kept |
| Final snapshot | taken when Terraform deletes the volume | never expires |

Snapshots are not removed when you `terraform destroy`; delete them yourself when you no longer need them.

**Restore a backup:** find the snapshot (`aws ec2 describe-snapshots --owner-ids self --filters Name=tag:Name,Values=<name>-data`),
set `data_volume_snapshot_id = "snap-..."` and apply. This replaces the data volume and the instance; the new
instance mounts the restored volume. Remove the variable again afterwards only if you want a later restore to be explicit.

**Grow the volume:** raise `data_volume_size_gb`, apply, then on the host run
`sudo resize2fs "$(findmnt -n -o SOURCE /home/trader/awesome/data)"`.

**Move to another Availability Zone** (for example after a zone outage): change `subnet_id`, set
`data_volume_snapshot_id` to a recent snapshot, and apply with
`-replace='module.<name>.aws_ebs_volume.data'`. The volume deliberately ignores zone changes otherwise, so a
changed subnet fails loudly at attach time instead of silently replacing the state.

## Updating and replacing the host

The bootstrap runs once per instance, so anything that changes `user_data` (`repo_url`, `repo_ref`, `app_port`,
the token parameter, or a new version of this module) **replaces the instance**. `data/` is kept: Terraform stops
the old instance (the engine shuts down cleanly and the filesystem is unmounted), detaches the volume, and the new
instance re-attaches it. Expect a few minutes during which the engine is down, so do it at a quiet moment.
Changing `ami_id` or `subnet_id` also replaces the host. Changing `instance_type` stops and starts it in place.

A newer Canonical Ubuntu release does **not** replace the host: `ami` is ignored so that an unannounced image
update never swaps a trading machine. To move to the latest image deliberately:

```sh
terraform apply -replace='module.engine.aws_instance.this'
```

Between replacements the OS patches itself (`unattended-upgrades`); reboot occasionally with `sudo reboot`
from a session. The data mount and the service come back on their own.

Changes you make on the dashboard's Settings page are stored in `data/settings.json` on the state volume and
survive replacement. Edits to `config.toml` on the host do not: they live in the cloned repository.

## Operating the host

```sh
sudo systemctl status crypto-hunter
sudo journalctl -u crypto-hunter -f
sudo cloud-init status --long          # did the first-boot bootstrap finish?
sudo less /var/log/cloud-init-output.log
```

## Security defaults

* IMDSv2 only, encrypted root and data volumes (optionally with your own key via `kms_key_arn`), no key pair and
  no SSH rule unless you ask for them, and a security group with no inbound rules by default.
* The service runs as an unprivileged user under `NoNewPrivileges`, `PrivateTmp` and `ProtectSystem=strict`, and may
  write only to the state directory. It does not start if the state volume is not mounted, so it can never fork its
  state onto the root disk.
* `user_data` holds no secrets. AWS provider v6 stores it in clear text in state, which is also why `repo_url`
  must be readable without credentials (the variable rejects URLs with embedded credentials).
* Plan-time warnings (never errors) appear when `app_cidrs` opens the dashboard to `0.0.0.0/0` / `::/0` without a
  token, or when `ssh_cidrs` opens SSH to the world.
* Static analysis with Checkov passes; three findings on the instance are suppressed in `main.tf` with reasons
  (detailed monitoring, explicit EBS optimization, public address).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `no matching EC2 VPC found`, or `No default subnet in an Availability Zone that offers <type>` | The account has no usable default VPC. Pass `subnet_id`. |
| Apply fails attaching the volume (`InvalidVolume.ZoneMismatch`) | The instance and the volume are in different zones. Point `subnet_id` back at the original zone, or follow "Move to another Availability Zone". |
| The service is not running after a fresh apply | `cloud-init status --long` and `/var/log/cloud-init-output.log`; the bootstrap retries transient network errors, then stops at the first hard failure. |
| The service keeps restarting right after boot | `journalctl -u crypto-hunter`. With a token configured: the instance role cannot read the parameter, or its KMS key. Without: the data volume is not mounted. |
| `repo_ref` rejected | Branches, tags and commit SHAs made of letters, digits and `. _ / -` only. |

## Upgrading from the pre-module root configuration

Earlier versions of this repository kept all Terraform at the repository root on AWS provider `~> 5.0`, with
`data/` on the instance's root disk. Applying the module over that state keeps the Elastic IP, the IAM role and
the instance profile (via `moved` blocks in the root `main.tf`), replaces the security group (its inline rules
cannot coexist with the module's standalone rules) and **replaces the instance, which discards the old `data/`**.

1. Back up `data/` first: from a Session Manager shell, `sudo systemctl stop crypto-hunter`, archive
   `/home/trader/awesome/data` and copy the archive somewhere safe. Or start fresh and re-enter exchange keys in
   Settings. Paper-mode history is the only thing lost.
2. `terraform init -upgrade`, then read `terraform plan` before applying. It should show the instance and the
   security group being replaced, and the data volume, the snapshot schedule and its IAM role being added. If it
   shows the Elastic IP being destroyed, stop and check that the `moved` blocks in `main.tf` are intact.
3. `terraform apply`.
4. Restore the archive into `/home/trader/awesome/data` on the new host, `sudo chown -R trader: /home/trader/awesome/data`,
   and `sudo systemctl restart crypto-hunter`.
5. Delete the `moved` blocks from the root `main.tf`.

The old configuration worked with provider v6 apart from its version pin; the changes that matter for this
stack are `aws_eip` `domain` (already used), `data.aws_region` `.region` instead of `.name`, and
`aws_instance.user_data` now being stored in clear text.

## Reference

### Inputs

| Name | Description | Type | Default |
|---|---|---|---|
| `name` | Name prefix for every resource and the value of the Name tag. Use a different name for each deployment in the same account and region. | `string` | `"crypto-hunter"` |
| `tags` | Extra tags applied to every resource. They are merged with (and win over) any default_tags configured on the AWS provider. | `map(string)` | `{}` |
| `subnet_id` | Subnet to launch in. Null picks a default subnet of the default VPC, in an Availability Zone that offers instance_type. Pin it for production: the data volume is created in this subnet's Availability Zone and cannot follow the instance to another one. | `string` | `null` |
| `create_eip` | Allocate an Elastic IP and attach it to the instance. This gives the dashboard / Vercel ENGINE_URL / exchange API-key IP allow-list a stable address that survives instance replacement. Set false for an instance in a private subnet that you reach through SSM Session Manager. | `bool` | `true` |
| `instance_type` | EC2 instance type. The default AMI is x86_64; for a Graviton (arm64) type also pass an arm64 ami_id. | `string` | `"t3.micro"` |
| `ami_id` | AMI to launch instead of the latest Canonical Ubuntu 24.04 LTS (amd64), which is resolved from Canonical's public SSM parameter. It must be Ubuntu 24.04 or newer (the bootstrap uses apt and needs Python 3.11+). Changing this value replaces the instance; newer releases of the default image do not. | `string` | `null` |
| `key_name` | Name of an existing EC2 key pair for SSH. Null (the default) means no SSH key at all: use SSM Session Manager. | `string` | `null` |
| `root_volume_size_gb` | Size in GB of the root volume (operating system, Python virtualenv, logs). Application state is not stored here. | `number` | `20` |
| `data_volume_size_gb` | Size in GB of the separate EBS volume mounted at <app>/data: the per-venue SQLite databases, the credential encryption key and the dashboard settings overrides. It outlives the instance. | `number` | `10` |
| `data_volume_snapshot_id` | Create the data volume from this snapshot, to restore a backup. Null creates a new, empty volume. Changing it replaces the data volume and therefore the instance. | `string` | `null` |
| `kms_key_arn` | ARN of a customer-managed KMS key for the root and data volumes. Null uses the AWS-managed aws/ebs key. Both volumes are always encrypted. | `string` | `null` |
| `snapshot_retention_days` | Days of daily EBS snapshots of the data volume to keep (Amazon Data Lifecycle Manager). 0 disables the schedule. Independently of this, a final snapshot is always taken if Terraform deletes the volume. | `number` | `14` |
| `repo_url` | HTTPS Git URL the instance clones the application from. It must be readable without credentials, because user_data is stored in clear text by AWS provider v6. | `string` | `"https://github.com/badalworld/AWESOME.git"` |
| `repo_ref` | Branch, tag or commit to check out. Pin a tag or commit SHA for reproducible deployments. Changing it replaces the instance (the data volume is kept and re-attached). | `string` | `"main"` |
| `app_port` | TCP port the dashboard and API listen on (run.py --port). | `number` | `8080` |
| `api_token_ssm_parameter_name` | Name of an existing SSM Parameter Store parameter (type SecureString recommended) that holds the dashboard API token. When set, the instance may read only that parameter, and every start of the service copies its value into data/settings.json as web.api_token, so the token never appears in user_data or the Terraform state. Null leaves the dashboard unauthenticated. | `string` | `null` |
| `app_cidrs` | IPv4 or IPv6 CIDR blocks allowed to reach app_port. Empty keeps the port closed: use the ssm_port_forward_command output instead. Vercel has no fixed egress IPs, so proxying through it needs a wide range, in which case also set api_token_ssm_parameter_name. | `list(string)` | `[]` |
| `ssh_cidrs` | IPv4 or IPv6 CIDR blocks allowed to reach SSH (port 22). Only useful together with key_name; SSM Session Manager needs no inbound rule at all. | `list(string)` | `[]` |
| `additional_iam_policy_arns` | Extra managed policies to attach to the instance role, for example one granting kms:Decrypt on the customer-managed key that protects the SSM token parameter. | `list(string)` | `[]` |
| `iam_permissions_boundary_arn` | ARN of a permissions boundary to put on the IAM roles this module creates, for accounts that require one. | `string` | `null` |

### Outputs

| Name | Description |
|---|---|
| `instance_id` | ID of the EC2 instance. |
| `public_ip` | Public IPv4 address (the Elastic IP when create_eip is true). Null when the instance has none. |
| `private_ip` | Private IPv4 address of the instance. |
| `engine_url` | Base URL of the engine, for the ENGINE_URL variable of the Vercel dashboard proxy. Null when the instance has no public address. |
| `ssm_session_command` | Opens a shell on the instance through Session Manager (needs the AWS CLI and the Session Manager plugin). Logs: journalctl -u crypto-hunter -f |
| `ssm_port_forward_command` | Forwards the dashboard to http://localhost:<app_port> through Session Manager, so no inbound port has to be open at all. |
| `security_group_id` | Security group of the instance. Attach extra aws_vpc_security_group_ingress_rule resources to it for any access this module does not model. |
| `iam_role_name` | Name of the instance role, for attaching additional policies. |
| `iam_role_arn` | ARN of the instance role. |
| `data_volume_id` | ID of the EBS volume that holds the application state (mounted at <app>/data). |
| `availability_zone` | Availability Zone of the instance and of the data volume. |
| `snapshot_policy_id` | ID of the Data Lifecycle Manager policy that snapshots the data volume daily. Null when snapshot_retention_days is 0. |
| `region` | AWS Region the deployment lives in. |
