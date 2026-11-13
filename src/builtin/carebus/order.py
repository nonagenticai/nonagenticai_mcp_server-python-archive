"""order tools: mounted under namespace "order". Side-effect tools forward idempotency_key unchanged."""

from __future__ import annotations

from fastmcp import FastMCP

from .client import demo_backends, path_id

order_mcp = FastMCP(name="carebus-order")


@order_mcp.tool()
async def get_order(order_id: str) -> dict:
    """Get one order with lines, status and shipment id."""
    return await demo_backends().get(f"/api/orders/{path_id('order_id', order_id)}")


@order_mcp.tool()
async def get_payments(order_id: str) -> dict:
    """All payments captured for an order."""
    return await demo_backends().get(f"/api/orders/{path_id('order_id', order_id)}/payments")


@order_mcp.tool()
async def create_rma(order_id: str, reason: str, idempotency_key: str) -> dict:
    """Create a return authorisation. Idempotent per idempotency_key."""
    return await demo_backends().post(
        f"/api/orders/{path_id('order_id', order_id)}/rmas",
        {"reason": reason, "idempotency_key": idempotency_key},
    )


@order_mcp.tool()
async def create_warranty_claim(order_id: str, defect: str, idempotency_key: str) -> dict:
    """Register a warranty claim and book a pickup. Idempotent per idempotency_key."""
    return await demo_backends().post(
        f"/api/orders/{path_id('order_id', order_id)}/warranty-claims",
        {"defect": defect, "idempotency_key": idempotency_key},
    )


@order_mcp.tool()
async def issue_refund(payment_id: str, amount: float, idempotency_key: str) -> dict:
    """Refund one payment in full or in part. Idempotent per idempotency_key."""
    return await demo_backends().post(
        f"/api/payments/{path_id('payment_id', payment_id)}/refunds",
        {"amount": amount, "idempotency_key": idempotency_key},
    )
