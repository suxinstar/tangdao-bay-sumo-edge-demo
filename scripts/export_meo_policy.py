"""Export supplied MEO actors safely and check NumPy/Torch inference parity.

Development only: requires patched PyTorch >= 2.6, NumPy and PyYAML. Production
only loads the resulting pickle-free NPZ assets; no training traces are copied.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

for name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS'):
    os.environ[name] = '1'
import numpy as np
import torch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integration.meo_policy import NumpyMeoActor


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parity_graph(rng, n, case, pref):
    node = np.zeros((n, 20), dtype=np.float32)
    node[:, :7] = rng.integers(0, 30, (n, 7))
    node[:, 7:10] = rng.uniform(0, 10, (n, 3))
    node[:, 10:13] = rng.uniform(0.5, 3, (n, 3))
    load = rng.uniform(0, 30, n)
    node[:, 13] = 2048 - load
    node[:, 14] = load / 2048
    node[:, 15] = rng.integers(-1, 5, n)
    node[:, 16:18] = rng.uniform(0, 15, (n, 2))
    node[:, 18:] = pref
    adjacency = np.eye(n, dtype=np.float32)
    edges = np.zeros((n, n, 6), dtype=np.float32)
    indices = np.zeros((n, 12), dtype=np.int64)
    mask = np.zeros((n, 12), dtype=np.float32)
    for row in range(n):
        neighbors = [i for i in rng.permutation(n) if i != row][:min(12, case % 13)]
        for slot, col in enumerate(neighbors):
            distance = float(rng.uniform(20, 3000))
            indices[row, slot] = col
            mask[row, slot] = 1
            adjacency[row, col] = 1
            edges[row, col] = (distance, distance / 2e8 + .01, load[row] - load[col],
                               (load[col] - load[row]) / 2048, node[col, 16] - node[row, 16], 1)
        edges[row, row, -1] = 1
    return dict(node_features=node, adjacency=adjacency, edge_features=edges,
                neighbor_indices=indices, neighbor_mask=mask)


def export(source, output, report_path):
    version = tuple(int(part) for part in torch.__version__.split('+')[0].split('.')[:2])
    if version < (2, 6):
        raise RuntimeError('Export requires PyTorch >= 2.6 for patched weights_only loading')
    torch.set_num_threads(1)
    source_file = source / 'algos/mappo/actor_critic.py'
    module_spec = importlib.util.spec_from_file_location('supplied_meo_actor', source_file)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    physical_file = source / 'configs/rsu_config.yaml'
    physical = yaml.safe_load(physical_file.read_text(encoding='utf-8'))
    profiles = [{'id': key, 'name': spec['name'], 'computeGcyc': spec['compute_gcyc'],
                 'memoryMb': spec['mem_mb'], 'parametersMillion': spec['params_m'],
                 'synthetic': True}
                for key, spec in physical['models'].items()]
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        'format': 'meo-specialist-numpy-v1', 'sourceArchive': 'MEO_Project_runnable.zip',
        'sourceActorSha256': sha(source_file), 'sourcePhysicalConfigSha256': sha(physical_file),
        'architecture': {'nodeDim': 20, 'edgeDim': 6, 'hiddenDim': 128, 'messageDim': 64,
                         'layers': 2, 'lanes': 7, 'models': 5, 'neighborSlots': 12,
                         'attention': False, 'hierarchical': True, 'preferenceDim': 2},
        'modelProfiles': profiles, 'specialists': {},
        'limits': ['The two supplied specialist checkpoints are used as separate policies.',
                   'No conditional-policy student checkpoint was supplied or synthesized.',
                   'Model profiles and SUMO task inputs are synthetic, not acoustic inference.',
                   'Transfer from training graph to this SUMO graph requires new evaluation.'],
    }
    report = {'torchVersion': torch.__version__, 'numpyVersion': np.__version__, 'specialists': {}}
    for variant, folder, preference in [('completion', 'spec_w000', [0, 1]), ('accuracy', 'spec_w100', [1, 0])]:
        checkpoint_path = source / 'outputs' / folder / 'checkpoints/best_model.pt'
        checkpoint = torch.load(checkpoint_path, map_location='cpu', weights_only=True)
        cfg, weights = checkpoint['cfg'], checkpoint['actor']
        if (cfg['actor_type'] != 'hetero_model_aware_graph' or cfg['use_attention']
                or not cfg['use_hierarchical'] or cfg['hidden_dim'] != 128 or cfg['num_layers'] != 2):
            raise ValueError('Unsupported checkpoint architecture')
        actor = module.HeteroModelAwareGraphPolicyNet(
            20, 6, [3, 5, 12, 5] * 7, weights['model_features'], hidden_dim=128,
            message_dim=64, num_layers=2, num_lanes=7, use_attention=False,
            use_hierarchical=True, pref_dim=2)
        actor.load_state_dict(weights, strict=True)
        actor.eval()
        asset = output / (variant + '.npz')
        np.savez_compressed(asset, **{key: value.detach().cpu().numpy() for key, value in weights.items()})
        exported = NumpyMeoActor(asset)
        rng = np.random.default_rng(42)
        maximum_error, mismatch, comparisons, graphs = 0.0, 0, 0, 0
        durations = []
        for n in (1, 4, 9, 16):
            for case in range(13):
                graph = parity_graph(rng, n, case, preference)
                with torch.no_grad():
                    expected = actor({key: torch.as_tensor(value) for key, value in graph.items()})
                started = time.perf_counter()
                actual = exported.forward(graph)
                durations.append((time.perf_counter() - started) * 1000)
                for one, two in zip(expected, actual):
                    one = one.numpy()
                    valid = one > -1e8
                    error = float(np.max(np.abs(one[valid] - two[valid]))) if valid.any() else 0.0
                    maximum_error = max(maximum_error, error)
                    np.testing.assert_allclose(one, two, rtol=2e-5, atol=0.015)
                    mismatch += int(np.count_nonzero(np.argmax(one, axis=-1) != np.argmax(two, axis=-1)))
                    comparisons += n
                graphs += 1
        if mismatch:
            raise AssertionError(f'{variant}: {mismatch} argmax action mismatches')
        manifest['specialists'][variant] = {
            'asset': asset.name, 'assetSha256': sha(asset), 'checkpointSha256': sha(checkpoint_path),
            'sourceCheckpoint': f'outputs/{folder}/checkpoints/best_model.pt',
            'preference': preference, 'seed': cfg['seed'],
        }
        report['specialists'][variant] = {
            'checkpointSha256': sha(checkpoint_path), 'actorSha256': sha(asset),
            'graphs': graphs, 'headArgmaxComparisons': comparisons, 'argmaxMismatches': mismatch,
            'maximumAbsoluteLogitDifference': maximum_error,
            'meanInferenceMs': float(np.mean(durations)), 'p95InferenceMs': float(np.percentile(durations, 95)),
            'assetBytes': asset.stat().st_size,
        }
    (output / 'manifest.json').write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes((json.dumps(report, indent=2) + '\n').encode('utf-8'))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'models/meo')
    parser.add_argument('--parity-report', type=Path, default=ROOT / 'qa/meo_export_parity.json')
    args = parser.parse_args()
    export(args.source_directory.resolve(), args.output.resolve(), args.parity_report.resolve())
