"""web_scrape: SSRF guard on the LLM-supplied url, and the webscraper /api/scrape/url contract."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from src.builtin.legal.client import PublicClient
from src.builtin.web import mount_web, scrape
from src.builtin.web.scrape import set_client, web_mcp
from tests.carebus_helpers import call_tool, list_tool_names
from tests.legal_helpers import Routes, json_answer

SCRAPED = {
    "url": "https://example.com/a",
    "final_url": "https://www.example.com/a",
    "title": "Example",
    "text_content": "Hello world. " * 10,
    "markdown_content": "# Hello",
    "status": "completed",
    "error": None,
}


@pytest.fixture
def public_dns(monkeypatch):
    async def resolve(host: str) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(scrape, "_resolve", resolve)


@pytest.fixture
def scraper(public_dns, monkeypatch):
    monkeypatch.delenv("WEBSCRAPER_TOKEN", raising=False)

    def install(answer) -> Routes:
        rec = Routes({("POST", "/api/scrape/url"): answer})
        set_client(PublicClient("http://webscraper.test:9119", transport=rec.transport))
        return rec

    yield install
    set_client(None)


def test_mounted_name_is_web_scrape():
    root = FastMCP(name="web-root")
    mount_web(root)
    assert list_tool_names(root) == {"web_scrape"}


def test_scrape_calls_webscraper_and_maps(scraper):
    rec = scraper(json_answer(200, SCRAPED))
    out = call_tool(web_mcp, "scrape", {"url": "https://example.com/a", "max_chars": 500})
    assert out == {
        "url": "https://example.com/a",
        "final_url": "https://www.example.com/a",
        "title": "Example",
        "text": SCRAPED["text_content"],
        "truncated": False,
    }
    body = json.loads(rec.last.content)
    assert body == {"url": "https://example.com/a", "scraper_type": "auto", "timeout": 30.0}
    assert "x-tenant-id" not in rec.last.headers


def test_scrape_sends_licence_token_when_set(scraper, monkeypatch):
    rec = scraper(json_answer(200, SCRAPED))
    monkeypatch.setenv("WEBSCRAPER_TOKEN", "lic-abc")
    call_tool(web_mcp, "scrape", {"url": "https://example.com/a"})
    assert rec.last.headers["x-tenant-id"] == "Bearer lic-abc"


def test_scrape_truncates(scraper):
    scraper(json_answer(200, {**SCRAPED, "text_content": "x" * 5000}))
    out = call_tool(web_mcp, "scrape", {"url": "https://example.com/a", "max_chars": 1000})
    assert out["truncated"] is True and len(out["text"]) == 1000


def test_scrape_failed_status_is_permanent(scraper):
    scraper(json_answer(200, {**SCRAPED, "status": "failed", "error": "403 from site"}))
    with pytest.raises(ToolError, match=r"^\[permanent\] scrape failed: 403 from site"):
        call_tool(web_mcp, "scrape", {"url": "https://example.com/a"})


def test_scrape_5xx_is_retryable(scraper):
    scraper(json_answer(500, {"detail": "Scraping failed"}))
    with pytest.raises(ToolError, match=r"^\[retryable\] 500"):
        call_tool(web_mcp, "scrape", {"url": "https://example.com/a"})


def test_scrape_timeout_is_retryable(scraper):
    scraper(httpx.ReadTimeout("slow"))
    with pytest.raises(ToolError, match=r"^\[retryable\] timeout"):
        call_tool(web_mcp, "scrape", {"url": "https://example.com/a"})


def test_missing_webscraper_url_is_permanent(public_dns, monkeypatch):
    monkeypatch.delenv("WEBSCRAPER_URL", raising=False)
    set_client(None)
    with pytest.raises(ToolError, match=r"^\[permanent\] WEBSCRAPER_URL is not set"):
        call_tool(web_mcp, "scrape", {"url": "https://example.com/a"})


_BLOCKED = [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "javascript:alert(1)",
    "http://127.0.0.1/",
    "http://127.0.0.1:9119/api/scrape/url",
    "http://10.42.0.5:8033/mcp",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://0.0.0.0/",
    "http://100.64.0.1/",  # tailnet CGNAT range
    "http://localhost/",
    "http://LOCALHOST./",
    "http://webscraper/",
    "http://2130706433/",
    "http://blocked.svc.cluster.local:9119/",
    "http://blocked.internal/",
    "http://printer.local/",
    "http://user:pw@example.com/",  # pragma: allowlist secret
    "http://example.com:22/",
    "http:///nohost",
    "https://example.com:99999/",
    "",
]


@pytest.mark.parametrize("url", _BLOCKED)
def test_ssrf_urls_are_rejected_before_any_request(scraper, url):
    rec = scraper(json_answer(200, SCRAPED))
    with pytest.raises(ToolError, match=r"^\[permanent\] url"):
        call_tool(web_mcp, "scrape", {"url": url})
    assert rec.requests == []


@pytest.mark.parametrize("resolved", [["127.0.0.1"], ["93.184.215.14", "10.0.0.7"], []])
def test_hostname_resolving_to_private_address_is_rejected(scraper, monkeypatch, resolved):
    rec = scraper(json_answer(200, SCRAPED))

    async def resolve(host: str) -> list[str]:
        return resolved

    monkeypatch.setattr(scrape, "_resolve", resolve)
    with pytest.raises(ToolError, match=r"^\[permanent\] url not allowed"):
        call_tool(web_mcp, "scrape", {"url": "https://rebind.example.com/"})
    assert rec.requests == []


def test_unresolvable_host_is_rejected(scraper, monkeypatch):
    rec = scraper(json_answer(200, SCRAPED))

    async def resolve(host: str) -> list[str]:
        raise OSError("nodename nor servname provided")

    monkeypatch.setattr(scrape, "_resolve", resolve)
    with pytest.raises(ToolError, match=r"^\[permanent\] url not allowed \(host does not resolve"):
        call_tool(web_mcp, "scrape", {"url": "https://nx.example.com/"})
    assert rec.requests == []


def test_public_ip_literal_is_allowed(scraper):
    rec = scraper(json_answer(200, SCRAPED))
    call_tool(web_mcp, "scrape", {"url": "http://93.184.215.14/"})
    assert len(rec.requests) == 1
