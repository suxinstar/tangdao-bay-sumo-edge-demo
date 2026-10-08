# 真实模型接入与数据边界

本项目已提供可运行的 Python 契约、默认演示实现、独立 HTTP 推理适配器、有界后台执行器，以及可调用的服务接口。未随项目提供训练后的声学权重、麦克风采集设备、真实 RSU 性能测量或市政信号机接入权限。网页播放的模拟车辆声音也不是麦克风测量值。

## 1. 当前实际执行什么

| 环节 | 当前运行实现 | 数据来源与接入位置 |
|---|---|---|
| 车辆运动 | SUMO / TraCI | OSM 路网上的合成交通需求；非实测车流 |
| 声学事件、车道 | `SyntheticPerception.infer(AudioFrame)` | 车辆经过 RSU 感知半径且位于其入口车道时，用 SUMO 真值生成演示事件 |
| 声学分类、方位、定位 | `PerceptionProvider` / `PerceptionResult` | 为标签、置信度、方位角、SUMO 平面坐标、不确定度预留明确字段；默认不编造模型结果 |
| 算力估计 | `ComputeProvider.estimate_seconds()` | 默认 `SyntheticCompute` 使用配置服务时长乘固定种子的工作量系数；未测量设备性能 |
| 上行、回传时延 | `TransportProvider.estimate_seconds()` | 默认 `SyntheticTransport` 使用本地/跨站距离公式；真实链路测量可替换此估计器 |
| 调度决策 | `SchedulerProvider.select()` | 默认 `MeoScheduler` 使用两份训练专家之一；最早完成与纯本地作为对照保留，见 `MEO_SCHEDULER.md` |
| 模型执行、远程 RSU 服务 | `HttpAcousticProvider` + `ModelGateway` | 后台 HTTP 推理，可通过显式音频引用试接独立服务；不阻塞 SUMO 步进 |
| 仿真信控 | `BoundedGreenPolicy` + `TraCISignalActuator` | 结果返回后、匹配当前绿灯、无黄灯、每绿相位一次、总绿灯最长 55 秒；仅作用于本地 SUMO |
| 真实信号机 | `SignalActuator` + `DisabledPhysicalSignalActuator` | 明确拒绝发出设备指令；未来接入时需要真实设备协议、鉴权和独立安全验证 |

核心代码位于 `integration/providers.py`、`integration/gateway.py`、`integration/http_acoustic.py`；接线位于 `simulation.py` 的构造函数、`_sense()`、`reserve_task()` 与 `_apply_control()`。这些默认类已被主仿真调用，非空 `TODO`。

## 2. 区分演示任务与真实模型试接

当前演示任务会保留：

```json
{
  "synthetic": true,
  "perception": {
    "source": "sumo_ideal_lane_observation",
    "model_version": "demo-synthetic-v1",
    "confidence": null,
    "synthetic": true,
    "direction_degrees": null,
    "position_m": null,
    "uncertainty_m": null
  }
}
```

没有声学模型置信度，就返回 `null`。默认值不会伪装成真实 WAV 识别结果。

真实模型的首个接入阶段是 **shadow（旁路评估）**：接收真实音频引用、执行推理、验证返回契约、保留结果，但不会把未验证输出用于信控。响应显式返回 `usedForSignalControl: false`。这种试接不替换或覆盖原有演示任务中的 SUMO 真值。把真实感知升级为闭环信控，仍需实现音频时间同步、定位到路网的映射、置信度门槛、误识别处置、调度输入接线，并独立验证安全门；本版本没有声称这一步已完成。

## 3. 零设备自检

在项目根目录使用项目便携 Python：

```powershell
& .\_runtime\python-3.12.10\python.exe -m integration.example_probe
```

程序把演示 `AudioFrame` 提交给后台执行器，返回 `accepted: true` 与 `status: ok`，结果仍标注 `synthetic: true`。此命令不启动 SUMO，也不控制红绿灯。

已有真实推理 HTTP 服务时：

```powershell
& .\_runtime\python-3.12.10\python.exe -m integration.example_probe `
  --endpoint http://127.0.0.1:9001/infer --audio-ref capture-23.wav
```

`audio-ref` 是推理服务能够访问的文件或对象 ID。此程序不会上传文件字节，也不会读取麦克风；采集程序和模型服务应约定引用的实际位置。项目不会把同一段素材自动当作所有车辆的真实录音。

## 4. HTTP 模型服务契约

服务接收 `POST` JSON，字段由 `AudioFrame` 定义：

```json
{
  "run_id": "run-id",
  "vehicle_id": "flow_coast_east.0",
  "rsu_id": "RSU_1",
  "sim_time": 12.4,
  "lane_id": "network_lane_0",
  "speed_mps": 8.5,
  "audio_ref": "capture-23.wav",
  "sample_rate_hz": 48000,
  "channels": 4,
  "source": "supplied_audio_reference"
}
```

其中 `lane_id` 和 `speed_mps` 是本次试接的仿真上下文，不能作为声学模型准确率测试时的输入泄漏。真实识别评估应仅用音频推理，然后把结果与保留的真值独立比较。

返回 `PerceptionResult` 对应 JSON：

```json
{
  "vehicle_id": "flow_coast_east.0",
  "rsu_id": "RSU_1",
  "lane_id": "network_lane_0",
  "label": "passenger_car",
  "confidence": 0.85,
  "source": "microphone_array_model",
  "model_version": "my-model-2026-10",
  "synthetic": false,
  "direction_degrees": 88.0,
  "position_m": [1200.0, 420.0],
  "uncertainty_m": 2.0
}
```

上面的值仅是契约示例，非项目实测结果。角度为 `[0,360)`；位置使用当前 SUMO 网络的平面米坐标；模型若使用地理经纬度或阵列局部坐标，需要在适配器中转换。方向、位置、不确定度均允许 `null`。返回车辆或 RSU 不匹配、非有限数字、置信度不在 `[0,1]`、没有真实音频引用却声明非合成，将被拒绝。单个 HTTP 返回限制为 64 KiB。

## 5. 在运行中的仿真里试接

先运行启动器准备便携运行时，停止旧服务或选用另一个端口，然后显式配置模型服务地址。直接启动 `server.py` 时需要为当前 PowerShell 指定包内 SUMO（不修改系统环境变量）：

```powershell
$env:SUMO_HOME = (Resolve-Path '.\_runtime\sumo-1.25.0').Path
$env:PATH = (Join-Path $env:SUMO_HOME 'bin') + ';' + $env:PATH
& .\_runtime\python-3.12.10\python.exe server.py --port 8766 `
  --model-endpoint http://127.0.0.1:9001/infer --model-timeout 1.0
```

可用 `--model-token-env MY_MODEL_TOKEN` 指定保存 Bearer token 的环境变量名。不要把 token 写入代码、URL、示例文档或公开仓库。项目状态接口不会返回 endpoint、token 或模型异常原文。

运行仿真产生车辆后，先从 `/api/state` 取得当前已观测到的车辆 ID，从 `/api/scene` 取得 RSU ID，再提交真实音频引用：

```powershell
$body = @{
  vehicleId = '替换为当前已观测车辆ID'
  rsuId = '替换为实际RSU ID'
  audioRef = 'capture-23.wav'
  sampleRateHz = 48000
  channels = 4
} | ConvertTo-Json
Invoke-RestMethod 'http://127.0.0.1:8766/api/model/probe' `
  -Method Post -ContentType 'application/json' -Body $body
Invoke-RestMethod 'http://127.0.0.1:8766/api/integration'
```

接口行为：

| 情况 | 结果 |
|---|---|
| 已配置且有容量 | HTTP 202，返回 jobId；请求线程不等推理结束 |
| 未配置模型、未知车辆/RSU、音频引用或采样元数据非法 | HTTP 400，明确拒绝 |
| 后台队列已满 | HTTP 429，`reason: model_queue_full` |
| 超过截止时间 | `recentModelResults[].status = timeout`；迟到结果丢弃 |
| 模型抛错、HTTP 失败或返回契约不合格 | `status = error`；不会回填伪造的真实结果 |
| 仿真重置后才返回旧 run 结果 | 丢弃，不附着到新一轮车辆 |

`ModelGateway` 默认最多 8 个未完成请求、1 个后台线程、1 秒墙钟截止时间、64 条待提取结果。单个原生模型线程如果卡死，Python 无法强杀它；它会占住唯一后台线程，后续请求被限流或超时，SUMO 仍可继续。需要强制终止能力的模型应运行在独立进程/服务内。可见接口仅保留当前 run 最近 16 条试接结果；这不是完整训练/评估数据集。

## 6. 替换调度、计算与通信估计器

以下构造参数已实际接入：

```python
from simulation import DemoSimulation

class ProfiledCompute:
    def estimate_seconds(self, node, work_factor):
        # Replace with an already-loaded calibration/profile, not blocking inference.
        profile_seconds = {"RSU_1": 0.25}
        return profile_seconds.get(node["id"], 0.4) * work_factor

sim = DemoSimulation(project_root, compute_provider=ProfiledCompute())
```

`scheduler_provider=` 接收具备 `select(candidates, origin=..., policy=...)` 的对象；`transport_provider=` 接收 `estimate_seconds(distance_m, remote=...)` 并返回 `(上行秒数, 回传秒数)`。这些函数在 SUMO 步进内执行，必须是快速、无 I/O 的本地决策或预先加载的估计；昂贵推理走 `ModelGateway`。调度器只能选已有候选，纯本地模式不能跨站；传输/计算数值必须有限、非负，服务时长必须为正。候选对象采用隔离副本，外部调度器不能篡改既定时间。

如改用 RL/GNN 等调度器，可以在后台更新策略快照，然后让 `select()` 只读取最新快照。接入真实执行时间后，还要处理测量结果与仿真时钟的映射；本版本仍按仿真秒推进排队与回传动画。

## 7. 单车 API 与性能约定

`GET /api/state?vehicle=<URL编码车辆ID>` 兼容原 `/api/state`，额外返回 `vehicleTrace`。整体状态与车辆档案在同一锁内取得，`runId`、`simTime` 一致；未知车辆的档案为 `status: unknown`，整体请求仍 HTTP 200。非法或重复车辆参数 HTTP 400。原 `/api/vehicle?id=...` 保留。

前端跟车时只需每 500 ms 请求合并接口，避免两个快照时刻不一致以及重复网络/JSON 工作。HTTP 状态序列化在锁内一次完成，避免序列化前再深复制整张地图状态；同一个暂停状态只缓存一份不可变 JSON 字节串。普通 Python `snapshot()` 和 `vehicle_trace()` 继续返回隔离副本。

后端热循环使用待完成任务索引和到期堆、入口车道到 RSU 的索引、单遍队列统计、每步一次信号读取；已完成历史保留给档案/导出，但不再每 0.2 仿真秒扫描整份任务历史。信控成功后立即更新状态中的时长和延长标记。

## 8. 验证与未完成边界

自动检查覆盖：默认来源标识、真实输入/返回契约、实际本地 HTTP 模型调用、无模型/错误/超时/队满、旧轮次隔离、不可执行的真实设备控制、调度替换与预约隔离、同刻合并 API，以及原有车辆档案/队列/信控门。

真实麦克风同步采集、WAV 解码与预处理、抗风噪声学识别、阵列标定/定位、真实链路与算力剖析、学习型调度训练、市政信号机协议和交通收益实测仍需要后续具体设备/模型/数据。这些环节对应上述输入、结果、服务和策略契约；本交付没有把接口存在等同于真实能力已验证。
