"""
Smart SDN controller (Ryu, OpenFlow 1.3) for a two-path network.

        h1 ─┐                 ┌── s2 ──┐   (primary path, 10 Mbps)
        h2 ─┼── s1 ───────────┤        ├── s4 ── h4 (server)
        h3 ─┘                 └── s3 ──┘   (backup path, 10 Mbps)

Modes (changed at run time by run_tests.py over a UNIX socket):
  static  : every client always uses the primary path        (baseline)
  dynamic : the controller monitors the network every second and
            1. reroutes a flow to the backup path when the primary is congested
            2. reacts to loss reports sent by the server application (app -> SDN)
            3. fails over all flows when a primary link goes down (recovery)

Run:  ryu-manager --ofp-tcp-listen-port 6653 controller.py
"""
import json
import os
import time

from eventlet.green import socket as gsocket
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

HERE = os.path.dirname(os.path.abspath(__file__))
FEEDBACK_SOCK = '/tmp/sdn_feedback.sock'
FLOW_LOG = os.path.join(HERE, 'flow_stats.csv')
EVENT_LOG = os.path.join(HERE, 'events.csv')

SERVER = '10.0.0.4'
CLIENTS = ['10.0.0.1', '10.0.0.2', '10.0.0.3']

CAPACITY = 10.0            # Mbps of each core link
HIGH = 0.8 * CAPACITY      # above this the primary path counts as congested
SAFE = 0.9 * CAPACITY      # never load a path above this when moving a flow
LOSS_LIMIT = 5.0           # % loss (reported by the server) that triggers action
COOLDOWN = 3.0             # seconds between two reroutes (avoids flapping)
POLL = 1.0                 # seconds between statistics requests

# Port numbers are fixed in run_tests.py (addLink port1/port2)
S1_HOST_PORT = {'10.0.0.1': 1, '10.0.0.2': 2, '10.0.0.3': 3}
S1_UP = {'primary': 4, 'backup': 5}
S4_DOWN = {'primary': 1, 'backup': 2}
S4_SERVER_PORT = 3
PRIMARY_LINK_PORTS = {(1, 4), (2, 1), (2, 2), (4, 1)}   # (dpid, port) on primary path


class SmartController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}
        self.mode = 'dynamic'
        self.path = {c: 'primary' for c in CLIENTS}
        self.rate = {c: 0.0 for c in CLIENTS}
        self.prev = {}
        self.primary_ok = True
        self.last_move = 0.0
        with open(FLOW_LOG, 'w') as f:
            f.write('time,client,path,rate_mbps,mode\n')
        with open(EVENT_LOG, 'w') as f:
            f.write('time,event,detail\n')
        hub.spawn(self._poll_loop)
        hub.spawn(self._feedback_loop)

    # ---------------------------------------------------------------- helpers
    def log_event(self, event, detail):
        with open(EVENT_LOG, 'a') as f:
            f.write(f'{time.time():.3f},{event},{detail}\n')
        self.logger.info('[%s] %s', event.upper(), detail)

    @staticmethod
    def ip_match(dp, src=None, dst=None):
        fields = {'eth_type': 0x0800}
        if src:
            fields['ipv4_src'] = src
        if dst:
            fields['ipv4_dst'] = dst
        return dp.ofproto_parser.OFPMatch(**fields)

    @staticmethod
    def add_flow(dp, priority, match, out_port, modify=False):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        actions = [parser.OFPActionOutput(out_port)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        command = ofp.OFPFC_MODIFY_STRICT if modify else ofp.OFPFC_ADD
        dp.send_msg(parser.OFPFlowMod(datapath=dp, command=command, priority=priority,
                                      match=match, instructions=inst))

    # ------------------------------------------------------- switch connects
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        self.dps[dp.id] = dp
        self.install_base_rules(dp)
        self.logger.info('switch s%d connected', dp.id)

    def install_base_rules(self, dp):
        """Proactive IPv4 routing. Unknown traffic is dropped (no flooding on the loop)."""
        m = self.ip_match
        if dp.id == 1:
            for c, port in S1_HOST_PORT.items():
                self.add_flow(dp, 10, m(dp, dst=c), port)
            self.add_flow(dp, 10, m(dp, dst=SERVER), S1_UP['primary'])
            for c in CLIENTS:                                  # per-client path choice
                self.add_flow(dp, 20, m(dp, src=c, dst=SERVER), S1_UP[self.path[c]])
        elif dp.id in (2, 3):
            self.add_flow(dp, 10, m(dp, dst=SERVER), 2)
            for c in CLIENTS:
                self.add_flow(dp, 10, m(dp, dst=c), 1)
        elif dp.id == 4:
            self.add_flow(dp, 10, m(dp, dst=SERVER), S4_SERVER_PORT)
            for c in CLIENTS:
                self.add_flow(dp, 10, m(dp, dst=c), S4_DOWN['primary'])
                self.add_flow(dp, 20, m(dp, src=SERVER, dst=c), S4_DOWN[self.path[c]])

    # --------------------------------------------------------- path control
    def set_path(self, client, path, reason):
        if self.path[client] == path:
            return
        self.path[client] = path
        dp1, dp4 = self.dps.get(1), self.dps.get(4)
        if dp1:
            self.add_flow(dp1, 20, self.ip_match(dp1, src=client, dst=SERVER),
                          S1_UP[path], modify=True)
        if dp4:
            self.add_flow(dp4, 20, self.ip_match(dp4, src=SERVER, dst=client),
                          S4_DOWN[path], modify=True)
        self.last_move = time.time()
        self.log_event('reroute', f'{client} -> {path} ({reason})')

    def reset_all(self):
        self.primary_ok = True
        for c in CLIENTS:
            self.set_path(c, 'primary', 'reset')
        self.last_move = 0.0

    def load(self, path):
        return sum(self.rate[c] for c in CLIENTS if self.path[c] == path)

    # ------------------------------------------------------------ monitoring
    def _poll_loop(self):
        while True:
            dp = self.dps.get(1)
            if dp:
                dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(dp))
            hub.sleep(POLL)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply(self, ev):
        if ev.msg.datapath.id != 1:
            return
        now = time.time()
        with open(FLOW_LOG, 'a') as f:
            for st in ev.msg.body:
                if st.priority != 20:
                    continue
                c = st.match.get('ipv4_src')
                if c not in self.rate:
                    continue
                rate = 0.0
                if c in self.prev:
                    pb, pt = self.prev[c]
                    if now > pt:
                        rate = max(0.0, (st.byte_count - pb) * 8 / (now - pt) / 1e6)
                self.prev[c] = (st.byte_count, now)
                self.rate[c] = rate
                f.write(f'{now:.3f},{c},{self.path[c]},{rate:.3f},{self.mode}\n')
        if self.mode == 'dynamic':
            self.check_congestion()

    def check_congestion(self):
        """Mechanism 1: load balancing when the primary path is congested."""
        if not self.primary_ok or time.time() - self.last_move < COOLDOWN:
            return
        on_primary = [c for c in CLIENTS if self.path[c] == 'primary' and self.rate[c] > 0.1]
        p_load, b_load = self.load('primary'), self.load('backup')
        if p_load <= HIGH or len(on_primary) < 2:
            return
        for c in sorted(on_primary, key=lambda x: self.rate[x], reverse=True):
            if b_load + self.rate[c] <= SAFE:
                self.set_path(c, 'backup', f'congestion: primary at {p_load:.1f} Mbps')
                return

    # ----------------------------------------------- application feedback
    def on_report(self, msg):
        """Mechanism 2: the server application reports per-client loss."""
        c, loss = msg.get('client'), float(msg.get('loss', 0))
        if (self.mode != 'dynamic' or c not in self.path or not self.primary_ok
                or self.path[c] != 'primary' or loss <= LOSS_LIMIT
                or time.time() - self.last_move < COOLDOWN):
            return
        if self.load('backup') + self.rate[c] <= SAFE:
            self.set_path(c, 'backup', f'app reported {loss:.1f}% loss')

    def _feedback_loop(self):
        try:
            os.unlink(FEEDBACK_SOCK)
        except FileNotFoundError:
            pass
        sock = gsocket.socket(gsocket.AF_UNIX, gsocket.SOCK_DGRAM)
        sock.bind(FEEDBACK_SOCK)
        os.chmod(FEEDBACK_SOCK, 0o777)
        self.logger.info('feedback socket listening on %s', FEEDBACK_SOCK)
        while True:
            data = sock.recv(4096)
            try:
                msg = json.loads(data.decode())
            except ValueError:
                continue
            kind = msg.get('type')
            if kind == 'mode' and msg.get('mode') in ('static', 'dynamic'):
                self.mode = msg['mode']
                self.reset_all()
                self.log_event('mode', self.mode)
            elif kind == 'reset':
                self.reset_all()
            elif kind == 'report':
                self.on_report(msg)

    # ------------------------------------------------------ link failures
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status(self, ev):
        """Mechanism 3: fast failover when a primary link goes down."""
        msg = ev.msg
        ofp = msg.datapath.ofproto
        key = (msg.datapath.id, msg.desc.port_no)
        if key not in PRIMARY_LINK_PORTS:
            return
        down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN) or \
            bool(msg.desc.config & ofp.OFPPC_PORT_DOWN)
        if down and self.primary_ok:
            self.primary_ok = False
            self.log_event('link_down', f's{key[0]} port {key[1]}')
            if self.mode == 'dynamic':
                for c in CLIENTS:
                    self.set_path(c, 'backup', 'failover')
        elif not down and not self.primary_ok:
            self.primary_ok = True
            self.log_event('link_up', f's{key[0]} port {key[1]}')
