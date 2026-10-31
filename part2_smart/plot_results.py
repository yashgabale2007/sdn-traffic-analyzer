"""
Compares the static (baseline) and dynamic controllers.
Creates results/comparison.png and results/timelines.png and prints a summary.
Run with the normal system Python:  python3 plot_results.py
"""
import glob
import json
import os

import matplotlib.pyplot as plt
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, 'results')
COLORS = {'static': '#9AA5B1', 'dynamic': '#2F6FEB'}

summary = pd.read_csv(os.path.join(RES, 'summary.csv'))
agg = summary.groupby(['scenario', 'mode']).agg(
    sent=('sent', 'sum'), received=('received', 'sum'),
    rx_mbps=('rx_mbps', 'sum'), delay=('avg_delay_ms', 'mean')).reset_index()
agg['loss_pct'] = (agg.sent - agg.received) / agg.sent * 100

events = pd.DataFrame()
ev_path = os.path.join(HERE, 'events.csv')
if os.path.exists(ev_path):
    events = pd.read_csv(ev_path)

# ------------------------------------------------ 1. bar chart comparison
scenarios = list(agg.scenario.unique())
fig, axes = plt.subplots(1, 3, figsize=(16, 5))
for ax, col, label in zip(axes, ['loss_pct', 'rx_mbps', 'delay'],
                          ['Packet loss (%)', 'Total throughput (Mbps)', 'Avg one-way delay (ms)']):
    width = 0.38
    for i, mode in enumerate(['static', 'dynamic']):
        vals = [agg[(agg.scenario == s) & (agg['mode'] == mode)][col].sum() for s in scenarios]
        xs = [x + (i - 0.5) * width for x in range(len(scenarios))]
        bars = ax.bar(xs, vals, width, label=mode, color=COLORS[mode])
        ax.bar_label(bars, fmt='%.1f', fontsize=9)
    ax.set_xticks(range(len(scenarios)), scenarios)
    ax.set_title(label)
    ax.grid(axis='y', alpha=.3)
axes[0].legend(title='controller')
plt.suptitle('Static (baseline) vs dynamic SDN controller')
plt.tight_layout()
plt.savefig(os.path.join(RES, 'comparison.png'), dpi=150)

# ------------------------------------------------ 2. timelines
metas = [json.load(open(p)) for p in sorted(glob.glob(os.path.join(RES, '*_meta.json')))]
fig, axes = plt.subplots(1, len(scenarios), figsize=(8 * len(scenarios), 5), squeeze=False)
print('\nRecovery / reaction times')
for ax, scen in zip(axes[0], scenarios):
    for m in [m for m in metas if m['scenario'] == scen]:
        tl = pd.read_csv(os.path.join(RES, f"{m['tag']}_timeline.csv"))
        tl['t'] = (tl.time - m['t0']).round().astype(int)
        total = tl.groupby('t').mbps.sum()
        total = total[(total.index >= 0) & (total.index <= m['duration'])]
        ax.plot(total.index, total.values, 'o-', label=m['mode'], color=COLORS[m['mode']])
        if m['fail_time']:
            ft = m['fail_time'] - m['t0']
            ax.axvline(ft, ls='--', c='#C2611F')
            before = total[(total.index > 2) & (total.index < ft)].mean()
            after = total[total.index > ft + 0.5]
            ok = after[after >= 0.8 * before]
            rec = f'{ok.index[0] - ft:.1f} s' if len(ok) else 'never'
            print(f'  {scen:13s} {m["mode"]:8s} recovery after failure: {rec}')
        if m['mode'] == 'dynamic' and not events.empty:
            ev = events[(events.event == 'reroute') &
                        (~events.detail.str.contains('reset')) &
                        (events.time >= m['t0']) &
                        (events.time <= m['t0'] + m['duration'])]
            for _, e in ev.iterrows():
                ax.axvline(e.time - m['t0'], ls=':', c=COLORS['dynamic'], alpha=.6)
            if len(ev):
                print(f'  {scen:13s} dynamic  first reroute after '
                      f'{ev.time.iloc[0] - m["t0"]:.1f} s: {ev.detail.iloc[0]}')
    ax.set(title=f'{scen}: total received throughput', xlabel='Time (s)',
           ylabel='Mbps')
    ax.grid(alpha=.3)
    ax.legend()
plt.tight_layout()
plt.savefig(os.path.join(RES, 'timelines.png'), dpi=150)

print('\nPer-scenario summary')
print(agg.round(2).to_string(index=False))
print('\nPer-client results')
print(summary.to_string(index=False))
print(f'\nGraphs saved in {RES}: comparison.png, timelines.png')
plt.show()
