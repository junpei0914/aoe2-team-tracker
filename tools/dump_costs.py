"""ゲームのデータファイル(empires2_x2_p1.dat)から、ユニット・建物・研究の費用と、文明ごとの割引を書き出す。

録画から「集めた資源の量」(手元の資源の増減 + 使った量)を出すために、tools/import_replays.py が
tools/replay_costs.json を読む。データファイルの解析ライブラリ genieutils-py は Python 3.11 以上が必要で、
取り込み(mgz 1.8.51)とは別の Python で動かす。ゲームのパッチで費用が変わったら実行し直してコミットする。

    py -3.14 -m pip install genieutils-py==0.1.2
    py -3.14 tools/dump_costs.py
"""
import json
import os

from genieutils.datfile import DatFile

DAT = r"C:\Program Files (x86)\Steam\steamapps\common\AoE2DE\resources\_common\dat\empires2_x2_p1.dat"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay_costs.json")

# 時代進化の研究の番号(データファイルの中の名前は Middle Age=領主・Feudal Age=城主・Imperial Age=帝王)
AGE_TECHS = {101: "feudal", 102: "castle", 103: "imperial"}
# 開始時に研究済みになるもの(暗黒の時代・町の中心の付属の影の研究)が条件の割引は、最初から効いている
START_TECHS = {104, 307}
AGE_ORDER = ["start", "feudal", "castle", "imperial"]
# ユニット・建物の費用を変える効果: 型 0(設定)・4(加算)・5(掛け算)で、属性 100(全部)・103(食料)・104(木)・105(金)・106(石)
COST_ATTRS = {100, 103, 104, 105, 106}
UNIT_COST_TYPES = {0, 4, 5}
TECH_COST_TYPE = 101  # 研究の費用を変える効果(a=研究, b=資源(-1は全部), c=0設定・1加算・2掛け算, d=量)
# 市場の手数料(属性78)・貢物の手数料(属性46)を変える研究
GUILDS, COINAGE, BANKING = 15, 23, 17


def costs(resource_costs):
    """[食料, 木, 石, 金]"""
    out = [0, 0, 0, 0]
    for r in resource_costs:
        if 0 <= r.type <= 3 and r.amount > 0:
            out[r.type] += r.amount
    return out


def cost_cmds(d, effect_id):
    if effect_id < 0 or effect_id >= len(d.effects):
        return []
    out = []
    for c in d.effects[effect_id].effect_commands:
        if (c.type in UNIT_COST_TYPES and c.c in COST_ATTRS) or c.type == TECH_COST_TYPE:
            out.append([c.type, c.a, c.b, c.c, round(c.d, 4)])
    return out


def set_value(d, tech_id, resource):
    """研究で設定される属性の値(Guilds なら市場の手数料)"""
    for c in d.effects[d.techs[tech_id].effect_id].effect_commands:
        if c.type == 1 and c.a == resource:
            return round(c.d, 4)
    raise ValueError(f"研究 {tech_id} に属性 {resource} の設定がない")


def main():
    d = DatFile.parse(DAT)
    gaia = d.civs[0]
    units, classes = {}, {}
    for i, u in enumerate(gaia.units):
        if u is None or u.creatable is None:
            continue
        c = costs(u.creatable.resource_costs)
        if any(c):
            units[i] = c
            classes[i] = u.class_
    techs = {i: costs(t.resource_costs) for i, t in enumerate(d.techs) if any(costs(t.resource_costs))}
    researchable = lambda t: any(l.location_id >= 0 for l in t.research_locations)
    # 研究時間(秒。研究できる建物が複数あれば最初のもの)。研究で費用や手数料が変わる時刻(押した時刻 + 研究時間)に使う
    tech_time = {i: next(l.research_time for l in t.research_locations if l.location_id >= 0)
                 for i, t in enumerate(d.techs) if researchable(t)}
    # 研究すると費用が変わるもの(城の固有の研究・兵舎の補給など)。研究し終わった時刻から効く
    research_mods = {}
    for ti, t in enumerate(d.techs):
        cmds = cost_cmds(d, t.effect_id)
        if cmds and researchable(t):
            research_mods[ti] = cmds
    # 文明ごとの割引: 技術ツリーの効果(最初から)と、その文明だけの技術のうち建物で研究せず費用もかからないもの
    # (文明のボーナスとして、最初から、または時代に到達したときに自動で効く)
    civs = {}
    skipped = []
    for ci, c in enumerate(d.civs):
        mods = []
        tree = cost_cmds(d, c.tech_tree_id)
        if tree:
            mods.append(["start", tree])
        for ti, t in enumerate(d.techs):
            if t.civ != ci or researchable(t) or any(costs(t.resource_costs)):
                continue  # 研究するもの(固有の研究など)は research_mods で、研究した時刻から当てる
            cmds = cost_cmds(d, t.effect_id)
            if not cmds:
                continue
            reqs = [r for r in t.required_techs if r >= 0 and r not in START_TECHS]
            if any(r not in AGE_TECHS for r in reqs):
                skipped.append((c.name, t.name))  # 時代以外の研究が条件のもの(録画では判定できないので入れない)
                continue
            age = max((AGE_TECHS[r] for r in reqs), key=AGE_ORDER.index, default="start")
            mods.append([age, cmds])
        if mods:
            civs[ci] = mods
    res0 = d.civs[1].resources
    out = {
        "source": os.path.basename(DAT),
        "units": units,
        "classes": classes,
        "techs": techs,
        "techTime": tech_time,
        "researchMods": research_mods,
        "civs": civs,
        "civNames": [c.name for c in d.civs],
        "market": {"fee": round(res0[78], 4), "guilds": GUILDS, "guildsFee": set_value(d, GUILDS, 78)},
        "tribute": {"fee": round(res0[46], 4), "coinage": COINAGE, "coinageFee": set_value(d, COINAGE, 46),
                    "banking": BANKING, "bankingFee": set_value(d, BANKING, 46)},
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"ユニット・建物 {len(units)}・研究 {len(techs)}・割引のある文明 {len(civs)}・費用を変える研究 {len(research_mods)} → {OUT}")
    if skipped:
        print("時代以外が条件の割引(入れていない):", skipped)


if __name__ == "__main__":
    main()
