"""Part 1 - plots application vs SDN measurements from results.csv"""
import os
import pandas as pd
import matplotlib.pyplot as plt

D = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(D, 'results.csv'))
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
ax1.plot(df.offered_mbps, df.app_tx_mbps, 'o-', label='App: sent')
ax1.plot(df.offered_mbps, df.sdn_mbps, 's-', label='SDN: switch flow stats')
ax1.plot(df.offered_mbps, df.app_rx_mbps, '^-', label='App: received')
ax1.axhline(10, ls='--', c='gray', label='Link capacity (10 Mbps)')
ax1.set(xlabel='Offered load (Mbps)', ylabel='Throughput (Mbps)',
        title='Application vs SDN throughput')
ax1.legend(); ax1.grid(alpha=.3)
ax2.bar(df.offered_mbps.astype(str), df.loss_pct, color='#E8833A')
ax2.set(xlabel='Offered load (Mbps)', ylabel='Packet loss (%)',
        title='Packet loss vs traffic load')
ax2.grid(axis='y', alpha=.3)
plt.tight_layout()
plt.savefig(os.path.join(D, 'results.png'), dpi=150)
print(df.to_string(index=False))
print('Graph saved as results.png')
plt.show()
