import unittest
from simulation import reserve_task, extension_decision


class QueueTests(unittest.TestCase):
    def nodes(self):
        return {'A': {'x': 0, 'y': 0, 'available': 0, 'serviceTimeS': 1.6},
                'B': {'x': 160, 'y': 0, 'available': 0, 'serviceTimeS': 1.6}}

    def test_local_queue_never_overlaps_single_server(self):
        nodes = self.nodes()
        first = reserve_task(nodes, 'A', 0, 'local')
        second = reserve_task(nodes, 'A', 0.2, 'local')
        self.assertEqual(second['target'], 'A')
        self.assertGreaterEqual(second['start'], first['finish'])
        self.assertGreater(second['start'], second['txEnd'])

    def test_least_finish_offloads_only_when_local_work_exceeds_transfer(self):
        nodes = self.nodes()
        first = reserve_task(nodes, 'A', 0, 'least_finish')
        self.assertEqual(first['target'], 'A')
        second = reserve_task(nodes, 'A', 0.2, 'least_finish')
        self.assertEqual(second['target'], 'B')
        self.assertGreater(second['txEnd'], 0.2)
        self.assertGreater(second['returnEnd'], second['finish'])


class SignalTests(unittest.TestCase):
    def decision(self, **updates):
        options = dict(matching_green=True, has_yellow=False, already_extended=False, spent=20, remaining=10)
        options.update(updates)
        return extension_decision({'returnEnd': 5}, options.pop('now', 5), **options)

    def test_result_must_return_before_any_signal_change(self):
        self.assertIsNone(self.decision(now=4.99)[0])
        self.assertEqual(self.decision(now=5)[0], 14)

    def test_yellow_and_nonmatching_lane_never_extend(self):
        self.assertIsNone(self.decision(has_yellow=True)[0])
        self.assertIsNone(self.decision(matching_green=False)[0])

    def test_once_per_green_and_total_green_bound(self):
        self.assertIsNone(self.decision(already_extended=True)[0])
        duration, _ = self.decision(spent=44, remaining=10)
        self.assertEqual(duration, 11)
        self.assertLessEqual(44 + duration, 55)
        self.assertIsNone(self.decision(spent=45, remaining=10)[0])


if __name__ == '__main__':
    unittest.main()
