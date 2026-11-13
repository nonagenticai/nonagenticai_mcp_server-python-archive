"""Validated catalog settings. No implicit database or execution configuration."""

import re
from pathlib import Path
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, PositiveInt, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Product -> toolkit names a tenant of that product is bound to.
#: Toolkit names follow the catalog Name pattern (underscores). A toolkit the tenant has not
#: published yet is skipped by bindings/ensure with a reported reason, never an error.
DEFAULT_PRODUCT_TOOLKITS: dict[str, tuple[str, ...]] = {
    # fr: the platform fr_project toolkit (start_fr_project); fr acts as agent:<tenant> (§3.2).
    "agent": ("coding", "knowledge", "cases", "rules", "fr"),
    "care-bus": ("care_bus_ops", "knowledge", "research", "legal"),
}
#: Product -> licences bundle key the minter names explicitly (``bundle-token?bundle=``), so a
#: tenant holding several bundles that contain the same backend app never gets 409
#: ``ambiguous_bundle``. Every product in the toolkit table needs an entry.
DEFAULT_PRODUCT_BUNDLES: dict[str, str] = {"agent": "agent", "care-bus": "care-bus"}
#: The reserved platform tenant that publishes first-party toolkits. A tenant's binding may pin
#: published versions of its own toolkits or of this tenant's, never another tenant's. Migration
#: 0008 carries the same constant (``public.mcp_platform_tenant()``) for the database check; the
#: two must match, so changing it is a migration, not a deployment setting.
PLATFORM_TENANT_ID = UUID("01900000-0000-7000-8000-00000000f001")
PRODUCT_PATTERN = r"^[a-z][a-z0-9-]{0,47}$"
BUNDLE_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
TOOLKIT_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


def checked_url(value: str, allow_http: bool) -> str:
    from urllib.parse import urlsplit

    url = urlsplit(value)
    if (
        url.scheme not in ({"https", "http"} if allow_http else {"https"})
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or url.query
    ):
        raise ValueError("authority URLs require HTTPS or explicitly approved internal HTTP")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_", extra="ignore", frozen=True, populate_by_name=True
    )

    database_url: SecretStr = Field(validation_alias="DATABASE_URL")
    pool_min_size: Annotated[int, Field(ge=0, le=20)] = 1
    pool_max_size: Annotated[int, Field(ge=1, le=100)] = 10
    pool_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 3.0
    pool_max_waiting: PositiveInt = 32
    #: Idle/lifetime bounds that recycle a pooled connection before pgbouncer or postgres can
    #: close it underneath us; a connection lost anyway is a transient error the worker retries.
    pool_max_idle_seconds: Annotated[float, Field(ge=1, le=3600)] = 60.0
    pool_max_lifetime_seconds: Annotated[float, Field(ge=60, le=86400)] = 1800.0
    pool_reconnect_timeout_seconds: Annotated[float, Field(ge=1, le=300)] = 30.0
    database_statement_timeout_ms: PositiveInt = 5000
    health_timeout_seconds: Annotated[float, Field(gt=0, le=10)] = 3.0
    max_package_files: PositiveInt = 64
    max_package_file_bytes: PositiveInt = 262144
    max_package_bytes: PositiveInt = 1048576
    max_schema_bytes: PositiveInt = 65536
    max_schema_depth: PositiveInt = 16
    max_schema_nodes: PositiveInt = 1024
    max_schema_references: PositiveInt = 32
    max_validation_work: PositiveInt = 100000
    max_instance_bytes: PositiveInt = 1048576
    max_instance_nodes: PositiveInt = 10000
    max_instance_depth: PositiveInt = 32
    approved_runner_images: tuple[str, ...] = ()
    # Omitted authentication settings keep protected work disabled. No bypass exists.
    license_introspect_url: str | None = Field(
        default=None, validation_alias="LICENSE_INTROSPECT_URL"
    )
    license_app_key: str | None = None
    license_allow_http: bool = False
    license_cache_ttl_seconds: Annotated[int, Field(ge=0, le=300)] = Field(
        default=30, validation_alias="LICENSE_CACHE_TTL_SECONDS"
    )
    license_cache_max_entries: PositiveInt = 1024
    license_timeout_seconds: Annotated[float, Field(gt=0, le=10)] = 3.0
    license_max_response_bytes: Annotated[int, Field(gt=0, le=1048576)] = 65536
    license_expiry_skew_seconds: Annotated[int, Field(ge=0, le=30)] = 2
    admin_key_file: Path | None = None
    admin_tenant_ids: frozenset[UUID] = frozenset()
    platform_admin_key_file: Path | None = None
    platform_database_url: SecretStr | None = None
    max_management_bytes: Annotated[int, Field(gt=0, le=1048576)] = 262144
    protocol_input_bytes: Annotated[int, Field(gt=0, le=1048576)] = 262144
    protocol_output_bytes: Annotated[int, Field(gt=0, le=8388608)] = 2097152
    protocol_max_inflight: Annotated[int, Field(gt=0, le=256)] = 32
    protocol_timeout_seconds: Annotated[float, Field(gt=0, le=360)] = 15
    # Builtin provider callers (care-bus): main had no transport bound; its tools bound
    # themselves (care-bus 2.5 s, faq 18 s, research up to 90 s). Outer guard only.
    builtin_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 120
    catalog_page_size: Annotated[int, Field(gt=0, le=100)] = 10
    session_ttl_seconds: Annotated[int, Field(gt=0, le=86400)] = 3600
    # Acceptance is an operator gate. Merely installing runtime code cannot enable it.
    execution_enabled: bool = False
    worker_probe_port: Annotated[int, Field(ge=1024, le=65535)] = 8034
    #: Work one worker process does at once. Kata: local controller tasks (also bounded by
    #: ``execution_global_slots``). External driver: runs being *dispatched* right now — a
    #: ``running`` external run costs a poll, never one of these slots.
    worker_max_inflight: Annotated[int, Field(gt=0, le=128)] = 8
    execution_global_slots: Annotated[int, Field(gt=0, le=128)] = 1
    execution_per_tenant_slots: Annotated[int, Field(gt=0, le=32)] = 1
    queue_global_max: Annotated[int, Field(gt=0, le=10000)] = 100
    queue_per_tenant_max: Annotated[int, Field(gt=0, le=1000)] = 20
    run_queue_timeout_seconds: Annotated[int, Field(gt=0, le=600)] = 240
    run_start_timeout_seconds: Annotated[int, Field(gt=0, le=120)] = 30
    run_max_seconds: Annotated[int, Field(gt=0, le=300)] = 60
    sync_wait_seconds: Annotated[float, Field(ge=0, le=300)] = 0
    lease_seconds: Annotated[int, Field(ge=10, le=300)] = 30
    heartbeat_seconds: Annotated[int, Field(gt=0, le=60)] = 5
    reconcile_seconds: Annotated[int, Field(gt=0, le=60)] = 10
    api_max_inflight: Annotated[int, Field(gt=0, le=100)] = 8
    api_concurrency_global: Annotated[int, Field(gt=0, le=100)] = 8
    api_concurrency_per_service: Annotated[int, Field(gt=0, le=100)] = 8
    api_max_calls_per_minute: Annotated[int, Field(gt=0, le=10000)] = 60
    api_rate_limits_file: Path | None = None
    api_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30
    api_max_response_bytes: Annotated[int, Field(gt=0, le=10485760)] = 262144
    execution_retention_days: Annotated[int, Field(ge=1, le=365)] = 7
    max_api_calls_per_run: Annotated[int, Field(ge=0, le=100)] = 10
    max_frame_bytes: Annotated[int, Field(gt=0, le=1048576)] = 65536
    max_result_bytes: Annotated[int, Field(gt=0, le=1048576)] = 262144
    max_output_bytes: Annotated[int, Field(gt=0, le=4194304)] = 1048576
    max_log_bytes: Annotated[int, Field(gt=0, le=65536)] = 8192
    max_frames: Annotated[int, Field(gt=0, le=1000)] = 256
    credential_bundle_file: Path | None = None
    # Runtime tool sources (mcp_sources): AES-256-GCM key for stored static tokens (32 bytes,
    # base64url) and the SSRF allow-list (exact hosts, "*.suffix", or "host:port" for a non-default
    # port; comma-separated in the environment). An empty list allows no source host.
    source_secret_key: SecretStr | None = None
    source_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()
    source_cache_ttl_seconds: Annotated[int, Field(ge=0, le=60)] = 5
    # Tenant tool builder (docs/TOOL_BUILDER.md). Off until an operator enables it; the quotas
    # are per tenant and counted under a per-tenant advisory lock. Env: MCP_BUILDER_<NAME>.
    builder_enabled: bool = False
    builder_max_sources: Annotated[int, Field(ge=1, le=1000)] = 20
    builder_max_operations: Annotated[int, Field(ge=1, le=10000)] = 300
    builder_max_tools: Annotated[int, Field(ge=1, le=1000)] = 50
    builder_max_toolkits: Annotated[int, Field(ge=1, le=1000)] = 10
    builder_spec_max_bytes: Annotated[int, Field(gt=0, le=67108864)] = 5 * 1024 * 1024
    #: Stored OpenAPI documents per tenant, summed over all its sources (last 3 kept per source).
    builder_spec_max_total_bytes_per_tenant: Annotated[int, Field(gt=0, le=1073741824)] = (
        25 * 1024 * 1024
    )
    builder_spec_fetches_per_hour: Annotated[int, Field(ge=1, le=10000)] = 30
    builder_test_runs_per_hour: Annotated[int, Field(ge=1, le=10000)] = 60
    builder_source_calls_per_minute: Annotated[int, Field(ge=1, le=100000)] = 60
    builder_source_concurrency: Annotated[int, Field(ge=1, le=100)] = 2
    builder_allowed_ports: Annotated[tuple[int, ...], NoDecode] = (443,)
    #: Extra CIDRs tenant egress must refuse (cluster/pod/service ranges), beyond non-global IPs.
    builder_deny_cidrs: Annotated[tuple[str, ...], NoDecode] = ()
    builder_oauth_token_max_ttl: Annotated[int, Field(ge=30, le=86400)] = 3600
    run_namespace: str = "mcp-runs"
    kata_runtime_class: str | None = None
    runner_image_digest: str | None = None
    run_memory_request_mi: Annotated[int, Field(ge=32, le=1024)] = 256
    run_memory_limit_mi: Annotated[int, Field(ge=32, le=1024)] = 256
    run_cpu_request: Annotated[int, Field(ge=10, le=1000)] = 250
    run_cpu_limit: Annotated[int, Field(ge=10, le=1000)] = 250
    # MCP tasks extension (io.modelcontextprotocol/tasks).
    # Off until cutover: tools/call keeps returning the run envelope.
    tasks_enabled: bool = False
    task_poll_interval_ms: Annotated[int, Field(ge=500, le=60000)] = 2000
    # External runtimes (n8n_workflow, fr_project): driven by src.external.driver, never Kata.
    external_poll_seconds: Annotated[float, Field(ge=1, le=300)] = 5
    #: Deadline for one external run (workflows wait on human approvals); run_max_seconds is Kata's.
    external_run_max_seconds: Annotated[int, Field(ge=60, le=2592000)] = 7 * 86400
    #: Total external runs one driver process may own (``starting``/``running``/``cancelling``).
    #: A load guard, not a dispatch limit: a project that runs for days is one poll per interval.
    external_active_max: Annotated[int, Field(gt=0, le=10000)] = 100
    #: Adapter calls (start, poll) in flight at once in the single replica's event loop.
    external_concurrency: Annotated[int, Field(gt=0, le=256)] = 32
    external_probe_port: Annotated[int, Field(ge=1024, le=65535)] = 8036
    n8n_base_url: str | None = None
    n8n_webhook_base_url: str | None = None
    n8n_api_key: SecretStr | None = None
    #: HS256 key: every workflow start carries ``Authorization: Bearer <JWT>`` (2-min expiry) signed
    #: with it; the n8n Webhook node checks it with a JWT Auth credential holding the same key.
    #: The key itself never travels. Unset: no header (unauthenticated webhook).
    n8n_webhook_secret: SecretStr | None = None
    n8n_unknown_status_limit: Annotated[int, Field(ge=1, le=1000)] = 10
    fr_base_url: str | None = None
    fr_channel_secret: SecretStr | None = None
    #: Signal thresholds for the task activity feed (``_meta`` "ai.nonagentic/fr"): an approval
    #: that has waited this long raises ``approval_waiting``; a phase that has not moved for this
    #: long raises ``phase_stalled``. Both are advisory: they never change the run.
    fr_approval_wait_seconds: Annotated[int, Field(ge=60, le=86400)] = Field(
        default=900, validation_alias="FR_APPROVAL_WAIT_SECONDS"
    )
    fr_phase_stall_seconds: Annotated[int, Field(ge=60, le=604800)] = Field(
        default=3600, validation_alias="FR_PHASE_STALL_SECONDS"
    )
    # POST /mcp/internal/tenants/{t}/bindings/ensure: Keycloak service clients (azp) allowed to
    # provision bindings. Empty disables the route (503). The JWT is validated by
    # src.auth.keycloak with the MCP_INTERNAL_KEYCLOAK_* config, whose allowed clients must include these.
    binding_ensure_clients: frozenset[str] = frozenset()
    # Verifier for /mcp/internal/* ONLY (separate from KEYCLOAK_*, which serves /mcp). Audience and
    # allowed clients are comma-separated like their KEYCLOAK_* counterparts. All four or none;
    # binding_ensure_clients must be a subset of the allowed clients.
    internal_keycloak_url: str | None = None
    internal_keycloak_realm: str | None = None
    internal_keycloak_audience: str | None = None
    internal_keycloak_allowed_clients: str | None = None
    binding_products: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: dict(DEFAULT_PRODUCT_TOOLKITS)
    )
    binding_ensure_ttl_days: Annotated[int, Field(ge=1, le=3650)] = 365
    product_bundles: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_PRODUCT_BUNDLES))
    platform_tenant_id: UUID = PLATFORM_TENANT_ID
    # Broker credentials minted per tenant from licences (bundle-token?app=<backend>) as the
    # Keycloak client licence_minter_client_id. Off by default; the file resolver stays.
    licence_minter_enabled: bool = False
    licence_minter_url: str | None = None
    licence_minter_token_url: str | None = None
    licence_minter_client_id: str = "mcp-bundle-minter"
    licence_minter_secret_file: Path | None = None
    licence_minter_ttl_minutes: Annotated[int, Field(ge=1, le=60)] = 15
    licence_minter_skew_seconds: Annotated[int, Field(ge=0, le=300)] = 30

    @field_validator("source_allowed_hosts", mode="before")
    @classmethod
    def split_hosts(cls, value):
        if isinstance(value, str):
            value = [v for v in value.split(",")]
        hosts = tuple(str(v).strip().lower() for v in value if str(v).strip())
        pattern = r"(?:\*\.)?[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?"
        if len(hosts) > 256 or any(not re.fullmatch(pattern, h) for h in hosts):
            raise ValueError("invalid source host allow-list entry")
        return hosts

    @field_validator("builder_allowed_ports", mode="before")
    @classmethod
    def split_ports(cls, value):
        if isinstance(value, str):
            value = [v for v in value.split(",") if v.strip()]
        ports = tuple(int(v) for v in value)
        if not 1 <= len(ports) <= 16 or any(not 1 <= p <= 65535 for p in ports):
            raise ValueError("invalid builder port allow-list")
        return ports

    @field_validator("builder_deny_cidrs", mode="before")
    @classmethod
    def split_cidrs(cls, value):
        import ipaddress

        if isinstance(value, str):
            value = [v for v in value.split(",") if v.strip()]
        cidrs = tuple(str(ipaddress.ip_network(str(v).strip(), strict=False)) for v in value)
        if len(cidrs) > 64:
            raise ValueError("too many builder deny CIDRs")
        return cidrs

    @field_validator("source_secret_key")
    @classmethod
    def source_key_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            from src.sources.crypto import decode_key

            decode_key(value.get_secret_value())
        return value

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        from urllib.parse import urlsplit

        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or not parsed.path.strip("/"):
            raise ValueError("an explicit PostgreSQL database URL is required")
        return value

    @model_validator(mode="after")
    def consistent_limits(self) -> Self:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool minimum exceeds maximum")
        if self.max_package_file_bytes > self.max_package_bytes:
            raise ValueError("file limit exceeds package limit")
        if bool(self.license_introspect_url) != bool(self.license_app_key):
            raise ValueError(
                "license introspection URL and application key must be configured together"
            )
        if self.license_introspect_url:
            from urllib.parse import urlsplit

            url = urlsplit(self.license_introspect_url)
            if (
                url.scheme not in ({"https", "http"} if self.license_allow_http else {"https"})
                or not url.hostname
                or url.username
                or url.password
                or url.fragment
                or url.query
            ):
                raise ValueError(
                    "introspection requires HTTPS or explicitly approved internal HTTP"
                )
        if self.admin_key_file and (not self.license_app_key or not self.admin_tenant_ids):
            raise ValueError(
                "management requires license authentication and an explicit tenant allowlist"
            )
        if self.platform_admin_key_file and not self.admin_key_file:
            raise ValueError("platform authorization requires management authorization")
        if self.platform_database_url and not self.platform_admin_key_file:
            raise ValueError("platform database access requires platform management authorization")
        if self.heartbeat_seconds * 3 >= self.lease_seconds:
            raise ValueError("lease must exceed three heartbeat intervals")
        if self.execution_per_tenant_slots > self.execution_global_slots:
            raise ValueError("tenant slots exceed global slots")
        if self.queue_per_tenant_max > self.queue_global_max:
            raise ValueError("tenant queue exceeds global queue")
        if self.max_frame_bytes > self.max_output_bytes:
            raise ValueError("frame limit exceeds aggregate output limit")
        if self.sync_wait_seconds >= self.protocol_timeout_seconds:
            raise ValueError("sync wait must be below the transport timeout")
        if (
            self.run_memory_request_mi > self.run_memory_limit_mi
            or self.run_cpu_request > self.run_cpu_limit
        ):
            raise ValueError("runner requests exceed limits")
        if self.run_namespace != "mcp-runs":
            raise ValueError("execution requires the dedicated mcp-runs namespace")
        if self.execution_enabled and (not self.approved_runner_images or not self.license_app_key):
            raise ValueError("execution requires approved images and license authentication")

        if len(self.binding_products) > 64 or any(
            not re.fullmatch(PRODUCT_PATTERN, product)
            or not 1 <= len(toolkits) <= 32
            or len(set(toolkits)) != len(toolkits)
            or any(not re.fullmatch(TOOLKIT_NAME_PATTERN, name) for name in toolkits)
            for product, toolkits in self.binding_products.items()
        ):
            raise ValueError("invalid product toolkit table")
        if any(
            product not in self.product_bundles
            or not re.fullmatch(BUNDLE_PATTERN, self.product_bundles[product])
            for product in self.binding_products
        ):
            raise ValueError("every product needs a licences bundle key")
        if self.platform_tenant_id.version != 7:
            raise ValueError("the platform tenant identity must be UUIDv7")
        if any(not 1 <= len(c) <= 256 for c in self.binding_ensure_clients):
            raise ValueError("invalid binding ensure client")
        internal = (
            self.internal_keycloak_url,
            self.internal_keycloak_realm,
            self.internal_keycloak_audience,
            self.internal_keycloak_allowed_clients,
        )
        if any(internal):
            if not all(v and v.strip() for v in internal):
                raise ValueError("internal Keycloak settings must all be set together")
            checked_url(self.internal_keycloak_url or "", self.license_allow_http)
            if not self.internal_clients or not self.internal_audiences:
                raise ValueError("internal Keycloak audience and clients must not be empty")
        if self.binding_ensure_clients and not self.binding_ensure_clients <= self.internal_clients:
            raise ValueError("binding ensure clients must be internal Keycloak allowed clients")
        if self.binding_ensure_clients and not self.license_app_key:
            raise ValueError("binding provisioning requires license authentication")
        if self.builder_enabled and not self.license_app_key:
            raise ValueError("the tool builder requires license authentication")
        if self.licence_minter_enabled:
            if (
                not self.licence_minter_url
                or not self.licence_minter_token_url
                or not self.licence_minter_secret_file
                or not self.licence_minter_client_id
            ):
                raise ValueError(
                    "licence minting requires the licences URL, token URL and client secret file"
                )
            checked_url(self.licence_minter_url, self.license_allow_http)
            checked_url(self.licence_minter_token_url, self.license_allow_http)
        return self

    @property
    def internal_clients(self) -> frozenset[str]:
        raw = self.internal_keycloak_allowed_clients or ""
        return frozenset(v.strip() for v in raw.split(",") if v.strip())

    @property
    def internal_audiences(self) -> tuple[str, ...]:
        raw = self.internal_keycloak_audience or ""
        return tuple(v.strip() for v in raw.split(",") if v.strip())

    @property
    def dsn(self) -> str:
        return self.database_url.get_secret_value().replace(
            "postgresql+psycopg://", "postgresql://", 1
        )
