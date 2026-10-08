"""Actual nine-RSU SUMO verification for both trained MEO specialist policies."""
from pathlib import Path
import sys, os, json, time, hashlib, math
from collections import Counter, defaultdict
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['SUMO_HOME'] = str(ROOT / '_runtime/sumo-1.25.0')
os.environ['PATH'] = str(ROOT / '_runtime/sumo-1.25.0/bin') + os.pathsep + os.environ['PATH']
from simulation import DemoSimulation

def run(policy, seconds=600):
    sim = DemoSimulation(ROOT, scheduler=policy, max_time=seconds)
    assert sim.status != 'error', sim.error
    durations=[]; peak=0; collisions=0; teleports=0
    try:
        for _ in range(round(seconds/sim.STEP)):
            start=time.perf_counter();sim.step();durations.append((time.perf_counter()-start)*1000)
            peak=max(peak,len(sim.vehicles))
            collisions+=sim.connection.simulation.getCollidingVehiclesNumber()
            teleports+=sim.connection.simulation.getStartingTeleportNumber()
            assert all(not t.get('returned') and not t.get('dropped') and 'controlApplied' not in t for t in sim.pending_tasks.values())
        completed=[t for t in sim.tasks if t.get('returned')]
        dropped=[t for t in sim.tasks if t.get('dropped')]
        accepted=[t for t in sim.tasks if not t.get('dropped')]
        checks={
            'all_nine_stations_observed':len({t['origin'] for t in sim.tasks})==9,
            'real_trained_decision_every_task':all(t.get('policyDecision',{}).get('mode') in ('local','offload','drop') for t in sim.tasks),
            'counter_partition':len(sim.tasks)==len(completed)+len(dropped)+len(sim.pending_tasks),
            'metrics_match':sim.metrics['completed']==len(completed) and sim.metrics['dropped']==len(dropped) and sim.metrics['sensed']==len(sim.tasks),
            'drop_never_completes_or_controls':all('returned' not in t and 'controlApplied' not in t and t['target'] is None and t['id'] not in sim.pending_tasks for t in dropped),
            'timings_ordered':all(t['created']<=t['txEnd']<=t['start']<t['finish']<=t['returnEnd'] for t in accepted),
            'control_only_after_observed_return':bool(completed) and all(t['controlCheckedTime']>=t['observedReturnTime'] and t['observedReturnTime']+1e-8>=t['returnEnd'] for t in completed),
            'correct_mode_target':all((t['target']==t['origin'])==(t['policyDecision']['mode']=='local') for t in accepted),
            'no_collisions_or_teleports':collisions==0 and teleports==0,
            'no_reservation_overlap':True,
            'single_vehicle_snapshot_coherent':True,
            'safe_green_extension':True,
        }
        queues=defaultdict(list)
        for t in accepted:queues[t['target']].append(t)
        for tasks in queues.values():
            for a,b in zip(tasks,tasks[1:]):
                checks['no_reservation_overlap'] &= b['start']+1e-8>=a['finish']
        for vehicle in list(sim.tasks_by_vehicle)[:20]:
            state=sim.snapshot(vehicle);trace=state['vehicleTrace'];s=trace['summary']
            checks['single_vehicle_snapshot_coherent'] &= trace['simTime']==state['simTime'] and trace['runId']==state['runId'] and s['sensed']==s['completed']+s['pending']+s.get('dropped',0)
        for event in sim.events:
            if event['type']=='signal':
                checks['safe_green_extension'] &= event['matchingGreen'] and 'y' not in event['beforeState'].lower() and event['spent']+event['afterRemaining']<=55+1e-8
        durations.sort()
        result={'policy':policy,'simTime':sim.sim_time,'checks':checks,'passed':all(checks.values()),'metrics':dict(sim.metrics),'peakVehicles':peak,'collisions':collisions,'teleports':teleports,
                'modeCounts':dict(Counter(t['policyDecision']['mode'] for t in sim.tasks)),
                'modelCounts':dict(Counter(t['policyDecision'].get('modelId','none') for t in sim.tasks)),
                'originCounts':dict(Counter(t['origin'] for t in sim.tasks)),
                'stepWallMs':{'mean':sum(durations)/len(durations),'p95':durations[math.ceil(.95*len(durations))-1],'max':max(durations)},
                'schedulerModel':sim._configuration()['schedulerModel'],
                'sampleTasks':sim.export()['tasks'][:10]}
        assert result['passed'],result['checks']
        return result
    finally:sim.close()

if __name__=='__main__':
    output=ROOT/'evidence/v1_5_20261008/meo_sumo_integration.json';output.parent.mkdir(parents=True,exist_ok=True)
    paths=['simulation.py','integration/meo_policy.py','data/scene.json','scenario/tangdao.net.xml','scenario/traffic.rou.xml']
    inputs={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    results=[]
    for policy in ('meo_completion','meo_accuracy'):
        r=run(policy);results.append(r);print(json.dumps({k:r[k] for k in ('policy','passed','metrics','stepWallMs')},ensure_ascii=False),flush=True)
    report={'passed':all(r['passed'] for r in results),'scope':'Actual SUMO with trained actor transferred through documented synthetic observations and serial queue; not acoustic inference or original training benchmark reproduction.','inputSha256':inputs,'results':results}
    assert inputs=={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},'inputs changed during check'
    output.write_bytes((json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode())
