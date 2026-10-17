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
            raw_name=p.name, civ=p.civilization, team=None, winner=bool(p.winner), eapm=p.eapm,
            ages={}, units=Counter(), buildings=Counter(), techs={}, resign=None,
            pos=(p.position.x, p.position.y) if p.position else None, events=[],
        )
    for i, team in enumerate(m.teams):
        for p in team:
            ps[p.number]["team"] = i
    seen = set()
    for a in m.actions:
        pl = getattr(a, "player", None)
        if pl is None or pl.number not in ps:
            continue
        P = ps[pl.number]
        t = round(a.timestamp.total_seconds())
        pay = a.payload or {}
        kind = a.type.name
        if kind == "RESEARCH":
            tech = pay.get("technology")
            if tech in AGES:
                P["ages"].setdefault(AGES[tech], t)
            elif tech in TECHS:
                P["techs"].setdefault(tech, t)
            if tech in BLACKSMITH_TECHS:
                P["events"].append((t, "bs", tech, 1))
        elif kind in ("DE_QUEUE", "QUEUE") and pay.get("unit"):
            # 同じ命令が二重に記録されていることがあるので除く
            key = (pl.number, pay.get("sequence"), tuple(pay.get("object_ids") or []), pay.get("unit"))
            if key in seen:
                continue
            seen.add(key)
            # 複数の建物を選んで生産しても、DEでは1回の命令で amount 体を空いている建物に振り分ける
            P["units"][pay["unit"]] += pay.get("amount") or 1
            P["events"].append((t, "unit", pay["unit"], pay.get("amount") or 1))
        elif kind == "BUILD" and pay.get("building"):
            P["buildings"][pay["building"]] += 1
            P["events"].append((t, "build", pay["building"], 1))
        elif kind == "RESIGN":
            P["resign"] = t
    info = dict(edition="DE", duration=round(m.duration.total_seconds()), map=m.map.name, size=m.map.dimension)
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
    _, dataset = get_dataset(Version.HD, None)
    ps = {}
    for p in players:
        civ = dataset["civilizations"].get(str(p["civilization"]), {}).get("name")
        ps[p["number"]] = dict(
            raw_name=p["name"], civ=HD_CIV_NAMES.get(civ, civ), team=p["team_id"], winner=bool(p["winner"]),
            eapm=p.get("eapm"), ages={}, units=Counter(), buildings=Counter(), techs={}, resign=None,
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
            if kind == Action.RESEARCH:
                tech = dataset["technologies"].get(str(pay.get("technology_id")))
                if tech in AGES:
                    P["ages"].setdefault(AGES[tech], t)
                elif tech in TECHS:
                    P["techs"].setdefault(tech, t)
            elif kind == Action.RESIGN:
                P["resign"] = t
    # カスタムマップは日本語環境だと「スタイル: カスタム」のような名前になる
    name = game_map.get("name") or ""
    info = dict(edition="HD", duration=duration, map="Custom" if game_map.get("custom") or "カスタム" in name else name,
                size=game_map.get("dimension"))
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
CHECKPOINTS = [10, 15, 20, 25, 30, 35, 40]


def checkpoints(P, end):
    """各時点までの累計: v=町の人, m=軍, tc=町の中心の建設, tr=荷馬車, sg=攻囲兵器, cs=城の建設, bs=鍛冶場の研究。

    生産はキューに入れた数、建物は建設を指示した回数(合計と同じく目安)。試合が終わった後の時点は入れない。
    HD の録画は生産命令の持ち主が分からないので空。
    """
    out = {}
    if not P.get("events"):
        return out
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
                if name == "Town Center":
                    c["tc"] += n
                elif name == "Castle":
                    c["cs"] += n
        # 鍛冶場の研究は、押し直しで二重に数えないよう種類の数にする
        c["bs"] = len({name for t, kind, name, _ in P["events"] if kind == "bs" and t <= lim})
        out[str(cp)] = c
    return out


def reached_ages(P, end):
    """研究を始めた時刻 + 研究時間 = 到達した時刻。到達する前に試合が終わった(投了した)時代は除く。

    途中で研究を取り消して押し直した場合は最初に押した時刻から数えるので、実際より早めに出ることがある。
    """
    out = {}
    for age, t in P["ages"].items():
        at = t + round(AGE_RESEARCH_SEC[age] / AGE_SPEED.get(P["civ"], {}).get(age, 1))
        if at <= end:
            out[age] = at
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
    ordered = [ps[n] for n in sorted(ps)]
    rec = dict(
        id=rec_id,
        start=start.strftime("%Y-%m-%dT%H:%M"),
        edition=info["edition"],
        map=info["map"],
        duration=duration,
        winner=winner,
        winnerSource=winner_source if winner else None,
        teamA=[P["name"] for P in ordered if P["team"] == team_a],
        teamB=[P["name"] for P in ordered if P["team"] != team_a],
        players={
            P["name"]: dict(
                civ=P["civ"],
                eapm=P["eapm"],
                ages=reached_ages(P, min(duration, P["resign"]) if P["resign"] is not None else duration),
                resign=P["resign"],
                units=dict(P["units"].most_common()),
                buildings=dict(P["buildings"].most_common()),
                techs=P["techs"],
                role=P["role"],
                at=checkpoints(P, min(duration, P["resign"]) if P["resign"] is not None else duration),
            )
            for P in ordered
        },
    )
    return rec, None


def write_index(records):
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    body = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    body = body.replace("</", "<\\/")
    new_block = f'<script type="application/json" id="replay-data">{body}</script>'
    pattern = re.compile(r'<script type="application/json" id="replay-data">.*?</script>', re.S)
    if not pattern.search(html):
        raise RuntimeError('index.html に <script type="application/json" id="replay-data"> がありません')
    html = pattern.sub(lambda _: new_block, html, count=1)
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
