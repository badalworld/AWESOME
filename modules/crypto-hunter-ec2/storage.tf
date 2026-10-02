###############################################################################
# State volume
#
# data/ holds the per-venue SQLite databases, the AES key that protects stored
# exchange credentials (data/.secrets/machine.key) and the settings saved from
# the dashboard. Keeping it on its own volume means replacing the instance (new
# repo_ref, new module version, new OS image) loses nothing: the replacement
# attaches the same volume and the engine resumes from its persisted state.
#
# This is why the module uses EBS and not EFS: SQLite in WAL mode is not safe
# on NFS.
###############################################################################

locals {
  backups_enabled = var.snapshot_retention_days > 0

  # Tag the Data Lifecycle Manager policy uses to find the volume. It carries
  # the deployment name so that two deployments never share a policy.
  snapshot_policy_tags = local.backups_enabled ? { "${var.name}:snapshot-policy" = "daily" } : {}
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.selected.availability_zone
  type              = "gp3"
  size              = var.data_volume_size_gb
  snapshot_id       = var.data_volume_snapshot_id
  encrypted         = true
  kms_key_id        = var.kms_key_arn

  # If Terraform ever deletes this volume (terraform destroy, or a deliberate
  # -replace), leave a snapshot behind so the data can be restored through
  # data_volume_snapshot_id.
  final_snapshot = true

  tags = merge(var.tags, local.snapshot_policy_tags, { Name = "${var.name}-data" })

  lifecycle {
    # The Availability Zone follows the subnet. If the subnet ever changes
    # (for example the default-subnet pick drifts), fail loudly at attach time
    # instead of silently replacing the volume that holds the trading state.
    # To move zones on purpose, restore from a snapshot with
    #   terraform apply -replace=<module path>.aws_ebs_volume.data
    ignore_changes = [availability_zone]
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.this.id

  # When the instance is replaced or destroyed, stop it first: the engine then
  # receives SIGTERM, closes its databases and the filesystem is unmounted
  # cleanly before the volume is detached.
  stop_instance_before_detaching = true
}

###############################################################################
# Daily snapshots (Amazon Data Lifecycle Manager)
###############################################################################

resource "aws_iam_role" "dlm" {
  count = local.backups_enabled ? 1 : 0

  name_prefix          = "${var.name}-dlm-"
  description          = "Lets Data Lifecycle Manager snapshot the ${var.name} data volume"
  permissions_boundary = var.iam_permissions_boundary_arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "dlm.${data.aws_partition.current.dns_suffix}" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "dlm" {
  count = local.backups_enabled ? 1 : 0

  role       = aws_iam_role.dlm[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "data" {
  count = local.backups_enabled ? 1 : 0

  description        = "Daily snapshots of the ${var.name} data volume"
  execution_role_arn = aws_iam_role.dlm[0].arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = local.snapshot_policy_tags

    schedule {
      name      = "daily"
      copy_tags = true

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["03:00"]
      }

      retain_rule {
        count = var.snapshot_retention_days
      }

      tags_to_add = {
        SnapshotCreator = "DLM"
      }
    }
  }

  tags = var.tags

  # The role must be usable before DLM validates it.
  depends_on = [aws_iam_role_policy_attachment.dlm]
}
