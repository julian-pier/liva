from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


RestorePolicy = Literal["automatic", "manual", "bootstrap"]
SourceKind = Literal["file", "tree", "sqlite"]


@dataclass(frozen=True)
class RecoverySource:
    category: str
    path: Path
    kind: SourceKind
    restore_policy: RestorePolicy = "automatic"
    required: bool = False
    excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryPolicy:
    """An allowlist. New databases are faults, never implicit backup input."""

    sources: tuple[RecoverySource, ...] = field(default_factory=tuple)
    sqlite_scan_roots: tuple[Path, ...] = field(default_factory=tuple)
    known_sqlite: frozenset[Path] = field(default_factory=frozenset)

    @classmethod
    def empty(cls) -> "RecoveryPolicy":
        return cls()

    def present_sources(self) -> tuple[RecoverySource, ...]:
        missing = [item.path for item in self.sources if item.required and not item.path.exists()]
        if missing:
            raise FileNotFoundError("required recovery source missing: " + ", ".join(map(str, missing)))
        return tuple(item for item in self.sources if item.path.exists())

    def inventory_drift(self) -> list[str]:
        suffixes = (".sqlite", ".sqlite3", ".db", ".db3")
        unknown: set[Path] = set()
        for root in self.sqlite_scan_roots:
            if not root.exists():
                continue
            for candidate in root.rglob("*"):
                if not candidate.is_file() or candidate.suffix.lower() not in suffixes:
                    continue
                if any(part in {".git", "node_modules", ".venv", "venv", "backups", "cache", "Cache", "logs", "__pycache__"} for part in candidate.parts):
                    continue
                if candidate.stat().st_size == 0:
                    continue
                resolved = candidate.resolve()
                if resolved not in self.known_sqlite:
                    unknown.add(resolved)
        return [str(item) for item in sorted(unknown)]


def production_policy() -> RecoveryPolicy:
    root = Path("/opt/liva")
    home = Path("/var/lib/liva")
    project_dbs = tuple(root / "database" / name for name in (
        "auth.sqlite3", "core.sqlite3", "polar.sqlite3", "ernaehrung.sqlite3",
        "digital_activity.sqlite3", "control_center.sqlite3", "plans.sqlite3",
        "training.sqlite3", "hrv.sqlite3", "runs.sqlite3", "ernaehrung_bu.sqlite3",
    ))
    mcp_dbs = (home / ".local/share/liva-mcp-remote/auth.sqlite3", home / ".local/share/liva-mcp-remote/liva_mcp_writes.sqlite3")
    runtime_dbs = (home / ".local/state/liva-memory/memory.sqlite", home / ".local/state/liva-memory/skill-engine.sqlite", home / "memos/data/memos_prod.db", home / "n8n/n8n_data/database.sqlite", home / "liva-monitoring/uptime-kuma/kuma.db")
    webuntis_dbs = (root / "var/playwright/webuntis_profile/Default/heavy_ad_intervention_opt_out.db", root / "var/playwright/webuntis_profile/first_party_sets.db")
    sources: list[RecoverySource] = []
    sources += [RecoverySource("liva_sqlite", path, "sqlite", required=True) for path in project_dbs]
    sources += [RecoverySource("mcp_sqlite", path, "sqlite", required=True) for path in mcp_dbs]
    sources += [RecoverySource("memory_runtime_audit", path, "sqlite") for path in runtime_dbs[:2]]
    sources += [RecoverySource("container_sqlite", path, "sqlite") for path in runtime_dbs[2:]]
    # Chromium persists these profile stores as SQLite. They are classified
    # explicitly so they receive an online snapshot rather than a raw tree copy.
    sources += [RecoverySource("webuntis_state", path, "sqlite") for path in webuntis_dbs]
    sources += [
        RecoverySource("liva_memory_vault", home / "liva-memory", "tree", excludes=(".git", ".trash", ".stfolder")),
        RecoverySource("liva_memory_runtime_audit", home / ".local/state/liva-memory", "tree", excludes=("logs", "locks", "memory.sqlite*", "skill-engine.sqlite*")),
        RecoverySource("uploads", root / "uploads", "tree"),
        RecoverySource("webuntis_state", root / "var/playwright/webuntis_state.json", "file"),
        RecoverySource("webuntis_state", root / "var/playwright/webuntis_profile", "tree", excludes=("Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "DawnGraphiteCache", "GraphiteDawnCache", "Safe Browsing", "LOCK", "LOG", "LOG.old", "heavy_ad_intervention_opt_out.db", "first_party_sets.db")),
        RecoverySource("schoolsync_state", root / "var/schoolsync", "tree", excludes=("debug", "*.log")),
        RecoverySource("private_app_data", root / "data", "tree"), RecoverySource("private_app_data", root / "import", "tree"),
        RecoverySource("private_app_data", root / "strava_sync/data", "tree"), RecoverySource("private_app_data", root / "smart_home/data", "tree", excludes=("*.lock", "*.tmp*")),
        RecoverySource("container_configuration", home / "memos/compose.yml", "file"), RecoverySource("container_configuration", home / "n8n/docker-compose.yml", "file"), RecoverySource("container_configuration", home / "n8n/n8n_data/config", "file"), RecoverySource("container_configuration", home / "liva-monitoring", "tree", excludes=("cache", "kuma.db*")),
        RecoverySource("secrets_credentials", root / ".env", "file", restore_policy="bootstrap"), RecoverySource("secrets_credentials", Path("/etc/liva.env"), "file", restore_policy="bootstrap"), RecoverySource("secrets_credentials", home / ".config/liva-mcp-remote", "tree", restore_policy="bootstrap"), RecoverySource("secrets_credentials", home / ".config/rclone/rclone.conf", "file", restore_policy="bootstrap"), RecoverySource("secrets_credentials", root / "var/gcalsync", "tree", restore_policy="bootstrap"), RecoverySource("secrets_credentials", home / ".config/liva/garmin", "tree", restore_policy="bootstrap"),
        RecoverySource("syncthing_identity", home / ".local/state/syncthing", "tree", restore_policy="manual", excludes=("index-v0.14.0.db",)),
        # System units are deliberately enumerated: a recovery allowlist must never
        # turn into an accidental backup of every service installed on the host.
        *[RecoverySource("systemd_configuration", Path("/etc/systemd/system") / unit, "file", restore_policy="bootstrap") for unit in (
            "liva-daily-backup.service", "liva-daily-backup.timer",
            "liva-control-center-collector.service", "liva-control-center-collector.timer",
            "liva-full-recovery.service", "liva-full-recovery.timer",
            "liva-full-recovery-catchup.service", "liva-full-recovery-catchup.timer",
            "tailscale-funnel.service",
        )],
        RecoverySource("ssh_bootstrap", home / ".ssh", "tree", restore_policy="bootstrap"), RecoverySource("tailscale_identity", Path("/var/lib/tailscale/tailscaled.state"), "file", restore_policy="manual"), RecoverySource("tailscale_identity", Path("/var/lib/tailscale/certs"), "tree", restore_policy="manual"), RecoverySource("liva_memos_nat_cutover", Path("/usr/local/sbin/liva-memos-nat-cutover"), "file", restore_policy="bootstrap"),
    ]
    known = frozenset(path.resolve() for path in (*project_dbs, *mcp_dbs, *runtime_dbs, *webuntis_dbs))
    return RecoveryPolicy(tuple(sources), (root, home / ".local/share/liva-mcp-remote", home / ".local/state/liva-memory", home / "memos/data", home / "n8n/n8n_data", home / "liva-monitoring"), known)
