"""Learned-action application and terminal drop contracts; no model/SUMO needed."""
import copy
import unittest
from types import SimpleNamespace

from simulation import DemoSimulation, reserve_meo_task
from test_vehicle_trace import observed, simulation_fixture


class StubPolicy:
    def __init__(self, decision):
        self.decision = decision

    def decide(self, nodes, origin, now, vehicle, pending, *, vehicles):
        return copy.deepcopy(self.decision)


def action(mode='local', target='A', factor=1.0):
    return {'mode': mode, 'target': target, 'computeFactor': factor, 'modelId': 'M3'}


class LearnedReservationTests(unittest.TestCase):
    def nodes(self):
        return {'A': dict(x=0, y=0, available=0, serviceTimeS=2),
                'B': dict(x=160, y=0, available=0, serviceTimeS=4)}

    def test_mode_and_exact_target_are_applied_instead_of_heuristic(self):
        nodes = self.nodes()
        nodes['B']['available'] = 100  # Inferior finish time must not rewrite learned choice.
        task = reserve_meo_task(nodes, 'A', 3, action('offload', 'B'))
        self.assertEqual(task['target'], 'B')
        self.assertTrue(task['offloaded'])
        self.assertEqual(task['start'], 100)
        self.assertEqual(nodes['A']['available'], 0)
        self.assertEqual(nodes['B']['available'], task['finish'])

    def test_model_factor_scales_real_reserved_duration(self):
        for factor in (0.125, 1.0, 4.0):
            with self.subTest(factor=factor):
                nodes = self.nodes()
                task = reserve_meo_task(nodes, 'A', 1, action(factor=factor), work_factor=1.1)
                self.assertAlmostEqual(task['finish'] - task['start'], 2 * factor * 1.1)
                next_task = reserve_meo_task(nodes, 'A', 1, action(factor=factor))
                self.assertGreaterEqual(next_task['start'], task['finish'])

    def test_invalid_mode_target_and_factor_leave_reservations_unchanged(self):
        invalid = [action('other'), action('offload', 'missing'), action('local', 'B'),
                   action('offload', 'A'), action('drop', 'B')]
        invalid += [action(factor=factor) for factor in (None, True, 0, -1, '1', float('nan'), float('inf'))]
        for decision in invalid:
            with self.subTest(decision=decision):
                nodes = self.nodes()
                original = copy.deepcopy(nodes)
                with self.assertRaises(ValueError):
                    reserve_meo_task(nodes, 'A', 0, decision)
                self.assertEqual(nodes, original)

    def test_drop_does_not_consult_compute_transport_or_reserve(self):
        class ExplodingProvider:
            def estimate_seconds(self, *args, **kwargs):
                raise AssertionError('dropped task must not estimate resources')
        nodes = self.nodes()
        original = copy.deepcopy(nodes)
        task = reserve_meo_task(nodes, 'A', 0, {'mode': 'drop', 'target': None},
                                compute_provider=ExplodingProvider(), transport_provider=ExplodingProvider())
        self.assertTrue(task['dropped'])
        self.assertIsNone(task['target'])
        self.assertNotIn('returnEnd', task)
        self.assertEqual(nodes, original)

    def test_invalid_resource_estimates_are_rejected_before_reserving(self):
        class Compute:
            def __init__(self, value): self.value = value
            def estimate_seconds(self, node, work): return self.value
        class Transport:
            def __init__(self, value): self.value = value
            def estimate_seconds(self, distance, *, remote): return self.value
        for resource in [dict(compute_provider=Compute(x)) for x in (0, -1, float('nan'), float('inf'))] + [
                dict(transport_provider=Transport(x)) for x in ((-1, 0), (0, -1), (float('nan'), 0), (0, float('inf')))]:
            with self.subTest(resource=resource):
                nodes = self.nodes()
                original = copy.deepcopy(nodes)
                with self.assertRaises(ValueError):
                    reserve_meo_task(nodes, 'A', 1, action(), **resource)
                self.assertEqual(nodes, original)


class LearnedDropLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.sim = simulation_fixture()
        self.car = observed(self.sim)
        self.sim.meo_scheduler = StubPolicy({'mode': 'drop', 'target': None})

    def test_drop_is_retained_but_never_pending_returned_or_signalled(self):
        self.sim._sense(self.car, 'A')
        task = self.sim.tasks[0]
        self.assertTrue(task['dropped'])
        self.assertEqual(self.sim.pending_tasks, {})
        self.assertEqual(self.sim.return_heap, [])
        self.assertEqual([e['type'] for e in self.sim.events], ['sense', 'drop'])
        self.sim._complete(task)
        # Calling the actual actuator path directly must also fail closed without a connection.
        self.sim._apply_control(task)
        self.assertFalse(task.get('returned', False))
        self.assertEqual(self.sim.metrics['completed'], 0)
        self.assertEqual(self.sim.metrics['signalActions'], 0)
        self.assertEqual(self.sim.metrics['signalRejected'], 0)
        self.assertEqual(self.sim.metrics['sensed'], 1)
        self.assertEqual(self.sim.metrics['dropped'], 1)
        self.assertEqual(self.sim.vehicle_trace(self.car['id'])['summary'],
                         dict(sensed=1, completed=0, offloaded=0, pending=0, dropped=1))

    def test_drop_snapshot_has_terminal_skipped_compute_and_signal(self):
        self.sim._sense(self.car, 'A')
        state = self.sim.snapshot(self.car['id'])
        self.assertEqual(state['tasks'][0]['status'], 'dropped')
        stages = {stage['key']: stage['state'] for stage in state['trace']['stages']}
        self.assertEqual(stages['compute'], 'skipped')
        self.assertEqual(stages['signal'], 'skipped')
        self.assertTrue(all(not n['busy'] and not n['queue'] for n in state['rsus']))

    def test_offload_without_a_neighbor_is_a_recorded_rejection_not_a_crash(self):
        self.sim.meo_scheduler = StubPolicy({'mode': 'offload', 'target': None,
                                            'dropped': True, 'reason': 'no_valid_neighbor'})
        self.sim._sense(self.car, 'A')
        task = self.sim.tasks[0]
        self.assertTrue(task['dropped'])
        self.assertEqual(task['policyDecision']['mode'], 'offload')
        self.assertEqual(task['policyDecision']['reason'], 'no_valid_neighbor')
        self.assertIn('无有效邻居', task['dropReason'])
        self.assertEqual(self.sim.pending_tasks, {})
        self.assertEqual(self.sim.metrics['offloaded'], 0)
        self.assertEqual(self.sim.metrics['dropped'], 1)

    def test_dropped_and_live_tasks_have_distinct_accounting(self):
        self.sim.rng = SimpleNamespace(uniform=lambda low, high: 1.1)
        self.sim._sense(self.car, 'A')
        self.sim.meo_scheduler = StubPolicy(action('offload', 'B', 2.0))
        self.sim._sense(self.car, 'A')
        task = self.sim.tasks[-1]
        self.assertAlmostEqual(task['finish'] - task['start'],
                               self.sim.node_by_id['B']['serviceTimeS'] * 2 * 1.1)
        self.assertEqual(len(self.sim.pending_tasks), 1)
        self.assertEqual(len(self.sim.return_heap), 1)
        trace = self.sim.vehicle_trace(self.car['id'])
        self.assertEqual(trace['summary'], dict(sensed=2, completed=0, dropped=1, offloaded=1, pending=1))
        self.sim.sim_time = task['returnEnd']
        self.sim._apply_control = lambda value: value.update(controlApplied=False, controlReason='fixture')
        self.sim._complete(task)
        self.assertEqual(self.sim.metrics['completed'], 1)
        self.assertEqual(self.sim.metrics['dropped'], 1)
        self.assertEqual(self.sim.vehicle_trace(self.car['id'])['summary']['pending'], 0)
        self.assertEqual(self.sim.vehicle_trace(self.car['id'])['summary']['completed'], 1)


if __name__ == '__main__':
    unittest.main()
