# 唐岛湾北岸地图与交通场景说明

这份演示以真实 OpenStreetMap 道路、建筑轮廓、海岸线和公园几何为底图。车辆需求、RSU位置、声学任务、计算通信参数及信号配时均为合成演示设置，不代表实测识别结果、已部署基础设施或真实市政信号方案。

## 原始数据与许可

- 获取时间：2026-10-01 01:49:49 UTC（北京时间 09:49:49）；确切时间见 `data/source_manifest.json`。
- 来源：[OpenStreetMap Map API](https://api.openstreetmap.org/api/0.6/map?bbox=120.18,35.941,120.21,35.959)。
- 请求范围：西边界经度 120.18、南边界纬度 35.941、东边界经度 120.21、北边界纬度 35.959；坐标为 WGS84。
- 原始快照：`data/tangdao.osm.xml`，6228 个 node、459 个 way、54 个 relation，原始内容完整保留。
- SHA-256：`7d008dd5adab60d4811444d9c331886782751e8431d813ba450227286f42754a`。
- 数据许可：[ODbL 1.0 / OpenStreetMap copyright](https://www.openstreetmap.org/copyright)，应持续展示“© OpenStreetMap contributors”。第三方使用、再分发衍生数据库须遵守原许可；本演示保留原快照、来源清单与构建脚本。

请求 bbox、原始数据范围和实际路网范围必须区分。Map API 返回跨越 bbox 的完整 way 及相关节点；netconvert 的 `keep-edges.in-geo-boundary`保留与范围相交的道路边，不会把所有跨界道路几何在矩形上切断。故 `location.origBoundary`为输入处理阶段范围，不能称为最终展示范围。实际导出的道路点反投影包围范围为：

`[120.1780705, 35.9405390, 120.2272248, 35.9673222]`

SUMO 本地路网包围框为 `[0,0,4491.53,2949.49]`米。建筑及区域显示轮廓按请求 bbox 裁切；跨界裁切边是视图边界，不是新测绘轮廓。界面初始聚焦4个沿海路口，`meta.focusBounds`为 `[225.78,27.17,1648.76,1136.60]`米。

## 选定的4个演示路口

| RSU | 路口 | 经度 | 纬度 | SUMO坐标 x,y（米） |
|---|---|---:|---:|---|
| RSU_1 | 漓江西路 × 太行山路 | 120.1830258 | 35.9432436 | 455.78, 287.17 |
| RSU_2 | 漓江西路 × 井冈山路 | 120.1878431 | 35.9453783 | 897.26, 511.48 |
| RSU_3 | 漓江西路 × 武夷山路 | 120.1910232 | 35.9466218 | 1188.15, 641.18 |
| RSU_4 | 漓江西路 × 阿里山路 | 120.1933284 | 35.9474456 | 1398.76, 726.60 |

数据还包含庐山路、珠江路、长江中路、长白山路、九连山路、青弋江路等；完整道路名清单保存在 `data/source_manifest.json`。不根据展示需要制造道路或建筑。

全路网有11个可用信号路口，演示集中采用上表4个。选点的信号信息来自OSM `traffic_signals`标签，由SUMO `tls.guess-signals`和路口合并逻辑解释；未采用无数据依据的全网 `tls.guess`。所有64秒静态周期、黄灯3秒、过渡全红1秒及车道连接均为SUMO合成方案，不是当地实测配时。原始导入警告完整保留在 `data/netconvert.log`，包括被排除的公共交通元素、有限几何/转向推断警告，未静默隐藏。

`scene.intersections[].id`与`rsus[].intersectionId`是从实际受控connection提取的 **TLS controller ID（GS_cluster_...）**；几何路口ID另存为 `junctionId`。上游实际外部受控车道列于 `incomingLaneIds`，不含internal lane。RSU大约在主路上游28米、侧向13米处，半径105米；位置和服务时间均为合成实验参数。

## 三维几何的真实性边界

- 324 条非internal道路边；包括真实SUMO转向内部边后，共1041条渲染道路记录。宽度、车道、速度为OSM导入与SUMO类型推断值，不能一律称为现场测量值。
- 129 个真实OSM建筑轮廓：10个采用OSM `height`，7个按 `building:levels × 3.2m`换算，112个无高度的建筑统一假设15米。每个建筑均有 `heightSource`和 `geometrySource`。
- 26 个区域：14个water、12个park。真实公园关系包括唐岛湾滨海公园；数据不承诺覆盖附近所有建筑或绿地。
- 唐岛湾开阔水面由两条相接真实OSM海岸线 `way/15240751`、`way/15240911`构造。两端均在显示bbox以南，在bbox外闭合后裁切；视图内的海岸线没有人为补画。海面矩形裁切边不得标为海岸。
- water/park multipolygon支持保留可见 `holes`；渲染器应据此挖空，而不将内环填为水。轮廓字段均为SUMO本地米制坐标。
- `data/map_preview.png`为数据俯视核对图：可见真实海岸、公园、建筑和4个覆盖圈。它用于数据QA，不作为GUI端到端验证。

## 构建与重建

运行目录为本交付根目录。需要Python 3.8+、SUMO/netconvert和sumolib；投影不依赖pyproj。

```powershell
python scripts/build_map.py --verify
```

默认复用已经交付的原始OSM快照；只有明确需要更新地图时才执行：

```powershell
python scripts/build_map.py --download --verify
```

重新下载会改变快照和可能的路网ID，须重新联调，不能声称与原演示逐字节相同。脚本将每次实际工具调用保存在 `data/build_commands.json`；主入口为 `scenario/tangdao.sumocfg`，网络和显式路由均在 `scenario/`。所用导入参数已按本机 `netconvert --help`及[SUMO官方OSM导入说明](https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html)核对。有关保留/转换参数见[netconvert官方文档](https://sumo.dlr.de/docs/netconvert.html)。

脚本采用WGS84→UTM51N标准展开式，并应用net.xml记录的netOffset。与113个未合并OSM/SUMO路口点核对，最大位置差为0.00781米，确认建筑与道路坐标对齐。反投影仅用于记录地理范围和路口位置；运行与渲染始终使用实际SUMO本地坐标。

## 实际执行验证

本机SUMO 1.25，seed 42，步长0.2秒，场景0–600秒。6条显式连通合成交通流：沿漓江西路双向流加4条支路流，发车结束于580秒。设置 `time-to-teleport=-1`；未用传送隐藏拥堵。

已完成完整600秒无界面运行，进程正常退出：

- 1171辆装载并成功插入；
- 817辆到达，最后一步354辆仍在路网；
- 最大416辆同时在场；
- summary及运行日志报告0碰撞、0传送；
- 最后一条step summary时间599.80秒，对应0.2秒步长到600秒结束，不代表只运行599.8秒；
- 投影、TLS唯一映射与6条最短路径连通性均由构建脚本显式检查。

证据：`data/build_stats.json`、`data/sumo_validation.log`、`data/sumo_summary.xml`、`data/tripinfo.xml`、`data/traffic_manifest.json`。这些是合成需求在真实地图上的可运行性证据，不是道路流量标定、真实声学识别或交通控制收益证明。后端闭环任务与浏览器交互需由集成验证单独确认。
