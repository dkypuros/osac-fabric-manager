"""Probes: how the conformance suites look at the components *below* the fabric manager's API.

The suites drive OSAC and, at every step, confirm that the fabric really did what OSAC was told. Those looks are
vendor-specific: on the mimic they read containerlab FRR nodes, DPU containers, libvirt and a Linux NVMe target; on
real equipment they read the switches, the DPUs, the BMC and the array. Everything the suites need to see is one of
the methods below. A vendor implements them once (`probes_<name>.py`, class `Probes`), selects it with
`OSAC_PROBES=<name>`, and the 41 + 36 steps run unchanged.

Rules for an implementation:
- read only; never change anything on the equipment
- answer quickly (a few seconds); the suite retries within each step's budget, so "not yet" is a normal answer
- raise ProbeError with a short reason when the equipment cannot be reached (the suite reports the component)
- hosts are OSAC's bare-metal host names (bm-0, bm-1 ...); VNIs are the numbers the fabric manager returned in
  its API status (l2vni / l3vni); addresses are plain strings
"""
import importlib
import os


class ProbeError(Exception):
    pass


class Probes:
    """The interface. Docstrings say exactly what each answer means and which suite steps use it."""

    name = "abstract"

    # ---- fabric -------------------------------------------------------------------------------
    def fabric_problems(self):
        """List of strings, empty when the fabric is healthy: every BGP/EVPN session that is not Established,
        written as 'node->peer:state'. Preflight P1 of both suites."""
        raise NotImplementedError

    def tenant_vnis(self):
        """Set of VNI numbers (as strings) currently configured on the fabric's tenant-facing devices (border/edge
        and every DPU), including permanent ones; the caller subtracts the permanent set. Preflight and leak check."""
        raise NotImplementedError

    def segment_present(self, l2_vni):
        """True when the L2 segment for this VNI exists on every server-facing DPU (bridge/VLAN + DHCP scope).
        Suite BM N6 after create_subnet."""
        raise NotImplementedError

    def mac_learned(self, mac, where="border"):
        """True when the fabric has learned this MAC as an EVPN type-2 route (seen at the border by default):
        the server's data NIC is really in the overlay. Suite BM BM10b, Suite CL handoff."""
        raise NotImplementedError

    # ---- DPU / server port ----------------------------------------------------------------------
    def port_binding(self, host):
        """Dict for the server's DPU data port: {'segment': <l2_vni as int or None>, 'operstate': 'UP'|'DOWN',
        'isolated': bool}. segment None = not bound to any tenant segment. Suite BM BM9/X-steps, Suite CL handoff."""
        raise NotImplementedError

    def lifecycle_port_state(self, host):
        """'down' when the server's lifecycle/provisioning path is cut while it sits in a tenant segment, 'up' when
        restored, 'n/a' when the platform has no such path. Suite CL handoff and teardown."""
        raise NotImplementedError

    # ---- border / NAT -----------------------------------------------------------------------------
    def nat_present(self, inside_ip):
        """True when the border translates some public address to this inside address (DNAT installed).
        Suite BM X2, Suite CL X2/X3."""
        raise NotImplementedError

    # ---- storage ---------------------------------------------------------------------------------
    def array_exports(self):
        """Number of volumes the array currently exports for tenants (0 when clean). Preflight and leak check."""
        raise NotImplementedError

    def host_storage_connections(self):
        """Number of NVMe-oF connections the host/DPU side still holds (0 when clean). Leak check."""
        raise NotImplementedError

    # ---- servers --------------------------------------------------------------------------------
    def server_power(self, host):
        """'on' or 'off' from the BMC's point of view. Leak check (a released server must be off)."""
        raise NotImplementedError

    # ---- evidence --------------------------------------------------------------------------------
    def snapshot(self, component, directory):
        """Write whatever helps a human debug `component` ('fabric', 'dpu', 'bmc', 'snap'/'storage_array') into
        `directory` as plain files. Called when a step fails. Never raises."""
        raise NotImplementedError


def active():
    """The probe set selected by OSAC_PROBES (default: mimic). A vendor ships probes_<name>.py next to this file."""
    name = os.environ.get("OSAC_PROBES", "mimic")
    mod = importlib.import_module(f"probes_{name}")
    return mod.Probes()
