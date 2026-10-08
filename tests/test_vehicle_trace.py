"""Journey retention and the read-only vehicle HTTP contract; no SUMO needed."""
import json
from pathlib import Path
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from server import make_handler
from simulation import DemoSimulation


def simulation_fixture():
    sim = DemoSimulation.__new__(DemoSimulation)
    sim.root = Path(__file__).resolve().parents[1]
    sim.scene = {'rsus': [
        {'id': 'A', 'x': 0, 'y': 0, 'serviceTimeS': 2, 'intersectionId': 'tlsA', 'sensingRadiusM': 100},
        {'id': 'B', 'x': 160, 'y': 0, 'serviceTimeS': 2, 'intersectionId': 'tlsB', 'sensingRadiusM': 100}],
        'intersections': []}
    sim.service_time = None
    sim.scheduler, sim.speed, sim.max_time = 'least_finish', 1, 900
    sim.lock, sim.event_output = threading.RLock(), None
    sim.vehicle_vars = (1, 2, 3, 4)
    sim._reset_data()
    sim.incoming = {'A': set(), 'B': set()}
    return sim


def observed(sim, vehicle_id='car-1'):
    vehicle = {'id': vehicle_id, 'x': 12, 'y': 20, 'angle': 90, 'speed': 7, 'laneId': 'lane_0'}
    sim.vehicle_history[vehicle_id] = {'present': True, 'firstSeen': sim.sim_time,
                                     'lastSeen': sim.sim_time, 'vehicle': vehicle}
    sim.vehicles.append(vehicle)
    return vehicle


class FakeConnection:
    """Each frame provides the observations the real TraCI step consumes."""
    def __init__(self, frames):
        self.frames = iter(frames)
        self.frame = None
        self.simulation = SimpleNamespace(
            getTime=lambda: self.frame['time'],
            getDepartedIDList=lambda: self.frame.get('entered', []),
            getArrivedIDList=lambda: self.frame.get('arrived', []),
            getMinExpectedNumber=lambda: 1)
        self.vehicle = SimpleNamespace(
            getIDList=lambda: self.frame.get('vehicles', {}),
            subscribe=lambda *args: None,
            getSubscriptionResults=lambda vehicle_id: self.frame['vehicles'][vehicle_id])
        self.trafficlight = SimpleNamespace()

    def simulationStep(self):
        self.frame = next(self.frames)


class VehicleTraceTests(unittest.TestCase):
    def setUp(self):
        self.sim = simulation_fixture()

    def test_history_retains_more_than_global_recent_limit_and_filters_other_vehicles(self):
        car = observed(self.sim)
        other = observed(self.sim, 'other')
        self.sim._apply_control = lambda task: task.update(controlApplied=False, controlReason='当前非可延长绿灯')
        for _ in range(23):
            self.sim._sense(car, 'A')
            task = self.sim.tasks[-1]
            self.sim._complete(task)
        self.sim._sense(other, 'B')
        trace = self.sim.vehicle_trace(car['id'])
        self.assertEqual(trace['summary'], {'sensed': 23, 'completed': 23, 'offloaded': 12, 'pending': 0})
        self.assertEqual(len(trace['tasks']), 23)
        self.assertEqual(len(trace['events']), 69)
        self.assertEqual(sum(task['status'] == 'done' for task in self.sim.snapshot()['tasks']), 10)
        self.assertTrue(all(task['vehicleId'] == car['id'] for task in trace['tasks']))
        self.assertTrue(all(event['vehicleId'] == car['id'] for event in trace['events']))
        self.assertEqual(trace['tasks'][0]['controlReason'], '当前非可延长绿灯')
        self.assertIn('observedReturnTime', trace['tasks'][0])

    def test_departed_vehicle_keeps_position_and_pending_task_until_actual_return(self):
        self.sim.connection = FakeConnection([
            {'time': 0.2, 'entered': ['car-1'], 'vehicles': {'car-1': {1: (12, 20), 2: 90, 3: 7, 4: 'lane_0'}}},
            {'time': 0.4, 'arrived': ['car-1']},
            {'time': 4.0},
        ])
        self.sim.step()
        self.sim._sense(self.sim.vehicles[0], 'A')
        self.sim.step()
        trace = self.sim.vehicle_trace('car-1')
        self.assertEqual(trace['status'], 'departed')
        self.assertEqual(trace['vehicle']['x'], 12)
        self.assertEqual(trace['enteredAt'], 0.2)
        self.assertEqual(trace['lastSeen'], 0.2)
        self.assertEqual(trace['arrivedAt'], 0.4)
        self.assertEqual(trace['leftAt'], 0.4)
        self.assertEqual(trace['summary']['pending'], 1)
        self.assertFalse(trace['tasks'][0].get('returned', False))
        self.assertNotIn('controlApplied', trace['tasks'][0])
        self.sim._apply_control = lambda task: task.update(controlApplied=False, controlReason='test decision')
        self.sim.step()
        finished = self.sim.vehicle_trace('car-1')
        self.assertEqual(finished['status'], 'departed')
        self.assertEqual(finished['summary']['pending'], 0)
        self.assertEqual(finished['summary']['completed'], 1)
        self.assertEqual(finished['tasks'][0]['observedReturnTime'], 4.0)
        self.assertEqual(finished['tasks'][0]['controlReason'], 'test decision')

    def test_short_trip_without_position_is_known_but_no_position_is_fabricated(self):
        self.sim.connection = FakeConnection([{'time': 0.2, 'entered': ['short'], 'arrived': ['short']}])
        self.sim.step()
        trace = self.sim.vehicle_trace('short')
        self.assertEqual(trace['status'], 'departed')
        self.assertIsNone(trace['vehicle'])
        self.assertIsNone(trace['lastSeen'])
        self.assertEqual(trace['arrivedAt'], 0.2)

    def test_removed_without_arrival_is_departed_and_not_claimed_arrived(self):
        self.sim.connection = FakeConnection([
            {'time': 0.2, 'vehicles': {'removed': {1: (12, 20), 2: 90, 3: 7, 4: 'lane_0'}}},
            {'time': 0.4},
        ])
        self.sim.step()
        self.sim.step()
        trace = self.sim.vehicle_trace('removed')
        self.assertEqual(trace['status'], 'departed')
        self.assertEqual(trace['leftAt'], 0.4)
        self.assertIsNone(trace['arrivedAt'])

    def test_reset_removes_old_journey_and_changes_run_identity(self):
        self.sim._sense(observed(self.sim), 'A')
        previous_run = self.sim.run_id
        self.sim._reset_data()
        trace = self.sim.vehicle_trace('car-1')
        self.assertNotEqual(trace['runId'], previous_run)
        self.assertEqual(trace['status'], 'unknown')
        self.assertEqual(trace['tasks'], [])
        self.assertEqual(trace['events'], [])
        self.assertEqual(trace['summary']['sensed'], 0)

    def test_response_cannot_mutate_simulation_objects(self):
        self.sim._sense(observed(self.sim), 'A')
        trace = self.sim.vehicle_trace('car-1')
        trace['vehicle']['x'] = -1
        trace['tasks'][0]['returned'] = True
        trace['events'][1]['task']['target'] = 'corrupted'
        again = self.sim.vehicle_trace('car-1')
        self.assertEqual(again['vehicle']['x'], 12)
        self.assertEqual(again['summary']['completed'], 0)
        self.assertNotEqual(again['events'][1]['task']['target'], 'corrupted')


class VehicleApiTests(unittest.TestCase):
    def setUp(self):
        self.sim = simulation_fixture()
        self.vehicle_id = 'car+ 车辆/1'
        self.sim._sense(observed(self.sim, self.vehicle_id), 'A')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.sim, self.sim.root))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, path):
        try:
            response = urlopen(self.base + path, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response), response.headers

    def test_url_encoded_id_returns_only_matching_current_run(self):
        status, data, headers = self.get('/api/vehicle?id=' + quote(self.vehicle_id, safe=''))
        self.assertEqual(status, 200)
        self.assertEqual(data['vehicleId'], self.vehicle_id)
        self.assertEqual(data['status'], 'present')
        self.assertEqual(data['runId'], self.sim.run_id)
        self.assertEqual(len(data['tasks']), 1)
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_missing_duplicate_blank_overlong_and_invalid_id_are_bad_request(self):
        for query in ('', '?id=', '?id=car&id=other', '?id=%00', '?id=%0A', '?id=%20',
                      '?id=' + 'a' * 257, '?id=%FF'):
            with self.subTest(query=query[:35]):
                status, data, _ = self.get('/api/vehicle' + query)
                self.assertEqual(status, 400)
                self.assertIn('error', data)

    def test_unknown_and_pre_reset_ids_return_404_not_another_vehicle(self):
        for vehicle_id in ('nonexistent', self.vehicle_id):
            if vehicle_id == self.vehicle_id:
                self.sim._reset_data()
            status, data, _ = self.get('/api/vehicle?id=' + quote(vehicle_id, safe=''))
            self.assertEqual(status, 404)
            self.assertEqual(data['status'], 'unknown')
            self.assertIsNone(data['vehicle'])
            self.assertEqual(data['tasks'], [])
            self.assertEqual(data['events'], [])


if __name__ == '__main__':
    unittest.main()
