"""Wait deterministically for an isolated disposable Neo4j service."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from math import isfinite
from time import monotonic, sleep
from urllib.parse import urlparse

from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable

_DEFAULT_TIMEOUT_SECONDS = 90.0
_RETRY_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class _ReadinessConfiguration:
    uri: str
    user: str
    password: str
    timeout_seconds: float


def _usage() -> str:
    return (
        "usage: wait_for_neo4j.py --uri-env NAME --user-env NAME --password-env NAME "
        "[--timeout-seconds SECONDS]"
    )


def _parse_arguments(arguments: list[str]) -> tuple[str, str, str, float]:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--uri-env", "--user-env", "--password-env", "--timeout-seconds"}:
            raise RuntimeError(f"Unknown readiness option {option!r}; {_usage()}")
        if index + 1 >= len(arguments):
            raise RuntimeError(f"Missing value for readiness option {option!r}; {_usage()}")
        values[option] = arguments[index + 1]
        index += 2
    required_options = ("--uri-env", "--user-env", "--password-env")
    if any(option not in values for option in required_options):
        raise RuntimeError(f"Missing required readiness option; {_usage()}")
    timeout_value = values.get("--timeout-seconds", str(_DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout_seconds = float(timeout_value)
    except ValueError as exc:
        raise RuntimeError("Neo4j readiness timeout must be a positive number") from exc
    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RuntimeError("Neo4j readiness timeout must be a finite positive number")
    return (
        values["--uri-env"],
        values["--user-env"],
        values["--password-env"],
        timeout_seconds,
    )


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Required Neo4j readiness environment variable {name} is not set")
    return value


def _configuration(arguments: list[str]) -> _ReadinessConfiguration:
    uri_env, user_env, password_env, timeout_seconds = _parse_arguments(arguments)
    uri = _required_environment(uri_env)
    parsed = urlparse(uri)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("Neo4j readiness URI has an invalid port") from exc
    if (
        parsed.scheme != "bolt"
        or parsed.hostname is None
        or port != 7687
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Neo4j readiness requires a direct bolt:// host:7687 URI")
    return _ReadinessConfiguration(
        uri=uri,
        user=_required_environment(user_env),
        password=_required_environment(password_env),
        timeout_seconds=timeout_seconds,
    )


def _wait_for_neo4j(configuration: _ReadinessConfiguration) -> None:
    deadline = monotonic() + configuration.timeout_seconds
    attempts = 0
    while True:
        attempts += 1
        driver = GraphDatabase.driver(
            configuration.uri,
            auth=(configuration.user, configuration.password),
            connection_timeout=5,
        )
        try:
            driver.verify_connectivity()
        except AuthError:
            raise
        except ServiceUnavailable as exc:
            if monotonic() >= deadline:
                raise RuntimeError(
                    "Timed out waiting for isolated Neo4j Bolt readiness after "
                    f"{configuration.timeout_seconds:g} seconds and {attempts} attempts"
                ) from exc
            sleep(_RETRY_INTERVAL_SECONDS)
        else:
            print(f"Neo4j Bolt readiness confirmed after {attempts} attempt(s).")
            return
        finally:
            driver.close()


def main() -> None:
    _wait_for_neo4j(_configuration(sys.argv[1:]))


if __name__ == "__main__":
    main()
