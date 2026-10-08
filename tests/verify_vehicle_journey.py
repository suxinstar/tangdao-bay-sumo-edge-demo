"""Bounded real-SUMO journey verification, using its own process and run folders.

No HTTP listener is opened and no existing demonstration service is contacted.
Run from the project root: python -B tests/verify_vehicle_journey.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from simulation import DemoSimulation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=300)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/cyberpunk_20261008/backend_trace.json')
    args = parser.parse_args()
    if not 50 <= args.seconds <= 350:
        parser.error('--seconds must be between 50 and 350')
    portable_sumo = ROOT / '_runtime/sumo-1.25.0'
    if (portable_sumo / 'bin/sumo.exe').is_file():
        os.environ['SUMO_HOME'] = str(portable_sumo)
        os.environ['PATH'] = str(portable_sumo / 'bin') + os.pathsep + os.environ.get('PATH', '')
    started = time.monotonic()
    sim = DemoSimulation(ROOT, max_time=900)
    processes = []
    report = {'checkedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
              'method': 'Separate actual SUMO process; manual simulation steps; no HTTP service contacted',
              'simulationParameters': {'seed': sim.SEED, 'stepLengthS': sim.STEP,
                                       'scheduler': sim.scheduler, 'maxTimeS': sim.max_time},
              'checks': {}}
    try:
        assert sim.connection is not None and sim.status != 'error', sim.error
        processes.append(sim.process)
        report['sumoVersion'] = sim.sumo_version
        report['runId'] = sim.run_id
        initial_pending = {}
        count = 0
        sim.control('resume')
        while sim.sim_time + 1e-8 < args.seconds:
            sim.step()
            for task in sim.tasks[count:]:
                vehicle_id = task['vehicleId']
                if vehicle_id not in initial_pending:
                    trace = sim.vehicle_trace(vehicle_id)
                    initial_pending[vehicle_id] = {
                        'simTime': trace['simTime'], 'status': trace['status'],
                        'summary': trace['summary'], 'firstTask': trace['tasks'][0]}
            count = len(sim.tasks)
        candidates = []
        for vehicle_id, history in sim.vehicle_history.items():
            if history.get('present') or not history.get('arrivedAt'):
                continue
            trace = sim.vehicle_trace(vehicle_id)
            summary = trace['summary']
            if summary['completed'] >= 2 and summary['pending'] == 0 and summary['offloaded'] > 0:
                score = (sum(bool(task.get('controlApplied')) for task in trace['tasks']),
                         summary['completed'], summary['offloaded'])
                candidates.append((score, vehicle_id, trace))
        assert candidates, 'No complete departed/offloaded multi-task journey in this interval; inspect or use --seconds 350'
        _, vehicle_id, trace = max(candidates, key=lambda item: item[0])
        snapshot = sim.snapshot()
        all_vehicle_tasks = [sim._task_view(task) for task in sim.tasks if task['vehicleId'] == vehicle_id]
        all_vehicle_events = [event for event in sim.events if event.get('vehicleId') == vehicle_id]
        visible_ids = {task['id'] for task in snapshot['tasks']}
        hidden_ids = [task['id'] for task in trace['tasks'] if task['id'] not in visible_ids]
        check = report['checks']
        check['allVehicleTasksRetained'] = trace['tasks'] == all_vehicle_tasks
        check['allVehicleEventsRetained'] = trace['events'] == all_vehicle_events
        check['historySurvivesSnapshotTruncation'] = bool(hidden_ids)
        check['departedLastObservationRetained'] = (
            trace['status'] == 'departed' and trace['vehicle'] is not None
            and trace['lastSeen'] < trace['arrivedAt'] <= trace['simTime'])
        check['pendingWasObservedBeforeCompletion'] = initial_pending[vehicle_id]['summary']['pending'] > 0
        check['completionUsesReturnedFlagAndObservedTime'] = all(
            task.get('returned') and task['status'] == 'done'
            and task['observedReturnTime'] + 1e-8 >= task['returnEnd']
            for task in trace['tasks'])
        check['everyCompletedTaskHasObservedSignalDecision'] = all(
            isinstance(task.get('controlApplied'), bool) and task.get('controlReason')
            and task['controlCheckedTime'] >= task['observedReturnTime']
            for task in trace['tasks'])
        check['containsActualCrossStationDispatch'] = any(
            task['offloaded'] and task['origin'] != task['target'] for task in trace['tasks'])
        check['containsActualAppliedGreenResponse'] = any(task.get('controlApplied') for task in trace['tasks'])
        applied = [event for event in trace['events'] if event['type'] == 'signal']
        check['appliedResponsesRespectExistingSafetyBounds'] = bool(applied) and all(
            event['time'] + 1e-8 >= event['returnEnd'] and event['matchingGreen']
            and 'y' not in event['beforeState'].lower()
            and event['spent'] + event['afterRemaining'] <= sim.MAX_GREEN + 1e-8
            for event in applied)
        report.update(simTime=sim.sim_time, globalMetrics=dict(sim.metrics),
                      selectedVehicle=vehicle_id, initialPending=initial_pending[vehicle_id],
                      journey=trace, taskIdsOmittedByGlobalSnapshot=hidden_ids,
                      snapshotCounts={'tasks': len(snapshot['tasks']), 'events': len(snapshot['events'])},
                      qualifyingDepartedJourneys=len(candidates))
        before_reset = sim.run_id
        response = sim.control('reset')
        processes.append(sim.process)
        after_reset = sim.vehicle_trace(vehicle_id)
        check['resetStartedFreshRun'] = response['ok'] and sim.sim_time == 0 and sim.run_id != before_reset
        check['resetClearedVehicleHistory'] = (after_reset['status'] == 'unknown'
            and after_reset['vehicle'] is None and not after_reset['tasks'] and not after_reset['events']
            and after_reset['summary'] == {'sensed': 0, 'completed': 0, 'offloaded': 0, 'pending': 0})
        report['resetEvidence'] = {'runId': sim.run_id, 'simTime': sim.sim_time,
                                   'previousVehicleStatus': after_reset['status'],
                                   'summary': after_reset['summary']}
        assert all(check.values()), {key: value for key, value in check.items() if not value}
    finally:
        sim.close()
        report['ownSumoProcessesClosed'] = all(process is None or process.poll() is not None for process in processes)
    report['wallTimeSeconds'] = round(time.monotonic() - started, 3)
    report['limitations'] = [
        'Actual SUMO vehicle trajectories; synthetic acoustic tasks and RSU/communication timings.',
        'Bounded integration check; does not establish real traffic-efficiency gains.',
        'HTTP parameter behavior is covered separately by tests/test_vehicle_trace.py.',
        'Current user HTTP service was neither queried nor restarted.']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': all(report['checks'].values()), 'checks': len(report['checks']),
                      'simTime': report['simTime'], 'vehicle': report['selectedVehicle'],
                      'summary': report['journey']['summary'], 'ownSumoProcessesClosed': report['ownSumoProcessesClosed'],
                      'wallTimeSeconds': report['wallTimeSeconds']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
