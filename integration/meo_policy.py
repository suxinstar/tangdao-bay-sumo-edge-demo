"""Exact float32 NumPy inference for the supplied MEO specialist actors.

The trained actor is preserved, including its unmasked edge-encoder aggregation.
This module does not train, load pickle, execute an acoustic model, or claim that
its SUMO observation adapter reproduces the original offline evaluation domain.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

# Set before NumPy is imported: tiny graphs must not fan out across all CPU cores.
for _name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[_name] = '1'
import numpy as np


class NumpyMeoActor:
    """Unbatched actor; output order per lane: mode, local model, neighbor, model."""

    def __init__(self, path, expected_sha256=None):
        path = Path(path)
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_sha256 and self.sha256 != expected_sha256:
            raise ValueError('MEO actor asset SHA256 mismatch: ' + path.name)
        with np.load(path, allow_pickle=False) as archive:
            self.weights = {key: archive[key].astype(np.float32, copy=False) for key in archive.files}
        if any(not np.isfinite(value).all() for value in self.weights.values()):
            raise ValueError('MEO actor contains non-finite weights')
        self.hidden_dim = self.weights['node_encoder.0.weight'].shape[0]
        if self.weights['node_encoder.0.weight'].shape != (128, 20):
            raise ValueError('Unsupported MEO actor architecture')
        self.model_features = self.weights['model_features']
        self.model_embedding = self.mlp('model_encoder', self.model_features)
        self.temperature = max(float(np.exp(self.weights['model_logit_log_temp'][0])), 0.01)
        self.beta = float(np.logaddexp(0, self.weights['acc_prior_logbeta']))

    def linear(self, name, value):
        return value @ self.weights[name + '.weight'].T + self.weights[name + '.bias']

    def mlp(self, name, value):
        layer = 0
        while name + '.' + str(layer) + '.weight' in self.weights:
            value = self.linear(name + '.' + str(layer), value)
            layer += 2
            if name + '.' + str(layer) + '.weight' in self.weights:
                value = np.maximum(value, np.float32(0))
        return value

    def forward(self, graph):
        node = np.asarray(graph['node_features'], dtype=np.float32)
        adj = np.asarray(graph['adjacency'], dtype=np.float32)
        edge = np.asarray(graph['edge_features'], dtype=np.float32)
        indices = np.asarray(graph['neighbor_indices'], dtype=np.int64)
        mask = np.asarray(graph['neighbor_mask'], dtype=bool)
        n = len(node)
        if (node.shape != (n, 20) or adj.shape != (n, n) or edge.shape != (n, n, 6)
                or indices.shape != (n, 12) or mask.shape != indices.shape
                or np.any(indices < 0) or np.any(indices >= n)
                or not all(np.isfinite(value).all() for value in (node, adj, edge))):
            raise ValueError('Invalid MEO graph shape, neighbor index or numeric value')
        degree = np.maximum(adj.sum(axis=1, keepdims=True), np.float32(1))
        latent = self.mlp('node_encoder', node)
        # Original actor encodes zero non-edge vectors too; retaining their biases
        # is necessary for checkpoint parity. Do not multiply this result by adj.
        edge_latent = self.mlp('edge_encoder', edge).sum(axis=1) / degree
        neighbors = self.mlp('neighbor_encoder', (adj @ node) / degree)
        updated = self.mlp('update_mlp', np.concatenate((latent, neighbors, edge_latent), axis=-1))
        gamma, beta = np.split(self.mlp('film_gen', node[:, -2:]), 2, axis=-1)
        feat = (np.float32(1) + gamma) * updated + beta
        row = np.arange(n)[:, None]
        keys = self.mlp('neighbor_key', np.concatenate((latent[indices], edge[row, indices]), axis=-1))
        result = []
        scale = math.sqrt(self.hidden_dim)
        for lane in range(7):
            mode = self.linear('mode_heads.' + str(lane), feat)
            query = self.linear('model_queries.' + str(lane), feat)
            models = query @ self.model_embedding.T / (scale / self.temperature)
            models += self.beta * self.model_features[:, 3 + lane]
            query_nb = self.linear('neighbor_query_heads.' + str(lane), feat)[:, None, :]
            neighbor = (query_nb * keys).sum(axis=-1) / scale
            neighbor = np.where(mask, neighbor, np.float32(-1e9))
            result.extend((mode, models, neighbor, models.copy()))
        if any(not np.isfinite(value).all() for value in result):
            raise ValueError('MEO inference produced non-finite logits')
        return result


class MeoScheduler:
    """Trained specialist plus explicitly synthetic online SUMO state adaptation.

    CPU cycles are demo estimates: one reference M3 task costs 0.45 Gcycles.
    Node serviceTimeS establishes heterogeneous capacity. No training metrics are
    reused as live recognition accuracy or as evidence of policy superiority.
    """

    MODES = ('local', 'offload', 'drop')
    REFERENCE_GCYC = 0.45
    MAX_NEIGHBORS = 12
    NEAREST_NEIGHBORS = 4

    def __init__(self, root, variant='completion'):
        if variant not in ('completion', 'accuracy'):
            raise ValueError('Unknown MEO specialist')
        self.variant = variant
        directory = Path(root) / 'models' / 'meo'
        self.manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        spec = self.manifest['specialists'][variant]
        self.actor = NumpyMeoActor(directory / spec['asset'], spec['assetSha256'])
        self.preference = tuple(spec['preference'])
        self.profiles = self.manifest['modelProfiles']
        self.reset()

    def reset(self):
        self.previous_models = {}

    @staticmethod
    def lane_channel(lane_id):
        try:
            return min(max(int(str(lane_id).rsplit('_', 1)[-1]), 0), 6)
        except (ValueError, TypeError):
            return 0

    def metadata(self):
        spec = self.manifest['specialists'][self.variant]
        return {
            'name': 'MEO trained specialist', 'provider': 'MeoScheduler', 'variant': self.variant,
            'architecture': 'HeteroModelAwareGraphPolicyNet',
            'checkpointSha256': spec['checkpointSha256'], 'actorSha256': self.actor.sha256,
            'preference': list(self.preference), 'runtime': 'numpy-float32-cpu-single-thread',
            'decision': 'deterministic argmax, mode-gated model/neighbor actions',
            'laneMapping': 'SUMO lane numeric suffix clamped to channels 0..6',
            'topology': '4 nearest RSUs per node, distance/id order, 12 masked slots',
            'observationAdapter': 'sumo-online-v1', 'transferValidated': False,
            'observationAssumptions': {
                'taskCounts': 'pending source tasks plus current task, grouped by lane channel',
                'remainingTime': 'max(0, 10 - task age in simulation seconds); feature only, not enforced deadline',
                'priority': 'uniform 1', 'inflow': 'current SUMO vehicles on incoming lanes; not a forecast',
                'load': 'reserved queue seconds * 0.45 / serviceTimeS, in synthetic Gcycles',
                'previousModel': 'last admitted model profile at execution node; reset to -1',
                'trainingLoadNormalization': 2048,
            },
            'costModel': 'synthetic profile compute_gcyc / 0.45 * demo serviceTimeS',
            'acousticModelsExecuted': False,
        }

    def build_graph(self, nodes, origin, now, task, pending_tasks, vehicles=None):
        ids = sorted(nodes)
        if origin not in nodes or not ids or len(ids) > 64 or not math.isfinite(now):
            raise ValueError('Invalid MEO node graph or simulation time')
        n = len(ids)
        indexed = {value: i for i, value in enumerate(ids)}
        by_source = {value: [] for value in ids}
        pending = pending_tasks.values() if hasattr(pending_tasks, 'values') else pending_tasks
        for item in pending:
            source = item.get('origin')
            if source in by_source and not item.get('dropped'):
                by_source[source].append(item)
        by_source[origin].append({**task, 'created': now})
        inflow = {value: 0.0 for value in ids}
        if vehicles is not None:
            vehicle_items = vehicles.values() if hasattr(vehicles, 'values') else vehicles
            lanes = {}
            for rid in ids:
                for lane_id in nodes[rid].get('incomingLaneIds', []):
                    lanes[lane_id] = rid
            for vehicle in vehicle_items:
                owner = lanes.get(vehicle.get('laneId'))
                if owner:
                    inflow[owner] += 1.0
        load = {}
        node = np.zeros((n, 20), dtype=np.float32)
        for rid in ids:
            item = nodes[rid]
            service = float(item['serviceTimeS'])
            if not math.isfinite(service) or service <= 0:
                raise ValueError('MEO requires finite positive service times')
            available = float(item.get('available', now))
            if not math.isfinite(available):
                raise ValueError('MEO requires finite queue availability')
            load[rid] = max(0.0, available - now) * self.REFERENCE_GCYC / service
            row = node[indexed[rid]]
            remaining = []
            for request in by_source[rid]:
                row[self.lane_channel(request.get('laneId'))] += 1
                # 10 s is an input feature horizon, not an enforced task deadline.
                remaining.append(max(0.0, 10.0 - (now - float(request.get('created', now)))))
            row[7:10] = ((min(remaining), float(np.mean(remaining)), float(np.std(remaining)))
                          if remaining else (10, 10, 0))
            row[10:13] = 1
            row[13:16] = (2048.0 - load[rid], load[rid] / 2048.0,
                          self.previous_models.get(rid, -1))
            row[16:18] = (inflow[rid], 5)
            row[18:] = self.preference
        adj = np.eye(n, dtype=np.float32)
        edge = np.zeros((n, n, 6), dtype=np.float32)
        indices = np.zeros((n, self.MAX_NEIGHBORS), dtype=np.int64)
        mask = np.zeros_like(indices, dtype=bool)
        for rid in ids:
            i = indexed[rid]
            distances = []
            for other in ids:
                if other == rid:
                    continue
                dist = math.hypot(float(nodes[rid]['x']) - float(nodes[other]['x']),
                                  float(nodes[rid]['y']) - float(nodes[other]['y']))
                if not math.isfinite(dist):
                    raise ValueError('MEO requires finite RSU coordinates')
                distances.append((dist, other))
            for slot, (dist, other) in enumerate(sorted(distances)[:self.NEAREST_NEIGHBORS]):
                j = indexed[other]
                indices[i, slot] = j
                mask[i, slot] = True
                adj[i, j] = 1
                edge[i, j] = (dist, dist / 2e8 + 0.01, load[rid] - load[other],
                              (load[other] - load[rid]) / 2048.0, inflow[other] - inflow[rid], 1)
            edge[i, i, 5] = 1
        return ids, {'node_features': node, 'adjacency': adj, 'edge_features': edge,
                     'neighbor_indices': indices, 'neighbor_mask': mask}

    def decide(self, nodes, origin, now, task, pending_tasks, vehicles=None):
        ids, graph = self.build_graph(nodes, origin, now, task, pending_tasks, vehicles)
        logits = self.actor.forward(graph)
        source = ids.index(origin)
        channel = self.lane_channel(task.get('laneId'))
        base = 4 * channel
        mode_logits = logits[base][source]
        mode = int(np.argmax(mode_logits))
        result = {
            'mode': self.MODES[mode], 'target': None, 'dropped': mode == 2,
            'modelIndex': None, 'modelId': None, 'computeFactor': None,
            'laneChannel': channel, 'variant': self.variant,
            'modeLogits': [float(value) for value in mode_logits],
            'reason': 'trained_actor_argmax', 'syntheticModelProfile': True,
        }
        if mode == 2:
            return result
        target = origin
        if mode == 1:
            # Original offline environment rejects offload with no valid neighbor.
            # Do not silently fall back to a different strategy or arbitrary index.
            if not graph['neighbor_mask'][source].any():
                result.update(dropped=True, reason='no_valid_neighbor')
                return result
            slot = int(np.argmax(logits[base + 2][source]))
            if not graph['neighbor_mask'][source, slot]:
                raise ValueError('MEO selected a masked neighbor')
            target = ids[int(graph['neighbor_indices'][source, slot])]
        model = int(np.argmax(logits[base + (1 if mode == 0 else 3)][source]))
        profile = self.profiles[model]
        result.update(target=target, modelIndex=model, modelId=profile['id'],
                      modelName=profile['name'], computeGcyc=profile['computeGcyc'],
                      computeFactor=profile['computeGcyc'] / self.REFERENCE_GCYC)
        self.previous_models[target] = model
        return result
