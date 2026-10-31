#!/usr/bin/env python3
"""
Automatic, narrated live demo of Part 1 (no Mininet commands to type).

Builds h1 --100 Mbps-- s1 --10 Mbps-- h2, shows the switch's flow table,
then sends UDP at a low and a high rate and compares
application measurements (sent / received) with SDN flow statistics.

Start the controller first (Tab 1):
    ryu-manager --ofp-tcp-listen-port 6653 monitor.py
Then (Tab 2):
    sudo python3 demo_auto.py                 # 5 and 15 Mbps
    sudo python3 demo_auto.py --rates 2,8,12  # your own rates
    sudo python3 demo_auto.py --auto          # no "press Enter" pauses
"""
import argparse
import json
import os
import time
from functools import partial

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

from run_experiment import BottleneckTopo, sdn_measure, D

B, CY, YE, GR, RE, X = '\033[1m', '\033[96m', '\033[93m', '\033[92m', '\033[91m', '\033[0m'
AUTO = False


def say(text):
    print(f'\n{CY}{B}>>> {text}{X}')


def note(text):
    print(f'    {text}')


def pause():
    if not AUTO:
        input(f'{YE}    [press Enter to continue]{X}')


def show_flows(net):
    out = net.get('s1').cmd('ovs-ofctl -O OpenFlow13 --no-names dump-flows s1')
    print(f'{B}    Flow table of s1:{X}')
    for line in out.splitlines():
        if 'priority' in line:
            keep = [p for p in line.replace(',', ' ').split()
                    if p.startswith(('priority', 'n_packets', 'n_bytes', 'dl_src', 'dl_dst',
                                     'in_port', 'actions'))]
            print('    ' + '  '.join(keep))


def main():
    global AUTO
    ap = argparse.ArgumentParser()
    ap.add_argument('--rates', default='5,15', help='Mbps, comma separated')
    ap.add_argument('--duration', type=float, default=10)
    ap.add_argument('--auto', action='store_true', help='no pauses')
    args = ap.parse_args()
    AUTO = args.auto
    rates = [float(r) for r in args.rates.split(',')]

    say('Building the network: h1 --100 Mbps-- s1 --10 Mbps (bottleneck)-- h2')
    net = Mininet(topo=BottleneckTopo(), link=TCLink, autoSetMacs=True,
                  switch=partial(OVSSwitch, protocols='OpenFlow13'),
                  controller=partial(RemoteController, ip='127.0.0.1', port=6653))
    net.start()
    time.sleep(3)
    try:
        say('pingall: the controller learns where h1 and h2 are and installs flow rules')
        if net.pingAll() > 0:
            print(f'{RE}Ping failed. Start the controller first: '
                  f'ryu-manager --ofp-tcp-listen-port 6653 monitor.py{X}')
            return
        show_flows(net)
        note('Each rule: match (dl_dst = destination MAC) -> action (output port), with counters.')
        pause()

        h1, h2 = net.get('h1', 'h2')
        rows = []
        for rate in rates:
            label = 'BELOW' if rate < 9.5 else 'ABOVE'
            say(f'Sending UDP at {rate:g} Mbps for {args.duration:g} s ({label} the 10 Mbps link capacity)')
            rx, tx = f'/tmp/demo_rx_{rate:g}.json', f'/tmp/demo_tx_{rate:g}.json'
            for f in (rx, tx):
                if os.path.exists(f):
                    os.remove(f)
            h2.cmd(f'python3 {D}/receiver.py --out {rx} > /dev/null 2>&1 &')
            time.sleep(1)
            t0 = time.time()
            h1.cmd(f'python3 {D}/sender.py --rate {rate} --duration {args.duration} --out {tx}')
            t1 = time.time()
            time.sleep(4)
            s, r = json.load(open(tx)), json.load(open(rx))
            sdn, _ = sdn_measure(t0, t1)
            loss = (s['sent'] - r['received']) / s['sent'] * 100
            rows.append((rate, s['sent'], r['received'], loss, r['rx_mbps'], sdn))
            colour = GR if loss < 1 else RE
            note(f'App  - sent:      {s["sent"]:6d} packets  ({s["tx_mbps"]:.2f} Mbps)')
            note(f'App  - received:  {r["received"]:6d} packets  ({r["rx_mbps"]:.2f} Mbps)')
            note(f'{colour}Packet loss:     {loss:6.1f} %{X}')
            note(f'SDN  - switch flow statistics: {sdn:.2f} Mbps (includes 42 bytes of headers per packet)')
            if loss < 1:
                note(f'{GR}Below capacity: everything arrives and app and SDN agree.{X}')
            else:
                note(f'{RE}Above capacity: the bottleneck lets out ~9.6 Mbps, the rest is dropped.{X}')
                note(f'Expected loss = ({rate:g} - 9.6) / {rate:g} = {(rate - 9.6) / rate * 100:.1f} %')
            pause()

        say('Summary: application vs SDN measurements')
        print(f'{B}    {"load":>6s}{"sent":>8s}{"received":>10s}{"loss":>8s}{"app rx":>10s}{"SDN":>9s}{X}')
        for rate, sent, recv, loss, app, sdn in rows:
            print(f'    {rate:5g}M{sent:8d}{recv:10d}{loss:7.1f}%{app:8.2f}M {sdn:7.2f}M')
        show_flows(net)
        note('The n_packets / n_bytes counters above are what the controller reads every second.')
    finally:
        net.stop()


if __name__ == '__main__':
    setLogLevel('warning')
    main()
