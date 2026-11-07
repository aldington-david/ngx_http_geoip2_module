"""Bounded loopback checks; fixtures contain no production configuration or credentials."""
import http.client
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time

nginx, modules, database, runtime = map(Path, sys.argv[1:])
runtime.mkdir(parents=True, exist_ok=True)
(runtime / "logs").mkdir(exist_ok=True)
cert, key = runtime / "test.crt", runtime / "test.key"
subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-nodes", "-days", "1", "-subj", "/CN=tls.test", "-addext", "subjectAltName=DNS:tls.test",
                "-out", str(cert), "-keyout", str(key)], check=True, capture_output=True, timeout=5)
server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
server_tls.load_cert_chain(cert, key)
client_tls = ssl.create_default_context(cafile=str(cert))


class TCP(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        line, source = self.rfile.readline(4096), b"direct"
        if line.startswith(b"PROXY "):
            source = line.split()[2]
            line = self.rfile.readline(4096)
        self.wfile.write(b"ok|" + source + b"|" + line)


class UDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        sock.sendto(data, self.client_address)


class TLS(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        with server_tls.wrap_socket(self.request, server_side=True) as conn:
            data = conn.recv(4096)
            conn.sendall(b"tls|" + data)


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


servers = [TCPServer(("127.0.0.1", 18100), TCP),
           socketserver.ThreadingUDPServer(("127.0.0.1", 18101), UDP),
           TCPServer(("127.0.0.1", 18102), TLS)]
for server in servers:
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

config = runtime / "nginx.conf"
config.write_text(f'''
load_module "{modules / 'ngx_http_geoip2_module.so'}";
load_module "{modules / 'ngx_stream_geoip2_module.so'}";
worker_processes 1;
pid "{runtime / 'nginx.pid'}";
error_log "{runtime / 'error.log'}" notice;
events {{ worker_connections 128; }}
stream {{
    geoip2 "{database}" {{ auto_reload 1s; $country default=NA country iso_code; }}
    map $country $geo_backend {{ GB 127.0.0.1:18100; default 127.0.0.1:18199; }}
    map $ssl_preread_server_name $tls_backend {{ tls.test 127.0.0.1:18102; default 127.0.0.1:18199; }}
    server {{ listen 127.0.0.1:18083 proxy_protocol reuseport; set_real_ip_from 127.0.0.1;
              proxy_pass $geo_backend; proxy_protocol on; }}
    server {{ listen 127.0.0.1:18084 reuseport; ssl_preread on; proxy_pass $tls_backend; }}
    server {{ listen 127.0.0.1:18085-18086 reuseport; proxy_pass 127.0.0.1:18100; }}
    server {{ listen 127.0.0.1:18085-18086 udp reuseport; proxy_pass 127.0.0.1:18101; }}
}}
http {{
    access_log off;
    geoip2 "{database}" {{ auto_reload 1s; $http_country default=NA source=$arg_ip country iso_code; }}
    server {{ listen 127.0.0.1:18090; location / {{ return 200 "$http_country"; }} }}
    server {{ listen 127.0.0.1:18080; server_name cert.test; return 444; }}
    server {{ listen 127.0.0.1:18080; server_name redirect.test; return 301 https://$host$request_uri; }}
    server {{ listen 127.0.0.1:18080 default_server; server_name _; return 444; }}
    server {{ listen 127.0.0.1:18088 ssl; ssl_reject_handshake on; }}
}}
''')
command = [str(nginx), "-p", str(runtime) + "/", "-c", str(config)]
process = None
try:
    subprocess.run(command + ["-t"], check=True, timeout=3)
    process = subprocess.Popen(command + ["-g", "daemon off;"])
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Nginx exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", 18090), timeout=0.1):
                break
        except OSError:
            time.sleep(0.02)
    else:
        raise RuntimeError("Nginx did not become ready within two seconds")

    for ip, country in [("81.2.69.160", b"GB"), ("2001:218::", b"JP"), ("127.0.0.1", b"NA")]:
        conn = http.client.HTTPConnection("127.0.0.1", 18090, timeout=2)
        conn.request("GET", "/?ip=" + ip)
        response = conn.getresponse()
        assert response.status == 200 and response.read() == country
        conn.close()

    for host in ("cert.test", "unknown.test"):
        conn = http.client.HTTPConnection("127.0.0.1", 18080, timeout=2)
        conn.request("GET", "/check", headers={"Host": host})
        try:
            conn.getresponse()
            raise AssertionError("Expected connection closure for " + host)
        except http.client.RemoteDisconnected:
            pass
        finally:
            conn.close()
    conn = http.client.HTTPConnection("127.0.0.1", 18080, timeout=2)
    conn.request("GET", "/check", headers={"Host": "redirect.test"})
    response = conn.getresponse()
    assert response.status == 301 and response.getheader("Location") == "https://redirect.test/check"
    conn.close()

    for port in (18085, 18086):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            conn.sendall(b"ping\n")
            assert conn.makefile("rb").readline() == b"ok|direct|ping\n"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as conn:
            conn.settimeout(2)
            conn.sendto(b"udp-ping", ("127.0.0.1", port))
            assert conn.recv(4096) == b"udp-ping"
    with socket.create_connection(("127.0.0.1", 18083), timeout=2) as conn:
        conn.sendall(b"PROXY TCP4 81.2.69.160 127.0.0.1 12345 18083\r\nping\n")
        assert conn.makefile("rb").readline() == b"ok|81.2.69.160|ping\n"
    with socket.create_connection(("127.0.0.1", 18084), timeout=2) as raw:
        with client_tls.wrap_socket(raw, server_hostname="tls.test") as conn:
            conn.sendall(b"sni-ping")
            assert conn.recv(4096) == b"tls|sni-ping"
    for port, host in [(18084, "unknown.test"), (18088, "tls.test")]:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            try:
                with client_tls.wrap_socket(raw, server_hostname=host):
                    raise AssertionError("Expected TLS rejection")
            except (ssl.SSLError, ConnectionResetError):
                pass
    print("PASS: IPv4/IPv6 GeoIP2, stream GeoIP2/realip/PROXY, TCP/UDP ranges, SNI, TLS rejection, three site behaviors")
finally:
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    for server in servers:
        server.shutdown()
        server.server_close()
    if (runtime / "error.log").exists():
        print((runtime / "error.log").read_text()[-4000:])
