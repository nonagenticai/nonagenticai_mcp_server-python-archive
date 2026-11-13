from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from src.builtin.carebus.client import (
    ServiceClient,
    demo_backends,
    set_client,
    strata_rag,
)
from src.builtin.carebus.crm import crm_mcp
from src.builtin.carebus.delivery import delivery_mcp
from src.builtin.carebus.faq import faq_mcp
from src.builtin.carebus.order import order_mcp
from src.builtin.carebus.procedures import procedures_mcp
from src.builtin.carebus.rules import rules_mcp
from tests.carebus_helpers import Recorder, call_tool

DEMO = "http://demo.test"


@pytest.fixture
def demo():
    def install(routes):
        rec = Recorder(routes)
        set_client("demo_backends", ServiceClient(DEMO, "demo-tok", transport=rec.transport))
        return rec

    yield install
    set_client("demo_backends", None)


def test_verify_customer_returns_verified_customer(demo):
    demo(
        {
            ("GET", "/api/customers/C-10482"): (
                200,
                {"customer_id": "C-10482", "name": "Anna de Vries", "tier": "Gold"},
            )
        }
    )
    out = call_tool(crm_mcp, "verify_customer", {"customer_id": "C-10482"})
    assert out == {
        "customer_id": "C-10482",
        "name": "Anna de Vries",
        "tier": "Gold",
        "verified": True,
    }


def test_get_start_data_passes_through(demo):
    body = {
        "customer": {"customer_id": "C-10482"},
        "orders": [{"order_id": "ORD-58790"}],
        "deliveries": [
            {
                "shipment_id": "SHP-88123",
                "order_id": "ORD-58790",
                "status": "in_transit",
            }
        ],
        "payments": [],
        "communications": [],
    }
    demo({("GET", "/api/customers/C-10482/start-data"): (200, body)})
    assert call_tool(crm_mcp, "get_start_data", {"customer_id": "C-10482"}) == body


def test_get_order_and_payments(demo):
    demo(
        {
            ("GET", "/api/orders/ORD-58790"): (
                200,
                {"order_id": "ORD-58790", "shipment_id": "SHP-88123"},
            ),
            ("GET", "/api/orders/ORD-58790/payments"): (
                200,
                {
                    "order_id": "ORD-58790",
                    "payments": [
                        {"payment_id": "PAY-3398"},
                        {"payment_id": "PAY-3399"},
                    ],
                },
            ),
        }
    )
    assert (
        call_tool(order_mcp, "get_order", {"order_id": "ORD-58790"})["shipment_id"] == "SHP-88123"
    )
    assert len(call_tool(order_mcp, "get_payments", {"order_id": "ORD-58790"})["payments"]) == 2


def test_create_rma_forwards_idempotency_key_unchanged_on_repeat(demo):
    rec = demo(
        {
            ("POST", "/api/orders/ORD-58213/rmas"): (
                200,
                {"rma_id": "RMA-41007", "order_id": "ORD-58213"},
            )
        }
    )
    args = {
        "order_id": "ORD-58213",
        "reason": "too uncomfortable",
        "idempotency_key": "CASE-0002:2:rma",
    }
    first = call_tool(order_mcp, "create_rma", args)
    second = call_tool(order_mcp, "create_rma", args)
    assert first == second == {"rma_id": "RMA-41007", "order_id": "ORD-58213"}
    assert [rec.body(i) for i in range(2)] == [
        {"reason": "too uncomfortable", "idempotency_key": "CASE-0002:2:rma"}
    ] * 2


def test_create_warranty_claim_posts_defect(demo):
    rec = demo(
        {
            ("POST", "/api/orders/ORD-57102/warranty-claims"): (
                201,
                {"claim_id": "WC-7719"},
            )
        }
    )
    call_tool(
        order_mcp,
        "create_warranty_claim",
        {"order_id": "ORD-57102", "defect": "stopped working", "idempotency_key": "k1"},
    )
    assert rec.body() == {"defect": "stopped working", "idempotency_key": "k1"}


def test_refund_over_capture_is_permanent_with_backend_message(demo):
    rec = demo(
        {
            ("POST", "/api/payments/PAY-3399/refunds"): (
                409,
                {
                    "error": {
                        "code": "refund_exceeds_capture",
                        "message": "refund 500.00 exceeds captured 229.00",
                    }
                },
            )
        }
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] 409 .*exceeds captured 229\.00"):
        call_tool(
            order_mcp,
            "issue_refund",
            {"payment_id": "PAY-3399", "amount": 500.0, "idempotency_key": "k2"},
        )
    assert rec.body() == {"amount": 500.0, "idempotency_key": "k2"}


def test_track_shipment_timeout_is_retryable(demo):
    import httpx

    demo({("GET", "/api/shipments/SHP-88123"): httpx.ReadTimeout("carrier slow")})
    with pytest.raises(ToolError, match=r"^\[retryable\] timeout"):
        call_tool(delivery_mcp, "track_shipment", {"shipment_id": "SHP-88123"})


def test_side_effect_tools_require_idempotency_key(demo):
    demo({})
    with pytest.raises(ToolError):
        call_tool(order_mcp, "create_rma", {"order_id": "ORD-58213", "reason": "x"})


RAG = "http://rag.test"
API = "http://api.test"


def test_return_window_passes_order_id_query_and_result(demo):
    rec = demo(
        {
            ("GET", "/api/rules/return-window"): (
                200,
                {
                    "eligible": False,
                    "rule": "RET-01",
                    "reason": "ended",
                    "alternative": "warranty",
                },
            )
        }
    )
    out = call_tool(
        rules_mcp,
        "check_return_window",
        {"order_id": "ORD-57102", "category": "tools", "delivered_on": "2026-06-18"},
    )
    assert out["eligible"] is False and out["rule"] == "RET-01"
    # category / delivered_on are not forwarded
    assert dict(rec.requests[0].url.params) == {"order_id": "ORD-57102"}


def test_warranty_accepts_args_without_delivered_on(demo):
    rec = demo({("GET", "/api/rules/warranty"): (200, {"eligible": True, "rule": "WAR-01"})})
    out = call_tool(rules_mcp, "check_warranty", {"order_id": "ORD-57102", "category": "tools"})
    assert out["rule"] == "WAR-01"
    assert dict(rec.requests[0].url.params) == {"order_id": "ORD-57102"}


def test_payment_issue_sends_error_type(demo):
    rec = demo(
        {
            ("GET", "/api/rules/payment-issue"): (
                200,
                {
                    "eligible": True,
                    "rule": "PAY-04",
                    "refund_payment_id": "PAY-3399",
                    "refund_amount": 229.0,
                },
            )
        }
    )
    out = call_tool(
        rules_mcp,
        "check_payment_issue",
        {"order_id": "ORD-58790", "error_type": "duplicate_charge"},
    )
    assert out["refund_payment_id"] == "PAY-3399"
    assert dict(rec.requests[0].url.params) == {
        "order_id": "ORD-58790",
        "error_type": "duplicate_charge",
    }


def test_get_procedure_reads_published_from_carebus_api():
    rec = Recorder(
        {
            ("GET", "/api/procedures/return"): (
                200,
                {
                    "name": "return",
                    "version": "1.3.0",
                    "version_status": "PUBLISHED",
                    "steps": [],
                },
            )
        }
    )
    set_client("carebus_api", ServiceClient(API, "api-tok", transport=rec.transport))
    try:
        out = call_tool(procedures_mcp, "get_procedure", {"name": "return"})
    finally:
        set_client("carebus_api", None)
    assert out["version_status"] == "PUBLISHED"
    assert rec.requests[0].url.params["status"] == "PUBLISHED"


C1 = "0b6f9a0e-6a51-4a2c-9a1e-2d1f00000001"
C2 = "0b6f9a0e-6a51-4a2c-9a1e-2d1f00000002"


def test_faq_ask_calls_strata_rag_hybrid_and_shapes_citations():
    # Shape copied from strata_rag src/ask.rs AskResponse: citations are chunk UUIDs, details live in results.
    # timings_ms is flattened from query::Timings plus generate (AskTimings #[serde(flatten)]).
    ask = {
        "answer": "Refunds go back within 5 business days.",
        "citations": [C1],
        "results": [
            {
                "chunk_id": C1,
                "document_id": "d-17",
                "store_path": ["care", "policies", "returns-policy"],
                "ord": 3,
                "text": "long chunk",
                "page_start": 2,
                "page_end": 3,
                "source": "returns-policy.pdf",
                "title": "Returns policy",
                "scores": {"dense": 0.8},
            },
            {
                "chunk_id": C2,
                "document_id": "d-18",
                "store_path": ["care", "logistics", "shipping-faq"],
                "text": "other chunk",
                "scores": {"dense": 0.4},
            },
        ],
        "timings_ms": {
            "embed": 38,
            "route": 410,
            "search": 22,
            "rerank": 300,
            "generate": 1300,
            "total": 2100,
        },
    }
    rec = Recorder({("POST", "/api/ask"): (200, ask)})
    set_client("strata_rag", ServiceClient(RAG, "rag-tok", transport=rec.transport))
    try:
        out = call_tool(faq_mcp, "ask", {"question": "How long does a refund take?"})
    finally:
        set_client("strata_rag", None)
    assert rec.body() == {
        "query": "How long does a refund take?",
        "route_mode": "hybrid",
        "top_k": 8,
    }
    assert out["answer"].startswith("Refunds go back")
    assert out["citations"] == [C1]
    assert out["sources"] == [
        {
            "chunk_id": C1,
            "document_id": "d-17",
            "store_path": ["care", "policies", "returns-policy"],
            "title": "Returns policy",
            "source": "returns-policy.pdf",
            "page_start": 2,
            "page_end": 3,
        }
    ]
    assert "routing" not in out and "text" not in str(out["sources"])
    assert out["timings_ms"]["generate"] == 1300 and out["timings_ms"]["total"] == 2100


L1 = "7f1c9a2e-0000-4000-8000-0000000000a1"
L2 = "7f1c9a2e-0000-4000-8000-0000000000a2"


def test_faq_ask_forwards_a_store_filter_and_confirms_it():
    # care-bus confines the customer chat to the stores outside every legal-matter wall: strata_rag
    # searches exactly those leaves, and the answer says which filter was applied.
    rec = Recorder({("POST", "/api/ask"): (200, {"answer": "a", "citations": [], "results": []})})
    set_client("strata_rag", ServiceClient(RAG, "rag-tok", transport=rec.transport))
    try:
        out = call_tool(faq_mcp, "ask", {"question": "q", "leaf_ids": [L1, L2]})
    finally:
        set_client("strata_rag", None)
    assert rec.body() == {"query": "q", "route_mode": "hybrid", "top_k": 8, "leaf_ids": [L1, L2]}
    assert out["scope"] == {"leaf_ids": [L1, L2]}


def test_faq_ask_without_a_filter_has_no_scope():
    rec = Recorder({("POST", "/api/ask"): (200, {"answer": "a", "citations": [], "results": []})})
    set_client("strata_rag", ServiceClient(RAG, "rag-tok", transport=rec.transport))
    try:
        out = call_tool(faq_mcp, "ask", {"question": "q"})
    finally:
        set_client("strata_rag", None)
    assert "leaf_ids" not in rec.body()
    assert "scope" not in out


@pytest.mark.parametrize(
    "bad",
    [[], ["not-a-uuid"], [L1] * 257],
)
def test_faq_ask_refuses_a_bad_filter_before_strata_rag(bad):
    rec = Recorder({("POST", "/api/ask"): (200, {"answer": "a", "citations": [], "results": []})})
    set_client("strata_rag", ServiceClient(RAG, "rag-tok", transport=rec.transport))
    try:
        with pytest.raises(ToolError, match=r"^\[permanent\] leaf_ids"):
            call_tool(faq_mcp, "ask", {"question": "q", "leaf_ids": bad})
    finally:
        set_client("strata_rag", None)
    assert rec.requests == []


def test_faq_client_uses_faq_timeout_not_the_default(monkeypatch):
    monkeypatch.setenv("STRATA_RAG_URL", RAG)
    monkeypatch.setenv("FAQ_TIMEOUT_S", "18")
    set_client("strata_rag", None)
    set_client("demo_backends", None)
    monkeypatch.setenv("DEMO_BACKENDS_URL", "http://demo.test")
    try:
        assert strata_rag().timeout == 18.0
        assert demo_backends().timeout == 2.5
    finally:
        set_client("strata_rag", None)
        set_client("demo_backends", None)


def test_missing_env_url_is_permanent(monkeypatch):
    monkeypatch.delenv("STRATA_RAG_URL", raising=False)
    set_client("strata_rag", None)
    with pytest.raises(ToolError, match=r"^\[permanent\] STRATA_RAG_URL is not set"):
        call_tool(faq_mcp, "ask", {"question": "x"})


# --- path-id validation (final review item 1: path traversal) -----------------------------------

_BAD_IDS = ["../admin/reset?", "..", ".", "a/b", "x?y", "", "%2e%2e", "a b", "x" * 500]

_PATH_TOOLS = [
    (crm_mcp, "verify_customer", "customer_id", {}),
    (crm_mcp, "get_start_data", "customer_id", {}),
    (order_mcp, "get_order", "order_id", {}),
    (order_mcp, "get_payments", "order_id", {}),
    (order_mcp, "create_rma", "order_id", {"reason": "r", "idempotency_key": "k"}),
    (
        order_mcp,
        "create_warranty_claim",
        "order_id",
        {"defect": "d", "idempotency_key": "k"},
    ),
    (order_mcp, "issue_refund", "payment_id", {"amount": 1.0, "idempotency_key": "k"}),
    (delivery_mcp, "track_shipment", "shipment_id", {}),
    (procedures_mcp, "get_procedure", "name", {}),
]


@pytest.mark.parametrize("bad", _BAD_IDS, ids=lambda b: repr(b[:20]))
@pytest.mark.parametrize("server,tool,field,extra", _PATH_TOOLS, ids=[t[1] for t in _PATH_TOOLS])
def test_path_ids_are_validated_before_any_request(server, tool, field, extra, bad):
    rec = Recorder({})
    client = ServiceClient(DEMO, "tok", transport=rec.transport)
    set_client("demo_backends", client)
    set_client("carebus_api", client)
    try:
        with pytest.raises(ToolError, match=rf"^\[permanent\] invalid {field}") as ei:
            call_tool(server, tool, {field: bad, **extra})
    finally:
        set_client("demo_backends", None)
        set_client("carebus_api", None)
    assert rec.requests == []
    assert len(str(ei.value)) < 200  # huge input is not echoed back


def test_normal_ids_still_reach_the_expected_path(demo):
    rec = demo({("GET", "/api/orders/ORD-58790"): (200, {"order_id": "ORD-58790"})})
    out = call_tool(order_mcp, "get_order", {"order_id": "ORD-58790"})
    assert out == {"order_id": "ORD-58790"}
    assert rec.requests[0].url.raw_path == b"/api/orders/ORD-58790"
