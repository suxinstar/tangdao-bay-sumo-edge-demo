"""Evidence-bound integration check for the expanded real-SUMO demonstration.

Starts its own SUMO process and manually advances 600 or 900 simulated seconds.
Never contacts, resets, or shuts down a running localhost demonstration service.
Run from the project root: python -B tests/verify_expanded_journey.py
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from simulation import DemoSimulation


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, max(0, math.ceil(len(ordered)*fraction)-1))] if ordered else 0.0


def fingerprint_inputs():
    paths = [ROOT / name for name in (
        'simulation.py', 'data/scene.json', 'data/source_manifest.json',
        'scenario/tangdao.net.xml', 'scenario/traffic.rou.xml',
        'scenario/tangdao.sumocfg', 'tests/verify_expanded_journey.py')]
    paths += sorted((ROOT / 'integration').glob('*.py'))
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, choices=(600, 900), default=600)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'evidence/v1_4_20261008/sumo_integration.json')
    args = parser.parse_args()
    portable_sumo = ROOT / '_runtime/sumo-1.25.0'
    if (portable_sumo / 'bin/sumo.exe').is_file():
        os.environ['SUMO_HOME'] = str(portable_sumo)
        os.environ['PATH'] = str(portable_sumo / 'bin') + os.pathsep + os.environ.get('PATH', '')

    started, cpu_started = time.perf_counter(), time.process_time()
    sim, process = None, None
    report = {
        'checkedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
        'method': 'Independent actual SUMO process; direct TraCI steps; no HTTP service contacted',
        'inputSha256': fingerprint_inputs(),
        'requestedSimulationSeconds': args.seconds,
        'checks': {},
        'limitations': [
            'Actual SUMO motion on an OSM map; generated traffic demand, synthetic acoustic observations and timing.',
            'No microphone recording or real sound-recognition model was evaluated.',
            'Wall-time figures measure this bounded backend run, not browser FPS or physical edge-device latency.',
            'Control assertions verify local SUMO safety gates, not real traffic-signal deployment or efficiency gains.',
            'The existing user HTTP service was neither contacted nor restarted.'
        ],
    }
    check = report['checks']
    try:
        sim = DemoSimulation(ROOT, max_time=args.seconds)
        process = sim.process
        if sim.connection is None or sim.status == 'error':
            raise RuntimeError(sim.error or 'SUMO did not start')
        report.update(runId=sim.run_id, sumoVersion=sim.sumo_version,
                      parameters=sim._configuration())
        initial_pending, steps_ms = {}, []
        coherent_samples, snapshot_samples, peak_vehicles, task_count = [], [], 0, 0
        observed_no_early_control = True
        sim.control('resume')
        while sim.sim_time + 1e-8 < args.seconds and sim.status == 'running':
            before = time.perf_counter()
            sim.step()
            steps_ms.append((time.perf_counter()-before)*1000)
            peak_vehicles = max(peak_vehicles, len(sim.vehicles))
            for task in sim.tasks[task_count:]:
                vehicle_id = task['vehicleId']
                if vehicle_id not in initial_pending:
                    trace = sim.vehicle_trace(vehicle_id)
                    initial_pending[vehicle_id] = {
                        'simTime': trace['simTime'], 'status': trace['status'],
                        'summary': trace['summary'], 'firstTask': trace['tasks'][0]}
            task_count = len(sim.tasks)
            observed_no_early_control &= all(
                'controlCheckedTime' not in task and not task.get('controlApplied')
                for task in sim.pending_tasks.values())
            # Exercise the same bounded combined response used by follow mode.
            # Only sample at five-second intervals; do not turn this test into a
            # synthetic HTTP load generator that distorts SUMO step timing.
            if len(steps_ms) % 25 == 0:
                vehicle_id = next(iter(initial_pending), None)
                if vehicle_id:
                    snapshot_started = time.perf_counter()
                    raw = sim.snapshot_json(vehicle_id)
                    decoded = json.loads(raw)
                    snapshot_samples.append({'simTime': sim.sim_time,
                        'encodeMilliseconds': (time.perf_counter()-snapshot_started)*1000,
                        'bytes': len(raw)})
                    coherent_samples.append(
                        decoded['runId'] == decoded['vehicleTrace']['runId'] == sim.run_id
                        and decoded['simTime'] == decoded['vehicleTrace']['simTime'] == sim.sim_time
                        and decoded == sim.snapshot(vehicle_id)
                        and raw == sim.snapshot_json(vehicle_id))

        check['simulationAdvancedToRequestedEndOrFinishedDemand'] = (
            sim.sim_time + 1e-8 >= args.seconds or
            (sim.status == 'finished' and not sim.pending_tasks
             and sim.connection.simulation.getMinExpectedNumber() == 0))
        check['exactlyNineConfiguredStations'] = len(sim.node_by_id) == 9
        origin_counts = Counter(task['origin'] for task in sim.tasks)
        completed = [task for task in sim.tasks if task.get('returned')]
        pending = [task for task in sim.tasks if not task.get('returned')]
        offloaded = [task for task in sim.tasks if task['offloaded'] and task['origin'] != task['target']]
        check['allNineStationsGeneratedObservedTasks'] = (
            set(origin_counts) == set(sim.node_by_id) and all(origin_counts.values()))
        check['actualCrossStationOffloadingObserved'] = bool(offloaded)
        check['pendingTasksNeverControlledBeforeReturn'] = observed_no_early_control
        check['pendingIndexMatchesUnreturnedTasks'] = (
            set(sim.pending_tasks) == {task['id'] for task in pending})
        check['allCompletionAndControlTimesRespectReturn'] = bool(completed) and all(
            task['observedReturnTime'] + 1e-8 >= task['returnEnd']
            and task['controlCheckedTime'] + 1e-8 >= task['observedReturnTime']
            and isinstance(task.get('controlApplied'), bool) and task.get('controlReason')
            for task in completed)
        check['allTaskReservationTimesAreOrdered'] = all(
            task['created'] <= task['txEnd'] <= task['start'] < task['finish'] <= task['returnEnd']
            for task in sim.tasks)
        check['oneTaskPerVehicleStationEncounter'] = (
            len({(task['vehicleId'], task['origin']) for task in sim.tasks}) == len(sim.tasks))
        complete_events = {event['taskId']: event for event in sim.events if event['type'] == 'complete'}
        decisions = [event for event in sim.events if event['type'] in ('signal', 'signal_skip')]
        actions = [event for event in decisions if event['type'] == 'signal']
        check['everySignalDecisionFollowsItsCompletionEvent'] = bool(decisions) and all(
            event['taskId'] in complete_events
            and complete_events[event['taskId']]['id'] < event['id']
            and complete_events[event['taskId']]['time'] <= event['time']
            and event['time'] + 1e-8 >= event['returnEnd'] for event in decisions)
        check['actualGreenExtensionObserved'] = bool(actions)
        check['allAppliedGreenExtensionsRespectSafetyLimits'] = bool(actions) and all(
            event['matchingGreen'] and 'y' not in event['beforeState'].lower()
            and event['afterRemaining'] > event['beforeRemaining']
            and event['spent'] + event['afterRemaining'] <= sim.MAX_GREEN + 1e-8
            for event in actions)
        check['globalCountersMatchActualTaskRecords'] = (
            sim.metrics['sensed'] == len(sim.tasks)
            and sim.metrics['completed'] == len(completed)
            and sim.metrics['offloaded'] == len(offloaded)
            and sim.metrics['signalActions'] == len(actions)
            and sim.metrics['signalRejected'] == len(decisions)-len(actions))
        check['combinedFollowSnapshotsRemainCoherent'] = bool(coherent_samples) and all(coherent_samples)

        snapshot = sim.snapshot()
        recent_ids = {task['id'] for task in sorted(completed, key=lambda task: task['id'])[-10:]}
        displayed_completed = {task['id'] for task in snapshot['tasks'] if task.get('returned')}
        check['recentCompletionWindowRetainsLatestTen'] = recent_ids <= displayed_completed
        candidates = []
        for vehicle_id, history in sim.vehicle_history.items():
            if history.get('present') or history.get('arrivedAt') is None:
                continue
            trace = sim.vehicle_trace(vehicle_id)
            summary = trace['summary']
            if summary['completed'] >= 2 and summary['pending'] == 0 and summary['offloaded'] > 0:
                score = (len({task['origin'] for task in trace['tasks']}),
                         sum(bool(task.get('controlApplied')) for task in trace['tasks']),
                         summary['completed'], summary['offloaded'])
                candidates.append((score, vehicle_id, trace))
        check['departedVehicleWithCompleteMultiStationJourneyExists'] = bool(candidates)
        if not candidates:
            raise AssertionError('No completed, departed, multi-station offloaded journey in this run')
        _, vehicle_id, trace = max(candidates, key=lambda row: row[0])
        retained_tasks = [sim._task_view(task) for task in sim.tasks if task['vehicleId'] == vehicle_id]
        retained_events = [event for event in sim.events if event.get('vehicleId') == vehicle_id]
        global_ids = {task['id'] for task in snapshot['tasks']}
        omitted = [task['id'] for task in trace['tasks'] if task['id'] not in global_ids]
        check['departedVehicleRetainsEveryTaskAndEvent'] = (
            trace['tasks'] == retained_tasks and trace['events'] == retained_events)
        check['departedHistorySurvivesGlobalSnapshotTruncation'] = bool(omitted)
        check['departedVehicleRetainsLastObservedPose'] = (
            trace['status'] == 'departed' and trace['vehicle'] is not None
            and trace['lastSeen'] < trace['arrivedAt'] <= trace['simTime'])
        check['selectedJourneyWasObservedWhilePending'] = initial_pending[vehicle_id]['summary']['pending'] > 0
        check['selectedJourneySpansMultipleStationsAndCompletes'] = (
            len({task['origin'] for task in trace['tasks']}) >= 2
            and all(task.get('returned') and task['status'] == 'done' for task in trace['tasks']))
        check['noInputFilesChangedDuringTheRun'] = fingerprint_inputs() == report['inputSha256']

        latencies = [task['observedReturnTime']-task['created'] for task in completed]
        queues = [task['start']-task['txEnd'] for task in completed]
        report.update(
            simTime=sim.sim_time, simulationStatus=sim.status, globalMetrics=dict(sim.metrics),
            selectedVehicle=vehicle_id, initialPending=initial_pending[vehicle_id], journey=trace,
            taskIdsOmittedByGlobalSnapshot=omitted, qualifyingDepartedJourneys=len(candidates),
            peakSimultaneousVehicles=peak_vehicles, taskCount=len(sim.tasks),
            pendingTaskCount=len(pending), crossStationTaskCount=len(offloaded),
            perStationSensed=dict(sorted(origin_counts.items())),
            perStationCompletedAsOrigin=dict(sorted(Counter(task['origin'] for task in completed).items())),
            latencySeconds={'mean':sum(latencies)/len(latencies), 'p95':percentile(latencies,.95), 'max':max(latencies)},
            queueSeconds={'mean':sum(queues)/len(queues), 'p95':percentile(queues,.95), 'max':max(queues)},
            backendStepTimingMilliseconds={'count':len(steps_ms), 'mean':sum(steps_ms)/len(steps_ms),
                'p95':percentile(steps_ms,.95),'p99':percentile(steps_ms,.99),'max':max(steps_ms)},
            combinedSnapshotMeasurements={'count':len(snapshot_samples),
                'maxBytes':max(s['bytes'] for s in snapshot_samples),
                'p95EncodeAndDecodeMilliseconds':percentile([s['encodeMilliseconds'] for s in snapshot_samples],.95)},
            firstAppliedSignalEvidence=actions[0] if actions else None,
            inputFileCount=len(report['inputSha256']))
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        check['executionCompletedWithoutError'] = False
    finally:
        if sim is not None:
            sim.close()
        report['ownSumoProcessClosed'] = process is None or process.poll() is not None
        check['ownSumoProcessClosed'] = report['ownSumoProcessClosed']
        report['wallTimeSeconds'] = round(time.perf_counter()-started,3)
        report['pythonProcessCpuSeconds'] = round(time.process_time()-cpu_started,3)
        report['passed'] = bool(check) and all(check.values())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'checks':len(check),
                      'failedChecks':[key for key,value in check.items() if not value],
                      'simTime':report.get('simTime'),'vehicle':report.get('selectedVehicle'),
                      'stations':report.get('perStationSensed'),
                      'tasks':report.get('taskCount'),'peakVehicles':report.get('peakSimultaneousVehicles'),
                      'wallTimeSeconds':report['wallTimeSeconds'],'error':report.get('error')},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
