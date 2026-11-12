"""Static checks for the optional nginx spike route; not a live nginx test."""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]


def test_spike_route_is_default_off_and_resolves_at_request_time() -> None:
    config = (_ROOT / "services/nginx/nginx.conf").read_text(encoding="utf-8")
    spike = config.split("    location /api/spike/v1/ {", 1)[1].split(
        "    location /api/ {", 1
    )[0]
    assert 'set $rust_spike_enabled "${RUST_SPIKE_ENABLED}";' in spike
    assert 'if ($rust_spike_enabled != "1") {\n            return 404;\n        }' in spike
    assert "resolver 127.0.0.11" in spike
    assert "set $rust_spike_upstream rust-read:3002;" in spike
    assert "rewrite ^/api/spike/v1/(.*)$ /v1/$1 break;" in spike
    assert "proxy_pass         http://$rust_spike_upstream;" in spike
    assert "upstream rust-read" not in config


def test_template_substitution_does_not_replace_nginx_runtime_variables() -> None:
    config = (_ROOT / "services/nginx/nginx.conf").read_text(encoding="utf-8")
    assert re.findall(r"\$\{[^}]*\}", config) == ["${RUST_SPIKE_ENABLED}"]
    for path in ("docker-compose.yml", ".docker/staging/docker-compose.yml"):
        compose = (_ROOT / path).read_text(encoding="utf-8")
        web = compose.split("  web:", 1)[1].split("  ingestion-worker:", 1)[0]
        assert "RUST_SPIKE_ENABLED: ${RUST_SPIKE_ENABLED:-0}" in web
        # Compose requires "$$" for a literal "$"; the container sees "^RUST_SPIKE_ENABLED$".
        assert "NGINX_ENVSUBST_FILTER: '^RUST_SPIKE_ENABLED$$'" in web
        assert "/etc/nginx/templates/default.conf.template:ro" in web
        assert "/etc/nginx/conf.d/default.conf" not in web


def test_existing_api_and_mcp_locations_keep_their_original_proxy_targets() -> None:
    config = (_ROOT / "services/nginx/nginx.conf").read_text(encoding="utf-8")
    assert "location /api/app/ {\n        return 404;" in config
    assert "location /api/oauth2/ {\n        proxy_pass         http://api;" in config
    assert "location /api/ {\n        proxy_pass         http://api/;" in config
    assert "location = /mcp {\n        proxy_pass         http://api/mcp;" in config
    assert "proxy_pass         http://frontend2;" in config
