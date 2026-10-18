"""AoE2 DE / HD の録画ファイル(.aoe2record)を解析して、index.html の録画データ(#replay-data)を書き換える。

使い方:
    python -m pip install mgz==1.8.51
    python tools/import_replays.py                  # 既定の録画フォルダ(DE と HD)を読む
    python tools/import_replays.py <フォルダ> ...    # 録画フォルダを指定する

録画に入っているのは各プレイヤーの「操作」だけで、ゲーム終了時のスコアは入っていない。
ここで取り出すのは、文明・チーム・勝敗・時代進化の時刻・生産したユニット・建てた建物・研究・eAPM
(HD の録画では生産したユニットと建物は取れない)。
"""
import glob
import inspect
import io
import json
import logging
import math
import os
import re
import struct
import sys
from collections import Counter
from datetime import datetime

logging.disable(logging.WARNING)  # mgz が「fast parse に失敗したので遅い方で読む」などと大量に出すので黙らせる

import mgz.fast.header as fast_header  # noqa: E402


def patch_mgz():
    """mgz 1.8.51 は save version 67.2 以降(2026/5 以降のゲーム)を読めないので、実行時に直す。

    67.2 以降では、DEヘッダーの
      ・各プレイヤーの情報の末尾に文字列が1つ
      ・ヘッダー末尾のタイムスタンプの前に文字列が2つ
    増えている(どちらも空文字列)。読み飛ばす処理を足す。mgz 側で対応済みなら何もしない。
    """
    src = inspect.getsource(fast_header.parse_de)
    if "67.2" in src:
        return
    a = "        if save >= 64.3:\n            data.read(4)\n\n        players.append("
    b = "    if not skip:\n        de_string(data)\n        data.read(8)"
    if a not in src or b not in src:
        raise RuntimeError("mgz のバージョンが想定と違うため修正を当てられません(mgz==1.8.51 を入れてください)")
    src = src.replace(a, "        if save >= 64.3:\n            data.read(4)\n        if save >= 67.2:\n            de_string(data)\n\n        players.append(")
    src = src.replace(b, "    if not skip:\n        de_string(data)\n        if save >= 67.2:\n            de_string(data)\n            de_string(data)\n        data.read(8)")
    exec(compile(src, fast_header.__file__, "exec"), fast_header.__dict__)


patch_mgz()


def patch_queue_action():
    """mgz 1.8.51 は 2025/4 以前(save version 66.3 未満)の生産命令(DE_QUEUE)を読めず、ERROR として捨ててしまう。

    この時期の DE_QUEUE は
      プレイヤー(1) / 不明(2) / 選択数(2) / 0埋め(4) / 建物の種類(2) / ユニット番号(2) / 数(2) / 建物ID(4×選択数)
    の並びになっている。mgz の読み方で失敗したときだけ、この並びで読み直す。
    """
    import struct
    import mgz.fast as fast
    from mgz.fast.enums import Action

    if getattr(fast.parse_action, "_queue_patched", False):
        return
    original = fast.parse_action

    def parse_action(action_type, data):
        try:
            return original(action_type, data)
        except struct.error:
            if action_type != Action.DE_QUEUE or len(data) < 15:
                raise
            player_id, selected = struct.unpack_from("<b2xh", data)
            _building, unit_id, amount = struct.unpack_from("<hhh", data, 9)
            if selected < 0 or len(data) < 15 + 4 * selected:
                raise
            object_ids = list(struct.unpack_from(f"<{selected}I", data, 15))
            return dict(player_id=player_id, object_ids=object_ids, amount=amount, unit_id=unit_id)

    parse_action._queue_patched = True
    fast.parse_action = parse_action


patch_queue_action()
import mgz.fast as fast  # noqa: E402
from mgz.fast.enums import Action, Operation  # noqa: E402
from mgz.model import parse_match  # noqa: E402
from mgz.reference import get_dataset  # noqa: E402
from mgz.summary import Summary  # noqa: E402
from mgz.util import Version  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
DEFAULT_DIRS = [
    os.path.expanduser(r"~\Games\Age of Empires 2 DE"),
    r"C:\Program Files (x86)\Steam\steamapps\common\Age2HD\SaveGame",  # AoE2 HD(2013年版)。2023/12〜2024/1 の試合
]

# 録画上の名前 → ツールの名前
NAME_MAP = {"Ke_ppy": "ke_ppy", "dimax": "max"}
# HD の文明名 → DE の文明名(画面の日本語名は DE の名前で引く)
HD_CIV_NAMES = {"Mayans": "Maya", "Indians": "Hindustanis"}
# 録画から勝敗が判定できなかった試合の勝ったチーム(そのチームにいた人の録画上の名前)。ユーザー確認済み
WINNER_OVERRIDES = {
    "rec-20240107-0035": "ishi_sawada",  # HD。両チームから投了者が出ていて判定できない(2026/10 確認)
}
# ゲームの技術ツリー(文明ごとに作れるもの)。パッチで変わるので、取り込みのたびに読み直す
CIV_TREE_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\AoE2DE\resources\_common\dat\CivTechTrees"
CIV_TREE_NAMES = {"MAYANS": "Maya", "INDIANS": "Hindustanis", "MAGYAR": "Magyars"}  # ファイル名 → 録画の文明名
CIV_TREE_MIN_CIVS = 10
JA_STRINGS = r"C:\Program Files (x86)\Steam\steamapps\common\AoE2DE\resources\jp\strings\key-value\key-value-strings-utf8.txt"
MIN_DURATION = 600  # 10分未満はやり直し(リスタート)とみなして取り込まない
AGES = {"Feudal Age": "feudal", "Castle Age": "castle", "Imperial Age": "imperial"}
# 時代進化の研究時間(秒、ゲーム内時間)。録画に残るのは研究を始めた(ボタンを押した)時刻なので、
# これを足して「時代に到達した時刻」にする(ゲーム画面の表示や体感と合わせるため。2026/10 ユーザー指摘)
AGE_RESEARCH_SEC = {"feudal": 130, "castle": 160, "imperial": 190}
# 文明ボーナスで時代進化が速い文明(ゲーム内の文明説明から)。マレーは時代進化+66%、
# ペルシアは町の中心の作業速度が暗黒/領主/城主の時代で+5/10/15%(進化の研究は1つ前の時代に行う)
AGE_SPEED = {
    "Malay": {"feudal": 1.66, "castle": 1.66, "imperial": 1.66},
    "Persians": {"feudal": 1.05, "castle": 1.10, "imperial": 1.15},
}
# 町の中心の生産時間(秒、ゲーム内時間)。町の中心が空いていた時間を出すために、待ち行列を再現する(simulate_production)
VILLAGER_SEC = 25
TC_TECH_SEC = {"Loom": 25, "Wheelbarrow": 75, "Hand Cart": 55, "Town Watch": 25, "Town Patrol": 40}
# 町の中心の作業速度が速い文明(ゲーム内の文明説明から)。ペルシアは暗黒/領主/城主/帝王の時代で +5/10/15/20%
TC_SPEED = {"Persians": [1.05, 1.10, 1.15, 1.20]}
TC_IDLE_END = 45 * 60  # 空いていた時間はこの時刻まで保存する(人口が埋まった後の空きは問題にならないため)
# 町の中心の研究の時間が文明で違うもの(ゲーム内の文明説明から): ゴートは機織りが即時、ベトナムは経済のアップグレードが+100%速い
TC_TECH_SPEED = {"Goths": {"Loom": None}, "Vietnamese": {"Wheelbarrow": 2.0, "Hand Cart": 2.0}}
# 開始時の食料(標準の開始資源 木200・食料200・金100・石200 から文明ボーナスで変わるもの。ゲーム内の文明説明から)。
# 開始直後は食料が入ってこないので、予約できる町の人はこの食料の分まで。それを超えて連打した分は資源不足で弾かれている
START_FOOD = {"Chinese": 0, "Persians": 250, "Maya": 150, "Lithuanians": 300}
START_FOOD_DEFAULT = 200
START_WINDOW = 20  # 最初に食料が入るまでの秒数(これより前の予約は開始時の食料で払う)
# その時代に入らないとできない行動(建物・研究・ユニット)。録画にこれが出た時刻には時代進化が終わっているので、
# 時代進化の研究はその時刻 − 研究時間 までに始まっている。再現した待ち行列がそれに間に合わなければ、
# 前に並んでいる町の人の予約の一部は資源不足で弾かれていた(作られていない)とみなす(2026/10 レビューで指摘)
AGE_GATED_BUILD = {
    "Archery Range": "feudal", "Stable": "feudal", "Market": "feudal", "Blacksmith": "feudal", "Watch Tower": "feudal",
    # 町の中心は外す(まだ建てられない時代の建設命令も録画に残っていた。2026/10 再レビューで指摘)
    "Castle": "castle", "University": "castle", "Monastery": "castle", "Siege Workshop": "castle",
    "Bombard Tower": "imperial", "Keep": "imperial",
}
AGE_GATED_TECH = {
    "Double-Bit Axe": "feudal", "Horse Collar": "feudal", "Wheelbarrow": "feudal", "Gold Mining": "feudal", "Stone Mining": "feudal",
    "Man-at-Arms": "feudal", "Fletching": "feudal", "Forging": "feudal", "Scale Mail Armor": "feudal", "Scale Barding Armor": "feudal",
    "Padded Archer Armor": "feudal", "Bloodlines": "feudal", "Town Watch": "feudal",
    "Bow Saw": "castle", "Heavy Plow": "castle", "Hand Cart": "castle", "Long Swordsman": "castle", "Pikeman": "castle",
    "Crossbowman": "castle", "Elite Skirmisher": "castle", "Light Cavalry": "castle", "Iron Casting": "castle",
    "Chain Mail Armor": "castle", "Chain Barding Armor": "castle", "Leather Archer Armor": "castle", "Bodkin Arrow": "castle",
    "Squires": "castle", "Ballistics": "castle", "Masonry": "castle", "Town Patrol": "castle",
    "Two-Handed Swordsman": "imperial", "Champion": "imperial", "Halberdier": "imperial", "Arbalester": "imperial",
    "Cavalier": "imperial", "Hussar": "imperial", "Blast Furnace": "imperial", "Plate Mail Armor": "imperial",
    "Plate Barding Armor": "imperial", "Ring Archer Armor": "imperial", "Bracer": "imperial", "Two-Man Saw": "imperial",
    "Crop Rotation": "imperial", "Conscription": "imperial", "Siege Ram": "imperial", "Onager": "imperial",
}
AGE_GATED_UNIT = {
    "Archer": "feudal", "Skirmisher": "feudal", "Spearman": "feudal", "Scout Cavalry": "feudal",
    "Knight": "castle", "Monk": "castle", "Mangonel": "castle", "Scorpion": "castle", "Camel Rider": "castle", "Cavalry Archer": "castle",
    "Hand Cannoneer": "imperial", "Bombard Cannon": "imperial",
}
# 文明ボーナスで早い時代からできるもの(ゲーム内の文明説明から。手がかりに使わない)。
# クマンは領主の時代に攻城兵器工房と破城槌、ブルゴーニュは経済のアップグレードを1つ前の時代に研究でき、重騎士も城主の時代に研究できる
AGE_GATED_EXCEPT = {
    "Cumans": {"Siege Workshop", "Battering Ram"},
    "Burgundians": {"Double-Bit Axe", "Horse Collar", "Wheelbarrow", "Gold Mining", "Stone Mining", "Bow Saw", "Heavy Plow",
                    "Hand Cart", "Two-Man Saw", "Crop Rotation", "Gold Shaft Mining", "Stone Shaft Mining", "Cavalier"},
}
# 同じボタンをこの秒数より短い間隔で押したものは、1回の操作(連打)とみなす
CLICK_BURST = 2
# 軍などの生産時間(秒、ゲーム内時間。標準の値)。建物ごとの待ち行列を再現して、試合が終わるまでに作り始められない予約を除くのに使う。
# 文明ボーナスや徴用で速くなる分は MIL_SPEED_FACTOR でまとめて甘めに見る(作れなかったと言い切れるものだけ除くため)
UNIT_TRAIN_SEC = {
    "Militia": 21, "Spearman": 22, "Eagle Scout": 60, "Eagle Warrior": 35, "Archer": 35, "Skirmisher": 22, "Cavalry Archer": 34,
    "Hand Cannoneer": 34, "Scout Cavalry": 30, "Knight": 30, "Camel Rider": 22, "Camel Scout": 22, "Battle Elephant": 24,
    "Steppe Lancer": 24, "Battering Ram": 36, "Mangonel": 46, "Scorpion": 30, "Trebuchet": 50, "Bombard Cannon": 56, "Monk": 51,
    "Trade Cart": 51, "Fishing Ship": 40, "Transport Ship": 45, "Galley": 60, "Fire Galley": 65, "Demolition Raft": 45, "Petard": 25,
}
UNIT_TRAIN_DEFAULT = 20  # 表にない固有ユニットなど(短めにして甘く見る)
MIL_SPEED_FACTOR = 0.8
QUEUE_MAX = 15  # 1つの建物の待ち行列に入る数(ゲームの上限)
# 保存する研究(経済・鍛冶屋・主なユニットのアップグレード)。全部入れるとデータが大きくなりすぎるため絞る
TECHS = {
    "Loom", "Wheelbarrow", "Hand Cart", "Double-Bit Axe", "Bow Saw", "Two-Man Saw", "Horse Collar",
    "Heavy Plow", "Crop Rotation", "Gold Mining", "Gold Shaft Mining", "Stone Mining", "Stone Shaft Mining",
    "Town Watch", "Town Patrol", "Fletching", "Bodkin Arrow", "Bracer", "Forging", "Iron Casting",
    "Blast Furnace", "Scale Mail Armor", "Chain Mail Armor", "Plate Mail Armor", "Scale Barding Armor",
    "Chain Barding Armor", "Plate Barding Armor", "Padded Archer Armor", "Leather Archer Armor",
    "Ring Archer Armor", "Bloodlines", "Husbandry", "Ballistics", "Thumb Ring", "Conscription",
    "Chemistry", "Squires", "Arson", "Man-at-Arms", "Long Swordsman", "Two-Handed Swordsman", "Champion",
    "Pikeman", "Halberdier", "Crossbowman", "Arbalester", "Elite Skirmisher", "Cavalier", "Paladin",
    "Light Cavalry", "Hussar", "Heavy Camel Rider", "Heavy Cavalry Archer", "Elite Eagle Warrior",
    "Supplies", "Gambesons", "Murder Holes", "Masonry", "Treadmill Crane", "Caravan", "Guilds",
    "Coinage", "Banking", "Elite Battle Elephant", "Onager", "Capped Ram", "Siege Ram",
}


def default_files(folders):
    files = []
    for folder in folders:
        files += glob.glob(os.path.join(folder, "**", "*.aoe2record"), recursive=True)
    return sorted(files, key=os.path.getmtime)


def parse_file(path):
    """録画を読み、(試合の情報, プレイヤーごとの集計, 勝者の判定方法) を返す。"""
    with open(path, "rb") as f:
        version = fast_header.parse_version(fast_header.decompress(f), f)[0]
    if version is Version.HD:
        info, ps = parse_hd(path)
    else:
        info, ps = parse_de(path)
    return info, ps, infer_winner(ps)


def parse_de(path):
    with open(path, "rb") as f:
        m = parse_match(f)
    ps = {}
    for p in m.players:
        ps[p.number] = dict(
            raw_name=p.name, civ=p.civilization, team=None, winner=bool(p.winner), actions=0, acts=[],
            ages={}, units=Counter(), buildings=Counter(), techs={}, resign=None,
            pos=(p.position.x, p.position.y) if p.position else None, events=[], prod_cmds=[], age_evidence={},
            age_clicks={}, tech_clicks={}, targets=[],
            # 手元の資源(木・食料・金・石の合計)。ゲームが同期のために録画に書いている値(約6〜14秒ごと)
            stock=[(r.timestamp.total_seconds(), r.total_resources) for r in (p.timeseries or [])],
        )
    for i, team in enumerate(m.teams):
        for p in team:
            ps[p.number]["team"] = i
    for a in m.actions:
        pl = getattr(a, "player", None)
        if pl is None or pl.number not in ps:
            continue
        P = ps[pl.number]
        t = round(a.timestamp.total_seconds())
        pay = a.payload or {}
        if a.type.name != "AI_ORDER":
            P["actions"] += 1
            P["acts"].append((t, ACTION_CATEGORY.get(a.type.name, OTHER_CATEGORY)))
        kind = a.type.name
        if kind in TARGET_ACTIONS and a.position is not None:
            P["targets"].append((t, a.position.x, a.position.y))
        if kind == "RESEARCH":
            tech = pay.get("technology")
            # 研究を押した時刻は全部とっておき、最後にまとめて決める(last_burst)
            if tech in AGES:
                P["age_clicks"].setdefault(AGES[tech], []).append(t)
            elif tech in TECHS or tech in BLACKSMITH_TECHS:
                P["tech_clicks"].setdefault(tech, []).append(t)
            ids = pay.get("object_ids") or []
            P["prod_cmds"].append((t, "research", ids[0] if ids else None, tech))
        elif kind in ("DE_QUEUE", "QUEUE") and pay.get("unit"):
            # 同じ sequence の命令が続くことがあるが、sequence はミリ秒単位の時刻で、同じ通信の区切りの中で2回押すと同じ値になる。
            # 開始直後の手元の資源と照らすと本物のクリックだったので、除かない(2026/10 レビューで確認)。
                        # 複数の建物を選んで生産しても、DEでは1回の命令で amount 体を空いている建物に振り分ける
            # 軍も資源不足で弾かれた予約が残るので、DE の人間は建物ごとの待ち行列の再現(apply_production)で作り始められた分に置き換える
            P["units"][pay["unit"]] += pay.get("amount") or 1
            P["events"].append((t, "unit", pay["unit"], pay.get("amount") or 1))
            P["prod_cmds"].append((t, "unit", tuple(i for i in (pay.get("object_ids") or []) if i is not None), (pay["unit"], pay.get("amount") or 1)))
        elif kind == "BUILD" and pay.get("building"):
            P["buildings"][pay["building"]] += 1
            P["events"].append((t, "build", pay["building"], 1))
        elif kind == "RESIGN":
            P["resign"] = t
        elif kind == "SPECIAL" and pay.get("order") == "Unqueue":
            # 生産の取り消し。object_ids には建物ID×256 が入っている(2024/2 以前の録画は建物IDそのもの。slot_id は待ち行列の何番目の枠か)
            for oid in pay.get("object_ids") or []:
                P["prod_cmds"].append((t, "unqueue", oid, pay.get("slot_id") or 0))
        # その時代に入らないとできない行動の時刻(時代進化の研究がいつまでに終わっていたかの手がかり)。
        # 録画には実際にはできなかった命令も残るので、全部とっておき、ボタンを押した時刻 + 研究時間 より後のものだけ使う(simulate_production)
        name = pay.get("building") if kind == "BUILD" else pay.get("technology") if kind == "RESEARCH" else pay.get("unit") if kind in ("DE_QUEUE", "QUEUE") else None
        gated = (AGE_GATED_BUILD if kind == "BUILD" else AGE_GATED_TECH if kind == "RESEARCH" else AGE_GATED_UNIT).get(name) if name else None
        if gated and name not in AGE_GATED_EXCEPT.get(P["civ"], ()):
            P["age_evidence"].setdefault(gated, []).append(t)
    for P in ps.values():
        finish_clicks(P)
    info = dict(edition="DE", duration=round(m.duration.total_seconds()), map=m.map.name, size=m.map.dimension,
                speed=(m.speed_id or 169) / 100)
    return info, ps


def parse_hd(path):
    """AoE2 HD(2013年版)の録画を読む。

    mgz の速い方の解析(parse_match)は HD のシナリオ設定部分を読めないので、
    プレイヤー・文明・チーム・勝敗・マップは遅い方の解析(Summary)で取り、
    研究と投了は本体の命令を直接読む。HD の生産命令(QUEUE)にはプレイヤー番号が入っておらず、
    建物が誰のものかも録画からは分からないため、生産数と建物は取れない(空にする)。
    """
    with open(path, "rb") as f:
        s = Summary(f)
        players = s.get_players()
        duration = round(s.get_duration() / 1000)
        game_map = s.get_map()
        speed = (s.get_settings().get("speed") or (150,))[0] / 100
    _, dataset = get_dataset(Version.HD, None)
    ps = {}
    for p in players:
        civ = dataset["civilizations"].get(str(p["civilization"]), {}).get("name")
        ps[p["number"]] = dict(
            raw_name=p["name"], civ=HD_CIV_NAMES.get(civ, civ), team=p["team_id"], winner=bool(p["winner"]),
            actions=0, acts=[], ages={}, units=Counter(), buildings=Counter(), techs={}, resign=None, age_clicks={}, tech_clicks={},
            pos=tuple(p["position"]) if p.get("position") else None,
        )
    with open(path, "rb") as f:
        data = io.BytesIO(f.read())
    header_len, _ = struct.unpack("<II", data.read(8))
    data.seek(header_len)
    fast.meta(data)
    ms = 0
    while True:
        try:
            op, payload = fast.operation(data)
        except EOFError:
            break
        if op == Operation.SYNC:
            ms += payload[0]
        elif op == Operation.ACTION:
            kind, pay = payload
            P = ps.get(pay.get("player_id"))
            if P is None:
                continue
            t = round(ms / 1000)
            if kind != Action.AI_ORDER:
                P["actions"] += 1
                P["acts"].append((t, ACTION_CATEGORY.get(kind.name, OTHER_CATEGORY)))
            if kind == Action.RESEARCH:
                tech = dataset["technologies"].get(str(pay.get("technology_id")))
                # DE と同じく、押した時刻は全部とっておき最後の操作を使う(finish_clicks)
                if tech in AGES:
                    P["age_clicks"].setdefault(AGES[tech], []).append(t)
                elif tech in TECHS:
                    P["tech_clicks"].setdefault(tech, []).append(t)
            elif kind == Action.RESIGN:
                P["resign"] = t
    for P in ps.values():
        finish_clicks(P, with_events=False)  # HD は生産が取れないので、途中経過(events)は作らない
    # カスタムマップは日本語環境だと「スタイル: カスタム」のような名前になる
    name = game_map.get("name") or ""
    info = dict(edition="HD", duration=duration, map="Custom" if game_map.get("custom") or "カスタム" in name else name,
                size=game_map.get("dimension"), speed=speed)
    return info, ps


def infer_winner(ps):
    winner_source = "game" if any(P["winner"] for P in ps.values()) else None
    if not winner_source:
        # 勝者が記録されていないとき: 人間が全員投了したチームを負けとする(両方なら先に全員投了した方)
        done = {}
        teams = {P["team"] for P in ps.values()}
        for tm in teams:
            humans = [P for P in ps.values() if P["team"] == tm and P["raw_name"]]
            if humans and all(P["resign"] is not None for P in humans):
                done[tm] = max(P["resign"] for P in humans)
        if done and len(teams) == 2:
            loser = min(done, key=done.get)
            others = teams - {loser}
            if len(done) == 1 or done[loser] < min(done[o] for o in others) - 5:
                for P in ps.values():
                    P["winner"] = P["team"] in others
                winner_source = "resign"
    return winner_source


BLACKSMITH_TECHS = {
    "Forging", "Iron Casting", "Blast Furnace", "Fletching", "Bodkin Arrow", "Bracer", "Scale Mail Armor",
    "Chain Mail Armor", "Plate Mail Armor", "Padded Archer Armor", "Leather Archer Armor", "Ring Archer Armor",
    "Scale Barding Armor", "Chain Barding Armor", "Plate Barding Armor",
}
NON_MILITARY = {"Villager", "Trade Cart", "Fishing Ship", "Transport Ship", "Trade Cog"}
SIEGE = {"Battering Ram", "Mangonel", "Scorpion", "Trebuchet", "Bombard Cannon"}
# 途中の時点(分)での累計。終了時の合計だと、優勢になって余裕ができたから増えた分(結果)が混ざるため、
# 勝敗が決まる前の時点で比べられるようにする(2026/10 ユーザー指摘)
CHECKPOINTS = list(range(5, 65, 5))  # 5〜60分。軍は40分以降に多く作られるので60分まで(2026/10 ユーザー指摘)
PER_MINUTE_END = 80  # グラフ用に1分ごとの累計を出す範囲(分)


def checkpoints(P, end):
    """各時点までの累計: v=町の人, m=軍, tc=追加した町の中心, tr=荷馬車, sg=攻囲兵器, cs=城の建設, bs=鍛冶場の研究。

    生産はキューに入れた数、城は建設を指示した回数(合計と同じく目安)。試合が終わった後の時点は入れない。
    町の中心は建設の指示の回数だと実際より多く出る(1試合13など)ので、最初の町の中心以外で、
    町の人か研究を予約した町の中心の数にする(建て終わってから最初に使うまでの分だけ遅めに出る)。
    HD の録画は生産命令の持ち主が分からないので空。
    """
    out = {}
    if not P.get("events"):
        return out
    tc_first = ((P.get("tc_result") or {}).get("f") or [0])[1:]
    for cp in CHECKPOINTS:
        lim = cp * 60
        if lim > end:
            break
        c = dict(v=0, m=0, tc=0, tr=0, sg=0, cs=0, bs=0)
        for t, kind, name, n in P["events"]:
            if t > lim:
                continue
            if kind == "unit":
                if name == "Villager":
                    c["v"] += n
                elif name == "Trade Cart":
                    c["tr"] += n
                elif name not in NON_MILITARY:
                    c["m"] += n
                    if name in SIEGE:
                        c["sg"] += n
            elif kind == "build":
                if name == "Castle":
                    c["cs"] += n
        # 鍛冶場の研究は、押し直しで二重に数えないよう種類の数にする
        c["bs"] = len({name for t, kind, name, _ in P["events"] if kind == "bs" and t <= lim})
        c["tc"] = sum(1 for f in tc_first if f <= lim)
        out[str(cp)] = c
    return out


def per_minute(P, end):
    """グラフ用: 0分から1分ごと(PER_MINUTE_END 分まで、試合が終わるまで)の町の人(v)・軍(m)の累計。HD は空"""
    if not P.get("events"):
        return {}
    last = min(PER_MINUTE_END, int(end // 60))
    v = [0] * (last + 1)
    m = [0] * (last + 1)
    for t, kind, name, n in P["events"]:
        if kind != "unit" or t > last * 60:
            continue
        i = -(-t // 60)  # その時刻を含む分の終わり(t=61秒なら2分時点から数える)
        if name == "Villager":
            v[i] += n
        elif name not in NON_MILITARY:
            m[i] += n
    for i in range(1, last + 1):
        v[i] += v[i - 1]
        m[i] += m[i - 1]
    return {"v": v, "m": m}


# 操作(録画に残る命令)の種類。選択や画面の移動は録画に残らない
ACTION_CATEGORIES = ["move", "order", "queue", "build", "research", "gather", "other"]
OTHER_CATEGORY = ACTION_CATEGORIES.index("other")
ACTION_CATEGORY = {name: ACTION_CATEGORIES.index(cat) for cat, names in {
    # 移動(移動・攻撃しながら移動・中継点・陣形・巡回・追従・護衛・停止・構え・退却)
    "move": ["MOVE", "DE_ATTACK_MOVE", "ADD_WAYPOINT", "GROUP_MULTI_WAYPOINTS", "FORMATION", "PATROL", "FOLLOW", "GUARD",
             "STOP", "STANCE", "DE_RETREAT"],
    # 指示(対象をクリックする命令。攻撃・採集・駐留など。町の人と軍の区別はできない)
    "order": ["ORDER", "ATTACK_GROUND", "WORK"],
    "queue": ["DE_QUEUE", "QUEUE", "MAKE", "MULTIQUEUE"],
    "build": ["BUILD", "WALL", "REPAIR", "GATE"],
    "research": ["RESEARCH"],
    "gather": ["GATHER_POINT", "DE_MULTI_GATHERPOINT"],
}.items() for name in names}
# 攻め先(tg)を数える命令(移動・指示)。命令には目標の座標が入っている(2026/10 確認)
TARGET_ACTIONS = {"MOVE", "ORDER", "PATROL", "ATTACK_GROUND", "DE_ATTACK_MOVE"}
TARGET_RADIUS = 0.15  # 開始位置からこの距離(マップの一辺に対する割合。200マスで30マス)以内を「その人の陣地」とみなす
TARGET_CASTLE_SEC = 600  # 城主の時代は到達してからこの秒数までを数える(城主の時代の最初の攻め)
EAPM_WINDOW = 5  # 時間ごとの eAPM は5分ごと(分)
EAPM_WINDOW_END = 60


def action_stats(P, end, speed):
    """操作の種類ごとの数を、時代ごと(ac)と5分ごと(aw)にまとめる。

    ac: {時代: [その時代にいた秒数(ゲーム内時間), 種類ごとの数...]}。時代は到達した時刻で区切る(暗黒=dark)
    aw: 5分ごとの [種類ごとの数...]。試合(投了)の終わりまで丸ごと入る区間だけ
    eAPM にするときは、数 ÷ (秒数 / 60 / 速度)。速度は試合ごとの値(DE 1.69・HD 1.5)を speed に入れる
    """
    if not P.get("acts"):
        return None, None
    ages = reached_ages(P, end)
    bounds = [("dark", 0)] + [(age, ages[age]) for age in ("feudal", "castle", "imperial") if age in ages]
    ac = {}
    for i, (age, start) in enumerate(bounds):
        stop = bounds[i + 1][1] if i + 1 < len(bounds) else end
        if stop <= start:
            continue
        counts = [0] * len(ACTION_CATEGORIES)
        for t, c in P["acts"]:
            if start <= t < stop:
                counts[c] += 1
        ac[age] = [stop - start] + counts
    aw = []
    for w in range(0, EAPM_WINDOW_END, EAPM_WINDOW):
        lo, hi = w * 60, (w + EAPM_WINDOW) * 60
        if hi > end:
            break
        counts = [0] * len(ACTION_CATEGORIES)
        for t, c in P["acts"]:
            if lo <= t < hi:
                counts[c] += 1
        aw.append(counts)
    return ac, aw


def units_by_age(P, end):
    """時代ごとに作った(キューに入れた)軍の数: {時代: {ユニット: 数}}。町の人・荷馬車などは除く。

    録画には戦闘の結果が残らないので、その時点で何体残っているかは分からない。代わりに「いつ何を作ったか」で
    時代ごとの主力の切り替え(城主の時代は騎士、帝王の時代は民兵系など)を見る(2026/10 ユーザー指摘)。HD は空
    """
    if not P.get("events"):
        return None
    ages = reached_ages(P, end)
    bounds = [("dark", 0)] + [(age, ages[age]) for age in ("feudal", "castle", "imperial") if age in ages]
    out = {}
    for t, kind, name, n in P["events"]:
        if kind != "unit" or name in NON_MILITARY:
            continue
        age = [a for a, start in bounds if start <= t][-1]
        out.setdefault(age, Counter())[name] += n
    return {age: dict(c.most_common()) for age, c in out.items()}


def action_fields(P, end, speed):
    """記録に書き込む操作の内訳。AI は操作がほとんど録画に残らず数が当てにならないので入れない"""
    if not P["raw_name"]:
        return {}
    ac, aw = action_stats(P, end, speed)
    out = {"ac": ac, "aw": aw} if ac else {}
    ua = units_by_age(P, end)
    if ua:
        out["ua"] = ua
    if P.get("tc_result"):
        out["tc"] = P["tc_result"]
    rs = stock_per_minute(P, end)
    if rs:
        out["rs"] = rs
        # 時代進化のボタンを押した直前の手元の資源(押した時刻より前の最後の値)
        rsa = {}
        for age, t in P["ages"].items():
            before = [v for x, v in P["stock"] if x <= t]
            if before and t <= end:
                rsa[age] = before[-1]
        if rsa:
            out["rsa"] = rsa
    return out


def stock_per_minute(P, end):
    """手元の資源(木・食料・金・石の合計)の1分ごとの平均: [0〜1分, 1〜2分, ...](試合が終わるまで、PER_MINUTE_END 分まで)。

    DE の録画には、ゲームが同期の確認のために各プレイヤーの値を約6〜14秒ごとに書いていて、mgz が timeseries として読む。
    開始直後に何も予約していない人は 700(初期資源の合計)、城主の時代のボタン(1000)を押すと800〜960減るので、
    「その時点で手元にある資源の合計」とみてよい(2026/10 確認)。種類ごとの内訳や、集めた量・使った量は入っていない。
    """
    samples = P.get("stock") or []
    if not samples:
        return None
    out = []
    last = min(PER_MINUTE_END, int(end // 60))
    for i in range(last):
        vals = [v for t, v in samples if i * 60 <= t < (i + 1) * 60]
        if not vals:
            # その1分に値がなければ、直前の値を使う
            prev = [v for t, v in samples if t < i * 60]
            vals = prev[-1:] if prev else []
        out.append(round(sum(vals) / len(vals)) if vals else None)
    return out


def real_eapm(P, end, speed):
    """1分あたりの操作数(実際の時間の1分あたり)。

    mgz の eapm は「ゲーム内時間の1分あたり」で、試合全体の長さで割っている。ゲームは標準の速度で
    DE 1.69倍・HD 1.5倍で進むので、実際の時間(体感やほかのツールの APM)より低く出る(2026/10 ユーザー指摘)。
    ここでは投了した人は投了までの時間で割り、ゲームの速度を掛けて実際の時間の1分あたりにする。
    数えているのは録画に残る「命令」(移動・生産・建設・研究など)だけで、選択や画面の移動は入らない。
    """
    if not end or not P.get("actions"):
        return None
    return round(P["actions"] / (end / 60) * speed)


def reached_ages(P, end):
    """時代に到達した時刻。到達する前に試合が終わった(投了した)時代は除く。

    建物ごとの待ち行列の再現ができた人(DE の人間)は、待ち行列で研究の開始が遅れた分も入れた時刻(apply_production の reach)。
    時代進化の研究は待ち行列の後ろに入るので、前に町の人が並んでいるとボタンを押した時刻 + 研究時間 より遅く到達する(2026/10 レビューで指摘)。
    それ以外(HD・AI)は、ボタンを押した時刻(時間をあけて押し直していれば最後の操作。last_burst) + 研究時間。待ち行列で研究の開始が遅れた分は入らない。
    """
    src = P["reach"] if P.get("reach") is not None else clicked_reach(P)
    return {age: at for age, at in src.items() if at <= end}


def target_fields(P, ps, size, end):
    """領主の時代・城主の時代の最初の10分に、移動・指示の命令をほかの誰の陣地へ出したか: {時代: {名前: 命令の数}}。

    命令の目標の座標から一番近い開始位置を探し、それが自分以外で TARGET_RADIUS 以内なら、その人の陣地への命令として数える。
    相手の後衛の陣地への命令が多ければ「後衛を攻めた(直)」、味方の陣地なら「味方を助けに行った」とみなせる(画面側の replayStrategy)。
    ユニットが着いたか・何を倒したかは録画に残らないので、攻めた成果は相手の時代進化の遅れなどで見る
    """
    if not P.get("targets") or not size or any(Q.get("pos") is None for Q in ps.values()):
        return None
    ages = reached_ages(P, end)
    windows = {}
    if "feudal" in ages:
        windows["feudal"] = (ages["feudal"], ages.get("castle", end))
    if "castle" in ages:
        windows["castle"] = (ages["castle"], min(end, ages["castle"] + TARGET_CASTLE_SEC))
    radius = size * TARGET_RADIUS
    out = {}
    for age, (lo, hi) in windows.items():
        c = Counter()
        for t, x, y in P["targets"]:
            if not lo <= t < hi:
                continue
            Q = min(ps.values(), key=lambda Q: math.hypot(x - Q["pos"][0], y - Q["pos"][1]))
            if Q is not P and math.hypot(x - Q["pos"][0], y - Q["pos"][1]) <= radius:
                c[Q["name"]] += 1
        out[age] = dict(c.most_common())
    return out


def assign_roles(ps, size):
    """開始位置から、各プレイヤーが前衛(両端。flank)か後衛(内側。pocket)かを判定して P["role"] に入れる。

    アラビアなどのチーム戦ではマップの中心を囲む円周上に並び、同じチームは連続した弧を占める。
    中心から見た角度で並べ、チームの弧の両端の人を前衛、間の人を後衛とする。
    2人チーム(両方が端)や、敵が弧の中に混ざっている(チームの位置がばらばら)場合は判定しない(None)。
    """
    for P in ps.values():
        P["role"] = None
    if not size or any(P.get("pos") is None for P in ps.values()):
        return
    c = size / 2
    ang = {n: math.atan2(P["pos"][1] - c, P["pos"][0] - c) for n, P in ps.items()}
    for team in {P["team"] for P in ps.values()}:
        mine = sorted((n for n in ps if ps[n]["team"] == team), key=lambda n: ang[n])
        if len(mine) < 3:
            continue
        # 角度の隙間が一番大きいところで切ると、チームの弧が一続きになる
        gaps = [(ang[mine[(i + 1) % len(mine)]] - ang[mine[i]]) % (2 * math.pi) for i in range(len(mine))]
        cut = max(range(len(mine)), key=lambda i: gaps[i])
        arc = mine[cut + 1:] + mine[:cut + 1]
        start, span = ang[arc[0]], (ang[arc[-1]] - ang[arc[0]]) % (2 * math.pi)
        enemies = [n for n in ps if ps[n]["team"] != team]
        if any(0 < (ang[e] - start) % (2 * math.pi) < span for e in enemies):
            continue
        for i, n in enumerate(arc):
            ps[n]["role"] = "flank" if i in (0, len(arc) - 1) else "pocket"


def tc_speed(P, t):
    """時刻 t での町の中心の作業速度の倍率(ペルシアは時代ごとに速くなる)。時代はボタンを押した時刻 + 研究時間で見る"""
    rates = TC_SPEED.get(P["civ"])
    if not rates:
        return 1
    ages = clicked_reach(P)
    i = sum(1 for age in ("feudal", "castle", "imperial") if age in ages and ages[age] <= t)
    return rates[i]


def age_research_sec(P, age):
    return AGE_RESEARCH_SEC[age] / AGE_SPEED.get(P["civ"], {}).get(age, 1)


def clicked_reach(P):
    """ボタンを押した時刻 + 研究時間(待ち行列で研究の開始が遅れた分は入らない)"""
    return {age: t + round(age_research_sec(P, age)) for age, t in P["ages"].items()}


def last_burst(times):
    """同じ研究のボタンを押した時刻の並びから、最後の操作の時刻を返す。

    研究は一度予約するとボタンが消えるので、時間をあけて押し直しているなら、前の予約は取り消したか資源不足で弾かれている。
    そこで最後の操作(CLICK_BURST 秒より短い間隔の連打は1回とみなし、その最初)の時刻を使う(2026/10 再レビューで指摘。
    以前は最初に押した時刻で、取り消して押し直した研究が早く出ていた)。
    """
    times = sorted(times)
    start = times[0]
    for prev, cur in zip(times, times[1:]):
        if cur - prev >= CLICK_BURST:
            start = cur
    return start


def finish_clicks(P, with_events=True):
    """研究・時代進化を押した時刻を決め、鍛冶場の研究の記録を作る"""
    P["ages"] = {age: last_burst(ts) for age, ts in P["age_clicks"].items()}
    P["techs"] = {tech: last_burst(ts) for tech, ts in P["tech_clicks"].items() if tech in TECHS}
    if with_events:
        P["events"] += [(last_burst(ts), "bs", tech, 1) for tech, ts in P["tech_clicks"].items() if tech in BLACKSMITH_TECHS]


def item_duration(P, item, start):
    """待ち行列の1件にかかる秒数(ゲーム内時間)"""
    kind, name = item["kind"], item["name"]
    if kind == "research":
        if name in AGES:
            return age_research_sec(P, AGES[name])
        if name in TC_TECH_SEC:
            speed = TC_TECH_SPEED.get(P["civ"], {}).get(name, 1)
            return 0 if speed is None else TC_TECH_SEC[name] / speed / tc_speed(P, start)
        return 0  # そのほかの研究は時間が分からないので、待ち行列を止めない扱い(甘めに見る)
    if name == "Villager":
        return VILLAGER_SEC / tc_speed(P, start)
    return UNIT_TRAIN_SEC.get(name, UNIT_TRAIN_DEFAULT) * MIL_SPEED_FACTOR


def simulate_production(P, end):
    """建物ごとの待ち行列を再現する。町の中心の空き時間・使っていた町の中心・作り始めたユニット・時代に到達した時刻を返す。

    録画に残るのは「予約した時刻」だけなので、まとめて予約すると5分ごとの数がずれ、時代進化・手押し車などの研究中に
    作れない時間も分からない(2026/10 ユーザー指摘)。そこで建物ごとに「予約 → 先頭から順に作る(町の人25秒、研究・軍は
    それぞれの時間) → 取り消しは待ち行列から外す」を並べる。町の中心の待ち行列が空だった区間を「空いていた時間」とする。

    資源が足りずに弾かれた予約も録画に残る(連打すると多い)。食料などの量は録画に入っていないので、次の手がかりで弾く(2026/10 レビューで指摘):
    ・町の人: 開始直後(START_WINDOW 秒まで)は食料が入ってこないので、開始時の食料(START_FOOD)で払える数まで
    ・町の人: その時代でしかできない行動(AGE_GATED_*)が出た時刻には時代進化が終わっている。時代進化の研究がそれに間に合わない
      待ち行列なら、前に並ぶ町の人の予約のうち新しいものから弾かれていたとみなす(fit_age_deadline)
    ・研究: 一度予約するとボタンが消えるので、時間をあけて同じ研究を押し直したら、前の予約は弾かれたか取り消したとみなす
    ・軍など: 試合が終わる(投了する)までに作り始められない予約は作られていない。生産時間は甘め(MIL_SPEED_FACTOR)に見るので、
      除くのは「作れなかったと言い切れる分」だけで、建物が空いている間に資源不足で弾かれた分は残る
    人口の上限(家が足りない)で止まっていた時間は分からないので、生産していた扱い。そのため空き時間は少なめ(下限)、作った数は多め(上限)に出る。
    町の中心は、町の人か町の中心の研究を最初に予約した時刻から数える(建て終わった時刻は録画に残らない)。
    最初の町の中心は0秒から。最後に使った後(壊された・人口が埋まったなど)は空き時間に数えない。

    戻り値: {"f": [各町の中心を最初に使った時刻], "i": [[空き始め, 空き終わり], ...](TC_IDLE_END 秒まで),
             "units": [(作り始めた時刻, ユニット)], "reach": {時代: 到達した時刻}, "rejected": 弾いた予約の数(町の人・軍), "unstarted": 作り始められなかった軍などの数}
    """
    cmds = sorted(P.get("prod_cmds") or [], key=lambda c: c[0])
    if not cmds:
        return None
    # 町の中心かどうか(町の人か町の中心の研究を予約した建物)
    tc_ids = set()
    for t, kind, target, arg in cmds:
        if kind == "unit" and arg[0] == "Villager":
            tc_ids.update(target)
        elif kind == "research" and target is not None and (arg in AGES or arg in TC_TECH_SEC):
            tc_ids.add(target)
    # 時代進化の研究を押した時刻(取り消しの解釈で、押し直しがあるかを見るため)
    age_click_times = {}
    for t, kind, target, arg in cmds:
        if kind == "research" and arg in AGES:
            age_click_times.setdefault(arg, []).append(t)
    evidence = P.get("age_evidence") or {}
    blds = {}
    items = []
    no_bld_units = []
    start_food = START_FOOD.get(P["civ"], START_FOOD_DEFAULT)
    start_villagers = 0

    def bld(oid, t):
        if oid not in blds:
            is_tc = oid in tc_ids
            first = 0 if is_tc and not any(b["tc"] for b in blds.values()) and t < 120 else t
            blds[oid] = dict(tc=is_tc, first=first, queue=[], busy_until=first, current=None, idle=[], last=t)
        return blds[oid]

    def advance(b, t):
        """t まで生産を進める(待ち行列の先頭から、前のものが終わり次第始める)。終わると queue は空か busy_until > t"""
        while b["queue"] and b["busy_until"] <= t:
            item = b["queue"].pop(0)
            item["start"] = b["busy_until"]
            item["end"] = item["start"] + item_duration(P, item, item["start"])
            item["state"] = "done"
            b["busy_until"] = item["end"]
            b["current"] = item

    def producing(b, t):
        return b["current"] if b["current"] is not None and b["busy_until"] > t else None

    def push(b, t, item):
        advance(b, t)
        if not b["queue"] and b["busy_until"] < t:
            if t - b["busy_until"] >= 1:
                b["idle"].append([b["busy_until"], t])
            b["busy_until"] = t
        b["queue"].append(item)
        item["bld"] = b
        b["last"] = t
        advance(b, t)

    def cancel(b, t, item, state):
        """待ち行列か作っている最中の1件を外す"""
        if item is producing(b, t):
            b["current"] = None
            b["busy_until"] = t
            advance(b, t)
        elif item in b["queue"]:
            b["queue"].remove(item)
        item["state"] = state

    def valid_evidence(age, click):
        """ボタンを押した時刻 + 研究時間 より後の、その時代でしかできない最初の行動の時刻(なければ None)。
        それより前の手がかりは、実際にはできなかった命令か文明ボーナスなので使わない(2026/10 再レビューで指摘)"""
        earliest = click + age_research_sec(P, age) - 1
        later = [e for e in evidence.get(age, []) if e >= earliest]
        return min(later) if later else None

    def fit_age_deadline(b, t, item):
        """時代進化の研究が、その時代でしかできない行動に間に合うよう、前に並ぶ町の人の予約を新しいものから弾く"""
        age = AGES[item["name"]]
        ev = valid_evidence(age, t)
        if ev is None:
            return
        deadline = ev - age_research_sec(P, age)
        advance(b, t)
        while True:
            ahead = (b["busy_until"] if producing(b, t) else t) + sum(item_duration(P, q, t) for q in b["queue"])
            pending = [q for q in b["queue"] if q["kind"] == "unit" and q["name"] == "Villager"]
            if ahead <= deadline + 1 or not pending:
                break
            cancel(b, t, pending[-1], "rejected")

    queued = {}  # 研究名 -> 予約中の項目(待ち行列か作っている最中)
    for t, kind, target, arg in cmds:
        if t > end:
            break
        if kind == "unit":
            name, amount = arg
            ids = [i for i in target if i is not None]
            for _ in range(amount):
                item = dict(kind="unit", name=name, t=t, start=None, end=None, state="queued")
                items.append(item)
                if name == "Villager" and t < START_WINDOW:
                    # 開始直後は、開始時の食料で払える分まで
                    if (start_villagers + 1) * 50 > start_food:
                        item["state"] = "rejected"
                        continue
                    start_villagers += 1
                if not ids:
                    no_bld_units.append(item)
                    continue
                # 複数の建物を選んで予約すると、待ち行列の短いところから振り分けられる
                for i in ids:
                    advance(bld(i, t), t)
                b = min((bld(i, t) for i in ids), key=lambda b: (len(b["queue"]), b["busy_until"]))
                # 1つの建物の待ち行列は QUEUE_MAX 件まで(作っている最中の1件を含む)。それを超えた予約は入らない
                if len(b["queue"]) + (1 if producing(b, t) else 0) >= QUEUE_MAX:
                    item["state"] = "rejected"
                    continue
                push(b, t, item)
        elif kind == "research":
            if target is None:
                continue
            # 研究は一度予約するとボタンが消えるので、同じ研究を時間をあけて押し直していれば、前の予約は通っていない。
            # 前の予約も一度は待ち行列に入れておき(その研究を取り消した操作が正しく当たるように)、押し直した時点で外す(2026/10 再レビューで指摘)
            prev = queued.get(arg)
            if prev is not None and prev["state"] in ("queued", "done"):
                if t - prev["t"] < CLICK_BURST:
                    continue  # 連打(1回の操作)
                if prev["state"] == "done" and prev["end"] <= t:
                    # 研究し終わった後に押し直している = 研究していた建物が壊れたなどで、研究が失われていた
                    prev["state"] = "lost"
                else:
                    # 待ち行列か研究中に押し直している = 前の予約は弾かれたか取り消した
                    cancel(prev["bld"], t, prev, "rejected")
            item = dict(kind="research", name=arg, t=t, start=None, end=None, state="queued")
            items.append(item)
            queued[arg] = item
            b = bld(target, t)
            if arg in AGES and b["tc"]:
                fit_age_deadline(b, t, item)
            # 待ち行列が満杯(QUEUE_MAX)なら研究は入らないはずなので、再現の待ち行列が満杯なのは、前に並ぶ予約の一部が
            # 資源不足で弾かれていたため。前に並ぶユニットの予約を新しいものから弾いて空きを作る(2026/10 4回目のレビューで指摘)
            advance(b, t)
            while len(b["queue"]) + (1 if producing(b, t) else 0) >= QUEUE_MAX:
                pending = [q for q in b["queue"] if q["kind"] == "unit"]
                if not pending:
                    break
                cancel(b, t, pending[-1], "rejected")
            push(b, t, item)
        elif kind == "unqueue":
            # object_ids は建物ID×256(2024/2 以前の録画は建物IDそのもの)
            oid = target >> 8 if (target >> 8) in blds else target if target in blds else None
            if oid is None:
                continue
            b = blds[oid]
            advance(b, t)
            cur = producing(b, t)
            # 画面の待ち行列: 作っている最中の1つが枠0、続いて同じものが続く分を1つの枠にまとめる。
            # 枠をクリックすると、その枠の最後の1つが外れる(2026/10 再レビューで、作っている最中を独立した枠にすると合う例が増えた)
            groups = [[cur]] if cur else []
            for item in b["queue"]:
                if groups and (not cur or len(groups) > 1) and (groups[-1][-1]["kind"], groups[-1][-1]["name"]) == (item["kind"], item["name"]):
                    groups[-1].append(item)
                else:
                    groups.append([item])
            if not groups:
                continue
            target_item = groups[min(arg, len(groups) - 1)][-1]
            # 時代進化の研究を外す解釈になっても、後にその時代の手がかりがあり押し直しもなければ、実際は町の人を外している
            if target_item["kind"] == "research" and target_item["name"] in AGES:
                re_click = any(c > t for c in age_click_times.get(target_item["name"], []))
                if not re_click and valid_evidence(AGES[target_item["name"]], target_item["t"]) is not None:
                    vills = [q for q in b["queue"] if q["kind"] == "unit"] or ([cur] if cur and cur["kind"] == "unit" else [])
                    if not vills:
                        continue
                    target_item = vills[-1]
            cancel(b, t, target_item, "cancelled")
    # 試合の終わりまで進める(最後の命令の後に始まる研究・生産も入れる。2026/10 再レビューで指摘)
    for b in blds.values():
        advance(b, end)
    out_idle = []
    for b in blds.values():
        if not b["tc"]:
            continue
        stop = min(end, b["last"], TC_IDLE_END)
        out_idle += [[round(a), round(min(c, stop))] for a, c in b["idle"] if a < stop and min(c, stop) - a >= 1]
    reach = {}
    for item in items:
        if item["kind"] == "research" and item["name"] in AGES and item["state"] == "done" and item["end"] <= end:
            age = AGES[item["name"]]
            ev = valid_evidence(age, item["t"])
            # その時代でしかできない行動の時刻には到達している(再現がずれた場合の上限)
            reach[age] = round(min(item["end"], ev) if ev is not None else item["end"])
    started = [(round(i["start"]), i["name"]) for i in items if i["kind"] == "unit" and i["state"] == "done" and i["start"] <= end]
    started += [(i["t"], i["name"]) for i in no_bld_units]
    return {
        "f": sorted(round(b["first"]) for b in blds.values() if b["tc"]),
        "i": sorted(out_idle),
        "units": sorted(started),
        "reach": reach,
        "rejected": sum(1 for i in items if i["state"] == "rejected" and i["kind"] == "unit"),
        "unstarted": sum(1 for i in items if i["kind"] == "unit" and i["name"] != "Villager" and i["state"] == "queued"),
    }


def apply_production(P, end):
    """建物ごとの待ち行列の再現の結果を記録に反映する。

    ユニットの数(units)と途中経過・時代ごとの軍(events)を、弾いた・取り消した・作り始められなかった予約を除き、作り始めた時刻で数え直す。
    時代に到達した時刻は、研究が実際に始まった時刻 + 研究時間にする(reached_ages)
    """
    res = simulate_production(P, end)
    if not res:
        return
    if res["f"]:
        P["tc_result"] = {"f": res["f"], "i": res["i"]}
    P["reach"] = res["reach"]
    P["prod_rejected"] = res["rejected"]
    P["prod_unstarted"] = res["unstarted"]
    P["events"] = [e for e in P["events"] if e[1] != "unit"] + [(t, "unit", name, 1) for t, name in res["units"]]
    P["units"] = Counter(name for _, name in res["units"])


def to_record(path, info, ps, winner_source):
    name = os.path.basename(path)
    mt = re.search(r"@(\d{4})\.(\d{2})\.(\d{2}) (\d{2})(\d{2})(\d{2})", name)
    start = datetime(*map(int, mt.groups())) if mt else datetime.fromtimestamp(os.path.getmtime(path))
    teams = sorted({P["team"] for P in ps.values()}, key=lambda x: (x is None, x))
    if len(teams) != 2 or None in teams:
        return None, "2チームに分かれていない"
    humans = [P for P in ps.values() if P["raw_name"]]
    if len(humans) < 2:
        return None, "人間が2人未満"
    duration = info["duration"]
    if duration < MIN_DURATION:
        return None, f"{duration // 60}分で終了(やり直し)"
    rec_id = "rec-" + start.strftime("%Y%m%d-%H%M")
    # 録画から勝敗が分からなかった試合で、ユーザーに確認したもの
    if rec_id in WINNER_OVERRIDES:
        won = next(P["team"] for P in ps.values() if P["raw_name"] == WINNER_OVERRIDES[rec_id])
        for P in ps.values():
            P["winner"] = P["team"] == won
        winner_source = "user"
    # Team A = 一番番号の小さいプレイヤーのチーム
    first = min(ps, key=lambda n: n)
    team_a = ps[first]["team"]
    winners = {P["team"] for P in ps.values() if P["winner"]}
    winner = None
    if len(winners) == 1:
        winner = "A" if winners == {team_a} else "B"
    # AIには勝ったチームから順に AI1〜 を振る(ツールのこれまでの記録と同じ)
    ais = sorted((n for n, P in ps.items() if not P["raw_name"]), key=lambda n: (not ps[n]["winner"], n))
    for i, n in enumerate(ais):
        ps[n]["name"] = f"AI{i + 1}"
    for P in ps.values():
        if P["raw_name"]:
            P["name"] = NAME_MAP.get(P["raw_name"], P["raw_name"])
    assign_roles(ps, info.get("size"))
    # 建物ごとの待ち行列の再現(人間だけ。AI は操作がほとんど録画に残らない)。ユニットの数と時代に到達した時刻を直すので、ほかの集計より先に行う
    for P in ps.values():
        if P["raw_name"] and P.get("prod_cmds"):
            apply_production(P, min(duration, P["resign"]) if P["resign"] is not None else duration)
    ordered = [ps[n] for n in sorted(ps)]
    rec = dict(
        id=rec_id,
        start=start.strftime("%Y-%m-%dT%H:%M"),
        edition=info["edition"],
        map=info["map"],
        duration=duration,
        winner=winner,
        winnerSource=winner_source if winner else None,
        speed=info.get("speed"),
        teamA=[P["name"] for P in ordered if P["team"] == team_a],
        teamB=[P["name"] for P in ordered if P["team"] != team_a],
        players={
            P["name"]: dict(
                civ=P["civ"],
                eapm=real_eapm(P, min(duration, P["resign"]) if P["resign"] is not None else duration, info.get("speed", 1)),
                **action_fields(P, min(duration, P["resign"]) if P["resign"] is not None else duration, info.get("speed", 1)),
                ages=reached_ages(P, min(duration, P["resign"]) if P["resign"] is not None else duration),
                resign=P["resign"],
                units=dict(P["units"].most_common()),
                buildings=dict(P["buildings"].most_common()),
                techs=P["techs"],
                role=P["role"],
                at=checkpoints(P, min(duration, P["resign"]) if P["resign"] is not None else duration),
                pm=per_minute(P, min(duration, P["resign"]) if P["resign"] is not None else duration),
                **({"tg": tg} if P["raw_name"] and (tg := target_fields(P, ps, info.get("size"), min(duration, P["resign"]) if P["resign"] is not None else duration)) else {}),
            )
            for P in ordered
        },
    )
    return rec, None


def civ_unavailable():
    """文明ごとに「作れない・研究できないもの」を、ゲームの技術ツリー(CivTechTrees/*.json)から作る。

    診断や相談文で、その文明にないもの(日本のユサール、ケルトの重石弓射手など)を勧めないために使う(2026/10 ユーザー指摘)。
    CIV_TREE_MIN_CIVS 以上の文明が持っている共通のもの(槍兵・ユサールなど)は「作れないもの」を、
    それより少ない文明にしかないもの(固有ユニット・らくだ以外の地域ユニットなど)は「作れるもの」を書く(小さくするため)。
    技術ツリーには「作れない」と書かれず、項目ごと載っていないこともある(日本のらくだ騎兵など)ので、載っていないものも作れない扱い。
    ゲームが見つからないときは None(index.html の今の内容を残す)。
    """
    files = glob.glob(os.path.join(CIV_TREE_DIR, "*.json"))
    if not files:
        return None
    have = {}
    string_ids = {}
    for path in files:
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
        stem = os.path.splitext(os.path.basename(path))[0]
        civ = CIV_TREE_NAMES.get(stem, stem.title())
        nodes = [n for k in ("civ_techs_buildings", "civ_techs_units") for n in j.get(k, [])]
        have[civ] = {n["Name"] for n in nodes if n.get("Node Status") != "NotAvailable"}
        for n in nodes:
            string_ids.setdefault(n["Name"], n.get("Name String ID"))
    count = Counter(name for names in have.values() for name in names)
    common = {name for name, c in count.items() if c >= CIV_TREE_MIN_CIVS}
    unavailable = {civ: sorted(common - names) for civ, names in sorted(have.items())}
    # 作れないものの日本語名(ゲーム内の表記)。相談文に入れる
    ja_strings = {}
    if os.path.exists(JA_STRINGS):
        with open(JA_STRINGS, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'(\d+)\s+"(.*)"', line)
                if m:
                    ja_strings[m.group(1)] = m.group(2).replace("\\n", "")  # ツリー表示用の改行を除く
    names = {x for v in unavailable.values() for x in v}
    ja = {x: ja_strings[str(string_ids[x])] for x in sorted(names) if str(string_ids.get(x)) in ja_strings}
    # u: 共通のもののうち作れないもの / r: 一部の文明にしかないもの(固有ユニットなど)のうち作れるもの / ja: 日本語名
    return {"u": unavailable, "r": {civ: sorted(names - common) for civ, names in sorted(have.items())}, "ja": ja}


def replace_block(html, block_id, data):
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    body = body.replace("</", "<\\/")
    new_block = f'<script type="application/json" id="{block_id}">{body}</script>'
    pattern = re.compile(rf'<script type="application/json" id="{block_id}">.*?</script>', re.S)
    if not pattern.search(html):
        raise RuntimeError(f'index.html に <script type="application/json" id="{block_id}"> がありません')
    return pattern.sub(lambda _: new_block, html, count=1)


def write_index(records):
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    html = replace_block(html, "replay-data", records)
    unavailable = civ_unavailable()
    if unavailable:
        html = replace_block(html, "civ-unavailable", unavailable)
    else:
        print(f"  ※ゲームの技術ツリーが見つからないので、文明ごとの作れないものは更新しません: {CIV_TREE_DIR}")
    with open(INDEX, "w", encoding="utf-8", newline="") as f:
        f.write(html)


def main():
    folders = sys.argv[1:] or DEFAULT_DIRS
    files = default_files(folders)
    if not files:
        print(f"録画が見つかりません: {', '.join(folders)}")
        return 1
    records = []
    for path in files:
        name = os.path.basename(path)
        try:
            info, ps, ws = parse_file(path)
        except Exception as e:  # noqa: BLE001
            print(f"  読めない  {name}: {type(e).__name__}: {str(e)[:100]}")
            continue
        rec, why = to_record(path, info, ps, ws)
        if rec is None:
            print(f"  対象外    {name}: {why}")
            continue
        unknown = [n for n in rec["teamA"] + rec["teamB"] if not n.startswith("AI") and n not in KNOWN_NAMES]
        w = {"A": "Aの勝ち", "B": "Bの勝ち", None: "勝敗不明"}[rec["winner"]]
        print(f"  取り込み  {rec['start']} {rec['edition']} {rec['map']} {rec['duration'] // 60}分 {w}"
              f" A:{'/'.join(rec['teamA'])} B:{'/'.join(rec['teamB'])}"
              + (f"  ※名簿にない名前: {', '.join(unknown)}" if unknown else ""))
        records.append(rec)
    records.sort(key=lambda r: r["start"])
    write_index(records)
    print(f"{len(records)}試合を index.html に書き込みました")
    return 0


def known_names():
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    mt = re.search(r"const SEED_PLAYERS = JSON\.parse\(`(.*?)`\)", html, re.S)
    return set(json.loads(mt.group(1))) if mt else set()


KNOWN_NAMES = known_names()

if __name__ == "__main__":
    sys.exit(main())
