"""Executable integration contracts, failure boundaries and combined state API."""
from dataclasses import replace
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from integration import (AudioFrame, SyntheticPerception, ModelGateway,
                         DisabledPhysicalSignalActuator, validate_perception_result)
from integration.http_acoustic import HttpAcousticProvider
from simulation import reserve_task
import test_vehicle_trace as vehicle_contract


def frame(run='run-1'):
    return AudioFrame(run, 'car-1', 'A', 12.0, 'lane_0', 8.0)


class ModelContractTests(unittest.TestCase):
    def test_default_result_is_synthetic_and_does_not_invent_confidence(self):
        result = SyntheticPerception().infer(frame())
        self.assertTrue(result.synthetic)
        self.assertIsNone(result.confidence)
        self.assertEqual(result.source, 'sumo_ideal_lane_observation')
        self.assertIs(validate_perception_result(result, frame()), result)

    def test_mismatched_invalid_or_unrecorded_real_results_are_rejected(self):
        result = SyntheticPerception().infer(frame())
        for changed in (dict(vehicle_id='other'), dict(confidence=float('nan')),
                        dict(confidence=1.01), dict(source=''), dict(synthetic=False),
                        dict(synthetic='false'), dict(direction_degrees=360),
                        dict(position_m=[1, float('inf')]), dict(uncertainty_m=-1)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_perception_result(replace(result, **changed), frame())

    def test_real_capture_is_allowed_but_only_when_audio_reference_exists(self):
        capture = replace(frame(), audio_ref='capture-23.wav', sample_rate_hz=48000, channels=4,
                          source='supplied_audio_reference')
        result = replace(SyntheticPerception().infer(capture), synthetic=False, source='microphone_array',
                         confidence=.85, model_version='example-real-v2', direction_degrees=88,
                         position_m=(12, 23), uncertainty_m=2)
        self.assertIs(validate_perception_result(result, capture), result)

    def test_real_device_actuator_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'no device command'):
            DisabledPhysicalSignalActuator().extend(None, 'signal-1', 10)

    def test_compute_scheduler_and_transport_hooks_are_used_without_reservation_corruption(self):
        class Compute:
            def estimate_seconds(self, node, work): return 3.0
        class Transport:
            def estimate_seconds(self, distance, *, remote): return (.5, .25)
        class Scheduler:
            def select(self, candidates, *, origin, policy):
                candidates[0]['finish'] = -100  # Adapter cannot mutate reserved timing.
                return 'B'
        nodes = {'A': dict(x=0, y=0, available=0, serviceTimeS=1),
                 'B': dict(x=10, y=0, available=2, serviceTimeS=1)}
        task = reserve_task(nodes, 'A', 1, 'least_finish', scheduler_provider=Scheduler(),
                            compute_provider=Compute(), transport_provider=Transport())
        self.assertEqual((task['target'], task['txEnd'], task['start'], task['finish'], task['returnEnd']),
                         ('B', 1.5, 2, 5, 5.25))
        self.assertEqual(nodes['B']['available'], 5)
        with self.assertRaises(ValueError):
            reserve_task(nodes, 'A', 1, 'local', scheduler_provider=Scheduler())

    def test_gateway_is_nonblocking_bounded_and_late_results_are_discarded(self):
        started, release = threading.Event(), threading.Event()
        class BlockingProvider:
            def infer(self, value):
                started.set()
                release.wait(2)
                return SyntheticPerception().infer(value)
        gateway = ModelGateway(BlockingProvider(), timeout_s=.04, max_pending=1)
        try:
            before = time.monotonic()
            self.assertTrue(gateway.submit(frame())['accepted'])
            self.assertLess(time.monotonic() - before, .03)
            self.assertTrue(started.wait(.2))
            self.assertEqual(gateway.submit(frame())['reason'], 'model_queue_full')
            time.sleep(.055)
            outputs = gateway.drain('run-1')
            self.assertEqual([item['status'] for item in outputs], ['timeout'])
            self.assertFalse(outputs[0]['usedForSignalControl'])
            release.set()
            time.sleep(.03)
            self.assertEqual(gateway.drain('run-1'), [])
            self.assertEqual(gateway.status()['counters']['completed'], 0)
        finally:
            release.set()
            gateway.close()

    def test_gateway_missing_error_and_reset_results_never_become_control(self):
        gateway = ModelGateway(None)
        self.assertFalse(gateway.submit(frame())['accepted'])
        gateway.close()
        class BrokenProvider:
            def infer(self, value): raise RuntimeError('secret should not be exposed')
        for provider, expected in ((BrokenProvider(), 'error'), (SyntheticPerception(), 'ok')):
            gateway = ModelGateway(provider)
            try:
                gateway.submit(frame())
                deadline = time.monotonic() + .3
                outputs = []
                while not outputs and time.monotonic() < deadline:
                    outputs = gateway.drain('run-1')
                    time.sleep(.005)
                self.assertEqual(outputs[0]['status'], expected)
                self.assertNotIn('secret', json.dumps(outputs))
                gateway.submit(frame('old-run'))
                time.sleep(.02)
                self.assertEqual(gateway.drain('new-run'), [])
            finally:
                gateway.close()

    def test_http_adapter_calls_real_endpoint_with_supplied_audio_contract(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(data)
                result = replace(SyntheticPerception().infer(AudioFrame(**data)), synthetic=False,
                                 source='test_http_model', model_version='fixture-v1', confidence=.8)
                raw = json.dumps(result.to_dict()).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        try:
            adapter = HttpAcousticProvider(f'http://127.0.0.1:{server.server_port}/infer')
            with self.assertRaises(ValueError): adapter.infer(frame())
            result = adapter.infer(replace(frame(), audio_ref='service-owned/123.wav'))
            self.assertFalse(result.synthetic)
            self.assertEqual(received[0]['audio_ref'], 'service-owned/123.wav')
        finally:
            server.shutdown()
            server.server_close()
            worker.join(1)


class CombinedApiTests(unittest.TestCase):
    setUp = vehicle_contract.VehicleApiTests.setUp
    tearDown = vehicle_contract.VehicleApiTests.tearDown
    get = vehicle_contract.VehicleApiTests.get

    def test_combined_state_is_same_run_and_time_without_an_extra_request(self):
        status, data, _ = self.get('/api/state?vehicle=' + quote(self.vehicle_id, safe=''))
        self.assertEqual(status, 200)
        self.assertEqual(data['vehicleTrace']['vehicleId'], self.vehicle_id)
        self.assertEqual(data['simTime'], data['vehicleTrace']['simTime'])
        self.assertEqual(data['runId'], data['vehicleTrace']['runId'])
        self.assertEqual(len(data['vehicleTrace']['tasks']), 1)
        self.assertEqual(self.get('/api/state?vehicle=missing')[1]['vehicleTrace']['status'], 'unknown')
        self.assertNotIn('vehicleTrace', self.get('/api/state')[1])

    def test_combined_state_validates_id_and_plain_snapshots_remain_isolated(self):
        for query in ('?vehicle=', '?vehicle=%00', '?vehicle=x&vehicle=y', '?vehicle=%FF'):
            self.assertEqual(self.get('/api/state' + query)[0], 400)
        response = self.sim.snapshot(self.vehicle_id)
        response['vehicleTrace']['tasks'][0]['returned'] = True
        response['vehicles'][0]['speed'] = 999
        self.assertFalse(self.sim.tasks[0].get('returned', False))
        self.assertNotEqual(self.sim.vehicles[0]['speed'], 999)

    def test_model_status_and_unconfigured_probe_are_explicit(self):
        status, data, _ = self.get('/api/integration')
        self.assertEqual(status, 200)
        self.assertFalse(data['modelGateway']['configured'])
        self.assertFalse(data['perception']['actualAudioInference'])
        request = Request(self.base + '/api/model/probe', b'{}', {'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 400)

    def test_probe_uses_background_gateway_and_rejects_bad_capture_metadata(self):
        self.sim.model_gateway = ModelGateway(SyntheticPerception())
        try:
            payload = {'vehicleId': self.vehicle_id, 'rsuId': 'A', 'audioRef': 'capture.wav',
                       'sampleRateHz': 48000, 'channels': 4}
            result = self.sim.submit_model_probe(payload)
            self.assertTrue(result['accepted'])
            for changes in ({'sampleRateHz': -1}, {'channels': 0}, {'vehicleId': 'missing'}, {'audioRef': ''}):
                with self.assertRaises(ValueError): self.sim.submit_model_probe({**payload, **changes})
        finally:
            self.sim.model_gateway.close()

    def test_task_indexes_match_history_after_completion(self):
        self.sim._apply_control = lambda task: task.update(controlApplied=False)
        for _ in range(100):
            self.sim._sense(self.sim.vehicles[0], 'A')
            self.sim._complete(self.sim.tasks[-1])
        self.assertEqual(len(self.sim.recent_completed), 10)
        self.assertEqual(len(self.sim.pending_tasks), 1)
        self.assertEqual(self.sim.snapshot()['metrics']['completed'], 100)
        self.assertEqual(self.sim.vehicle_trace(self.vehicle_id)['summary']['sensed'], 101)


if __name__ == '__main__':
    unittest.main()
