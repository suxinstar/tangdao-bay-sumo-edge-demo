"""Collect recorded checks without relabeling demonstration data as field results."""
from pathlib import Path
from datetime import datetime, timezone
import json

root = Path(__file__).resolve().parents[1]
def read(path):
    return json.loads((root / path).read_text(encoding='utf-8'))

result = {
    'assembledAt': datetime.now(timezone.utc).isoformat(),
    'scope': 'Local software demo acceptance; not field acoustic or traffic-benefit validation',
    'map': read('data/build_stats.json'),
    'backend': read('verification/backend_verification.json'),
    'http': read('evidence/http_acceptance.json'),
    'launcher': read('evidence/launcher_acceptance.json'),
    'browser': read('evidence/browser_acceptance.json'),
    'reportChecks': {
        '地图和投影': '324条非内部道路边、129栋建筑、4个RSU；113节点投影检查最大差0.00781米',
        '基础路网运行': '600秒配置；1171辆发车，817辆到达；无碰撞和瞬移',
        '关键逻辑检查': '5项通过；含单服务器预约、卸载、回传后控制和绿灯约束',
        '闭环对照运行': '两策略各120秒；最早完成策略205项回传、296项卸载、13次信号动作',
        '重置与本地策略': '同种子任务、事件和指标一致；本地策略卸载数为0',
        'HTTP与启动器': '任务五种状态、暂停时钟、导出、重复启动、停止和重新启动均通过',
        '浏览器界面': '三维显示、全景、路口聚焦、控制、数据说明和重置事件清理通过',
    },
    'limitations': [
        'Synthetic acoustics, RSU placement, travel demand, signal timings and resource timings.',
        'Completed-only latency means do not cover tasks still pending at the end of the 120-second check.',
        'No traffic improvement claim; no 900-second full closed-loop acceptance run.',
        'OSM building outlines do not imply complete inventory; 112 heights are assumed, 7 derived from levels.',
    ],
}
(root / 'evidence/acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print('Wrote evidence/acceptance.json')
