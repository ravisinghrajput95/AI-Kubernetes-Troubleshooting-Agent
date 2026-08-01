"""Operator commands for agent identity.

    python -m app.agentctl issue-token --cluster prod-eu-1
    python -m app.agentctl list --cluster prod-eu-1
    python -m app.agentctl revoke --cluster prod-eu-1 --reason "node compromised"
    python -m app.agentctl revoke --serial 3f9a1c... --reason "rotated early"
    python -m app.agentctl ca --out ca.crt

Deliberately a CLI and not an HTTP endpoint. Minting a credential that enrols a
cluster is the most sensitive operation the platform has, and the platform has
no authentication yet (`SECURITY.md`, F13). An endpoint for it would be an
unauthenticated token-minting service; a CLI is protected by whatever protects
shell access to the deployment, which is a great deal more.

It reads the same configuration the gateway does, so it writes to the same
place the gateway will read — Postgres when `DATABASE_URL` is set, otherwise
the file under `AGENT_IDENTITY_DIR`.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import settings
from app.security.enrolment import (
    DEFAULT_TOKEN_TTL,
    EnrolmentStore,
    FileEnrolmentStore,
    set_enrolment_store,
)
from app.security.identity import IdentityError, require_cluster_id


def build_store() -> EnrolmentStore:
    """The same store the gateway will use, chosen the same way."""
    if settings.distributed_state:
        from app.persistence.agent_identity import PostgresEnrolmentStore
        from app.persistence.postgres import Database

        database = Database(settings.database_url, min_size=1, max_size=2)
        database.migrate()
        store: EnrolmentStore = PostgresEnrolmentStore(database)
    else:
        store = FileEnrolmentStore(Path(settings.agent_identity_dir) / "enrolment.json")
    set_enrolment_store(store)
    return store


def _authority():
    from app.security.ca import CertificateAuthority

    certificate_path, key_path = settings.agent_ca_paths
    return CertificateAuthority.load_or_create(
        Path(certificate_path), Path(key_path), settings.agent_trust_domain
    )


def issue_token(arguments: argparse.Namespace) -> int:
    store = build_store()
    cluster = require_cluster_id(arguments.cluster)
    ttl = timedelta(minutes=arguments.ttl_minutes)
    token = store.issue_token(cluster, ttl)

    certificate_path, _ = settings.agent_ca_paths
    endpoint = settings.agent_gateway_advertise or "<gateway-host>:<port>"
    enrolment_port = settings.agent_enrolment_port or (settings.agent_gateway_port + 1)

    # Printed once. The platform stores only a SHA-256 digest and cannot show
    # it again, which is the point.
    print(f"Bootstrap token for cluster {cluster!r} (single use, valid {arguments.ttl_minutes}m):")
    print()
    print(f"  {token}")
    print()
    print("Enrol the agent with:")
    print()
    print(
        f"  agent --cluster {cluster} \\\n"
        f"        --enrol <gateway-host>:{enrolment_port} \\\n"
        f"        --gateway {endpoint} \\\n"
        f"        --ca-file {certificate_path} \\\n"
        f"        --bootstrap-token {token}"
    )
    print()
    print(
        f"Copy {certificate_path} to the agent host. Without --ca-file the "
        f"agent trusts the gateway on first use and pins the CA it is handed, "
        f"which is weaker."
    )
    return 0


def revoke(arguments: argparse.Namespace) -> int:
    store = build_store()
    if arguments.serial:
        if store.revoke_certificate(arguments.serial, arguments.reason):
            print(f"Revoked certificate {arguments.serial}.")
        else:
            print(f"No live certificate with serial {arguments.serial}.", file=sys.stderr)
            return 1
    else:
        count = store.revoke_cluster(require_cluster_id(arguments.cluster), arguments.reason)
        print(f"Revoked {count} certificate(s) for cluster {arguments.cluster!r}.")
        if not count:
            return 1

    print(
        "The gateway drops any live stream using a revoked certificate within "
        f"{settings.agent_revocation_sweep_seconds:.0f}s."
    )
    return 0


def show(arguments: argparse.Namespace) -> int:
    store = build_store()
    cluster = arguments.cluster or ""
    now = datetime.now(UTC)

    certificates = store.certificates(cluster)
    print(f"Certificates ({len(certificates)}):")
    if not certificates:
        print("  none")
    for record in certificates:
        if record.revoked:
            state = f"revoked — {record.revoked_reason or 'no reason given'}"
        elif record.expires_at <= now:
            state = "expired"
        else:
            state = f"valid for {(record.expires_at - now).days}d"
        print(f"  {record.serial}  {record.cluster_id}  {state}")

    tokens = store.tokens(cluster)
    live = [token for token in tokens if not token.spent and token.expires_at > now]
    print()
    print(f"Bootstrap tokens ({len(tokens)} total, {len(live)} still usable):")
    if not tokens:
        print("  none")
    for record in tokens:
        if record.spent:
            state = "spent"
        elif record.expires_at <= now:
            state = "expired"
        else:
            state = f"usable for {int((record.expires_at - now).total_seconds() // 60)}m"
        # The digest prefix, never the token: the platform does not hold it.
        print(f"  {record.token_hash[:12]}…  {record.cluster_id}  {state}")
    return 0


def show_ca(arguments: argparse.Namespace) -> int:
    bundle = _authority().ca_bundle_pem()
    if arguments.out:
        Path(arguments.out).write_bytes(bundle)
        print(f"Wrote the CA bundle to {arguments.out}.")
    else:
        sys.stdout.write(bundle.decode("utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.agentctl", description="Agent identity administration."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    issue = commands.add_parser("issue-token", help="Mint a single-use bootstrap token.")
    issue.add_argument("--cluster", required=True, help="Cluster the token may enrol.")
    issue.add_argument(
        "--ttl-minutes",
        type=int,
        default=int(DEFAULT_TOKEN_TTL.total_seconds() // 60),
        help="How long the token stays usable.",
    )
    issue.set_defaults(handler=issue_token)

    revocation = commands.add_parser("revoke", help="Revoke a certificate, or a cluster's.")
    target = revocation.add_mutually_exclusive_group(required=True)
    target.add_argument("--cluster", help="Revoke every live certificate for this cluster.")
    target.add_argument("--serial", help="Revoke one certificate by serial.")
    revocation.add_argument("--reason", default="", help="Recorded with the revocation.")
    revocation.set_defaults(handler=revoke)

    listing = commands.add_parser("list", help="Show certificates and tokens.")
    listing.add_argument("--cluster", default="", help="Limit to one cluster.")
    listing.set_defaults(handler=show)

    authority = commands.add_parser("ca", help="Print the CA bundle agents must trust.")
    authority.add_argument("--out", default="", help="Write to this file instead of stdout.")
    authority.set_defaults(handler=show_ca)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except IdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
