"""
Multi-client UDP server (runs on h4).

Threads:
  * receiver thread - reads datagrams from all clients on one UDP socket
  * reporter thread - every second computes per-client throughput, loss and
                      one-way delay, writes a timeline and sends a loss report
                      to the SDN controller (application -> SDN feedback)
"""
import argparse
import json
import socket
import struct
import threading
import time

HDR = struct.Struct('!Id')          # sequence number, send timestamp

p = argparse.ArgumentParser()
p.add_argument('--port', type=int, default=5001)
p.add_argument('--duration', type=float, default=30)
p.add_argument('--timeline', default='timeline.csv')
p.add_argument('--summary', default='server.json')
p.add_argument('--feedback', default='/tmp/sdn_feedback.sock')
p.add_argument('--no-feedback', action='store_true')
a = p.parse_args()

lock = threading.Lock()
stats = {}                          # client ip -> counters
stop = threading.Event()


def new_client():
    return {'received': 0, 'bytes': 0, 'max_seq': -1, 'delay_sum': 0.0,
            'iv_recv': 0, 'iv_bytes': 0, 'iv_delay': 0.0, 'iv_start_seq': -1,
            'malformed': 0}


def receiver(sock):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError as e:
            print('receive error:', e)
            continue
        now = time.time()
        with lock:
            st = stats.setdefault(addr[0], new_client())
            if len(data) < HDR.size:
                st['malformed'] += 1
                continue
            seq, sent_at = HDR.unpack_from(data)
            delay = max(0.0, now - sent_at)
            st['received'] += 1
            st['bytes'] += len(data)
            st['max_seq'] = max(st['max_seq'], seq)
            st['delay_sum'] += delay
            st['iv_recv'] += 1
            st['iv_bytes'] += len(data)
            st['iv_delay'] += delay


def reporter(writer, out):
    fb = None
    if not a.no_feedback:
        fb = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    warned = False
    last = time.time()
    while not stop.wait(1.0):
        now = time.time()
        span = now - last
        last = now
        with lock:
            rows = []
            for ip, st in stats.items():
                expected = st['max_seq'] - st['iv_start_seq']
                loss = 0.0
                if expected > 0:
                    loss = max(0.0, (expected - st['iv_recv']) / expected * 100)
                elif st['iv_recv'] == 0 and st['received'] > 0:
                    loss = 100.0       # a known client sent nothing that arrived this second
                mbps = st['iv_bytes'] * 8 / span / 1e6
                delay = st['iv_delay'] / st['iv_recv'] * 1000 if st['iv_recv'] else 0.0
                rows.append((ip, mbps, loss, delay))
                st['iv_start_seq'] = st['max_seq']
                st['iv_recv'] = st['iv_bytes'] = 0
                st['iv_delay'] = 0.0
        for ip, mbps, loss, delay in rows:
            writer.write(f'{now:.3f},{ip},{mbps:.3f},{loss:.2f},{delay:.2f}\n')
            if fb:
                msg = json.dumps({'type': 'report', 'client': ip,
                                  'mbps': round(mbps, 3), 'loss': round(loss, 2)})
                try:
                    fb.sendto(msg.encode(), a.feedback)
                except OSError as e:
                    if not warned:
                        print('controller feedback unavailable:', e)
                        warned = True
        out.flush()


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(('0.0.0.0', a.port))
    sock.settimeout(0.5)

    out = open(a.timeline, 'w')
    out.write('time,client,mbps,loss_pct,delay_ms\n')

    threads = [threading.Thread(target=receiver, args=(sock,), daemon=True),
               threading.Thread(target=reporter, args=(out, out), daemon=True)]
    for t in threads:
        t.start()
    print(f'server listening on UDP {a.port} for {a.duration}s')
    try:
        time.sleep(a.duration)
    except KeyboardInterrupt:
        pass
    stop.set()
    for t in threads:
        t.join(timeout=2)
    out.close()

    with lock:
        summary = {ip: {'received': st['received'], 'bytes': st['bytes'],
                        'max_seq': st['max_seq'], 'malformed': st['malformed'],
                        'avg_delay_ms': st['delay_sum'] / st['received'] * 1000
                        if st['received'] else 0.0}
                   for ip, st in stats.items()}
    json.dump({'clients': summary}, open(a.summary, 'w'), indent=2)
    print('SERVER', summary)


if __name__ == '__main__':
    main()
