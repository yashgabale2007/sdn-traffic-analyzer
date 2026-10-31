"""Part 1 - Ryu learning switch that also polls flow statistics every second."""
import os, time
from ryu.app import simple_switch_13
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.lib import hub

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sdn_stats.csv')
INTERVAL = 1


class TrafficMonitor(simple_switch_13.SimpleSwitch13):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.prev = {}
        with open(LOG, 'w') as f:
            f.write('time,src,dst,packets,bytes,rate_mbps\n')
        self.poller = hub.spawn(self._poll)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif dp.id in self.datapaths:
            del self.datapaths[dp.id]

    def _poll(self):
        while True:
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(dp))
            hub.sleep(INTERVAL)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply(self, ev):
        now = time.time()
        dpid = ev.msg.datapath.id
        with open(LOG, 'a') as f:
            for st in ev.msg.body:
                if st.priority != 1:
                    continue
                src, dst = st.match.get('eth_src'), st.match.get('eth_dst')
                key = (dpid, src, dst)
                rate = 0.0
                if key in self.prev:
                    pb, pt = self.prev[key]
                    if now > pt:
                        rate = (st.byte_count - pb) * 8 / (now - pt) / 1e6
                self.prev[key] = (st.byte_count, now)
                f.write(f'{now:.3f},{src},{dst},{st.packet_count},'
                        f'{st.byte_count},{rate:.3f}\n')
