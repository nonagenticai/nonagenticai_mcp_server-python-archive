"""Synthetic HTTP-only image acceptance scripts; never shipped as a runtime mode."""

AUTHORITY = r"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        info = {'active': payload.get('token') == 'synthetic-gateway' and payload.get('app') == 'mcp',
                'tenant_id': '10000000-0000-7000-8000-000000000001',
                'license_id': '10000000-0000-7000-8000-000000000003',
                'subject_id': 'synthetic-image-operator', 'subject_kind': 'tenant', 'app': {'key': 'mcp'},
                'expires_at': None, 'token_expires_at': None}
        body = json.dumps(info).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""

ACCEPTANCE = r"""
import hashlib, json, urllib.request
from datetime import datetime, UTC, timedelta
from uuid import uuid7
base = 'http://127.0.0.1:8033'
tenant = '10000000-0000-7000-8000-000000000001'
license_id = '10000000-0000-7000-8000-000000000003'
admin = {'X-Tenant-Id': 'Bearer synthetic-gateway', 'X-MCP-Admin-Key': 'a' * 32}
def request(path, payload=None, headers=admin):
    data = None if payload is None else json.dumps(payload).encode()
    with urllib.request.urlopen(urllib.request.Request(base+path, data=data,
            headers={'Content-Type': 'application/json', **headers}), timeout=10) as response:
        raw = response.read().decode()
        if response.headers.get('Content-Type', '').startswith('text/event-stream'):
            assert 'Mcp-Session-Id' not in response.headers
            return json.loads(next(line[6:] for line in raw.splitlines() if line.startswith('data: ')))
        return json.loads(raw)
tool, toolkit, profile = [str(uuid7()) for _ in range(3)]
files = [{'path': 'main.py', 'content': "raise RuntimeError('uploaded code must never run')\n"}]
digest = hashlib.sha256(json.dumps([[f['path'],f['content']] for f in files],
    ensure_ascii=False, separators=(',',':')).encode()).hexdigest()
schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
manifest = {'tool_id': tool, 'tenant_id': tenant, 'version': 1, 'public_name': 'image_tool', 'operation': 'image.probe',
    'description': 'Synthetic image fixture', 'entrypoint': 'main:run',
    'input_schema': schema, 'result_schema': schema, 'context_schema': schema,
    'package_sha256': digest, 'runner_image_digest': 'registry.example/runner@sha256:'+'a'*64,
    'effect': 'read_only'}
created = request('/mcp/tools', {'manifest': manifest, 'files': files})
assert created['status'] == 'draft'
path = '/mcp/tools/'+tool+'/versions/1'
assert request(path+'/validate', {})['execution'] == 'unavailable'
assert request(path+'/publish', {'expected_revision': 1})['status'] == 'published'
request('/mcp/credential-profiles', {'profile_id': profile, 'tenant_id': tenant,
    'name': 'image_profile', 'gateway': {'service': 'mcp', 'alias': 'synthetic_gateway',
    'license_id': license_id, 'app_key': 'mcp'}})
request('/mcp/toolkits', {'toolkit_id': toolkit, 'tenant_id': tenant, 'version': 1,
    'name': 'image_toolkit', 'description': 'Synthetic',
    'tools': [{'tool': {'tenant_id': tenant, 'id': tool, 'version': 1}}]})
request('/mcp/toolkits/'+toolkit+'/versions/1/publish', {'expected_revision': 1})
binding = request('/mcp/bindings', {'credential_profile_id': profile,
    'toolkits': [{'id': toolkit, 'version': 1}], 'context': {'tenant_id': tenant},
    'expires_at': (datetime.now(UTC)+timedelta(minutes=10)).isoformat()})
agent = {'X-Tenant-Id': 'Bearer synthetic-gateway', 'X-MCP-Binding': binding['token'],
    'Accept': 'application/json, text/event-stream'}
initialized = request('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
    'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
    'clientInfo': {'name': 'image-acceptance', 'version': '1'}}}, agent)
assert initialized['result']['capabilities'] == {'tools': {'listChanged': False}}
listed = request('/mcp/', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, agent)
assert listed['result']['tools'][0]['inputSchema'] == schema
called = request('/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
    'params': {'name': 'image_tool', 'arguments': {}}}, agent)
assert called['result']['isError'] and called['result']['content'][0]['text'] == 'execution_unavailable'
print('authenticated authoring, publication, discovery and unavailable dispatch verified')
"""
