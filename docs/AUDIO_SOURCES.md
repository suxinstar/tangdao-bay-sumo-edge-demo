# 跟随车辆的声音与信号展示

跟随车辆后点击“开启车辆声音”，即可听到一个随该车 SUMO 车速变化的引擎循环音效，同时查看实际播放链路的波形和频谱。默认音量为 22%，可调整或静音。退出跟随、车辆驶离、仿真暂停、页面切到后台时停止声音；恢复同一车辆跟随且仿真运行时继续。首次播放需要用户点击，这是浏览器的音频自动播放策略。

此功能用于演示“跟随车辆时能够听到并观察本车声音”的交互。音效属于公开素材驱动的模拟，波形和频谱取自 Web Audio 播放链的 `AnalyserNode`，并非随机绘图。显示的 dBFS 是数字输出幅度，不是道路现场的 dB SPL；“模拟 rpm”是由车速映射的演示值，不是 SUMO 提供的发动机转速。现有 RSU 感知任务不使用该循环音效作神经网络推理输入。

波形图标明“归一化显示”：按每个 512 点采样窗的峰值缩放绘图，低音量时也能看清波形形状。此缩放只改变 canvas 坐标，不改变播放音量；右侧 dBFS 始终从未归一化的真实播放采样计算。接近数字静音的采样不放大，静音或暂停时显示平线。

## 网上素材及授权

| 字段 | 内容 |
|---|---|
| 作品 | racing car engine sound loops / `loop_0.wav` |
| 作者 | domasx2 |
| 作品页面 | https://opengameart.org/content/racing-car-engine-sound-loops |
| 原始下载 | https://opengameart.org/sites/default/files/loop_0.wav |
| 许可证 | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| 本地文件 | `web/audio/engine-loop.wav` |
| SHA-256 | `69d74b106509037ce547429e4c3f9ae906b0fffe88d0bb8b3b5a8b0cfc239048` |
| 格式 | 单声道、44,100 Hz、16-bit PCM WAV，38,106 帧，0.8641 秒 |
| 查验日期 | 2026-10-08 |

作品当前页面标示 CC0，并说明素材由公共领域的 pdsounds 录音剪辑形成；作者在 2011-07-24 的评论中确认已用公共领域素材重新制作。页面保留了更早版本的授权讨论，本项目使用该页面当前公开下载的 CC0 文件。原文件仅重命名，未改动字节。CC0 正式法律文本随包保存于 `web/audio/CC0-1.0.txt`；机器可读来源、哈希和边界见 `web/audio/SOURCES.json`。浏览器运行时在已解码缓冲区对循环接缝作 18 ms 交叉淡化，再按车速改变播放速率、低通频率和增益。

素材已经随项目保存，正常启动和播放不需要联网，不会访问麦克风，也不会上传声音。

## 前端集成契约

```javascript
import {VehicleAudio} from './vehicle-audio.js';
const vehicleAudio = new VehicleAudio({mount: document.getElementById('vehicle-audio')});

// 每个仿真快照、选择车辆、退出/恢复跟随、暂停/恢复、可见性变化时调用。
vehicleAudio.update({
  vehicle: {id: 'flow_coast_east.0', speed: 8.4, present: true}, // m/s
  following: true,
  running: true,
  hidden: document.hidden,
  simTime: 10.2,
  task: null
});

// 接在现有画面绘制循环内。内部最高 12.5 FPS，没有额外 rAF 循环。
vehicleAudio.renderSignal(performance.now());
// 本模块的“开启车辆声音”按钮已调用 enable()。外部调用也必须在点击事件内。
await vehicleAudio.enable();
vehicleAudio.setVolume(.22); // 0..1
vehicleAudio.setMuted(true);
await vehicleAudio.dispose(); // 页面销毁时释放 source、节点、监听器和 AudioContext
```

模块只有一个 `AudioContext` 和一个主播放源；换车时旧源用独立增益包络作最多 25 ms 的短淡出，最多保留一个淡出旧源，随后停止并断开连接。它复用已加载的短音频缓冲区。FFT 大小为 512，使用预分配采样数组和固定分辨率的小 canvas。音频停止后，短淡出结束即暂停 AudioContext；隐藏页面不维持独立动画循环。素材加载或浏览器音频启动失败会在音频区域显示原因，不影响交通仿真。

后续接入真实录音时应独立实现真实采集/音频输入适配器，传入采样率、通道数、采集时间戳和对应车辆/路侧设备 ID，按模型要求切窗、重采样和推理。不可将此演示素材或由它绘制的频谱当作真实唐岛湾采集数据，也不可把车速映射的模拟 rpm 当作声学识别结果。

## 验证

`tests/vehicle_audio.mjs` 覆盖首次点击前不创建音频上下文、重复开启复用上下文、车速变调、暂停/后台/离场停止、恢复、静音/零音量、换车、加载失败、加载中退出和销毁，以及绘图限频。它使用可控 Web Audio mock 验证生命周期，不代替浏览器的真实音频解码和声音试听。
