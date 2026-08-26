"""POSIX-independent CLI argument contract for CRM-deal graph discovery."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse graph-discovery arguments without importing artifact filesystem code."""
    parser = argparse.ArgumentParser(
        prog="python -m src.crm_deal_identity_repair_control",
        description="Seal staging-only graph discovery for historical Bitrix CRM-deal repair.",
    )
    parser.add_argument("inventory", nargs="?", choices=("inventory",), default="inventory")
    parser.add_argument("--repair-id", required=True)
    parser.add_argument("--source-contract-uuid", required=True)
    parser.add_argument("--source-system", default="bitrix_chat")
    parser.add_argument(
        "--representative-replay-limit",
        "--negative-control-limit",
        dest="representative_replay_limit",
        type=int,
        default=100,
    )
    parser.add_argument("--retention-days", type=int, default=30)
    arguments = parser.parse_args(argv)
    if arguments.retention_days < 1:
        parser.error("--retention-days must be positive")
    if arguments.representative_replay_limit < 1:
        parser.error("--representative-replay-limit must be positive")
    return arguments
