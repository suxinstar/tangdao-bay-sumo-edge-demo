"""Exercise a fresh local demonstration via its public HTTP API."""
from pathlib import Path
import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:8765')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
base = args.url.rstrip('/')

def get(path):
    with urllib.request.urlopen(base + path, timeout=20) as response:
        return json.load(response)

def control(action, value=None):
    request = urllib.request.Request(base + '/api/control', data=json.dumps({'action':action,'value':value}).encode(), headers={'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)

health = get('/api/health')
assert health['app'] == 'tangdao-bay-demo' and health['ready'], health
scene = get('/api/scene')
assert scene['meta']['source'] == 'OpenStreetMap'
assert len(scene['buildings']) > 0 and len(scene['rsus']) >= 3
control('reset')
control('pause')
before = get('/api/state')['simTime']
time.sleep(.4)
assert get('/api/state')['simTime'] == before, 'paused simulation clock advanced'
control('speed', 4)
control('resume')
deadline = time.monotonic() + 100
observed_statuses = set()
max_offloaded = max_responses = 0
while time.monotonic() < deadline:
    state = get('/api/state')
    assert state['status'] != 'error', state.get('error')
    observed_statuses.update(t['status'] for t in state['tasks'])
    max_offloaded = max(max_offloaded, state['metrics']['offloaded'])
    max_responses = max(max_responses, state['metrics']['signalActions'])
    if state['simTime'] >= 100:
        break
    time.sleep(.25)
else:
    raise AssertionError('simulation did not reach 100s in the bounded check')
control('pause')
state = get('/api/state')
assert state['metrics']['sensed'] > 0 and state['metrics']['completed'] > 0
assert max_offloaded > 0, 'no offload observed'
assert max_responses > 0, 'no actual signal response observed'
for task in state['tasks']:
    assert task['created'] <= task['txEnd'] <= task['start'] <= task['finish'] <= task['returnEnd']
export = get('/api/export')
result = {
    'checkedAt':dt.datetime.now(dt.timezone.utc).isoformat(),
    'url':base,'backend':health['backend'],'sumo':health.get('sumo'),
    'pauseClockStable':True,'mapSource':scene['meta']['source'],
    'buildings':len(scene['buildings']),'rsus':len(scene['rsus']),
    'simTime':state['simTime'],'metrics':state['metrics'],
    'observedTaskStatuses':sorted(observed_statuses),'taskTimeOrderValid':True,
    'exportReceived':True,
}
(root/'evidence/http_acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
(root/'evidence/http_run_export.json').write_text(json.dumps(export,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
