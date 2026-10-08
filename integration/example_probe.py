"""Exercise the real-model seam without starting SUMO or controlling a signal.

python -m integration.example_probe
python -m integration.example_probe --endpoint http://127.0.0.1:9001/infer --audio-ref capture.wav
"""
import argparse
import json
import time
from . import AudioFrame, ModelGateway, SyntheticPerception
from .http_acoustic import HttpAcousticProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint')
    parser.add_argument('--audio-ref')
    parser.add_argument('--token-env')
    args = parser.parse_args()
    if args.endpoint and not args.audio_ref:
        parser.error('--endpoint requires --audio-ref')
    provider = (HttpAcousticProvider(args.endpoint, token_env=args.token_env)
                if args.endpoint else SyntheticPerception())
    gateway = ModelGateway(provider)
    frame = AudioFrame('probe', 'probe-car', 'RSU_1', 0, 'unknown', 0,
                       audio_ref=args.audio_ref, source='capture_file' if args.audio_ref else 'sumo_vehicle_truth')
    try:
        print(json.dumps(gateway.submit(frame)))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            outputs = gateway.drain('probe')
            if outputs:
                print(json.dumps(outputs, ensure_ascii=False, indent=2))
                return 0 if outputs[0]['status'] == 'ok' else 1
            time.sleep(0.02)
        return 1
    finally:
        gateway.close()


if __name__ == '__main__':
    raise SystemExit(main())
