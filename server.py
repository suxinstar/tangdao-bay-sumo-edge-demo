"""Local-only HTTP bridge for the editable three-dimensional SUMO demo."""
from __future__ import annotations
import argparse
import json
import mimetypes
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
from simulation import DemoSimulation


def make_handler(simulation, root):
    root = Path(root).resolve()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if args and ('/api/state' in str(args[0]) or '/api/health' in str(args[0])):
                return
            super().log_message(fmt, *args)

        def send_json(self, payload, status=200, download=False):
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="tangdao_run.json"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == '/api/scene':
                return self.send_json(simulation.scene)
            if route == '/api/state':
                return self.send_json(simulation.snapshot())
            if route == '/api/health':
                return self.send_json(simulation.health())
            if route == '/api/export':
                return self.send_json(simulation.export(), download=True)
            if route.startswith('/api/'):
                return self.send_json({'error': 'unknown endpoint'}, 404)
            static_root = root / ('data' if route.startswith('/data/') else 'web')
            relative = route[len('/data/'):] if route.startswith('/data/') else route.lstrip('/') or 'index.html'
            target = (static_root / unquote(relative)).resolve()
            if not target.is_relative_to(static_root.resolve()) or not target.is_file():
                return self.send_json({'error': 'not found'}, 404)
            mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            if target.suffix in ('.js', '.mjs'):
                mime = 'text/javascript'
            data = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            route = urlsplit(self.path).path
            if route not in ('/api/control', '/api/shutdown'):
                return self.send_json({'error': 'unknown endpoint'}, 404)
            origin = self.headers.get('Origin')
            if origin:
                try:
                    parsed = urlsplit(origin)
                    permitted = parsed.hostname in ('localhost', '127.0.0.1') and parsed.port == self.server.server_port
                except ValueError:
                    permitted = False
                if not permitted:
                    return self.send_json({'error': 'origin rejected'}, 403)
            if route == '/api/shutdown':
                self.send_json({'ok': True, 'message': '正在保存并关闭 SUMO'})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('invalid request length')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('JSON object required')
                return self.send_json(simulation.control(payload.get('action'), payload.get('value')))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self.send_json({'ok': False, 'error': str(exc)}, 400)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=['127.0.0.1', 'localhost'], default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--scheduler', choices=['least_finish', 'local'], default='least_finish')
    parser.add_argument('--speed', type=float, choices=[0.5, 1, 2, 4], default=1)
    parser.add_argument('--service-time', type=float, default=None, help='synthetic demo seconds, not measured device time')
    parser.add_argument('--max-time', type=float, default=900)
    parser.add_argument('--autostart', action='store_true', help='default is paused')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or (args.service_time is not None and args.service_time <= 0):
        parser.error('invalid port or service time')
    root = Path(__file__).resolve().parent
    simulation = DemoSimulation(root, scheduler=args.scheduler, speed=args.speed,
                                service_time=args.service_time, max_time=args.max_time,
                                autostart=args.autostart)
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(simulation, root))
    except OSError:
        simulation.close()
        raise
    server.daemon_threads = True
    simulation.start_background()
    print(f'Tangdao demo: http://127.0.0.1:{args.port}  status={simulation.status}', flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        simulation.close()


if __name__ == '__main__':
    main()
