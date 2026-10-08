"""Checks the two real neighboring roads, synthetic TLS provenance and real routes."""
import hashlib
import json
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


class AdjacentRoadsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene = json.loads((ROOT / "data/scene.json").read_text(encoding="utf-8"))
        cls.net = ET.parse(ROOT / "scenario/tangdao.net.xml").getroot()
        cls.routes = ET.parse(ROOT / "scenario/traffic.rou.xml").getroot()
        cls.stats = json.loads((ROOT / "data/build_stats.json").read_text(encoding="utf-8"))

    def test_station_numbering_is_two_nearby_real_roads(self):
        self.assertEqual([r["id"] for r in self.scene["rsus"]], ["RSU_"+str(i) for i in range(1, 10)])
        self.assertEqual([r["corridor"] for r in self.scene["rsus"]], ["漓江西路"]*4+["珠江路"]*5)
        self.assertEqual([(r["x"], r["y"]) for r in self.scene["intersections"][:4]],
                         [(455.78,287.17),(897.26,511.48),(1188.15,641.18),(1398.76,726.6)])
        for junction in self.scene["intersections"]:
            incoming = {lane.rsplit("_", 1)[0] for lane in junction["incomingLaneIds"]}
            road_names = {e.get("name") for e in self.net.findall("edge") if e.get("id") in incoming}
            self.assertTrue(set(junction["name"].split(" × ")).issubset(road_names))

    def test_signal_provenance_and_controller_mapping(self):
        junctions = self.scene["intersections"]
        self.assertEqual([j["signalSynthetic"] for j in junctions], [False]*4+[True]*5)
        actual_tls = {t.get("id") for t in self.net.findall("tlLogic")}
        self.assertEqual(len({j["id"] for j in junctions}), 9)
        self.assertTrue({j["id"] for j in junctions}.issubset(actual_tls))
        for junction in junctions:
            controlled_lanes = {c.get("from")+"_"+c.get("fromLane")
                                for c in self.net.findall("connection") if c.get("tl") == junction["id"]}
            self.assertEqual(set(junction["incomingLaneIds"]), controlled_lanes)

    def test_source_osm_is_preserved_and_patches_only_existing_nodes(self):
        source = ROOT / "data/tangdao.osm.xml"
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         "7d008dd5adab60d4811444d9c331886782751e8431d813ba450227286f42754a")
        osm_nodes = {n.get("id") for n in ET.parse(source).getroot().findall("node")}
        patch = ET.parse(ROOT / "data/synthetic-signals.nod.xml").getroot()
        self.assertEqual(len(patch.findall("join")), 5)
        self.assertTrue({n.get("id") for n in patch.findall("node")}.issubset(osm_nodes))
        self.assertTrue(all(n.get("type") == "traffic_light" for n in patch.findall("node")))
        self.assertTrue(all(set(j.get("nodes").split()).issubset(osm_nodes) for j in patch.findall("join")))

    def test_matching_crossroads_are_under_700_metres_apart(self):
        pairs = self.scene["meta"]["demoArea"]["adjacentCorridorDistances"]
        self.assertEqual({p["crossRoad"] for p in pairs}, {"井冈山路", "武夷山路", "阿里山路"})
        by_name = {j["name"]:j for j in self.scene["intersections"]}
        for pair in pairs:
            a,b = by_name[pair["from"]],by_name[pair["to"]]
            measured = math.hypot(a["x"]-b["x"], a["y"]-b["y"])
            self.assertAlmostEqual(measured, pair["junctionCenterDistanceM"], places=2)
            self.assertLess(measured, 700)
            self.assertGreater(measured, 500)

    def test_all_routes_are_connected_and_cross_corridor_flows_use_both_roads(self):
        connections = {(c.get("from"),c.get("to")) for c in self.net.findall("connection")}
        edge_names = {e.get("id"):e.get("name") for e in self.net.findall("edge")}
        routes = self.routes.findall("route")
        self.assertEqual(len(routes), 8)
        for route in routes:
            edges = route.get("edges").split()
            self.assertTrue(set(zip(edges, edges[1:])).issubset(connections), route.get("id"))
            if route.get("id").startswith("side_"):
                self.assertTrue({"漓江西路", "珠江路"}.issubset({edge_names[e] for e in edges}), route.get("id"))

    def test_every_selected_signal_is_crossed_by_a_route(self):
        route_pairs = set()
        for route in self.routes.findall("route"):
            edges = route.get("edges").split()
            route_pairs.update(zip(edges, edges[1:]))
        for junction in self.scene["intersections"]:
            controlled_pairs = {(c.get("from"),c.get("to")) for c in self.net.findall("connection")
                                if c.get("tl") == junction["id"]}
            self.assertTrue(controlled_pairs.intersection(route_pairs), junction["name"])

    def test_actual_sumo_run_reached_all_nine_without_collisions_or_teleports(self):
        verification = self.stats["verification"]
        self.assertEqual(float(verification["ranToTime"]), 599.8)
        self.assertEqual(int(verification["lastSummary"]["collisions"]), 0)
        self.assertEqual(int(verification["lastSummary"]["teleports"]), 0)
        self.assertEqual(len(verification["actualIntersectionApproaches"]), 9)
        self.assertTrue(all(a["sampledVehicleSeconds"] > 0 for a in verification["actualIntersectionApproaches"].values()))
        self.assertLessEqual(verification["maxRunningVehicles"], 160)
        for relative, digest in self.stats["inputSha256"].items():
            self.assertEqual(hashlib.sha256((ROOT/relative).read_bytes()).hexdigest(), digest, relative)


if __name__ == "__main__":
    unittest.main()
