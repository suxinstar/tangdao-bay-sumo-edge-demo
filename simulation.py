"""Actual SUMO traffic plus explicitly synthetic acoustic/edge demonstration.

The edge service, transmission and lane observations are demonstration parameters,
not measured microphone inference. All times below are SUMO simulation seconds.
"""
from __future__ import annotations

import copy
import datetime as dt
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


def reserve_task(nodes, origin, now, scheduler, work_factor=1.0):
    """Reserve one non-preemptive worker; existing dispatch reservations stay fixed."""
    candidates = []
    source = nodes[origin]
    for node_id, node in sorted(nodes.items()):
        if scheduler == 'local' and node_id != origin:
            continue
        remote = node_id != origin
        distance = math.hypot(node['x'] - source['x'], node['y'] - source['y'])
        transfer = 0.28 + distance / 1600.0 if remote else 0.04
        return_time = 0.16 + distance / 2400.0 if remote else 0.04
        arrival = now + transfer
        start = max(arrival, node['available'])
        finish = start + node['serviceTimeS'] * work_factor
        candidates.append((finish, node_id != origin, node_id, arrival, start, return_time))
    finish, remote, target, arrival, start, return_time = min(candidates)
    nodes[target]['available'] = finish
    return {'target': target, 'txEnd': arrival, 'start': start, 'finish': finish,
            'returnEnd': finish + return_time, 'offloaded': remote}


def extension_decision(task, now, *, matching_green, has_yellow, already_extended,
                       spent, remaining, maximum_green=55.0, extension=4.0):
    """Pure safety gate: no result, no control; never request a phase transition."""
    if now + 1e-8 < task['returnEnd']:
        return None, '结果尚未返回'
    if has_yellow or not matching_green:
        return None, '检测车道当前非可延长绿灯'
    if already_extended:
        return None, '本绿相位已延长一次'
    extra = min(extension, maximum_green - spent - remaining)
    if extra <= 0.05:
        return None, '已达本演示最大绿灯时长'
    return remaining + extra, '已完成任务触发有界绿灯延长'


class DemoSimulation:
    STEP = 0.2
    SEED = 42
    MAX_GREEN = 55.0
    EXTENSION = 4.0

    def __init__(self, root, *, scheduler='least_finish', speed=1.0, autostart=False,
                 service_time=None, max_time=900.0):
        self.root = Path(root).resolve()
        self.scene = json.loads((self.root / 'data/scene.json').read_text(encoding='utf-8'))
        self.scheduler = scheduler
        self.speed = float(speed)
        self.service_time = service_time
        self.max_time = float(max_time)
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
        self.rng = random.Random(self.SEED)
        self.status, self.error, self.sim_time = 'paused', None, 0.0
        self.tasks, self.events, self.vehicles, self.signals = [], [], [], []
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
        self.metrics = {'sensed': 0, 'completed': 0, 'offloaded': 0, 'meanLatency': 0.0,
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
                'extensionS': self.EXTENSION, 'maximumSimulationTimeS': self.max_time,
                'serviceTimeSByRSU': {k: v['serviceTimeS'] for k, v in self.node_by_id.items()},
                'transmissionModel': 'local 0.04s; remote 0.28s + distance/1600m/s',
                'returnModel': 'local 0.04s; remote 0.16s + distance/2400m/s',
                'queueModel': 'one non-preemptive worker per RSU; immutable dispatch-order reservations',
                'acousticModel': 'synthetic event and ideal lane observation from actual SUMO incoming-lane encounter; no WAV inference',
                'serviceModel': 'demonstration service duration times seeded uniform[0.9,1.1]; not measured compute performance',
                'signalModel': 'SUMO synthetic plan; extend matching active green only after returned task; never set phase index',
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
                self.trip_times.append(self.sim_time - self.departure_times.pop(vehicle_id))
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
        for vehicle in self.vehicles:
            for node_id, node in sorted(self.node_by_id.items()):
                key = (vehicle['id'], node_id)
                if key in self.seen or vehicle['laneId'] not in self.incoming[node_id]:
                    continue
                if math.hypot(vehicle['x'] - node['x'], vehicle['y'] - node['y']) <= node['sensingRadiusM']:
                    self.seen.add(key)
                    self._sense(vehicle, node_id)
        for task in self.tasks:
            if not task.get('returned') and self.sim_time + 1e-8 >= task['returnEnd']:
                self._complete(task)
        self._read_signals()
        self.metrics.update(vehicles=len(self.vehicles), completedTrips=len(self.trip_times),
                            meanTripTime=sum(self.trip_times) / len(self.trip_times) if self.trip_times else None,
                            haltingVehicles=sum(v['speed'] < 0.1 for v in self.vehicles))
        if self.sim_time >= self.max_time or (conn.simulation.getMinExpectedNumber() == 0 and all(t.get('returned') for t in self.tasks)):
            self.status = 'finished'
            self._event('finished', '本轮仿真结束，可导出日志或重置')
            self._write_export()

    def _sense(self, vehicle, origin):
        task_id = f'T{len(self.tasks) + 1:05d}'
        timing = reserve_task(self.node_by_id, origin, self.sim_time,
                              self.scheduler, self.rng.uniform(0.9, 1.1))
        task = {'id': task_id, 'vehicleId': vehicle['id'], 'origin': origin,
                'x': vehicle['x'], 'y': vehicle['y'], 'laneId': vehicle['laneId'],
                'created': self.sim_time, 'synthetic': True, **timing}
        self.tasks.append(task)
        self.tasks_by_vehicle.setdefault(vehicle['id'], []).append(task)
        self.task_by_id[task_id] = task
        self.metrics['sensed'] += 1
        self.metrics['offloaded'] += int(task['offloaded'])
        self.node_by_id[origin]['offloaded'] += int(task['offloaded'])
        self._event('sense', f'{origin} 感知车辆 {vehicle["id"]}，生成合成声学任务', origin, task_id)
        self._event('dispatch', f'{task_id} → {task["target"]}（{"跨站卸载" if task["offloaded"] else "本地处理"}）', origin, task_id,
                    task=copy.deepcopy(task))

    def _complete(self, task):
        task['returned'] = True
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
            domain.setPhaseDuration(tls, duration)
            self.extended.add(epoch)
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

    def _read_signals(self):
        if not hasattr(self, 'last_action'):
            self.last_action = {}
        positions = {x['id']: x for x in self.scene['intersections']}
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
            self.event_output.flush()

    def _task_view(self, task):
        now = self.sim_time
        status = ('done' if task.get('returned') else 'returning' if now >= task['finish'] else
                  'processing' if now >= task['start'] else 'queued' if now >= task['txEnd'] else 'transmitting')
        return dict(task, status=status)

    def vehicle_trace(self, vehicle_id):
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
                                  'pending': len(tasks) - completed}}
            if status == 'unknown':
                result['error'] = 'vehicle not observed in the current run'
            return copy.deepcopy(result)

    def snapshot(self):
        with self.lock:
            active = [t for t in self.tasks if not t.get('returned')]
            recent = [t for t in self.tasks if t.get('returned')][-10:]
            active_views = [self._task_view(t) for t in active]
            moving_views = [t for t in active_views if t['status'] != 'queued']
            queue_views = [t for t in active_views if t['status'] == 'queued']
            views = [self._task_view(t) for t in recent] + moving_views + queue_views[:max(0, 90 - len(moving_views))]
            nodes = []
            for node_id, node in self.node_by_id.items():
                queue = sum(t['target'] == node_id and t['txEnd'] <= self.sim_time < t['start'] for t in active)
                busy = any(t['target'] == node_id and t['start'] <= self.sim_time < t['finish'] for t in active)
                nodes.append({'id': node_id, 'queue': queue, 'busy': busy, 'completed': node['completed'],
                              'offloaded': node['offloaded'], 'serviceTimeS': node['serviceTimeS']})
            applied = [t for t in self.tasks if t.get('controlApplied') and self.sim_time - t.get('controlCheckedTime', 0) < 7]
            selected = applied[-1] if applied else active[0] if active else self.tasks[-1] if self.tasks else None
            trace = {'taskId': None, 'stages': []}
            if selected:
                if not any(t['id'] == selected['id'] for t in views):
                    views.append(self._task_view(selected))
                trace = {'taskId': selected['id'], 'origin': selected['origin'], 'target': selected['target'],
                         'controlApplied': selected.get('controlApplied'), 'controlReason': selected.get('controlReason'),
                         'stages': [{'key': 'sense', 'state': 'done'}, {'key': 'dispatch', 'state': 'done'},
                                    {'key': 'compute', 'state': 'done' if self.sim_time >= selected['finish'] else 'active' if self.sim_time >= selected['start'] else 'waiting'},
                                    {'key': 'signal', 'state': 'done' if selected.get('returned') else 'waiting'}]}
            return copy.deepcopy({'status': self.status, 'simTime': self.sim_time, 'speed': self.speed,
                                  'scheduler': self.scheduler, 'error': self.error, 'runId': self.run_id,
                                  'vehicles': self.vehicles, 'signals': self.signals, 'rsus': nodes, 'tasks': views,
                                  'events': self.events[-60:], 'metrics': self.metrics, 'trace': trace})

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
                    if value not in ('least_finish', 'local'):
                        raise ValueError('未知调度策略')
                self._write_export()
                self._close_sumo()
                if action == 'scheduler':
                    self.scheduler = value
                self._reset_data()
                self.last_action = {}
                try:
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
