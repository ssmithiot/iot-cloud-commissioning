from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import hash_gateway_token
from app.database import SessionLocal
from app.models import EdgeNode, GatewayCredential


DEFAULT_SCOPES = ["edge:heartbeat", "edge:jobs"]


def generate_token() -> tuple[str, str]:
    token_prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return token_prefix, f"iotcc_gw_{token_prefix}_{secret}"


def create_gateway_credential(db: Session, gateway_id: str, name: str | None = None) -> str:
    token_prefix, raw_token = generate_token()
    token_hash = hash_gateway_token(raw_token)

    edge_node = db.scalar(select(EdgeNode).where(EdgeNode.gateway_id == gateway_id))
    if edge_node is None:
        raise SystemExit(f"Gateway not found: {gateway_id}")

    credential = GatewayCredential(
        gateway_id=gateway_id,
        token_prefix=token_prefix,
        token_hash=token_hash,
        name=name,
        scopes=DEFAULT_SCOPES,
    )
    db.add(credential)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise

    return raw_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a gateway API credential.")
    parser.add_argument("gateway_id")
    parser.add_argument("--label", help="Friendly credential name")
    args = parser.parse_args()

    with SessionLocal() as db:
        raw_token = create_gateway_credential(db, args.gateway_id, name=args.label)

    print("Gateway API token. Store it securely; it will not be shown again.")
    print(raw_token)


if __name__ == "__main__":
    main()
