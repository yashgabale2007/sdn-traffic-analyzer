#!/usr/bin/env python3
"""Part 1 - h1 --100Mbps-- s1 --10Mbps-- h2, tests at several loads."""
import csv, json, os, time
from functools import partial
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.log import setLogLevel

D = os.path.dirname(os.path.abspath(__file__))
RATES = [1, 2, 4, 6, 8, 10, 12, 15]
DURATION = 10
H1, H2 = '00:00:00:00:00:01', '00:00:00:00:00:02'


class BottleneckTopo(Topo):
    def build(self):
        h1, h2 = self.addHost('h1'), self.addHost('h2')
        s1 = self.addSwitch('s1')
        self.addLink(h1, s1, bw=100)
        self.addLink(s1, h2, bw=10, max_queue_size=100)


def sdn_measure(t0, t1):
    rows = [r for r in csv.DictReader(open(os.path.join(D, 'sdn_stats.csv')))
            if r['src'] == H1 and r['dst'] == H2]
    window = [r for r in rows if t0 <= float(r['time']) <= t1 + 1.5]
    if len(window) < 2:
        return 0.0, 0
    rates = [float(r['rate_mbps']) for r in window
             if t0 + 1 < float(r['time']) < t1 and float(r['rate_mbps']) > 0]
    pkts = int(window[-1]['packets']) - int(window[0]['packets'])
    return (sum(rates) / len(rates) if rates else 0.0), pkts


def main():
    net = Mininet(topo=BottleneckTopo(), link=TCLink, autoSetMacs=True,
                  switch=partial(OVSSwitch, protocols='OpenFlow13'),
                  controller=partial(RemoteController, ip='127.0.0.1', port=6653))
    net.start()
    time.sleep(3)
    if net.pingAll() > 0:
        print('\nPing failed: is ryu-manager running in the other terminal?')
        net.stop()
        return
    h1, h2 = net.get('h1', 'h2')

    out = open(os.path.join(D, 'results.csv'), 'w', newline='')
    w = csv.writer(out)
    w.writerow(['offered_mbps', 'sent', 'switch_pkts', 'received', 'loss_pct',
                'app_tx_mbps', 'app_rx_mbps', 'sdn_mbps'])
    for rate in RATES:
        print(f'\n=== Testing {rate} Mbps ===')
        rx_file, tx_file = f'{D}/rx_{rate}.json', f'{D}/tx_{rate}.json'
        h2.cmd(f'python3 {D}/receiver.py --out {rx_file} > /dev/null 2>&1 &')
        time.sleep(1)
        t0 = time.time()
        h1.cmd(f'python3 {D}/sender.py --rate {rate} --duration {DURATION} --out {tx_file}')
        t1 = time.time()
        time.sleep(4)
        tx, rx = json.load(open(tx_file)), json.load(open(rx_file))
        sdn_mbps, sw_pkts = sdn_measure(t0, t1)
        loss = (tx['sent'] - rx['received']) / tx['sent'] * 100
        row = [rate, tx['sent'], sw_pkts, rx['received'], round(loss, 2),
               round(tx['tx_mbps'], 3), round(rx['rx_mbps'], 3), round(sdn_mbps, 3)]
        w.writerow(row)
        out.flush()
        print('Result:', row)
    out.close()
    net.stop()
    print(f'\nDone! Results saved to {D}/results.csv')


if __name__ == '__main__':
    setLogLevel('info')
    main()
