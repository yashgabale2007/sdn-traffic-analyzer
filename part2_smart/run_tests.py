#!/usr/bin/env python3
"""
Builds the two-path topology and runs every test scenario twice:
once with the STATIC controller (baseline) and once with the DYNAMIC one.

  h1,h2,h3 --100M-- s1 ==10M== s2 ==10M== s4 --100M-- h4 (server)
                     \\==10M== s3 ==10M==//
Usage:
  sudo python3 run_tests.py                       # all scenarios, both modes
  sudo python3 run_tests.py --modes dynamic --scenarios link_failure
"""
import argparse
import csv
import json
import os
import socket
import time
from functools import partial

from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, 'results')
FEEDBACK_SOCK = '/tmp/sdn_feedback.sock'
CLIENT_HOSTS = ['h1', 'h2', 'h3']

SCENARIOS = {
    # three clients together offer 15 Mbps, more than one 10 Mbps path
    'congestion':   {'rates': {'h1': 6, 'h2': 5, 'h3': 4}, 'duration': 20, 'fail_at': None},
    # 9 Mbps fits one path, but the primary link breaks 8 s into the test
    'link_failure': {'rates': {'h1': 4, 'h2': 3, 'h3': 2}, 'duration': 20, 'fail_at': 8},
}


class TwoPathTopo(Topo):
    def build(self):
        s1, s2, s3, s4 = (self.addSwitch(f's{i}') for i in range(1, 5))
        clients = [self.addHost(f'h{i}', ip=f'10.0.0.{i}/24') for i in range(1, 4)]
        server = self.addHost('h4', ip='10.0.0.4/24')
        core = dict(bw=10, max_queue_size=100)
        for i, h in enumerate(clients, start=1):          # s1 ports 1-3
            self.addLink(h, s1, port2=i, bw=100)
        self.addLink(s1, s2, port1=4, port2=1, **core)    # primary
        self.addLink(s2, s4, port1=2, port2=1, **core)
        self.addLink(s1, s3, port1=5, port2=1, **core)    # backup
        self.addLink(s3, s4, port1=2, port2=2, **core)
        self.addLink(server, s4, port2=3, bw=100)         # s4 port 3


def send_ctl(msg):
    """Talk to the controller over its UNIX feedback socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(msg).encode(), FEEDBACK_SOCK)
        return True
    except OSError as e:
        print(f'!!! cannot reach controller ({e}). Is controller.py running?')
        return False
    finally:
        s.close()


def run_one(net, mode, name, cfg):
    tag = f'{mode}_{name}'
    dur, fail_at = cfg['duration'], cfg['fail_at']
    info(f'\n===== {tag}: {cfg["rates"]} Mbps, {dur}s, failure at {fail_at} =====\n')

    net.configLinkStatus('s2', 's4', 'up')
    if not send_ctl({'type': 'mode', 'mode': mode}):
        raise SystemExit(1)
    time.sleep(3)

    h4 = net.get('h4')
    h4.cmd(f'python3 {HERE}/server.py --duration {dur + 4} '
           f'--timeline {RES}/{tag}_timeline.csv --summary {RES}/{tag}_server.json '
           f'> {RES}/{tag}_server.log 2>&1 &')
    time.sleep(1)

    t0 = time.time()
    for h, rate in cfg['rates'].items():
        net.get(h).cmd(f'python3 {HERE}/client.py --rate {rate} --duration {dur} '
                       f'--name {h} --out {RES}/{tag}_{h}.json > /dev/null 2>&1 &')

    fail_time = None
    if fail_at is not None:
        time.sleep(fail_at)
        info('*** breaking link s2-s4\n')
        net.configLinkStatus('s2', 's4', 'down')
        fail_time = time.time()
        time.sleep(dur - fail_at + 6)
    else:
        time.sleep(dur + 6)
    net.configLinkStatus('s2', 's4', 'up')

    json.dump({'tag': tag, 'mode': mode, 'scenario': name, 't0': t0,
               'fail_time': fail_time, 'duration': dur},
              open(f'{RES}/{tag}_meta.json', 'w'))

    server = json.load(open(f'{RES}/{tag}_server.json'))['clients']
    rows = []
    for h, rate in cfg['rates'].items():
        c = json.load(open(f'{RES}/{tag}_{h}.json'))
        s = server.get(net.get(h).IP(), {'received': 0, 'bytes': 0, 'avg_delay_ms': 0})
        loss = (c['sent'] - s['received']) / c['sent'] * 100 if c['sent'] else 0
        rows.append({'mode': mode, 'scenario': name, 'client': h, 'offered_mbps': rate,
                     'sent': c['sent'], 'received': s['received'],
                     'loss_pct': round(loss, 2),
                     'rx_mbps': round(s['bytes'] * 8 / dur / 1e6, 3),
                     'avg_delay_ms': round(s['avg_delay_ms'], 2)})
        info(f'  {h}: {rows[-1]}\n')
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--modes', default='static,dynamic')
    ap.add_argument('--scenarios', default=','.join(SCENARIOS))
    args = ap.parse_args()

    os.makedirs(RES, exist_ok=True)
    os.chmod(RES, 0o777)

    net = Mininet(topo=TwoPathTopo(), link=TCLink, autoSetMacs=True,
                  switch=partial(OVSSwitch, protocols='OpenFlow13'),
                  controller=partial(RemoteController, ip='127.0.0.1', port=6653))
    net.start()
    net.staticArp()                      # no ARP flooding on the looped topology
    time.sleep(3)
    if net.pingAll() > 0:
        print('\n!!! Ping failed. Start the controller first:\n'
              '    ryu-manager --ofp-tcp-listen-port 6653 controller.py')
        net.stop()
        return

    all_rows = []
    try:
        for mode in args.modes.split(','):
            for name in args.scenarios.split(','):
                all_rows += run_one(net, mode, name, SCENARIOS[name])
    finally:
        net.stop()

    path = os.path.join(RES, 'summary.csv')
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)
    print(f'\nDone. Summary written to {path}')
    print('Now run:  python3 plot_results.py')


if __name__ == '__main__':
    setLogLevel('info')
    main()
