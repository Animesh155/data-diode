"""
opcua_server.py — OPC UA PLC simulator (TCP/4840).

OPC UA is the modern ICS protocol the Modbus/S7 generation is being
replaced by: typed address space, sessions, optional TLS.

Security mode is controlled by the OPCUA_SECURITY env var:
  - "none"             → SecurityPolicy None, AuthAnonymous (default)
  - "basic256sha256"   → Basic256Sha256 + Sign&Encrypt, certificate auth

The two-mode design lets the protocol-diversity lab module flip the
server's security posture between exercises without rebuilding the
image. See lab_protocols/opcua-secure/{up.sh, down.sh}.
"""

import asyncio
import logging
import os
import subprocess
from pathlib import Path

from asyncua import Server, ua
from asyncua.crypto.permission_rules import SimpleRoleRuleset

from process_sim import ProcessSim, TAGS


logging.basicConfig(format="[OPCUA-PLC] %(message)s", level=logging.INFO)
log = logging.getLogger()


ENDPOINT = "opc.tcp://0.0.0.0:4840/lab/"
NAMESPACE_URI = "http://lab.diode/plc"
UPDATE_INTERVAL = 1.0

SECURITY_MODE = os.environ.get("OPCUA_SECURITY", "none").lower()

CERT_DIR = Path("/app/certs")
SERVER_KEY = CERT_DIR / "server-key.pem"
SERVER_CERT = CERT_DIR / "server-cert.pem"
SERVER_CERT_DER = CERT_DIR / "server-cert.der"


def ensure_server_cert():
    """Generate a self-signed server cert if missing. Idempotent."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if SERVER_CERT_DER.exists() and SERVER_KEY.exists():
        log.info("Reusing existing server cert at %s", SERVER_CERT_DER)
        return
    log.info("Generating self-signed server cert in %s", CERT_DIR)
    subprocess.check_call([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(SERVER_KEY),
        "-out", str(SERVER_CERT),
        "-days", "365", "-nodes",
        "-subj", "/CN=plc-opcua/O=Lab/C=IN",
        "-addext", "subjectAltName=DNS:plc-opcua,URI:urn:lab:plc-opcua",
        "-addext", "keyUsage=digitalSignature,nonRepudiation,keyEncipherment,dataEncipherment,keyCertSign",
        "-addext", "extendedKeyUsage=serverAuth,clientAuth",
    ], stderr=subprocess.DEVNULL)
    subprocess.check_call([
        "openssl", "x509", "-outform", "der",
        "-in", str(SERVER_CERT), "-out", str(SERVER_CERT_DER),
    ])
    log.info("Cert generated. Public DER at %s — copy to clients.", SERVER_CERT_DER)


async def main():
    log.info("=" * 60)
    log.info("  WATER TREATMENT PLANT — OPC UA simulator")
    log.info("  Security mode: %s", SECURITY_MODE)
    log.info("=" * 60)

    server = Server()
    await server.init()
    server.set_endpoint(ENDPOINT)
    server.set_server_name("Lab Process PLC (OPC UA)")
    server.set_application_uri("urn:lab:plc-opcua")

    if SECURITY_MODE == "basic256sha256":
        ensure_server_cert()
        await server.load_certificate(str(SERVER_CERT))
        await server.load_private_key(str(SERVER_KEY))
        server.set_security_policy([
            ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
        ])
        # No anonymous auth in secure mode — clients must present a cert.
        server.set_security_IDs(["Basic256Sha256"])
        log.info("Endpoint requires Basic256Sha256 + Sign&Encrypt + cert")
    else:
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        log.info("Endpoint is anonymous, cleartext (SecurityPolicy=None)")

    idx = await server.register_namespace(NAMESPACE_URI)
    log.info("Namespace %s registered as ns=%d", NAMESPACE_URI, idx)

    container = await server.nodes.objects.add_object(idx, "ProcessVariables")

    variables = []
    for name, _, _, _ in TAGS:
        # String NodeId — ns=2;s=<name>. The whole pedagogical point of
        # OPC UA in this lab is human-readable browse paths, so the
        # server explicitly hands out string identifiers rather than
        # auto-assigned numeric ones.
        # Browse name must be qualified with the same namespace as the NodeId.
        # A bare string here would default to ns=0, leaving the tag findable by
        # NodeId but NOT by the ns=2 browse path the lab exercises use.
        node = await container.add_variable(
            ua.NodeId(name, idx),
            ua.QualifiedName(name, idx),
            0.0,
            ua.VariantType.Double,
        )
        await node.set_writable()
        variables.append((name, node))

    sim = ProcessSim()

    async with server:
        log.info("OPC UA server up at %s — %d variables", ENDPOINT, len(variables))
        log.info("=" * 60)
        tick = 0
        while True:
            readings = sim.tick()
            for (name, node), (_, value, _) in zip(variables, readings):
                await node.write_value(value)
            tick += 1
            if tick % 10 == 0:
                summary = ", ".join(f"{n}={v:.1f}" for n, v, _ in readings[:3])
                log.info("tick %d — %s ...", tick, summary)
            await asyncio.sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
