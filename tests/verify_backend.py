"""Run a bounded actual-SUMO backend check without opening a GUI or HTTP port."""
import hashlib
import atexit
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from simulation import DemoSimulation


def main():
    output = ROOT / 'verification'
    output.mkdir(exist_ok=True)
    sim = DemoSimulation(ROOT)
    atexit.register(sim.close)
    report = {'health': sim.health(), 'unit_test_command': 'python -m unittest discover -s tests -v'}
    assert sim.health()['ready'], sim.error
    initial = sim.snapshot()
    assert initial['status'] == 'paused' and initial['simTime'] == 0
    sim.control('resume')
    started = time.monotonic()
    for _ in range(600):
        sim.step()
    first = sim.export()
    report['wallTimeFor120SimulationSeconds'] = time.monotonic() - started
    report['firstMetrics'] = first['metrics']
    report['firstRunId'] = sim.run_id
    active = sim.snapshot()
    (output / 'backend_120s_state.json').write_text(json.dumps(active, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'backend_120s_export.json').write_text(json.dumps(first, ensure_ascii=False, indent=2), encoding='utf-8')
    signals = [e for e in first['events'] if e['type'] == 'signal']
    assert first['metrics']['sensed'] > 0
    assert first['metrics']['offloaded'] > 0
    assert signals, 'No actual green extensions occurred; inspect geometry/phase/traffic conditions.'
    assert all(e['time'] + 1e-8 >= e['returnEnd'] for e in signals)
    assert all(e['spent'] + e['afterRemaining'] <= sim.MAX_GREEN + 1e-7 for e in signals)
    assert all(e['matchingGreen'] and 'y' not in e['beforeState'].lower() for e in signals)
    assert len({(e['tlsId'], e['phaseEpoch']) for e in signals}) == len(signals)
    report['checks'] = {'signalsAfterReturnedResults': True, 'greenDurationWithinBound': True,
                        'actualSignalCount': len(signals), 'signalExample': signals[0],
                        'signalsMatchGreenWithoutYellow': True, 'atMostOneExtensionPerPhase': True,
                        'completeTaskCount': first['metrics']['completed'],
                        'signalLinksHaveActualPerLinkState': all('state' in link for x in active['signals'] for link in x['links'])}
    by_node = {}
    for task in first['tasks']:
        by_node.setdefault(task['target'], []).append(task)
    report['checks']['singleServerReservationsDoNotOverlap'] = all(
        b['start'] + 1e-9 >= a['finish'] for jobs in by_node.values()
        for a, b in zip(sorted(jobs, key=lambda t: t['start']), sorted(jobs, key=lambda t: t['start'])[1:]))
    assert report['checks']['singleServerReservationsDoNotOverlap']
    sim.control('reset')
    assert sim.sim_time == 0 and not sim.tasks and sim.metrics['sensed'] == 0
    for _ in range(600):
        sim.step()
    second = sim.export()
    digest = lambda obj: hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
    report['checks']['resetSameSeedTasksIdentical'] = first['tasks'] == second['tasks']
    report['checks']['resetSameSeedEventsIdentical'] = first['events'] == second['events']
    report['checks']['resetSameSeedMetricsIdentical'] = first['metrics'] == second['metrics']
    report['taskSha256'] = [digest(first['tasks']), digest(second['tasks'])]
    assert all(report['checks'][x] for x in ['resetSameSeedTasksIdentical', 'resetSameSeedEventsIdentical', 'resetSameSeedMetricsIdentical'])
    response = sim.control('scheduler', 'local')
    assert response['reset'] and sim.sim_time == 0 and sim.scheduler == 'local'
    for _ in range(600):
        sim.step()
    local = sim.export()
    report['local120SecondsMetrics'] = local['metrics']
    (output / 'backend_local120s_export.json').write_text(json.dumps(local, ensure_ascii=False, indent=2), encoding='utf-8')
    report['checks']['localSchedulerHasNoOffload'] = all(not t['offloaded'] and t['origin'] == t['target'] for t in local['tasks'])
    assert local['tasks'] and report['checks']['localSchedulerHasNoOffload']
    sim.close()
    report['limitations'] = ['synthetic acoustics and timing', 'not a traffic-benefit study', '120 seconds is a bounded integration check']
    (output / 'backend_verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
