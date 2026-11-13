from __future__ import annotations

from fastmcp import FastMCP

from src.builtin.carebus import mount_carebus
from tests.carebus_helpers import list_tool_names

CONTRACT = {
    "crm_verify_customer",
    "crm_get_start_data",
    "order_get_order",
    "order_get_payments",
    "order_create_rma",
    "order_create_warranty_claim",
    "order_issue_refund",
    "delivery_track_shipment",
    "faq_ask",
    "rules_check_return_window",
    "rules_check_warranty",
    "rules_check_payment_issue",
    "procedures_get_procedure",
}


def test_mounted_names_equal_the_contract_exactly():
    root = FastMCP(name="test-root")
    mount_carebus(root)
    assert list_tool_names(root) == CONTRACT


def test_builtin_server_mounts_carebus():
    # Guards against mount_carebus being defined but never called by the builtin provider.
    import inspect

    from src import builtin

    assert "mount_carebus(server)" in inspect.getsource(builtin.create_builtin_server)
    assert list_tool_names(builtin.create_builtin_server()) >= CONTRACT
