"""AoE2 DE の録画ファイル(.aoe2record)を解析して、index.html の録画データ(#replay-data)を書き換える。

使い方:
    python -m pip install mgz==1.8.51
    python tools/import_replays.py              # 既定の録画フォルダを読む
    python tools/import_replays.py <フォルダ>    # 録画フォルダを指定する

録画に入っているのは各プレイヤーの「操作」だけで、ゲーム終了時のスコアは入っていない。
ここで取り出すのは、文明・チーム・勝敗・時代進化の時刻・生産したユニット・建てた建物・研究・eAPM。
"""
import glob
import inspect
import json
import logging
import os
import re
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
from mgz.model import parse_match  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
DEFAULT_DIR = os.path.expanduser(r"~\Games\Age of Empires 2 DE")

# 録画上の名前 → ツールの名前
NAME_MAP = {"Ke_ppy": "ke_ppy", "dimax": "max"}
MIN_DURATION = 600  # 10分未満はやり直し(リスタート)とみなして取り込まない
AGES = {"Feudal Age": "feudal", "Castle Age": "castle", "Imperial Age": "imperial"}
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


def default_files(folder):
    return sorted(glob.glob(os.path.join(folder, "**", "*.aoe2record"), recursive=True), key=os.path.getmtime)


def parse_file(path):
    with open(path, "rb") as f:
        m = parse_match(f)
    ps = {}
    for p in m.players:
        ps[p.number] = dict(
            raw_name=p.name, civ=p.civilization, team=None, winner=bool(p.winner), eapm=p.eapm,
            ages={}, units=Counter(), buildings=Counter(), techs={}, resign=None,
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
        elif kind in ("DE_QUEUE", "QUEUE") and pay.get("unit"):
            # 同じ命令が二重に記録されていることがあるので除く
            key = (pl.number, pay.get("sequence"), tuple(pay.get("object_ids") or []), pay.get("unit"))
            if key in seen:
                continue
            seen.add(key)
            # 複数の建物を選んで生産しても、DEでは1回の命令で amount 体を空いている建物に振り分ける
            P["units"][pay["unit"]] += pay.get("amount") or 1
        elif kind == "BUILD" and pay.get("building"):
            P["buildings"][pay["building"]] += 1
        elif kind == "RESIGN":
            P["resign"] = t
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
    return m, ps, winner_source


def to_record(path, m, ps, winner_source):
    name = os.path.basename(path)
    mt = re.search(r"@(\d{4})\.(\d{2})\.(\d{2}) (\d{2})(\d{2})(\d{2})", name)
    start = datetime(*map(int, mt.groups())) if mt else datetime.fromtimestamp(os.path.getmtime(path))
    teams = sorted({P["team"] for P in ps.values()}, key=lambda x: (x is None, x))
    if len(teams) != 2 or None in teams:
        return None, "2チームに分かれていない"
    humans = [P for P in ps.values() if P["raw_name"]]
    if len(humans) < 2:
        return None, "人間が2人未満"
    duration = round(m.duration.total_seconds())
    if duration < MIN_DURATION:
        return None, f"{duration // 60}分で終了(やり直し)"
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
    ordered = [ps[n] for n in sorted(ps)]
    rec = dict(
        id="rec-" + start.strftime("%Y%m%d-%H%M"),
        start=start.strftime("%Y-%m-%dT%H:%M"),
        map=m.map.name,
        duration=duration,
        winner=winner,
        winnerSource=winner_source if winner else None,
        teamA=[P["name"] for P in ordered if P["team"] == team_a],
        teamB=[P["name"] for P in ordered if P["team"] != team_a],
        players={
            P["name"]: dict(
                civ=P["civ"],
                eapm=P["eapm"],
                ages=P["ages"],
                resign=P["resign"],
                units=dict(P["units"].most_common()),
                buildings=dict(P["buildings"].most_common()),
                techs=P["techs"],
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
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    files = default_files(folder)
    if not files:
        print(f"録画が見つかりません: {folder}")
        return 1
    records = []
    for path in files:
        name = os.path.basename(path)
        try:
            m, ps, ws = parse_file(path)
        except Exception as e:  # noqa: BLE001
            print(f"  読めない  {name}: {type(e).__name__}: {str(e)[:100]}")
            continue
        rec, why = to_record(path, m, ps, ws)
        if rec is None:
            print(f"  対象外    {name}: {why}")
            continue
        unknown = [n for n in rec["teamA"] + rec["teamB"] if not n.startswith("AI") and n not in KNOWN_NAMES]
        w = {"A": "Aの勝ち", "B": "Bの勝ち", None: "勝敗不明"}[rec["winner"]]
        print(f"  取り込み  {rec['start']} {rec['map']} {rec['duration'] // 60}分 {w}"
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
