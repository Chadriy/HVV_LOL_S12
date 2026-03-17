import json
import requests
import hashlib
from datetime import datetime
import re

tier_order = {
    "黑铁": 1,
    "青铜": 2,
    "白银": 3,
    "黄金": 4,
    "铂金": 5,
    "翡翠": 6,
    "钻石": 7,
    "大师": 8,
    "宗师": 9,
    "王者": 10
}
roman_order = {
    "Ⅰ": 4,
    "Ⅱ": 3,
    "Ⅲ": 2,
    "Ⅳ": 1
}


def tier_score(tier):
    if not tier or tier == "-":
        return 0

    for k in tier_order:
        if k in tier:
            base = tier_order[k] * 10
            for r in roman_order:
                if r in tier:
                    base += roman_order[r]
            return base
    return 0


def parse_season(text):
    m = re.search(r"S(\d+).*?第([一二三])赛段", text)
    if not m:
        return None
    season = int(m.group(1))
    seg_map = {"一": 1, "二": 2, "三": 3}
    segment = seg_map[m.group(2)]
    return season, segment


def recent6_highest_rank(api_json):
    lst = api_json["battleInfo"]["mapOneInfoList"]

    solo_list = []

    for i in lst:
        t = i.get("type", "")
        if "单双排" not in t:
            continue

        season_info = parse_season(t)
        if not season_info:
            continue

        solo_list.append({
            "season": season_info[0],
            "segment": season_info[1],
            "tier": i.get("tier"),
            "rate": i.get("rate"),
            "point": i.get("winPoint", 0)
        })

    solo_list.sort(key=lambda x: (x["season"], x["segment"]), reverse=True)
    recent4 = solo_list
    best = None
    best_score = -1
    for i in recent4:
        score = tier_score(i["tier"])
        if score > best_score:
            best_score = score
            best = i
    if not best:
        return None

    return f'{best["tier"]} {best["point"]}点(胜率{best["rate"]}%)'


def build_sign():
    now = datetime.now()

    # signStr：不补零
    m = str(now.month)
    d = str(now.day)
    h = str(now.hour)
    mi = str(now.minute)
    s = str(now.second)

    signStr = (
            m + d + h + mi + s +
            str(len(m) * 3) +
            str(len(d) * 3) +
            str(len(h) * 3) +
            str(len(mi) * 3) +
            str(len(s) * 3)
    )

    # lzyumiSign：补零
    raw = f"dld{now.month:02d}o{now.day:02d}u{now.hour:02d}d{now.minute:02d}o{now.second:02d}dld"
    lzyumiSign = hashlib.md5(raw.encode()).hexdigest()

    return signStr, lzyumiSign


def query_player(game_id):
    name, tag = game_id.split("#")
    nickname = f"{name}*~*~*{tag}"

    signStr, lzyumiSign = build_sign()

    url = "https://a.lzyumi.top/lzyumi/lol/info"
    params = {
        "nickname": nickname,
        "allCount": 10,
        "areaId": 1,
        "areaName": "艾欧尼亚",
        "seleMe": 1,
        "filter": 1,
        "openId": "",
        "lzyumiSign": lzyumiSign,
        "signStr": signStr,
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://a.lzyumi.top/",
    }

    session = requests.Session()
    session.trust_env = False

    req = requests.Request("GET", url, params=params, headers=headers)
    prepared = session.prepare_request(req)

    # 这里就是打印可直接访问的完整 URL
    # print("\n可直接访问的 URL：\n")
    # print(prepared.url)
    # print()

    resp = session.send(prepared, timeout=10)
    return resp.json()


if __name__ == "__main__":
    data = query_player("鹿饮溪#0520")
    print(recent6_highest_rank(data))
