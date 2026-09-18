"""Read-only offline probes. Results cannot authorize an experiment."""

import hashlib
import os
import platform
import stat
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from containment.deployment import (
    Asset,
    AssetManifest,
    DeploymentConfig,
    check_assets_for_profile,
    configuration_digest,
    required_live_checks,
)


def verify_asset(root: Path, asset: Asset) -> dict:
    """Reject traversal, symlinks, devices and FIFOs; read only the declared file size.

    The asset root is operator-selected. dir_fd and O_NOFOLLOW protect every path
    component beneath it even if an untrusted asset changes during verification.
    Hashing is an observation at this time, not protection against later mutation.
    """
    try:
        with ExitStack() as stack:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            parts = asset.path.split("/")
            for part in parts[:-1]:
                directory = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                stack.callback(os.close, directory)
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            stack.callback(os.close, descriptor)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size != asset.size_bytes:
                raise ValueError("Expected regular file with declared size")
            hasher = hashlib.sha256()
            remaining = asset.size_bytes
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError("Asset truncated while reading")
                hasher.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or os.read(descriptor, 1):
                raise ValueError("Asset changed while reading")
            if hasher.hexdigest() != asset.sha256:
                raise ValueError("Asset digest mismatch")
        return {"id": f"asset:{asset.path}", "status": "pass", "detail": "Size and digest match"}
    except (OSError, ValueError) as exc:
        return {"id": f"asset:{asset.path}", "status": "fail", "detail": str(exc)}


def preflight(config: DeploymentConfig, assets: AssetManifest, root: Path) -> dict:
    check_assets_for_profile(config, assets)
    checks = [verify_asset(root, asset) for asset in assets.assets]
    checks.append(
        {
            "id": "python_runtime",
            "status": "pass" if sys.version_info >= (3, 12) else "fail",
            "detail": platform.python_version(),
        }
    )
    checks.append(
        {
            "id": "non_root_process",
            "status": "pass" if os.geteuid() != 0 else "fail",
            "detail": "Local process identity only; not a workload-identity attestation",
        }
    )
    checks.extend(
        {"id": name, "status": "unverified", "detail": "Requires trusted live AWS checks"}
        for name in required_live_checks(config)
    )
    return {
        "schema_version": 1,
        "configuration_digest": configuration_digest(config, assets),
        "observed_at": datetime.now(UTC).isoformat(),
        "readiness": "blocked",
        "execution_authorized": False,
        "checks": checks,
    }
