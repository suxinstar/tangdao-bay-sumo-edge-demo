"""Rebuild a real-OSM Tangdao Bay map and a clearly synthetic SUMO demo.

Python 3.8+, SUMO/netconvert and sumolib are required. No GIS package needed.
Download is explicit; ordinary rebuilds retain the exact original OSM snapshot.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BBOX = [120.18, 35.941, 120.21, 35.959]
API = "https://api.openstreetmap.org/api/0.6/map?bbox=" + ",".join(map(str, BBOX))
OSM = ROOT / "data/tangdao.osm.xml"
SOURCE = ROOT / "data/source_manifest.json"
NET = ROOT / "scenario/tangdao.net.xml"
# Keep the first four station identifiers stable for existing saved demonstrations.
# Additional stations make the north-shore demo a two-corridor network instead
# of a single short coastal strip. All pairs must exist in the imported OSM net.
INTERSECTION_ROADS = [
    ("漓江西路", "太行山路"), ("漓江西路", "井冈山路"),
    ("漓江西路", "武夷山路"), ("漓江西路", "阿里山路"),
    ("漓江西路", "庐山路"), ("漓江西路", "九连山路"),
    ("长江中路", "井冈山路"), ("长江中路", "武夷山路"),
    ("长江中路", "阿里山路"),
]
COMMANDS = []

def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def run(cmd, log_name):
    COMMANDS.append(cmd)
    proc = subprocess.run(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = proc.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
    (ROOT / "data" / log_name).write_text(output, encoding="utf-8")
    if proc.returncode:
        raise RuntimeError("Command failed; see data/" + log_name)
    return output

def tags(element):
    return {t.get("k"): t.get("v") for t in element.findall("tag")}

def utm(lon, lat, zone=51):
    """WGS84 transverse Mercator series, matching netconvert UTM zone 51."""
    a, e2, k0 = 6378137.0, 0.0066943799901413165, 0.9996
    ep2 = e2 / (1.0 - e2)
    phi = math.radians(lat)
    lam = math.radians(lon)
    lam0 = math.radians(zone * 6 - 183)
    n = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = ep2 * math.cos(phi) ** 2
    aa = math.cos(phi) * (lam - lam0)
    m = a * ((1-e2/4-3*e2**2/64-5*e2**3/256)*phi
             -(3*e2/8+3*e2**2/32+45*e2**3/1024)*math.sin(2*phi)
             +(15*e2**2/256+45*e2**3/1024)*math.sin(4*phi)
             -(35*e2**3/3072)*math.sin(6*phi))
    x = k0*n*(aa+(1-t+c)*aa**3/6+(5-18*t+t*t+72*c-58*ep2)*aa**5/120)+500000
    y = k0*(m+n*math.tan(phi)*(aa*aa/2+(5-t+9*c+4*c*c)*aa**4/24+
                               (61-58*t+t*t+600*c-330*ep2)*aa**6/720))
    return x, y

def inverse_utm(x, y):
    """Newton inverse of the same local UTM formula, no external GIS dependency."""
    lon, lat = 120.195, 35.95
    epsilon = 0.00001
    for unused in range(7):
        px, py = utm(lon, lat)
        ex, ey = x-px, y-py
        if abs(ex)+abs(ey) < 0.00001:
            break
        lx, ly = utm(lon+epsilon, lat)
        bx, by = utm(lon, lat+epsilon)
        a,b,c,d = (lx-px)/epsilon,(bx-px)/epsilon,(ly-py)/epsilon,(by-py)/epsilon
        determinant = a*d-b*c
        lon += (d*ex-b*ey)/determinant
        lat += (-c*ex+a*ey)/determinant
    return [round(lon,7),round(lat,7)]

def clip_polygon(points):
    """Clip actual OSM polygon to display bbox; introduced edges are viewport edges."""
    west, south, east, north = BBOX
    output = points[:-1] if points and points[0] == points[-1] else list(points)
    for axis, bound, positive in [(0, west, True), (0, east, False), (1, south, True), (1, north, False)]:
        source, output = output, []
        if not source:
            break
        def inside(p):
            return p[axis] >= bound if positive else p[axis] <= bound
        previous = source[-1]
        for current in source:
            prev_in, cur_in = inside(previous), inside(current)
            if cur_in != prev_in:
                delta = current[axis] - previous[axis]
                f = (bound - previous[axis]) / delta
                output.append([previous[0]+f*(current[0]-previous[0]), previous[1]+f*(current[1]-previous[1])])
            if cur_in:
                output.append(list(current))
            previous = current
    if len(output) >= 3:
        output.append(output[0])
    return output

def assemble(ref_chains):
    chains = [list(c) for c in ref_chains if c]
    rings = []
    while chains:
        chain = chains.pop(0)
        changed = True
        while changed and chain[0] != chain[-1]:
            changed = False
            for i, other in enumerate(chains):
                if chain[-1] == other[0]:
                    chain += other[1:]
                elif chain[-1] == other[-1]:
                    chain += other[-2::-1]
                elif chain[0] == other[-1]:
                    chain = other[:-1] + chain
                elif chain[0] == other[0]:
                    chain = other[:0:-1] + chain
                else:
                    continue
                chains.pop(i)
                changed = True
                break
        rings.append(chain)
    return rings

def number(value):
    m = re.search(r"[0-9]+(?:\.[0-9]+)?", value or "")
    return float(m.group()) if m else None

def write_map_preview(scene):
    """Dependency-free QA overview; this is a data plot, not a GUI screenshot."""
    from html import escape
    bounds = scene["bounds"]
    scale = min(1420 / (bounds["maxX"]-bounds["minX"]), 880 / (bounds["maxY"]-bounds["minY"]))
    def point(p):
        return (40+(p[0]-bounds["minX"])*scale, 60+(bounds["maxY"]-p[1])*scale)
    def points(poly):
        return " ".join("%.2f,%.2f" % point(p) for p in poly)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1500" height="1160" viewBox="0 0 1500 1160">',
             '<rect width="1500" height="1160" fill="#111e2b"/>',
             '<g font-family="Microsoft YaHei, sans-serif" fill="#e2eef4">',
             '<text x="40" y="32" font-size="23">唐岛湾北岸 · 双主干道 / 9 路口 / 9 RSU</text>']
    for area in scene["areas"]:
        rings = [area["polygon"]] + area.get("holes", [])
        path = " ".join("M "+" L ".join("%.2f %.2f" % point(p) for p in ring)+" Z" for ring in rings)
        color = "#163954" if area["type"] == "water" else "#204238"
        parts.append('<path d="%s" fill="%s" fill-rule="evenodd"/>' % (path, color))
    for b in scene["buildings"]:
        parts.append('<polygon points="%s" fill="#485461"/>' % points(b["polygon"]))
    for r in scene["roads"]:
        parts.append('<polyline points="%s" fill="none" stroke="#8c9baa" stroke-width="%.2f"/>' % (points(r["shape"]),max(.6,r["width"]*scale)))
    for r in scene["rsus"]:
        x,y = point([r["x"],r["y"]])
        parts.append('<circle cx="%.2f" cy="%.2f" r="%.2f" fill="#31d5ba" fill-opacity=".1" stroke="#31d5ba"/>' % (x,y,r["sensingRadiusM"]*scale))
        parts.append('<circle cx="%.2f" cy="%.2f" r="5" fill="#f3ed29"/>' % (x,y))
        parts.append('<text x="%.2f" y="%.2f" font-size="16" fill="#f3ed29">%s</text>' % (x+8,y-7,escape(r["id"])))
    f = scene["meta"]["focusBounds"]
    fx,fy = point([f["minX"],f["maxY"]])
    parts.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="none" stroke="#ed839d" stroke-dasharray="9 7"/>' % (fx,fy,(f["maxX"]-f["minX"])*scale,(f["maxY"]-f["minY"])*scale))
    for i, junction in enumerate(scene["intersections"]):
        parts.append('<text x="%d" y="%d" font-size="15">RSU_%d  %s</text>' % (40+(i%3)*480,980+(i//3)*32,i+1,escape(junction["name"])))
    parts.append('<text x="40" y="1100" font-size="15">粉色虚线：默认演示范围 %.0f × %.0f m；圈：合成 RSU 105 m 覆盖区。道路/建筑轮廓来源 OSM。</text>' % (f["maxX"]-f["minX"],f["maxY"]-f["minY"]))
    parts.append('<text x="40" y="1132" font-size="15">© OpenStreetMap contributors / ODbL 1.0 · 数据核对图（不是浏览器运行截图）</text></g></svg>')
    (ROOT/"data/map_preview.svg").write_text("\n".join(parts)+"\n",encoding="utf-8")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="Explicitly refresh OSM snapshot")
    parser.add_argument("--verify", action="store_true", help="Run the full 600s headless SUMO scenario")
    args = parser.parse_args()
    for d in ("data", "scenario"):
        (ROOT / d).mkdir(exist_ok=True)
    if args.download or not OSM.exists():
        request = urllib.request.Request(API, headers={"User-Agent": "TangdaoBayResearchDemo/1.0 (local educational SUMO demonstration)"})
        with urllib.request.urlopen(request, timeout=50) as response:
            OSM.write_bytes(response.read())
        downloaded = dt.datetime.now(dt.timezone.utc).isoformat()
    elif SOURCE.exists():
        downloaded = json.loads(SOURCE.read_text(encoding="utf-8"))["downloadedAt"]
    else:
        downloaded = dt.datetime.fromtimestamp(OSM.stat().st_mtime, dt.timezone.utc).isoformat()
    osm_root = ET.parse(OSM).getroot()
    nodes = {n.get("id"): [float(n.get("lon")), float(n.get("lat"))] for n in osm_root.findall("node")}
    ways = {w.get("id"): w for w in osm_root.findall("way")}
    refs = {wid: [n.get("ref") for n in w.findall("nd")] for wid, w in ways.items()}
    netconvert = shutil.which("netconvert")
    sumo = shutil.which("sumo")
    if not netconvert or not sumo:
        raise RuntimeError("SUMO and netconvert must be on PATH")
    cmd = [netconvert, "--osm-files", "data/tangdao.osm.xml", "--output-file", "scenario/tangdao.net.xml",
           "--geometry.remove", "--junctions.join", "--tls.guess-signals", "--tls.discard-simple",
           "--tls.join", "--tls.default-type", "static", "--tls.cycle.time", "64",
           "--tls.yellow.time", "3", "--tls.allred.time", "1",
           "--keep-edges.by-vclass", "passenger", "--keep-edges.in-geo-boundary", ",".join(map(str, BBOX)),
           "--remove-edges.isolated", "--output.street-names", "--no-turnarounds",
           "--junctions.join-output", "data/joined-junctions.xml"]
    run(cmd, "netconvert.log")
    sys.path.insert(0, str(Path(netconvert).resolve().parents[1] / "tools"))
    import sumolib
    net = sumolib.net.readNet(str(NET), withInternal=True)
    net_xml = ET.parse(NET).getroot()
    location = net_xml.find("location").attrib
    offset = [float(x) for x in location["netOffset"].split(",")]
    def xy(lonlat):
        x, y = utm(*lonlat)
        return [round(x+offset[0], 3), round(y+offset[1], 3)]
    def lonlat(local):
        return inverse_utm(local[0]-offset[0], local[1]-offset[1])
    def polygon(ids):
        return [nodes[i] for i in ids if i in nodes]
    def local_polygon(points):
        return [xy(q) for q in clip_polygon(points)]
    align_errors = []
    for junction in net_xml.findall("junction"):
        if junction.get("id") in nodes and junction.get("type") != "internal":
            projected = xy(nodes[junction.get("id")])
            align_errors.append(math.hypot(projected[0]-float(junction.get("x")), projected[1]-float(junction.get("y"))))
    if not align_errors or max(align_errors) > 0.1:
        raise RuntimeError("OSM/SUMO projection alignment failed: " + str(max(align_errors, default=-1)))
    roads = []
    for edge in net.getEdges(withInternal=True):
        if not edge.allows("passenger"):
            continue
        shape = edge.getShape()
        roads.append({"id": edge.getID(), "name": edge.getName() or "", "shape": [[round(x,3), round(y,3)] for x,y in shape],
                      "width": round(sum(l.getWidth() for l in edge.getLanes()), 2), "lanes": len(edge.getLanes()),
                      "speed": edge.getSpeed(), "internal": edge.getFunction() == "internal"})
    buildings, areas, coastlines = [], [], []
    used_outer = set()
    incomplete_relations = []
    for relation in osm_root.findall("relation"):
        ts = tags(relation)
        area_type = "water" if ts.get("natural") == "water" else ("park" if ts.get("leisure") == "park" else None)
        if not area_type:
            continue
        members = [m.get("ref") for m in relation.findall("member") if m.get("type") == "way" and m.get("role") == "outer"]
        if any(wid not in refs for wid in members):
            incomplete_relations.append(relation.get("id"))
            continue
        inner_members = [m.get("ref") for m in relation.findall("member") if m.get("type") == "way" and m.get("role") == "inner"]
        inner_polygons = []
        for inner in assemble([refs[wid] for wid in inner_members if wid in refs]):
            if inner[0] == inner[-1]:
                clipped = local_polygon(polygon(inner))
                if len(clipped) >= 4:
                    inner_polygons.append(clipped)
        for index, ring in enumerate(assemble([refs[wid] for wid in members])):
            if ring[0] != ring[-1]:
                incomplete_relations.append(relation.get("id"))
                continue
            poly = local_polygon(polygon(ring))
            if len(poly) >= 4:
                areas.append({"id": "relation/"+relation.get("id")+"/"+str(index), "type": area_type,
                              "name": ts.get("name",""), "polygon": poly, "geometrySource": "osm_multipolygon_outer_clipped",
                              "displayClipped": True, "holes": inner_polygons, "holesOmitted": False})
                used_outer.update(members)
    coast_refs = []
    for wid, way in ways.items():
        ts = tags(way)
        points = polygon(refs[wid])
        if len(points) != len(refs[wid]) or len(points) < 3:
            continue
        if ts.get("natural") == "coastline":
            coast_refs.append(refs[wid])
            coastlines.append({"id":"way/"+wid, "shape":[xy(p) for p in points], "source":"OpenStreetMap coastline"})
            continue
        if refs[wid][0] != refs[wid][-1]:
            continue
        poly = local_polygon(points)
        if len(poly) < 4:
            continue
        if "building" in ts and ts["building"] != "no":
            h = number(ts.get("height"))
            levels = number(ts.get("building:levels"))
            source = "osm_height" if h else ("osm_levels" if levels else "assumed")
            h = h or (levels * 3.2 if levels else 15.0)
            buildings.append({"id":"way/"+wid, "polygon":poly, "height":round(h,2),
                              "heightSource":source, "name":ts.get("name",""), "buildingType":ts["building"],
                              "geometrySource":"osm_way", "displayClipped": True})
        elif wid not in used_outer and (ts.get("natural") == "water" or ts.get("leisure") == "park"):
            areas.append({"id":"way/"+wid, "type":"water" if ts.get("natural") == "water" else "park",
                          "polygon":poly, "name":ts.get("name",""), "geometrySource":"osm_way_clipped","displayClipped":True})
    # Two actual OSM coastlines form the bay's north/east coast. Their endpoints
    # are south of this view. Closing below the bbox and clipping introduces only
    # viewport edges; it never invents an in-view shoreline.
    for index, chain in enumerate(assemble(coast_refs)):
        points = polygon(chain)
        if points[0][1] < BBOX[1] and points[-1][1] < BBOX[1]:
            closure_lat = min(points[0][1], points[-1][1]) - .01
            points += [[points[-1][0], closure_lat], [points[0][0], closure_lat], points[0]]
            poly = local_polygon(points)
            if len(poly) >= 4:
                areas.append({"id":"coastline-derived-bay-"+str(index), "type":"water", "name":"唐岛湾",
                              "polygon":poly, "geometrySource":"joined_osm_coastline_with_outside_bbox_closure",
                              "displayClipped": True, "note":"Visible shoreline is OSM; rectangle edges are viewport clipping, not surveyed shoreline."})
    tls_nodes = [n for n in net.getNodes() if n.getType() == "traffic_light"]
    selected = []
    for main_road, cross_road in INTERSECTION_ROADS:
        candidates = [n for n in tls_nodes if cross_road in [e.getName() for e in n.getIncoming()] and
                      main_road in [e.getName() for e in n.getIncoming()]]
        if not candidates:
            raise RuntimeError("Selected real intersection missing: "+main_road+" / "+cross_road)
        selected.append(min(candidates, key=lambda n:n.getCoord()[1]))
    if len({n.getID() for n in selected}) != len(INTERSECTION_ROADS):
        raise RuntimeError("Different named intersections resolved to a duplicate node")
    intersections = []
    rsus = []
    for index, node in enumerate(selected):
        main_road, cross_road = INTERSECTION_ROADS[index]
        ix, iy = node.getCoord()
        tls_ids = {c.get("tl") for c in net_xml.findall("connection") if c.get("tl") and
                   net.getEdge(c.get("from")).getToNode().getID() == node.getID()}
        if len(tls_ids) != 1:
            raise RuntimeError("Ambiguous actual TLS controller for " + node.getID() + ": " + str(tls_ids))
        tls_id = next(iter(tls_ids))
        incoming = [l.getID() for e in node.getIncoming() if e.getFunction() != "internal" for l in e.getLanes() if l.allows("passenger")]
        eastward = [e for e in node.getIncoming() if e.getName()==main_road and e.getFromNode().getCoord()[0] < ix]
        approach = max(eastward or node.getIncoming(), key=lambda e:e.getLength())
        shape = approach.getShape()
        ax,ay = shape[-2] if len(shape)>1 else approach.getFromNode().getCoord()
        length = math.hypot(ix-ax,iy-ay) or 1
        # 28m upstream plus 13m roadside lateral shift; both carriageways remain in coverage.
        rx = ix-(ix-ax)/length*28-(iy-ay)/length*13
        ry = iy-(iy-ay)/length*28+(ix-ax)/length*13
        junction_name = main_road + " × " + cross_road
        intersections.append({"id":tls_id, "junctionId":node.getID(), "x":ix, "y":iy, "lonLat":lonlat([ix,iy]), "name":junction_name,
                              "signalSource":"OSM signal tags interpreted/merged by netconvert; synthetic SUMO timing",
                              "incomingLaneIds":incoming, "shape":[list(p) for p in node.getShape()]})
        rsus.append({"id":"RSU_"+str(index+1),"x":round(rx,2),"y":round(ry,2),"intersectionId":tls_id,"junctionId":node.getID(),
                     "name":("长江·" if main_road == "长江中路" else "")+cross_road+"感知站","sensingRadiusM":105,
                     "serviceTimeS":[2.6,1.6,2.3,1.35,2.1,1.9,2.4,1.5,2.0][index], "incomingLaneIds":incoming,
                     "deploymentSource":"synthetic_demo","heightM":8})
    focus = {"minX":min(n.getCoord()[0] for n in selected)-230,
             "maxX":max(n.getCoord()[0] for n in selected)+250,
             "minY":min(n.getCoord()[1] for n in selected)-260,
             "maxY":max(n.getCoord()[1] for n in selected)+410}
    bounds_values = list(map(float,location["convBoundary"].split(",")))
    bounds = dict(zip(("minX","minY","maxX","maxY"),bounds_values))
    road_geo = [lonlat(point) for road in roads for point in road["shape"]]
    road_geo_bounds = [min(p[0] for p in road_geo),min(p[1] for p in road_geo),max(p[0] for p in road_geo),max(p[1] for p in road_geo)]
    road_names = sorted({tags(w).get("name") for w in ways.values() if tags(w).get("highway") and tags(w).get("name")})
    version = run([sumo,"--version"],"sumo-version.txt").splitlines()[0]
    source_manifest = {"downloadedAt":downloaded,"source":"OpenStreetMap","apiUrl":API,"bbox":BBOX,
                       "license":"ODbL 1.0","attribution":"© OpenStreetMap contributors",
                       "licenseUrl":"https://www.openstreetmap.org/copyright","sha256":sha(OSM),
                       "osmSnapshotBase":osm_root.find("meta").get("osm_base") if osm_root.find("meta") is not None else None,
                       "counts":{"nodes":len(nodes),"ways":len(ways),"relations":len(osm_root.findall("relation"))},
                       "roadNames":road_names,"downloadNote":"OSM map API includes full ways crossing bbox; source extents therefore exceed requested bbox."}
    dump(SOURCE,source_manifest)
    scene = {"meta":{"name":"唐岛湾北岸 · 漓江西路 / 长江中路","bbox":BBOX,"source":"OpenStreetMap","sourceUrl":"https://www.openstreetmap.org/copyright",
                     "downloadedAt":downloaded,"license":"ODbL 1.0","attribution":"© OpenStreetMap contributors",
                     "osmSha256":sha(OSM),"sumoVersion":version,"coordinateSystem":"SUMO local metres; WGS84 UTM zone 51N + netOffset",
                     "projection":location,"actualRoadGeoBounds":road_geo_bounds,"focusBounds":focus,"focusCenter":[(focus["minX"]+focus["maxX"])/2,(focus["minY"]+focus["maxY"])/2],
                     "demoArea":{"corridors":["漓江西路","长江中路"],"selectedIntersections":len(selected),
                                 "coverage":"North shore coastal corridor, east to Jiulianshan Road, and the Changjiang Middle Road corridor",
                                 "selectionStrategy":"Named signalized intersections in the imported real OSM network"},
                     "notes":["道路和建筑轮廓来自真实OSM；缺失高度按15m假设，层数按3.2m换算。",
                              "车辆需求、RSU部署、声学任务、计算通信时间和信号配时均为演示仿真。",
                              "OSM信号位置经SUMO合并解释；64秒静态配时为合成，不是市政实测配时。",
                              "唐岛湾水面沿真实OSM海岸线生成，视图边界闭合不是新造海岸。",
                              "道路全部使用SUMO实际坐标；建筑与区域按请求bbox裁切，不表示城市完整数据。"]},
             "bounds":bounds,"roads":roads,"buildings":buildings,"areas":areas,"coastlines":coastlines,
             "intersections":intersections,"rsus":rsus}
    dump(ROOT/"data/scene.json",scene)
    write_map_preview(scene)
    # Deterministic explicit routes cover both main corridors and connectors.
    # Lower injection rates keep the larger active area inexpensive to render;
    # the simulation retains every vehicle instead of hiding demand via teleport.
    routes = ET.Element("routes")
    ET.SubElement(routes,"vType",id="demo_car",vClass="passenger",accel="2.6",decel="4.5",
                  length="4.7",minGap="2.5",maxSpeed="16.67",sigma="0.5",color="0.9,0.65,0.25")
    traffic = []
    def add_route(rid, start, end, period, begin=0):
        path, cost = net.getShortestPath(net.getEdge(start),net.getEdge(end),vClass="passenger")
        if not path:
            raise RuntimeError("No connected route "+rid)
        ids = [e.getID() for e in path]
        ET.SubElement(routes,"route",id=rid,edges=" ".join(ids))
        ET.SubElement(routes,"flow",id="flow_"+rid,type="demo_car",route=rid,begin=str(begin),end="580",
                      period=str(period),departLane="best",departSpeed="max")
        traffic.append({"id":rid,"edges":ids,"periodS":period,"beginS":begin,"endS":580,"source":"synthetic_seeded_demo"})
    add_route("coast_east","320042747#1","364477967#1",9)
    add_route("coast_west","834910233#3","834910233#34",11,1)
    add_route("north_east","452302426#1","452302425#3",14,2)
    add_route("north_west","543852189#4","138421943#5",16,3)
    side_one = max([e for e in selected[0].getIncoming() if e.getName()=="太行山路"],key=lambda e:e.getLength())
    add_route("side_1",side_one.getID(),"320042747#22",23,4)
    add_route("side_2","141451700#2","364477967#1",21,5)
    add_route("side_3","425696586#1","452302425#3",22,6)
    add_route("side_4","-320048295","834910233#34",24,7)
    route_coverage = {}
    for index, node in enumerate(selected):
        approach_edges = {e.getID() for e in node.getIncoming() if e.getFunction() != "internal"}
        covered_by = [r["id"] for r in traffic if approach_edges.intersection(r["edges"])]
        if not covered_by:
            raise RuntimeError("No synthetic demand approaches selected intersection "+node.getID())
        route_coverage[rsus[index]["id"]] = covered_by
    ET.ElementTree(routes).write(str(ROOT/"scenario/traffic.rou.xml"),encoding="utf-8",xml_declaration=True)
    cfg = ET.Element("configuration")
    inputs = ET.SubElement(cfg,"input")
    ET.SubElement(inputs,"net-file",value="tangdao.net.xml")
    ET.SubElement(inputs,"route-files",value="traffic.rou.xml")
    timing=ET.SubElement(cfg,"time")
    ET.SubElement(timing,"begin",value="0")
    ET.SubElement(timing,"end",value="600")
    ET.SubElement(timing,"step-length",value="0.2")
    random=ET.SubElement(cfg,"random_number")
    ET.SubElement(random,"seed",value="42")
    processing=ET.SubElement(cfg,"processing")
    ET.SubElement(processing,"time-to-teleport",value="-1")
    reports=ET.SubElement(cfg,"report")
    ET.SubElement(reports,"no-step-log",value="true")
    ET.SubElement(reports,"duration-log.statistics",value="true")
    ET.ElementTree(cfg).write(str(ROOT/"scenario/tangdao.sumocfg"),encoding="utf-8",xml_declaration=True)
    dump(ROOT/"data/traffic_manifest.json",{"seed":42,"stepLengthS":.2,"durationS":600,"source":"synthetic", "routes":traffic,
                                          "intersectionRouteCoverage":route_coverage,
                                          "demandDesign":"Two main corridors and north/south connector flows; deliberately sparse for follow-mode performance."})
    stats={"roadsIncludingInternal":len(roads),"roadEdges":len([r for r in roads if not r["internal"]]),
           "buildings":len(buildings),"buildingHeightSources":{s:sum(b["heightSource"]==s for b in buildings) for s in ["osm_height","osm_levels","assumed"]},
           "areas":len(areas),"areaTypes":{t:sum(a["type"]==t for a in areas) for t in ["water","park"]},
           "allNetworkTrafficLights":len(tls_nodes),"selectedTrafficLights":len(selected),"rsus":len(rsus),
           "projectionCheckNodeCount":len(align_errors),"projectionMaxErrorM":round(max(align_errors),6),
           "incompleteAreaRelationsOmitted":incomplete_relations,"selectedIntersections":intersections,
           "syntheticFlowCount":len(traffic),"intersectionRouteCoverage":route_coverage,
           "focusWidthM":round(focus["maxX"]-focus["minX"],2),"focusHeightM":round(focus["maxY"]-focus["minY"],2)}
    if args.verify:
        output=run([sumo,"-c","scenario/tangdao.sumocfg","--summary-output","data/sumo_summary.xml",
                    "--tripinfo-output","data/tripinfo.xml","--edgedata-output","data/edge_traffic.xml",
                    "--collision.action","warn"],"sumo_validation.log")
        summaries=ET.parse(ROOT/"data/sumo_summary.xml").getroot().findall("step")
        trips=ET.parse(ROOT/"data/tripinfo.xml").getroot().findall("tripinfo")
        edge_stats = {edge.get("id"):edge.attrib for edge in ET.parse(ROOT/"data/edge_traffic.xml").getroot().findall("interval/edge")}
        actual_approaches = {}
        for index, node in enumerate(selected):
            edges = [edge_stats.get(e.getID(), {}) for e in node.getIncoming() if e.getFunction() != "internal"]
            sampled = sum(float(e.get("sampledSeconds", "0")) for e in edges)
            # Left counts vehicles exiting approach edges, including route-end exits;
            # sampled time independently proves demand reached each approach.
            actual_approaches[rsus[index]["id"]] = {"sampledVehicleSeconds":round(sampled,2),
                "approachEdgeExits":sum(int(e.get("left", "0")) for e in edges)}
            if sampled <= 0:
                raise RuntimeError("600-second verification did not reach "+rsus[index]["id"])
        stats["verification"]={"ranToTime":summaries[-1].get("time"),"lastSummary":summaries[-1].attrib,
                               "completedTrips":len(trips),"maxRunningVehicles":max(int(s.get("running","0")) for s in summaries),
                               "collisionWarnings":len(re.findall("collision",output,re.I)),
                               "teleportWarnings":len(re.findall("Teleport",output)),
                               "actualIntersectionApproaches":actual_approaches}
    dump(ROOT/"data/build_stats.json",stats)
    dump(ROOT/"data/build_commands.json",COMMANDS)
    print(json.dumps(stats,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
