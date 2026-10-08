import copy
import json
from pathlib import Path
import tempfile
import unittest

from integration.meo_policy import MeoScheduler, NumpyMeoActor, np


ROOT = Path(__file__).resolve().parents[1]


class MeoPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = MeoScheduler(ROOT)
        self.nodes = {f'R{i}': {'x': i * 100, 'y': i % 2 * 80, 'serviceTimeS': 2,
                                'available': 5, 'incomingLaneIds': [f'road{i}_0']}
                      for i in range(9)}
        self.task = dict(laneId='road0_0', speed=8, vehicleId='test')

    def stub(self, mode, model=0, neighbor=0):
        def forward(graph):
            n = len(graph['node_features'])
            heads = []
            for _ in range(7):
                mode_logits = np.zeros((n, 3), np.float32)
                mode_logits[:, mode] = 2
                model_logits = np.zeros((n, 5), np.float32)
                model_logits[:, model] = 2
                nb = np.full((n, 12), -1e9, np.float32)
                nb[graph['neighbor_mask']] = 0
                nb[:, neighbor] = np.where(graph['neighbor_mask'][:, neighbor], 2, -1e9)
                heads += [mode_logits, model_logits, nb, model_logits.copy()]
            return heads
        self.policy.actor.forward = forward

    def test_assets_load_real_both_specialists(self):
        for variant in ('completion', 'accuracy'):
            policy = MeoScheduler(ROOT, variant)
            result = policy.decide(self.nodes, 'R0', 4, self.task, [])
            self.assertIn(result['mode'], ('local', 'offload', 'drop'))
            self.assertEqual(len(policy.metadata()['checkpointSha256']), 64)

    def test_local_head_changes_service_factor(self):
        self.stub(0, model=4)
        result = self.policy.decide(self.nodes, 'R0', 4, self.task, [])
        self.assertEqual((result['target'], result['modelId']), ('R0', 'M5'))
        self.assertEqual(result['computeFactor'], 4)

    def test_offload_selects_valid_neighbor(self):
        self.stub(1, model=2, neighbor=1)
        result = self.policy.decide(self.nodes, 'R0', 4, self.task, [])
        self.assertEqual(result['target'], 'R2')
        self.assertEqual(result['modelId'], 'M3')

    def test_drop_has_no_target_or_profile(self):
        self.stub(2, model=4)
        result = self.policy.decide(self.nodes, 'R0', 4, self.task, [])
        self.assertTrue(result['dropped'])
        self.assertIsNone(result['target'])
        self.assertIsNone(result['computeFactor'])
        self.assertEqual(self.policy.previous_models, {})

    def test_no_neighbor_offload_rejected_without_fallback(self):
        self.stub(1)
        result = self.policy.decide({'R0': self.nodes['R0']}, 'R0', 4, self.task, [])
        self.assertTrue(result['dropped'])
        self.assertIsNone(result['target'])
        self.assertEqual(result['reason'], 'no_valid_neighbor')

    def test_queue_and_input_objects_not_mutated(self):
        original = copy.deepcopy(self.nodes)
        self.policy.decide(self.nodes, 'R0', 4, self.task, [])
        self.assertEqual(self.nodes, original)

    def test_graph_features_and_masks(self):
        pending = {'T1': dict(origin='R0', laneId='road0_0', created=2)}
        ids, graph = self.policy.build_graph(self.nodes, 'R0', 4, self.task, pending,
                                            [dict(laneId='road0_0')])
        row = graph['node_features'][0]
        self.assertEqual(row[0], 2)
        self.assertEqual(row[7], 8)
        self.assertAlmostEqual(row[13], 2048 - .225, places=3)
        self.assertEqual(row[15], -1)
        self.assertEqual(row[16], 1)
        np.testing.assert_array_equal(row[-2:], [0, 1])
        self.assertTrue((graph['neighbor_mask'].sum(axis=1) == 4).all())
        self.assertEqual(float(graph['edge_features'][0, 0, 5]), 1)
        self.assertEqual(float(graph['edge_features'][0, 8].sum()), 0)

    def test_lane_mapping_matches_original_suffix_parser(self):
        for lane, channel in [('edge_3', 3), ('edge_22', 6), ('bad', 0), ('edge_-2', 0)]:
            self.assertEqual(self.policy.lane_channel(lane), channel)

    def test_reset_is_reproducible(self):
        one = self.policy.decide(self.nodes, 'R0', 4, self.task, [])
        self.policy.reset()
        two = self.policy.decide(self.nodes, 'R0', 4, self.task, [])
        self.assertEqual(one, two)

    def test_tampered_asset_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            NumpyMeoActor(ROOT / 'models/meo/completion.npz', '0' * 64)

    def test_invalid_graph_rejected(self):
        _, graph = self.policy.build_graph(self.nodes, 'R0', 4, self.task, [])
        graph['node_features'][0, 0] = np.nan
        with self.assertRaises(ValueError):
            self.policy.actor.forward(graph)

    def test_invalid_service_rejected(self):
        self.nodes['R0']['serviceTimeS'] = 0
        with self.assertRaises(ValueError):
            self.policy.decide(self.nodes, 'R0', 4, self.task, [])

    def test_masked_logits_and_shared_profile_head(self):
        _, graph = self.policy.build_graph(self.nodes, 'R0', 4, self.task, [])
        logits = self.policy.actor.forward(graph)
        for lane in range(7):
            np.testing.assert_array_equal(logits[lane * 4 + 1], logits[lane * 4 + 3])
            self.assertTrue((logits[lane * 4 + 2][~graph['neighbor_mask']] == -1e9).all())


if __name__ == '__main__':
    unittest.main()
