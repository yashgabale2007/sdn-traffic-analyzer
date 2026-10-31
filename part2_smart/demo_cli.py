#!/usr/bin/env python3
"""Interactive demo: builds the two-path network and opens the Mininet CLI.
Extra command:  mininet> mode static   |   mininet> mode dynamic
"""
from functools import partial
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from run_tests import TwoPathTopo, send_ctl


class DemoCLI(CLI):
    def do_mode(self, line):
        """mode static|dynamic : switch the controller's mode (resets paths)"""
        mode = line.strip()
        if mode in ('static', 'dynamic') and send_ctl({'type': 'mode', 'mode': mode}):
            print(f'controller is now in {mode} mode')
        else:
            print('usage: mode static | mode dynamic')


if __name__ == '__main__':
    setLogLevel('info')
    net = Mininet(topo=TwoPathTopo(), link=TCLink, autoSetMacs=True,
                  switch=partial(OVSSwitch, protocols='OpenFlow13'),
                  controller=partial(RemoteController, ip='127.0.0.1', port=6653))
    net.start()
    net.staticArp()
    DemoCLI(net)
    net.stop()
