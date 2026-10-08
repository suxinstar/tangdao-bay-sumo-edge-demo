"""Actual SUMO traffic plus explicitly synthetic acoustic/edge demonstration.

The edge service, transmission and lane observations are demonstration parameters,
not measured microphone inference. All times below are SUMO simulation seconds.
"""
from __future__ import annotations

import copy
import datetime as dt
from collections import deque
import heapq
import json
import math
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4
from integration import (AudioFrame, SyntheticPerception, SyntheticCompute, SyntheticTransport,
                         LeastFinishScheduler, BoundedGreenPolicy, TraCISignalActuator)

SCHEDULERS = ('meo_completion', 'meo_accuracy', 'least_finish', 'local')


def reserve_meo_task(nodes, origin, now, decision, work_factor=1.0, *,
                     compute_provider=None, transport_provider=None):
    """Apply a learned action without replacing it with a heuristic choice."""
    mode = decision.get('mode')
    target = decision.get('target')
    if mode == 'drop' or (mode == 'offload' and decision.get('dropped')):
        if target is not None:
            raise ValueError('a dropped action cannot reserve a compute node')
        return {'target': None, 'offloaded': False, 'dropped': True,
                'dropReason': 'MEO 卸载无有效邻居，丢弃本次任务' if mode == 'offload' else 'MEO 策略选择丢弃本次任务',
                'policyDecision': decision}
    if mode not in ('local', 'offload') or target not in nodes:
        raise ValueError('MEO returned an invalid execution mode or node')
    if (mode == 'local') != (target == origin):
        raise ValueError('MEO execution mode and node disagree')
    factor = decision.get('computeFactor')
    if type(factor) not in (int, float) or not math.isfinite(factor) or factor <= 0:
        raise ValueError('MEO returned an invalid model compute factor')
    source, node = nodes[origin], nodes[target]
    distance = math.hypot(node['x']-source['x'], node['y']-source['y'])
    tx, back = (transport_provider or SyntheticTransport()).estimate_seconds(distance, remote=target != origin)
    service = (compute_provider or SyntheticCompute()).estimate_seconds(node, work_factor * factor)
    if any(not math.isfinite(x) or x < 0 for x in (tx, back, service)) or service == 0:
        raise ValueError('invalid MEO transport or compute estimate')
    arrival = now + tx
    start = max(arrival, node['available'])
    finish = start + service
    nodes[target]['available'] = finish
    return {'target': target, 'txEnd': arrival, 'start': start, 'finish': finish,
            'returnEnd': finish + back, 'offloaded': target != origin, 'policyDecision': decision}


def reserve_task(nodes, origin, now, scheduler, work_factor=1.0, *,
                 scheduler_provider=None, compute_provider=None, transport_provider=None):
    """Reserve one non-preemptive worker; existing dispatch reservations stay fixed."""
    candidates = []
    source = nodes[origin]
    for node_id, node in sorted(nodes.items()):
        if scheduler == 'local' and node_id != origin:
            continue
        remote = node_id != origin
        distance = math.hypot(node['x'] - source['x'], node['y'] - source['y'])
        transfer, return_time = (transport_provider or SyntheticTransport()).estimate_seconds(distance, remote=remote)
        service = (compute_provider or SyntheticCompute()).estimate_seconds(node, work_factor)
        if any(not math.isfinite(value) or value < 0 for value in (transfer, return_time, service)) or service == 0:
            raise ValueError('transport and compute estimates must be finite and non-negative; compute must be positive')
        arrival = now + transfer
        start = max(arrival, node['available'])
        finish = start + service
        candidates.append({'target': node_id, 'txEnd': arrival, 'start': start,
                           'finish': finish, 'returnEnd': finish + return_time, 'offloaded': remote})
    target = (scheduler_provider or LeastFinishScheduler()).select([dict(item) for item in candidates], origin=origin, policy=scheduler)
    selected = next((item for item in candidates if item['target'] == target), None)
    if selected is None or (scheduler == 'local' and target != origin):
        raise ValueError('scheduler returned an unavailable or forbidden compute node')
    nodes[target]['available'] = selected['finish']
    return selected


def extension_decision(task, now, *, matching_green, has_yellow, already_extended,
                       spent, remaining, maximum_green=55.0, extension=4.0):
    """Pure safety gate: no result, no control; never request a phase transition."""
    return BoundedGreenPolicy().decide(result_returned=now + 1e-8 >= task['returnEnd'],
                                      matching_green=matching_green, has_yellow=has_yellow,
                                      already_extended=already_extended, spent=spent, remaining=remaining,
                                      maximum_green=maximum_green, extension=extension)


class DemoSimulation:
    STEP = 0.2
    SEED = 42
    MAX_GREEN = 55.0
    EXTENSION = 4.0

    def __init__(self, root, *, scheduler='least_finish', speed=1.0, autostart=False,
                 service_time=None, max_time=900.0, model_gateway=None,
                 scheduler_provider=None, compute_provider=None, transport_provider=None):
        self.root = Path(root).resolve()
        self.scene = json.loads((self.root / 'data/scene.json').read_text(encoding='utf-8'))
        if scheduler not in SCHEDULERS:
            raise ValueError('unknown scheduler policy')
        self.scheduler = scheduler
        self.speed = float(speed)
        self.service_time = service_time
        self.max_time = float(max_time)
        self.model_gateway = model_gateway
        self.scheduler_provider = scheduler_provider or LeastFinishScheduler()
        self.compute_provider = compute_provider or SyntheticCompute()
        self.transport_provider = transport_provider or SyntheticTransport()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.connection = None
        self.process = None
        self.sumo_output = None
        self.event_output = None
        self.thread = None
        self.startup_wall_time = dt.datetime.now(dt.timezone.utc).isoformat()
        self._reset_data()
        try:
            self._start_sumo()
            self.status = 'running' if autostart else 'paused'
        except Exception as exc:
            self.status = 'error'
            self.error = str(exc)
            self._close_sumo()

    def _reset_data(self):
        if self.scheduler.startswith('meo_'):
            from integration.meo_policy import MeoScheduler
            self.meo_scheduler = MeoScheduler(self.root, variant=self.scheduler.removeprefix('meo_'))
        else:
            self.meo_scheduler = None
        self.rng = random.Random(self.SEED)
        self.status, self.error, self.sim_time = 'paused', None, 0.0
        self.tasks, self.events, self.vehicles, self.signals = [], [], [], []
        self.pending_tasks, self.return_heap, self.recent_completed = {}, [], []
        self.recent_controls = deque()
        self.model_results = deque(maxlen=16)
        self._incoming_by_lane = None
        self._snapshot_json_cache = None
        self.intersection_positions = {item['id']: item for item in self.scene['intersections']}
        self.perception_provider = SyntheticPerception()
        self.signal_actuator = TraCISignalActuator()
        # Keep per-vehicle observations and task references for the journey API.
        # The global snapshot remains bounded; no extra simulation work is reserved.
        self.vehicle_history = {}
        self.tasks_by_vehicle, self.events_by_vehicle, self.task_by_id = {}, {}, {}
        self.subscribed = set()
        self.seen, self.extended, self.phase_epochs, self.last_phases = set(), set(), {}, {}
        self.node_by_id = {}
        for raw in self.scene['rsus']:
            node = dict(raw)
            node.update(available=0.0, completed=0, offloaded=0)
            node['serviceTimeS'] = float(self.service_time or node.get('serviceTimeS', 1.6))
            self.node_by_id[node['id']] = node
        self.incoming, self.link_positions, self.tls_links = {}, {}, {}
        self.departure_times, self.trip_times = {}, []
        self.trip_time_sum = 0.0
        self.metrics = {'sensed': 0, 'completed': 0, 'dropped': 0, 'offloaded': 0, 'meanLatency': 0.0,
                        'meanQueueDelay': 0.0, 'signalActions': 0, 'signalRejected': 0,
                        'vehicles': 0, 'completedTrips': 0, 'meanTripTime': None,
                        'haltingVehicles': 0}
        self.latency_sum = self.queue_sum = 0.0
        self.run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ') + '_' + uuid4().hex[:8]

    def _start_sumo(self):
        sumo_home = Path(os.environ.get('SUMO_HOME', r'C:\Program Files (x86)\Eclipse\Sumo'))
        tools_dir = str(sumo_home / 'tools')
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import traci
        import traci.constants as tc
        self.vehicle_vars = (tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_LANE_ID)
        executable = shutil.which('sumo') or str(sumo_home / 'bin/sumo.exe')
        config = self.root / 'scenario/tangdao.sumocfg'
        if not config.is_file():
            raise FileNotFoundError(f'缺少 SUMO 场景：{config}')
        if not self.node_by_id:
            raise ValueError('场景至少需要一个 RSU')
        run_dir = self.root / 'runs' / self.run_id
        run_dir.mkdir(parents=True)
        self.run_dir = run_dir
        self.sumo_output = (run_dir / 'sumo.log').open('w', encoding='utf-8')
        self.event_output = (run_dir / 'events.jsonl').open('w', encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        command = [executable, '-c', str(config), '--seed', str(self.SEED),
                   '--step-length', str(self.STEP), '--remote-port', str(port),
                   '--end', str(self.max_time),
                   '--no-step-log', 'true', '--duration-log.disable', 'true',
                   '--quit-on-end', 'true', '--start', 'true']
        self.process = subprocess.Popen(command, cwd=self.root, stdout=self.sumo_output,
                                        stderr=subprocess.STDOUT,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.connection = traci.connect(port, numRetries=30, host='127.0.0.1',
                                        proc=self.process, waitBetweenRetries=0.1)
        self.sumo_version = self.connection.getVersion()[1]
        tls_domain = self.connection.trafficlight
        actual_tls = set(tls_domain.getIDList())
        for node_id, node in self.node_by_id.items():
            tls = node['intersectionId']
            if tls not in actual_tls:
                raise ValueError(f'{node_id} 关联信号灯不存在：{tls}')
            links = tls_domain.getControlledLinks(tls)
            self.tls_links[tls] = links
            self.incoming[node_id] = {link[0] for group in links for link in group}
            node['incomingLaneIds'] = sorted(self.incoming[node_id])
            positions = []
            for link_index, group in enumerate(links):
                for incoming_lane, _, _ in group:
                    shape = self.connection.lane.getShape(incoming_lane)
                    if not shape:
                        continue
                    x, y = shape[-1]
                    angle = 0.0
                    if len(shape) > 1:
                        dx, dy = x - shape[-2][0], y - shape[-2][1]
                        angle = math.degrees(math.atan2(dx, dy)) % 360
                    positions.append({'laneId': incoming_lane, 'x': x, 'y': y,
                                      'angle': angle, 'linkIndex': link_index})
            self.link_positions[tls] = positions
        self._read_signals()
        (run_dir / 'run_config.json').write_text(json.dumps(self._configuration(), ensure_ascii=False, indent=2), encoding='utf-8')

    def _configuration(self):
        return {'runId': self.run_id, 'seed': self.SEED, 'stepLengthS': self.STEP,
                'scheduler': self.scheduler, 'maximumGreenS': self.MAX_GREEN,
                'schedulerModel': self.meo_scheduler.metadata() if self.meo_scheduler else None,
                'extensionS': self.EXTENSION, 'maximumSimulationTimeS': self.max_time,
                'serviceTimeSByRSU': {k: v['serviceTimeS'] for k, v in self.node_by_id.items()},
                'transmissionModel': 'local 0.04s; remote 0.28s + distance/1600m/s',
                'returnModel': 'local 0.04s; remote 0.16s + distance/2400m/s',
                'queueModel': 'one non-preemptive worker per RSU; immutable dispatch-order reservations',
                'acousticModel': 'synthetic event and ideal lane observation from actual SUMO incoming-lane encounter; no WAV inference',
                'serviceModel': 'demonstration service duration times seeded uniform[0.9,1.1]; not measured compute performance',
                'signalModel': 'SUMO synthetic plan; extend matching active green only after returned task; never set phase index',
                'modelIntegrationMode': 'shadow' if getattr(self, 'model_gateway', None) else 'not_configured',
                'realModelControlsSignals': False,
                'sumoVersion': getattr(self, 'sumo_version', None)}

    def start_background(self):
        self.thread = threading.Thread(target=self._loop, name='SUMO-demo', daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            with self.lock:
                if self.status == 'running':
                    try:
                        self.step()
                    except Exception as exc:
                        self.error, self.status = str(exc), 'error'
                        self._event('error', f'仿真停止：{exc}')
                delay = self.STEP / self.speed if self.status == 'running' else 0.05
            self.stop_event.wait(max(0.005, delay - (time.monotonic() - started)))

    def step(self):
        """Advance one actual SUMO step. Caller holds the lock or owns the instance."""
        conn = self.connection
        if conn is None:
            raise RuntimeError('SUMO 未连接')
        conn.simulationStep()
        self.sim_time = conn.simulation.getTime()
        for vehicle_id in conn.simulation.getDepartedIDList():
            self.departure_times[vehicle_id] = self.sim_time
            history = self.vehicle_history.setdefault(vehicle_id, {})
            history['enteredAt'] = self.sim_time
        for vehicle_id in conn.simulation.getArrivedIDList():
            history = self.vehicle_history.setdefault(vehicle_id, {})
            history.update(present=False, leftAt=self.sim_time, arrivedAt=self.sim_time)
            if vehicle_id in self.departure_times:
                trip_time = self.sim_time - self.departure_times.pop(vehicle_id)
                self.trip_times.append(trip_time)
                self.trip_time_sum += trip_time
        self.vehicles = []
        vehicle_ids = set(conn.vehicle.getIDList())
        for vehicle_id in self.subscribed - vehicle_ids:
            self.vehicle_history[vehicle_id].update(present=False, leftAt=self.sim_time)
        for vehicle_id in sorted(vehicle_ids - self.subscribed):
            conn.vehicle.subscribe(vehicle_id, self.vehicle_vars)
        self.subscribed = vehicle_ids
        position_var, angle_var, speed_var, lane_var = self.vehicle_vars
        for vehicle_id in sorted(vehicle_ids):
            observed = conn.vehicle.getSubscriptionResults(vehicle_id)
            x, y = observed[position_var]
            self.vehicles.append({'id': vehicle_id, 'x': x, 'y': y,
                                  'angle': observed[angle_var], 'speed': observed[speed_var],
                                  'laneId': observed[lane_var]})
            history = self.vehicle_history.setdefault(vehicle_id, {})
            history.setdefault('firstSeen', self.sim_time)
            history.update(present=True, lastSeen=self.sim_time, vehicle=dict(self.vehicles[-1]))
        self._read_signals()
        if self._incoming_by_lane is None:
            self._incoming_by_lane = {}
            for node_id in sorted(self.node_by_id):
                for lane_id in self.incoming[node_id]:
                    self._incoming_by_lane.setdefault(lane_id, []).append(node_id)
        for vehicle in self.vehicles:
            for node_id in self._incoming_by_lane.get(vehicle['laneId'], ()):
                node = self.node_by_id[node_id]
                key = (vehicle['id'], node_id)
                if key in self.seen:
                    continue
                if math.hypot(vehicle['x'] - node['x'], vehicle['y'] - node['y']) <= node['sensingRadiusM']:
                    self.seen.add(key)
                    self._sense(vehicle, node_id)
        due = []
        while self.return_heap and self.return_heap[0][0] <= self.sim_time + 1e-8:
            _, task_id = heapq.heappop(self.return_heap)
            task = self.pending_tasks.get(task_id)
            if task is not None:
                due.append(task)
        # Preserve dispatch-order control checks for results observed in the same step.
        for task in sorted(due, key=lambda item: item['id']):
            self._complete(task)
        if self.event_output:
            self.event_output.flush()
        self.metrics.update(vehicles=len(self.vehicles), completedTrips=len(self.trip_times),
                            meanTripTime=self.trip_time_sum / len(self.trip_times) if self.trip_times else None,
                            haltingVehicles=sum(v['speed'] < 0.1 for v in self.vehicles))
        self._collect_model_results()
        if self.sim_time >= self.max_time or (conn.simulation.getMinExpectedNumber() == 0 and not self.pending_tasks):
            self.status = 'finished'
            self._event('finished', '本轮仿真结束，可导出日志或重置')
            self._write_export()

    def _sense(self, vehicle, origin):
        task_id = f'T{len(self.tasks) + 1:05d}'
        work_factor = self.rng.uniform(0.9, 1.1)
        if self.meo_scheduler:
            decision = self.meo_scheduler.decide(self.node_by_id, origin, self.sim_time,
                        {'vehicleId': vehicle['id'], **vehicle}, self.pending_tasks, vehicles=self.vehicles)
            timing = reserve_meo_task(self.node_by_id, origin, self.sim_time, decision, work_factor,
                              compute_provider=getattr(self, 'compute_provider', None),
                              transport_provider=getattr(self, 'transport_provider', None))
        else:
            timing = reserve_task(self.node_by_id, origin, self.sim_time,
                              self.scheduler, work_factor,
                              scheduler_provider=getattr(self, 'scheduler_provider', None),
                              compute_provider=getattr(self, 'compute_provider', None),
                              transport_provider=getattr(self, 'transport_provider', None))
        observation = self.perception_provider.infer(AudioFrame(
            self.run_id, vehicle['id'], origin, self.sim_time, vehicle['laneId'], vehicle['speed']))
        task = {'id': task_id, 'vehicleId': vehicle['id'], 'origin': origin,
                'x': vehicle['x'], 'y': vehicle['y'], 'laneId': vehicle['laneId'],
                'created': self.sim_time, 'synthetic': True, 'perception': observation.to_dict(), **timing}
        self.tasks.append(task)
        if not task.get('dropped'):
            self.pending_tasks[task_id] = task
            heapq.heappush(self.return_heap, (task['returnEnd'], task_id))
        self.tasks_by_vehicle.setdefault(vehicle['id'], []).append(task)
        self.task_by_id[task_id] = task
        self.metrics['sensed'] += 1
        self.metrics['offloaded'] += int(task['offloaded'])
        self.node_by_id[origin]['offloaded'] += int(task['offloaded'])
        self._event('sense', f'{origin} 感知车辆 {vehicle["id"]}，生成合成声学任务', origin, task_id)
        if task.get('dropped'):
            self.metrics['dropped'] += 1
            heapq.heappush(self.recent_completed, (task_id, task))
            if len(self.recent_completed) > 10:
                heapq.heappop(self.recent_completed)
            self._event('drop', f'{task_id}：{task["dropReason"]}；不计算、不触发信号', origin, task_id,
                        task=copy.deepcopy(task))
            return
        self._event('dispatch', f'{task_id} → {task["target"]}（{"跨站卸载" if task["offloaded"] else "本地处理"}）', origin, task_id,
                    task=copy.deepcopy(task))

    def _complete(self, task):
        if task.get('returned') or task.get('dropped'):
            return
        task['returned'] = True
        self.pending_tasks.pop(task['id'], None)
        heapq.heappush(self.recent_completed, (task['id'], task))
        if len(self.recent_completed) > 10:
            heapq.heappop(self.recent_completed)
        task['observedReturnTime'] = self.sim_time
        self.metrics['completed'] += 1
        self.node_by_id[task['target']]['completed'] += 1
        self.latency_sum += task['observedReturnTime'] - task['created']
        self.queue_sum += task['start'] - task['txEnd']
        self.metrics['meanLatency'] = self.latency_sum / self.metrics['completed']
        self.metrics['meanQueueDelay'] = self.queue_sum / self.metrics['completed']
        self._event('complete', f'{task["id"]} 计算结果回传 {task["origin"]}', task['origin'], task['id'])
        self._apply_control(task)

    def _apply_control(self, task):
        if task.get('dropped'):
            return
        tls = self.node_by_id[task['origin']]['intersectionId']
        domain = self.connection.trafficlight
        state = domain.getRedYellowGreenState(tls)
        phase = domain.getPhase(tls)
        remaining = max(0.0, domain.getNextSwitch(tls) - self.sim_time)
        spent = domain.getSpentDuration(tls)
        matching = any(i < len(state) and state[i] in 'gG' and any(link[0] == task['laneId'] for link in group)
                       for i, group in enumerate(self.tls_links[tls]))
        epoch = (tls, self.phase_epochs.get(tls, 0))
        duration, reason = extension_decision(task, self.sim_time, matching_green=matching,
                                             has_yellow='y' in state.lower(), already_extended=epoch in self.extended,
                                             spent=spent, remaining=remaining,
                                             maximum_green=self.MAX_GREEN, extension=self.EXTENSION)
        task['controlCheckedTime'] = self.sim_time
        task['controlApplied'], task['controlReason'] = duration is not None, reason
        if duration is not None:
            self.signal_actuator.extend(domain, tls, duration)
            self.extended.add(epoch)
            self.recent_controls.append(task)
            self.metrics['signalActions'] += 1
            task['controlTls'], task['extensionS'] = tls, duration - remaining
            text = f'{task["id"]} 返回 → {tls} 当前绿灯延长 {duration - remaining:.1f} 秒'
            self._event('signal', text, task['origin'], task['id'], applied=True, tlsId=tls,
                        phase=phase, laneId=task['laneId'], spent=spent,
                        beforeRemaining=remaining, afterRemaining=duration, returnEnd=task['returnEnd'],
                        beforeState=state, matchingGreen=matching, phaseEpoch=epoch[1])
        else:
            self.metrics['signalRejected'] += 1
            text = f'{task["id"]} 返回 → 保持信号计划：{reason}'
            self._event('signal_skip', text, task['origin'], task['id'], applied=False, tlsId=tls,
                        phase=phase, laneId=task['laneId'], returnEnd=task['returnEnd'],
                        beforeState=state, matchingGreen=matching, phaseEpoch=epoch[1])
        self.last_action[tls] = text
        for signal in self.signals:
            if signal['id'] == tls:
                signal['lastAction'] = text
                if duration is not None:
                    signal.update(remaining=duration, mode='extended')
                break

    def _read_signals(self):
        if not hasattr(self, 'last_action'):
            self.last_action = {}
        positions = self.intersection_positions
        self.signals = []
        domain = self.connection.trafficlight
        for tls in sorted(self.tls_links):
            phase = domain.getPhase(tls)
            if self.last_phases.get(tls) != phase:
                self.phase_epochs[tls] = self.phase_epochs.get(tls, -1) + 1
                self.last_phases[tls] = phase
            state = domain.getRedYellowGreenState(tls)
            position = positions.get(tls, {'x': 0, 'y': 0})
            links = [dict(link, state=state[link['linkIndex']] if link['linkIndex'] < len(state) else 'r')
                     for link in self.link_positions.get(tls, [])]
            self.signals.append({'id': tls, 'x': position['x'], 'y': position['y'], 'phase': phase,
                                 'state': state, 'remaining': max(0, domain.getNextSwitch(tls) - self.sim_time),
                                 'mode': 'extended' if (tls, self.phase_epochs[tls]) in self.extended else 'baseline',
                                 'lastAction': self.last_action.get(tls, '等待已完成任务'), 'links': links,
                                 'controlledLanes': sorted({x['laneId'] for x in links})})

    def _event(self, event_type, text, rsu_id=None, task_id=None, **extra):
        event = {'id': len(self.events) + 1, 'time': self.sim_time, 'type': event_type,
                 'text': text, 'rsuId': rsu_id, 'taskId': task_id, **extra}
        if task_id in self.task_by_id:
            vehicle_id = self.task_by_id[task_id]['vehicleId']
            event['vehicleId'] = vehicle_id
            self.events_by_vehicle.setdefault(vehicle_id, []).append(event)
        self.events.append(event)
        if self.event_output:
            self.event_output.write(json.dumps(event, ensure_ascii=False) + '\n')

    def _task_view(self, task):
        if task.get('dropped'):
            return dict(task, status='dropped')
        now = self.sim_time
        status = ('done' if task.get('returned') else 'returning' if now >= task['finish'] else
                  'processing' if now >= task['start'] else 'queued' if now >= task['txEnd'] else 'transmitting')
        return dict(task, status=status)

    def vehicle_trace(self, vehicle_id, *, _copy=True):
        """Return only this vehicle's observed journey in the current run.

        A departed vehicle retains its last observation and unfinished tasks.
        Task status and control outcomes use the same observed state as export.
        """
        if (not isinstance(vehicle_id, str) or not vehicle_id.strip() or len(vehicle_id) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in vehicle_id)):
            raise ValueError('vehicle id must contain 1–256 characters and no control characters')
        with self.lock:
            history = self.vehicle_history.get(vehicle_id)
            tasks = self.tasks_by_vehicle.get(vehicle_id, [])
            status = ('present' if history.get('present') else 'departed') if history is not None else 'unknown'
            completed = sum(bool(task.get('returned')) for task in tasks)
            dropped = sum(bool(task.get('dropped')) for task in tasks)
            result = {'runId': self.run_id, 'simTime': self.sim_time, 'vehicleId': vehicle_id,
                      'status': status, 'vehicle': history.get('vehicle') if history else None,
                      'firstSeen': history.get('firstSeen') if history else None,
                      'lastSeen': history.get('lastSeen') if history else None,
                      'enteredAt': history.get('enteredAt') if history else None,
                      'leftAt': history.get('leftAt') if history else None,
                      'arrivedAt': history.get('arrivedAt') if history else None,
                      'tasks': [self._task_view(task) for task in tasks],
                      'events': self.events_by_vehicle.get(vehicle_id, []),
                      'summary': {'sensed': len(tasks), 'completed': completed,
                                  'offloaded': sum(bool(task['offloaded']) for task in tasks),
                                  'pending': len(tasks) - completed - dropped}}
            if dropped:
                result['summary']['dropped'] = dropped
            if status == 'unknown':
                result['error'] = 'vehicle not observed in the current run'
            return copy.deepcopy(result) if _copy else result

    def snapshot(self, vehicle_id=None, *, _copy=True):
        with self.lock:
            active = list(self.pending_tasks.values())
            recent = [task for _, task in sorted(self.recent_completed)]
            active_views = [self._task_view(t) for t in active]
            moving_views = [t for t in active_views if t['status'] != 'queued']
            queue_views = [t for t in active_views if t['status'] == 'queued']
            views = [self._task_view(t) for t in recent] + moving_views + queue_views[:max(0, 90 - len(moving_views))]
            node_load = {node_id: {'queue': 0, 'busy': False} for node_id in self.node_by_id}
            for task in active_views:
                if task['status'] == 'queued':
                    node_load[task['target']]['queue'] += 1
                elif task['status'] == 'processing':
                    node_load[task['target']]['busy'] = True
            nodes = []
            for node_id, node in self.node_by_id.items():
                nodes.append({'id': node_id, **node_load[node_id], 'completed': node['completed'],
                              'offloaded': node['offloaded'], 'serviceTimeS': node['serviceTimeS']})
            while self.recent_controls and self.sim_time - self.recent_controls[0].get('controlCheckedTime', 0) >= 7:
                self.recent_controls.popleft()
            selected = self.recent_controls[-1] if self.recent_controls else active[0] if active else self.tasks[-1] if self.tasks else None
            trace = {'taskId': None, 'stages': []}
            if selected:
                if not any(t['id'] == selected['id'] for t in views):
                    views.append(self._task_view(selected))
                trace = {'taskId': selected['id'], 'origin': selected['origin'], 'target': selected['target'],
                         'controlApplied': selected.get('controlApplied'), 'controlReason': selected.get('controlReason'),
                         'stages': [{'key': 'sense', 'state': 'done'}, {'key': 'dispatch', 'state': 'done'},
                                    {'key': 'compute', 'state': 'skipped' if selected.get('dropped') else 'done' if self.sim_time >= selected['finish'] else 'active' if self.sim_time >= selected['start'] else 'waiting'},
                                    {'key': 'signal', 'state': 'skipped' if selected.get('dropped') else 'done' if selected.get('returned') else 'waiting'}]}
            result = {'status': self.status, 'simTime': self.sim_time, 'speed': self.speed,
                                  'scheduler': self.scheduler, 'error': self.error, 'runId': self.run_id,
                                  'vehicles': self.vehicles, 'signals': self.signals, 'rsus': nodes, 'tasks': views,
                                  'events': self.events[-60:], 'metrics': self.metrics, 'trace': trace}
            if vehicle_id is not None:
                result['vehicleTrace'] = self.vehicle_trace(vehicle_id, _copy=False)
            return copy.deepcopy(result) if _copy else result

    def snapshot_json(self, vehicle_id=None):
        """Serialize once while holding the state lock; avoid deepcopy before HTTP JSON.

        The cache keeps only one immutable response, so switching selected cars
        cannot accumulate snapshots. Public snapshot()/vehicle_trace() still copy.
        """
        with self.lock:
            key = (self.run_id, self.sim_time, self.status, self.error, self.speed,
                   self.scheduler, len(self.tasks), len(self.events), vehicle_id)
            if self._snapshot_json_cache and self._snapshot_json_cache[0] == key:
                return self._snapshot_json_cache[1]
            raw = json.dumps(self.snapshot(vehicle_id, _copy=False), ensure_ascii=False,
                             allow_nan=False, separators=(',', ':')).encode('utf-8')
            self._snapshot_json_cache = (key, raw)
            return raw

    def _collect_model_results(self):
        gateway = getattr(self, 'model_gateway', None)
        if gateway:
            self.model_results.extend(gateway.drain(self.run_id))

    def integration_status(self):
        with self.lock:
            self._collect_model_results()
            gateway = getattr(self, 'model_gateway', None)
            return {'perception': {'provider': 'SyntheticPerception', 'source': 'sumo_ideal_lane_observation',
                                   'synthetic': True, 'actualAudioInference': False},
                    'compute': {'provider': type(getattr(self, 'compute_provider', SyntheticCompute())).__name__,
                                'source': 'configured_demo_service_seconds', 'measured': False},
                    'scheduler': self.meo_scheduler.metadata() if self.meo_scheduler else {
                        'provider': type(getattr(self, 'scheduler_provider', LeastFinishScheduler())).__name__},
                    'transport': {'provider': type(getattr(self, 'transport_provider', SyntheticTransport())).__name__,
                                  'measured': False},
                    'signal': {'provider': 'TraCISignalActuator', 'target': 'local_SUMO', 'physicalDevice': False},
                    'modelGateway': gateway.status() if gateway else {'configured': False, 'mode': 'shadow',
                                                                    'usedForSignalControl': False},
                    'recentModelResults': copy.deepcopy(list(self.model_results))}

    def submit_model_probe(self, payload):
        """Optional, explicit audio reference submission; never blocks SUMO for inference."""
        with self.lock:
            gateway = getattr(self, 'model_gateway', None)
            if gateway is None:
                raise ValueError('real model is not configured; start server with --model-endpoint')
            vehicle_id, rsu_id, audio_ref = payload.get('vehicleId'), payload.get('rsuId'), payload.get('audioRef')
            history = self.vehicle_history.get(vehicle_id) if isinstance(vehicle_id, str) else None
            if not history or not history.get('vehicle') or rsu_id not in self.node_by_id:
                raise ValueError('model probe requires a known current-run vehicle and RSU')
            if (not isinstance(audio_ref, str) or not audio_ref.strip() or len(audio_ref) > 2048
                    or any(ord(char) < 32 for char in audio_ref)):
                raise ValueError('audioRef must identify a supplied audio file or service object')
            rate, channels = payload.get('sampleRateHz'), payload.get('channels')
            if rate is not None and (type(rate) is not int or not 8000 <= rate <= 192000):
                raise ValueError('sampleRateHz must be an integer in [8000, 192000]')
            if channels is not None and (type(channels) is not int or not 1 <= channels <= 32):
                raise ValueError('channels must be an integer in [1, 32]')
            vehicle = history['vehicle']
            frame = AudioFrame(self.run_id, vehicle_id, rsu_id, self.sim_time, vehicle['laneId'],
                               vehicle['speed'], audio_ref, rate, channels, 'supplied_audio_reference')
            return gateway.submit(frame)

    def control(self, action, value=None):
        with self.lock:
            if action == 'pause':
                if self.status == 'running':
                    self.status = 'paused'
            elif action == 'resume':
                if self.status == 'error':
                    raise ValueError('请先修复错误并重置')
                if self.status == 'paused':
                    self.status = 'running'
            elif action == 'speed':
                if value not in (0.5, 1, 2, 4):
                    raise ValueError('速度必须为 0.5、1、2 或 4')
                self.speed = float(value)
            elif action in ('reset', 'scheduler'):
                running = self.status == 'running'
                if action == 'scheduler':
                    if value not in SCHEDULERS:
                        raise ValueError('未知调度策略')
                self._write_export()
                self._close_sumo()
                if action == 'scheduler':
                    self.scheduler = value
                try:
                    self._reset_data()
                    self.last_action = {}
                    self._start_sumo()
                    self.status = 'running' if running else 'paused'
                except Exception as exc:
                    self.status, self.error = 'error', str(exc)
                    self._close_sumo()
            else:
                raise ValueError('未知控制动作')
            return {'ok': self.status != 'error', 'reset': action in ('reset', 'scheduler'),
                    'message': '调度策略已切换并以种子 42 重置' if action == 'scheduler' else '已执行',
                    'status': self.status, 'scheduler': self.scheduler, 'error': self.error}

    def health(self):
        with self.lock:
            return {'app': 'tangdao-bay-demo', 'root': str(self.root), 'pid': os.getpid(),
                    'ready': self.connection is not None and self.status != 'error', 'backend': 'SUMO/TraCI',
                    'sumo': getattr(self, 'sumo_version', None), 'status': self.status, 'error': self.error,
                    'syntheticAcoustics': True, 'portProtocol': 'localhost HTTP', 'runId': self.run_id}

    def export(self):
        with self.lock:
            return copy.deepcopy({'configuration': self._configuration(), 'sceneMetadata': self.scene['meta'],
                                  'simTime': self.sim_time, 'status': self.status, 'metrics': self.metrics,
                                  'tasks': [self._task_view(t) for t in self.tasks], 'events': self.events,
                                  'modelIntegration': self.integration_status(),
                                  'limitations': ['真实 OSM 地图与 SUMO 车辆运动；非真实道路交通观测',
                                                  '合成声学事件与理想车道观测；未执行 WAV 定位模型',
                                                  '排队、服务和传输时间均为演示设定；非设备测量',
                                                  '信号时序为 SUMO 合成方案；非市政配时或真实信号机控制',
                                                  '局部有界延长不构成交通效率收益证明']})

    def _write_export(self):
        if hasattr(self, 'run_dir'):
            (self.run_dir / 'export.json').write_text(json.dumps(self.export(), ensure_ascii=False, indent=2), encoding='utf-8')

    def _close_sumo(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None
        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=3)
            self.process = None
        for name in ('sumo_output', 'event_output'):
            handle = getattr(self, name, None)
            if handle:
                handle.close()
                setattr(self, name, None)

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        with self.lock:
            self._write_export()
            self._close_sumo()
        gateway = getattr(self, 'model_gateway', None)
        if gateway:
            gateway.close()
