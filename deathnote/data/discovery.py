from __future__ import annotations
import socket
import ssl
import struct
from urllib.parse import urlsplit
import h2.config
import h2.connection
import h2.events
import requests

from deathnote.protocol.decode import SERVER_FIELDS, fields


def grpc_empty(base_url, method, client_version):
    """Use the same public empty unary requests as the client's startup flow."""
    host = urlsplit(base_url).hostname
    context = ssl.create_default_context()
    context.set_alpn_protocols(['h2'])
    connection = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True, header_encoding='utf-8'))
    headers = [(':method', 'POST'), (':scheme', 'https'), (':authority', host), (':path', method),
               ('content-type', 'application/grpc'), ('te', 'trailers'),
               ('x-client-version', client_version), ('user-agent', 'grpc-dotnet/2.64.0')]
    body = bytearray()
    response = {}
    with socket.create_connection((host, 443), timeout=25) as sock, context.wrap_socket(sock, server_hostname=host) as tls:
        if tls.selected_alpn_protocol() != 'h2':
            raise RuntimeError(f'{host} did not negotiate HTTP/2 for the public gRPC bootstrap API')
        connection.initiate_connection()
        connection.send_headers(1, headers)
        connection.send_data(1, b'\0\0\0\0\0', end_stream=True)
        tls.sendall(connection.data_to_send())
        ended = False
        while not ended:
            data = tls.recv(65535)
            if not data:
                raise ConnectionError('gRPC connection closed before the response completed')
            for event in connection.receive_data(data):
                if isinstance(event, (h2.events.ResponseReceived, h2.events.TrailersReceived)):
                    response.update(event.headers)
                elif isinstance(event, h2.events.DataReceived):
                    body.extend(event.data)
                    connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    ended = True
                elif isinstance(event, (h2.events.StreamReset, h2.events.ConnectionTerminated)):
                    raise ConnectionError(str(event))
            pending = connection.data_to_send()
            if pending:
                tls.sendall(pending)
    if response.get(':status') in ('401', '403') or response.get('grpc-status') in ('7', '16'):
        raise PermissionError(f'API requires authorization: {base_url}{method}')
    if response.get(':status') != '200' or response.get('grpc-status') != '0':
        raise RuntimeError(f'gRPC request failed: {response}')
    if len(body) < 5:
        raise ValueError('gRPC response is missing its message frame')
    compressed, size = struct.unpack_from('>BI', body)
    if compressed or size != len(body) - 5:
        raise ValueError('Unsupported gRPC compression or invalid message frame length')
    return bytes(body[5:]), {'headers': response, 'host': host, 'method': method}


def discover(package, region):
    payload, servers_headers = grpc_empty(package['bootstrapUrl'], '/app.playerlogin.PlayerLoginService/GetServerList', package['versionName'])
    servers = [{SERVER_FIELDS[n]: value.decode('utf-8') for n, value in fields(entry) if n in SERVER_FIELDS}
               for n, entry in fields(payload) if n == 1]
    selected = next((server for server in servers if server['name'] == region), None)
    if selected is None:
        raise ValueError(f'Region {region!r} was not found. Available regions: ' + ', '.join(s['name'] for s in servers))
    api = selected['apiServerRoots'].split('|')[0]
    payload, version_headers = grpc_empty(api, '/app.masterdata.MasterdataService/Version', package['versionName'])
    values = dict(fields(payload))
    version = {'masterVersion': values[1].decode(), 'resourceVersion': values[2].decode(), 'region': region,
               'cdnRoot': selected['cdnRoots'].split('|')[0], 'apiRoot': api, 'clientVersion': package['versionName']}
    version['masterManifestUrl'] = version['cdnRoot'] + '/master/' + version['masterVersion'] + '/MasterManifest.json'
    return version, servers, {'servers': servers_headers, 'version': version_headers}


def get_bytes(url):
    response = requests.get(url, timeout=(20, 60))
    if response.status_code in (401, 403):
        raise PermissionError(f'Resource requires authorization: {url}')
    response.raise_for_status()
    return response.content
