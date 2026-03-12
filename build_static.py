# build_static.py
# 依赖：
#   pip install pandas jinja2
#
# 运行：
#   python build_static.py --data ./matches --champion-json ./champion.json --out ./docs
#
# 生成：
#   docs/
#     index.html
#     players.html
#     champions.html
#     teams.html
#     bans.html
#     player/*.html
#     team/*.html
#     champion/*.html
#     match/*.html

import os
import json
import re
import argparse
import posixpath
from html import escape as html_escape
from urllib.parse import quote
from typing import Any, Dict, List, Optional, Tuple
from query_rank import query_player, recent6_highest_rank
import pandas as pd
from jinja2 import Template
from concurrent.futures import ThreadPoolExecutor, as_completed

RANK_CACHE_FILE = "rank_cache.json"

def load_rank_cache():
    if os.path.exists(RANK_CACHE_FILE):
        try:
            with open(RANK_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_rank_cache(cache):
    with open(RANK_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

import time
import random

def fetch_player_rank(game_id, cache, retry=3):

    if not game_id:
        return "-"

    if game_id in cache:
        return cache[game_id]

    for i in range(retry):

        try:

            # 每次请求间隔，防止接口限流
            time.sleep(random.uniform(0.4, 0.8))

            data = query_player(game_id)

            if not data or "battleInfo" not in data:
                raise RuntimeError("API返回异常")

            rank = recent6_highest_rank(data)

            if not rank:
                rank = "-"

            cache[game_id] = rank
            return rank

        except Exception as e:

            if i == retry - 1:
                print(f"段位查询失败 {game_id}: {e}")
                return "-"

            # 重试等待
            time.sleep(1 + random.random())

LANE_MAP = {
    "TOP": "上",
    "JUNGLE": "野",
    "MIDDLE": "中",
    "BOTTOM": "下",
    "UTILITY": "辅",
}
LANES = ["上", "野", "中", "下", "辅"]


def _safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _safe_int(x, default=None):
    try:
        if x is None:
            return default
        return int(float(x))
    except Exception:
        return default


def parse_kda(score_info: Optional[str]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    if not score_info or not isinstance(score_info, str):
        return (None, None, None)
    parts = score_info.split("/")
    if len(parts) != 3:
        return (None, None, None)
    try:
        return (int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip()))
    except Exception:
        return (None, None, None)


def iter_json_files(data_path: str):
    if os.path.isdir(data_path):
        for root, _, files in os.walk(data_path):
            for fn in files:
                if fn.lower().endswith(".json"):
                    yield os.path.join(root, fn)
    else:
        yield data_path


def load_matches(data_path: str) -> List[Dict[str, Any]]:
    matches = []
    for fp in iter_json_files(data_path):
        with open(fp, "r", encoding="utf-8") as f:
            obj = json.load(f)

        if isinstance(obj, list):
            for it in obj:
                if isinstance(it, dict):
                    it["_src_file"] = fp
                    matches.append(it)
        elif isinstance(obj, dict):
            obj["_src_file"] = fp
            matches.append(obj)
        else:
            raise RuntimeError(f"不支持的JSON结构: {fp}")
    return matches


def load_champion_map_from_champion_json(champion_json_path: str) -> Dict[str, str]:
    if not os.path.exists(champion_json_path):
        raise FileNotFoundError(f"找不到 {champion_json_path}")

    with open(champion_json_path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    data = obj.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("champion.json 格式不正确")

    mp = {}
    for champ in data.values():
        if not isinstance(champ, dict):
            continue
        cid = champ.get("key")
        name = champ.get("name")
        if cid is not None and name is not None:
            mp[str(cid)] = str(name)

    if not mp:
        raise RuntimeError("champion.json 未解析到英雄映射")
    return mp


def extract_kp_percent(echarts_map: Dict[str, Any]) -> Optional[float]:
    if not isinstance(echarts_map, dict):
        return None

    v = _safe_float(echarts_map.get("killAssisScore"), None)
    if v is not None:
        return float(v)

    s = echarts_map.get("killAssisInfo")
    if isinstance(s, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
        if m:
            return float(m.group(1))
    return None


def extract_gold(echarts_map: Dict[str, Any], goldEarned_field: Any) -> Optional[int]:
    if isinstance(echarts_map, dict):
        g = _safe_int(echarts_map.get("goldEarned"), None)
        if g is not None:
            return g

    g2 = _safe_float(goldEarned_field, None)
    if g2 is None:
        return None
    if g2 < 1000:
        return int(round(g2 * 1000))
    return int(round(g2))


def get_match_key(m: Dict[str, Any], fallback_idx: int) -> str:
    data = (m or {}).get("data") or {}
    for k in ("battleId", "gameId", "matchId", "id", "battle_id", "match_id"):
        v = data.get(k)
        if v is not None and str(v).strip() != "":
            return str(v)
    return str(fallback_idx)


_FILENAME_RE = re.compile(
    r"^(?P<date>\d{8})_(?P<no>\d+?)_(?P<t100>.+?)_(?P<t200>.+?)\.json$",
    re.IGNORECASE
)


def parse_match_meta_from_filename(src_file: Optional[str]) -> Dict[str, Optional[str]]:
    if not src_file:
        return {"date": None, "no": None, "team100": None, "team200": None}

    base = os.path.basename(src_file)
    m = _FILENAME_RE.match(base)
    if not m:
        return {"date": None, "no": None, "team100": None, "team200": None}

    return {
        "date": m.group("date"),
        "no": m.group("no"),
        "team100": m.group("t100"),
        "team200": m.group("t200"),
    }


def build_match_meta(matches: List[Dict[str, Any]]) -> Dict[str, Dict[str, Optional[str]]]:
    out = {}
    for idx, m in enumerate(matches, start=1):
        match_id = get_match_key(m, idx)
        src = (m or {}).get("_src_file")
        meta = parse_match_meta_from_filename(src)
        meta["src_file"] = src
        out[str(match_id)] = meta
    return out


def team_name_from_meta(match_meta: Dict[str, Dict[str, Optional[str]]], match_id: str, team_id: str) -> str:
    meta = match_meta.get(str(match_id)) or {}
    if str(team_id) == "100":
        return meta.get("team100") or "队伍100"
    if str(team_id) == "200":
        return meta.get("team200") or "队伍200"
    return str(team_id)


def extract_rows(matches: List[Dict[str, Any]], champ_map: Dict[str, str], match_meta: Dict[str, Dict[str, Optional[str]]]) -> pd.DataFrame:
    rows = []

    for idx, m in enumerate(matches, start=1):
        match_id = str(get_match_key(m, idx))
        data = (m or {}).get("data") or {}
        players = data.get("wgBattleDetailInfo") or []

        meta = match_meta.get(match_id) or {}
        match_date = meta.get("date")
        match_no = meta.get("no")

        for p in players:
            pos = p.get("position")
            lane = LANE_MAP.get(pos)
            if lane not in LANES:
                continue

            team_id = str(p.get("teamId") or "")
            player = p.get("nickNameStr") or p.get("nickName") or p.get("openIdNow") or "未知选手"

            champ_id = str(p.get("detailChampionId") or "")
            champ_name = champ_map.get(champ_id, champ_id or "未知英雄")

            win = True if p.get("win") == "Win" else False

            score = _safe_float(p.get("scoreInfoNum"), None)
            if score is None:
                score = _safe_float(((p.get("echartsMap") or {}).get("score")), None)

            k, d, a = parse_kda(p.get("scoreInfo"))
            ech = p.get("echartsMap") or {}
            kp = extract_kp_percent(ech)
            gold = extract_gold(ech, p.get("goldEarned"))
            level = _safe_int(p.get("dengji"), None)
            team_name = team_name_from_meta(match_meta, match_id, team_id)

            rows.append({
                "match_id": match_id,
                "match_ord": idx,
                "match_date": match_date,
                "match_no": match_no,
                "lane": lane,
                "teamId": team_id,
                "team_name": team_name,
                "player": player,
                "champion_id": champ_id,
                "champion_name": champ_name,
                "win": win,
                "score": score,
                "kills": k,
                "deaths": d,
                "assists": a,
                "kpr": kp,
                "gold": gold,
                "level": level,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["kda"] = (df["kills"].fillna(0) + df["assists"].fillna(0)) / df["deaths"].fillna(0).replace(0, 1)

    df["opponent_teamId"] = df["teamId"].map(
        lambda x: "200" if str(x) == "100" else ("100" if str(x) == "200" else None)
    )
    df["opponent_team_name"] = df.apply(
        lambda r: team_name_from_meta(match_meta, r["match_id"], r["opponent_teamId"]) if r["opponent_teamId"] else None,
        axis=1
    )

    opp = df[["match_id", "lane", "teamId", "team_name", "gold", "level", "player", "champion_name"]].rename(
        columns={
            "teamId": "opp_teamId",
            "team_name": "opp_team_name",
            "gold": "opp_gold",
            "level": "opp_level",
            "player": "opp_player",
            "champion_name": "opp_champion",
        }
    )

    df = df.merge(
        opp,
        left_on=["match_id", "lane", "opponent_teamId"],
        right_on=["match_id", "lane", "opp_teamId"],
        how="left",
    )

    df["gold_diff"] = df["gold"] - df["opp_gold"]
    df["level_diff"] = df["level"] - df["opp_level"]

    return df


def extract_bans(matches: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for idx, m in enumerate(matches, start=1):
        match_id = str(get_match_key(m, idx))
        data = (m or {}).get("data") or {}
        team_details = data.get("teamDetails") or []

        for team in team_details:
            team_id = str((team or {}).get("teamId") or "")
            for b in (team or {}).get("banInfoList") or []:
                cid = b.get("championId")
                if cid is None:
                    continue
                cid = str(cid).strip()
                if cid.isdigit() and cid != "0":
                    rows.append({"match_id": match_id, "teamId": team_id, "champion_id": cid})
    return pd.DataFrame(rows)


def make_ban_stats(ban_df: pd.DataFrame, total_matches: int) -> pd.DataFrame:
    if ban_df.empty or total_matches <= 0:
        return pd.DataFrame(columns=["champion_id", "ban_matches", "banrate", "bans"])

    g = ban_df.groupby("champion_id", dropna=False)
    out = g.agg(
        bans=("match_id", "count"),
        ban_matches=("match_id", "nunique"),
    ).reset_index()

    out["banrate"] = (out["ban_matches"] / total_matches * 100).round(2)
    return out.sort_values(["banrate", "ban_matches", "bans"], ascending=[False, False, False])


def agg_players(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    g = df.groupby(["lane", "player", "team_name"], dropna=False)
    out = g.agg(
        games=("win", "count"),
        wins=("win", "sum"),
        winrate=("win", "mean"),
        avg_score=("score", "mean"),
        avg_kda=("kda", "mean"),
        avg_kp=("kpr", "mean"),
        avg_gold_diff=("gold_diff", "mean"),
        avg_level_diff=("level_diff", "mean"),
    ).reset_index()

    out["winrate"] = (out["winrate"] * 100).round(2)
    for c in ["avg_score", "avg_kda", "avg_kp", "avg_gold_diff", "avg_level_diff"]:
        out[c] = out[c].round(2)

    return out.sort_values(["lane", "winrate", "avg_score", "games"], ascending=[True, False, False, False])


def agg_champions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    g = df.groupby(["lane", "champion_id", "champion_name"], dropna=False)
    out = g.agg(
        games=("win", "count"),
        wins=("win", "sum"),
        winrate=("win", "mean"),
        avg_score=("score", "mean"),
        avg_kda=("kda", "mean"),
        avg_kp=("kpr", "mean"),
        avg_gold_diff=("gold_diff", "mean"),
        avg_level_diff=("level_diff", "mean"),
    ).reset_index()

    out["winrate"] = (out["winrate"] * 100).round(2)
    for c in ["avg_score", "avg_kda", "avg_kp", "avg_gold_diff", "avg_level_diff"]:
        out[c] = out[c].round(2)

    return out.sort_values(["lane", "winrate", "avg_score", "games"], ascending=[True, False, False, False])


def build_team_match_stats(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "match_id", "match_ord", "match_date", "match_no",
            "teamId", "team_name", "opponent",
            "win", "avg_score", "team_kills", "team_deaths", "team_assists",
            "team_kda", "avg_kp", "team_gold", "avg_level",
            "team_gold_diff", "team_level_diff", "result"
        ])

    out = (
        df.groupby(["match_id", "match_ord", "match_date", "match_no", "teamId", "team_name"], dropna=False)
        .agg(
            win=("win", "max"),
            opponent=("opponent_team_name", "first"),
            avg_score=("score", "mean"),
            team_kills=("kills", "sum"),
            team_deaths=("deaths", "sum"),
            team_assists=("assists", "sum"),
            avg_kp=("kpr", "mean"),
            team_gold=("gold", "sum"),
            avg_level=("level", "mean"),
            team_gold_diff=("gold_diff", "sum"),
            team_level_diff=("level_diff", "sum"),
        )
        .reset_index()
    )

    out["team_kda"] = (
        (out["team_kills"].fillna(0) + out["team_assists"].fillna(0))
        / out["team_deaths"].fillna(0).replace(0, 1)
    )
    out["result"] = out["win"].map(lambda x: "胜" if bool(x) else "负")

    for c in ["avg_score", "team_kda", "avg_kp", "avg_level", "team_gold_diff", "team_level_diff"]:
        out[c] = out[c].round(2)
    out["team_gold"] = out["team_gold"].round(0)

    return out.sort_values(["match_ord"], ascending=[False])


def agg_teams(team_match_df: pd.DataFrame) -> pd.DataFrame:
    if team_match_df.empty:
        return pd.DataFrame(columns=[
            "team_name", "games", "wins", "winrate",
            "avg_score", "avg_kda", "avg_kp",
            "avg_gold", "avg_level",
            "avg_gold_diff", "avg_level_diff"
        ])

    out = (
        team_match_df.groupby(["team_name"], dropna=False)
        .agg(
            games=("win", "count"),
            wins=("win", "sum"),
            winrate=("win", "mean"),
            avg_score=("avg_score", "mean"),
            avg_kda=("team_kda", "mean"),
            avg_kp=("avg_kp", "mean"),
            avg_gold=("team_gold", "mean"),
            avg_level=("avg_level", "mean"),
            avg_gold_diff=("team_gold_diff", "mean"),
            avg_level_diff=("team_level_diff", "mean"),
        )
        .reset_index()
    )

    out["winrate"] = (out["winrate"] * 100).round(2)
    for c in ["avg_score", "avg_kda", "avg_kp", "avg_gold", "avg_level", "avg_gold_diff", "avg_level_diff"]:
        out[c] = out[c].round(2)

    return out.sort_values(["winrate", "wins", "avg_score", "games"], ascending=[False, False, False, False])


def agg_team_pick_champions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "team_name", "champion_id", "champion_name",
            "games", "wins", "winrate", "avg_score"
        ])

    out = (
        df.groupby(["team_name", "champion_id", "champion_name"], dropna=False)
        .agg(
            games=("match_id", "count"),
            wins=("win", "sum"),
            winrate=("win", "mean"),
            avg_score=("score", "mean"),
        )
        .reset_index()
    )

    out["winrate"] = (out["winrate"] * 100).round(2)
    out["avg_score"] = out["avg_score"].round(2)

    return out.sort_values(
        ["team_name", "games", "winrate", "wins", "avg_score"],
        ascending=[True, False, False, False, False]
    )


def agg_team_ban_champions(ban_df: pd.DataFrame) -> pd.DataFrame:
    if ban_df.empty:
        return pd.DataFrame(columns=[
            "team_name", "champion_id", "champion_name",
            "ban_games", "bans"
        ])

    out = (
        ban_df.groupby(["team_name", "champion_id", "champion_name"], dropna=False)
        .agg(
            bans=("match_id", "count"),
            ban_games=("match_id", "nunique"),
        )
        .reset_index()
    )

    return out.sort_values(
        ["team_name", "ban_games", "bans"],
        ascending=[True, False, False]
    )


import re

def safe_file_stem(s: str) -> str:
    s = str(s)
    s = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]+', "_", s)
    return s.strip("_")


def player_path(name: str) -> str:
    return f"player/{safe_file_stem(name)}.html"


def team_path(name: str) -> str:
    return f"team/{safe_file_stem(name)}.html"


def champion_path(champion_id: str) -> str:
    return f"champion/{safe_file_stem(champion_id)}.html"


def match_path(match_id: str) -> str:
    return f"match/{safe_file_stem(match_id)}.html"


def rel_href(current_dir: str, target: str) -> str:
    start = current_dir if current_dir else "."
    return posixpath.relpath(target, start=start)


def df_to_table(df: pd.DataFrame, columns: List[str], col_rename: Dict[str, str], table_id: str, escape: bool = True) -> str:
    if df.empty:
        return ""
    view = df[columns].copy().rename(columns=col_rename)
    return view.to_html(
        index=False,
        classes=["table", "table-hover", "align-middle", "data-table", "mb-0"],
        border=0,
        table_id=table_id,
        escape=escape,
        na_rep="-",
    )


def fmt_num(x, digits=2, suffix=""):
    if x is None:
        return "-"
    try:
        if pd.isna(x):
            return "-"
    except Exception:
        pass
    try:
        v = float(x)
        if digits == 0:
            return f"{int(round(v))}{suffix}"
        s = f"{v:.{digits}f}".rstrip("0").rstrip(".")
        return f"{s}{suffix}"
    except Exception:
        return f"{x}{suffix}"


def result_badge(x: Any) -> str:
    ok = bool(x)
    cls = "badge-win" if ok else "badge-lose"
    text = "胜" if ok else "负"
    return f'<span class="result-badge {cls}">{text}</span>'


def build_summary_cards_html(cards: List[Dict[str, Any]]) -> str:
    if not cards:
        return ""
    parts = []
    for c in cards:
        label = html_escape(str(c.get("label", "")))
        value = html_escape(str(c.get("value", "-")))
        sub = c.get("sub")
        sub_html = f'<div class="stat-sub">{html_escape(str(sub))}</div>' if sub not in (None, "") else ""
        parts.append(f"""  
        <div class="col-6 col-xl-3">  
          <div class="stat-card h-100">  
            <div class="stat-label">{label}</div>  
            <div class="stat-value">{value}</div>  
            {sub_html}  
          </div>  
        </div>  
        """)
    return f'<div class="row g-3 mb-4">{"".join(parts)}</div>'


def build_section_card(title: str, body_html: str, subtitle: Optional[str] = None, actions_html: str = "") -> str:
    subtitle_html = f'<div class="section-subtitle">{html_escape(subtitle)}</div>' if subtitle else ""
    return f"""  
    <section class="card app-card mb-4">  
      <div class="card-header bg-white border-0 p-4 pb-0">  
        <div class="d-flex flex-column flex-lg-row justify-content-between align-items-lg-center gap-3">  
          <div>  
            <div class="section-title">{html_escape(title)}</div>  
            {subtitle_html}  
          </div>  
          <div>{actions_html}</div>  
        </div>  
      </div>  
      <div class="card-body p-4">  
        {body_html}  
      </div>  
    </section>  
    """


def sort_matches_df(df: pd.DataFrame, ascending: bool = False) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    out["_date_sort"] = pd.to_numeric(out.get("match_date"), errors="coerce").fillna(0).astype(int)
    out["_no_sort"] = pd.to_numeric(out.get("match_no"), errors="coerce").fillna(0).astype(int)
    out["_ord_sort"] = pd.to_numeric(out.get("match_ord"), errors="coerce").fillna(0).astype(int)
    out = out.sort_values(["_date_sort", "_no_sort", "_ord_sort"], ascending=[ascending, ascending, ascending])
    return out.drop(columns=["_date_sort", "_no_sort", "_ord_sort"], errors="ignore")


BASE_HTML = r"""  
<!doctype html>  
<html lang="zh-CN">  
<head>  
  <meta charset="utf-8">  
  <meta name="viewport" content="width=device-width, initial-scale=1">  
  <title>{{ title }}</title>  

  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">  
  <link href="https://cdn.jsdelivr.net/npm/datatables.net-bs5@1.13.8/css/dataTables.bootstrap5.min.css" rel="stylesheet">  

  <script src="https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js"></script>  
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>  
  <script src="https://cdn.jsdelivr.net/npm/datatables.net@1.13.8/js/jquery.dataTables.min.js"></script>  
  <script src="https://cdn.jsdelivr.net/npm/datatables.net-bs5@1.13.8/js/dataTables.bootstrap5.min.js"></script>  
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>  

  <style>  
    :root{  
      --bg1:#f8fafc; --bg2:#eef2ff; --card:#ffffff; --text:#0f172a;  
      --muted:#64748b; --primary:#4f46e5; --shadow:0 10px 30px rgba(15,23,42,.08);  
      --radius:18px;  
    }  
    body{min-height:100vh;background:linear-gradient(180deg,var(--bg1) 0%,var(--bg2) 100%);color:var(--text);}  
    .navbar{background:rgba(15,23,42,.92)!important;backdrop-filter:blur(12px);box-shadow:0 8px 24px rgba(2,6,23,.18);}  
    .navbar-brand{font-weight:700;}  
    .page-shell{padding-top:28px;padding-bottom:40px;}  
    .hero-card{background:linear-gradient(135deg,#111827 0%,#312e81 100%);color:#fff;border:none;border-radius:24px;box-shadow:0 18px 40px rgba(31,41,55,.25);overflow:hidden;}  
    .hero-card .meta{color:rgba(255,255,255,.78);font-size:.95rem;}  
    .hero-badge{display:inline-flex;align-items:center;padding:.4rem .75rem;border-radius:999px;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.14);font-size:.86rem;margin-right:.5rem;margin-bottom:.5rem;}  
    .app-card{border:none;border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;}  
    .section-title{font-size:1.15rem;font-weight:700;color:var(--text);}  
    .section-subtitle{margin-top:.35rem;color:var(--muted);font-size:.92rem;}  
    .stat-card{background:var(--card);border:1px solid #edf2f7;border-radius:18px;padding:18px 18px 16px;box-shadow:0 8px 24px rgba(15,23,42,.05);}  
    .stat-label{color:var(--muted);font-size:.92rem;margin-bottom:.45rem;}  
    .stat-value{font-size:1.6rem;font-weight:800;line-height:1.2;color:var(--text);}  
    .stat-sub{margin-top:.35rem;color:var(--muted);font-size:.85rem;}  
    .nav-pills.lane-pills{gap:.65rem;flex-wrap:wrap;}  
    .nav-pills.lane-pills .nav-link{border:none;border-radius:999px;padding:.65rem 1rem;font-weight:600;color:#334155;background:#f8fafc;box-shadow:inset 0 0 0 1px #e2e8f0;}  
    .nav-pills.lane-pills .nav-link.active{background:var(--primary);color:#fff;box-shadow:none;}  
    .lane-badge{margin-left:.45rem;border-radius:999px;padding:.15rem .5rem;font-size:.78rem;background:rgba(255,255,255,.2);}  
    .nav-pills.lane-pills .nav-link:not(.active) .lane-badge{background:#e2e8f0;color:#334155;}  
    .table-wrap{overflow-x:auto;border:1px solid #edf2f7;border-radius:16px;background:#fff;}  
    table.dataTable{margin-top:0!important;margin-bottom:0!important;}  
    table.dataTable thead th{background:#f8fafc;color:#334155;font-weight:700;border-bottom:1px solid #e5e7eb!important;white-space:nowrap;}  
    table.dataTable tbody td{white-space:nowrap;vertical-align:middle;}  
    .dataTables_wrapper .dataTables_filter input,.dataTables_wrapper .dataTables_length select{border-radius:10px!important;border:1px solid #dbe2ea!important;box-shadow:none!important;}  
    .dataTables_wrapper .dataTables_filter,.dataTables_wrapper .dataTables_length{margin-bottom:14px;}  
    a{text-decoration:none;} a:hover{text-decoration:underline;}  
    .result-badge{display:inline-block;padding:.28rem .7rem;border-radius:999px;font-size:.84rem;font-weight:700;}  
    .badge-win{color:#166534;background:#dcfce7;} .badge-lose{color:#991b1b;background:#fee2e2;}  
    .team-panel{border:1px solid #edf2f7;border-radius:18px;background:#fff;padding:18px;height:100%;}  
    .team-title{font-size:1.05rem;font-weight:800;margin-bottom:4px;}  
    .team-sub{color:var(--muted);font-size:.9rem;margin-bottom:14px;}  
    .empty-state{padding:32px 16px;text-align:center;color:var(--muted);border:1px dashed #dbe2ea;border-radius:16px;background:#fbfdff;}  
    .btn-soft{background:#fff;border:1px solid #dbe2ea;color:#334155;border-radius:12px;padding:.55rem .9rem;font-weight:600;}  
    .btn-soft:hover{background:#f8fafc;color:#0f172a;}  
    code{background:#eef2ff;color:#3730a3;padding:.15rem .35rem;border-radius:8px;}  
  </style>  
</head>  

<body>  
<nav class="navbar navbar-expand-lg navbar-dark">  
  <div class="container-fluid px-4">  
    <a class="navbar-brand" href="{{ link_index }}">HVV杯英雄联盟S12赛季比赛数据总览</a>  
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#topNav">  
      <span class="navbar-toggler-icon"></span>  
    </button>  
    <div class="collapse navbar-collapse" id="topNav">  
      <div class="navbar-nav ms-auto">  
        <a class="nav-link {% if active=='daily' %}active{% endif %}" href="{{ link_index }}">赛事情况</a>
        <a class="nav-link {% if active=='players' %}active{% endif %}" href="{{ link_players }}">选手数据</a>
        <a class="nav-link {% if active=='champions' %}active{% endif %}" href="{{ link_champions }}">英雄数据</a>  
        <a class="nav-link {% if active=='teams' %}active{% endif %}" href="{{ link_teams }}">队伍榜</a>  
        <a class="nav-link {% if active=='bans' %}active{% endif %}" href="{{ link_bans }}">Ban榜</a>  
      </div>  
    </div>  
  </div>  
</nav>  

<div class="container-fluid page-shell px-3 px-lg-4">  
  <div class="card hero-card mb-4">  
    <div class="card-body p-4 p-lg-5">  
      <div class="row g-4 align-items-center">  
        <div class="col-lg-8">  
          <div class="d-flex flex-wrap mb-2">  
            <span class="hero-badge">总对局：{{ total_matches if total_matches is not none else '-' }}</span>  
            <span class="hero-badge">数据源：{{ data_path }}</span>  
          </div>  
          <h1 class="h3 mb-2">{{ title }}</h1>  
          {% if page_desc %}  
            <div class="meta mb-3">{{ page_desc }}</div>  
          {% endif %}  
          <div class="meta">英雄映射：<code>{{ champion_json_path }}</code></div>  
        </div>  
        <div class="col-lg-4 text-lg-end">  
          <a class="btn btn-light btn-sm me-2 mb-2" href="{{ link_players }}">选手榜</a>  
          <a class="btn btn-light btn-sm me-2 mb-2" href="{{ link_champions }}">英雄榜</a>  
          <a class="btn btn-light btn-sm me-2 mb-2" href="{{ link_teams }}">队伍榜</a>  
          <a class="btn btn-light btn-sm mb-2" href="{{ link_bans }}">Ban榜</a>  
        </div>  
      </div>  
    </div>  
  </div>  

  {% if summary_cards_html %}  
    {{ summary_cards_html | safe }}  
  {% endif %}  

  {% if tabs %}  
    <section class="card app-card">  
      <div class="card-header bg-white border-0 p-4 pb-0">  
        <div class="section-title">{{ tabs_title or '分路统计' }}</div>  
        {% if tabs_subtitle %}  
          <div class="section-subtitle">{{ tabs_subtitle }}</div>  
        {% endif %}  
      </div>  
      <div class="card-body p-4">  
        <ul class="nav nav-pills lane-pills mb-4" id="laneTabs" role="tablist">  
          {% for lane in lanes %}  
            <li class="nav-item" role="presentation">  
              <button  
                class="nav-link {% if loop.first %}active{% endif %}"  
                id="tab-{{ lane }}-tab"  
                data-bs-toggle="pill"  
                data-bs-target="#tab-{{ lane }}"  
                type="button"  
                role="tab">  
                {{ lane }}  
                <span class="lane-badge">{{ counts.get(lane, 0) }}</span>  
              </button>  
            </li>  
          {% endfor %}  
        </ul>  

        <div class="tab-content" id="laneTabsContent">  
          {% for lane in lanes %}  
            <div class="tab-pane fade {% if loop.first %}show active{% endif %}" id="tab-{{ lane }}" role="tabpanel">  
              {% if tables.get(lane) %}  
                <div class="table-wrap">{{ tables[lane] | safe }}</div>  
              {% else %}  
                <div class="empty-state">该分路暂无数据。</div>  
              {% endif %}  
            </div>  
          {% endfor %}  
        </div>  
      </div>  
    </section>  
  {% else %}  
    {{ content | safe }}  
  {% endif %}  
</div>  

<script>  
  const dtOpts = {  
    pageLength: 25,  
    order: [],  
    autoWidth: false,  
    language: {  
      search: "搜索：",  
      lengthMenu: "每页 _MENU_ 行",  
      info: "第 _START_ 到 _END_ 行，共 _TOTAL_ 行",  
      infoEmpty: "无数据",  
      zeroRecords: "无匹配结果",  
      paginate: { first: "首页", last: "末页", next: "下一页", previous: "上一页" }  
    }  
  };  

  $(function() {  
    $('table.data-table').each(function() {  
      $(this).DataTable(dtOpts);  
    });  
    document.getElementById('laneTabs')?.addEventListener('shown.bs.tab', function () {  
      $.fn.dataTable.tables({ visible: true, api: true }).columns.adjust();  
    });  
  });  
</script>  
</body>  
</html>  
"""


class StaticSiteBuilder:
    def __init__(self, data_path: str, champion_json_path: str, out_dir: str):
        self.data_path = data_path
        self.champion_json_path = champion_json_path
        self.out_dir = out_dir
        self.template = Template(BASE_HTML)

        self.champ_map = load_champion_map_from_champion_json(champion_json_path)
        self.matches = load_matches(data_path)
        self.total_matches = len(self.matches)
        self.match_meta = build_match_meta(self.matches)

        self.df_detail = extract_rows(self.matches, champ_map=self.champ_map, match_meta=self.match_meta)
        # -----------------------------
        # 校验：选手只能属于一个队伍
        # -----------------------------
        if not self.df_detail.empty:

            conflict_players = {}

            for player, g in self.df_detail.groupby("player"):
                teams = set(g["team_name"].dropna().astype(str))

                if len(teams) > 1:

                    files = set()

                    for mid in g["match_id"]:
                        meta = self.match_meta.get(str(mid)) or {}
                        src = meta.get("src_file")
                        if src:
                            files.add(os.path.basename(src))

                    conflict_players[player] = {
                        "teams": list(teams),
                        "files": sorted(files)
                    }

            if conflict_players:

                print("\n错误：检测到选手属于多个队伍\n")

                for player, info in conflict_players.items():

                    print(f"选手: {player}")
                    print(f"队伍: {', '.join(info['teams'])}")

                    print("涉及文件:")
                    for f in info["files"]:
                        print("  ", f)

                    print()

                raise RuntimeError("选手队伍不唯一，请修复数据后重新生成静态页面")

        # -----------------------------
        # 建立 player -> team 映射
        # -----------------------------
        self.player_team_map = (
            self.df_detail.groupby("player")["team_name"]
            .first()
            .to_dict()
        )

        # -----------------------------
        # 查询选手段位
        # -----------------------------
        self.rank_cache = load_rank_cache()

        self.player_rank_map = {}

        players = self.df_detail["player"].dropna().unique().tolist()

        need_query = [p for p in players if p not in self.rank_cache]

        print(f"段位查询：缓存 {len(players) - len(need_query)} 人，需要查询 {len(need_query)} 人")

        for player in need_query:

            game_id = player

            rank = fetch_player_rank(game_id, self.rank_cache)

            self.rank_cache[player] = rank

        for p in players:
            self.player_rank_map[p] = self.rank_cache.get(p, "-")

        save_rank_cache(self.rank_cache)
        self.players_df = agg_players(self.df_detail)
        self.champs_df = agg_champions(self.df_detail)
        self.team_match_stats = build_team_match_stats(self.df_detail)
        self.teams_df = agg_teams(self.team_match_stats)

        self.ban_df = extract_bans(self.matches)
        if not self.ban_df.empty:
            self.ban_df = self.ban_df.copy()
            self.ban_df["team_name"] = self.ban_df.apply(
                lambda r: team_name_from_meta(self.match_meta, r["match_id"], r["teamId"]),
                axis=1
            )
            self.ban_df["champion_name"] = self.ban_df["champion_id"].map(lambda x: self.champ_map.get(str(x), str(x)))

        self.ban_stats = make_ban_stats(self.ban_df, total_matches=self.total_matches)
        if not self.ban_stats.empty:
            self.ban_stats["champion_name"] = self.ban_stats["champion_id"].map(lambda x: self.champ_map.get(str(x), str(x)))

        self.team_pick_champs_df = agg_team_pick_champions(self.df_detail)
        self.team_ban_champs_df = agg_team_ban_champions(self.ban_df)

        self.match_ord_map = {}
        if not self.df_detail.empty:
            self.match_ord_map = (
                self.df_detail[["match_id", "match_ord"]]
                .drop_duplicates(subset=["match_id"])
                .set_index("match_id")["match_ord"]
                .to_dict()
            )
    def build_daily_results(self):

        if self.team_match_stats.empty:
            return

        df = self.team_match_stats.copy()

        df["date"] = df["match_date"]
        df["team"] = df["team_name"]
        df["opponent"] = df["opponent"]
        df["win"] = df["win"].astype(int)

        rows = []

        for match_id, g in df.groupby("match_id"):

            if len(g) != 2:
                continue

            t1 = g.iloc[0]
            t2 = g.iloc[1]

            date = t1["date"]
            teamA = str(t1["team"])
            teamB = str(t2["team"])

            winner = teamA if t1["win"] else teamB

            pair = tuple(sorted([teamA, teamB]))

            rows.append({
                "date": date,
                "teamA": pair[0],
                "teamB": pair[1],
                "winner": winner
            })

        rdf = pd.DataFrame(rows)

        if rdf.empty:
            return

        result_rows = []

        for (date, teamA, teamB), g in rdf.groupby(["date", "teamA", "teamB"]):

            winsA = (g["winner"] == teamA).sum()
            winsB = (g["winner"] == teamB).sum()

            if winsA >= winsB:
                left = teamA
                right = teamB
                left_wins = winsA
                right_wins = winsB
            else:
                left = teamB
                right = teamA
                left_wins = winsB
                right_wins = winsA

            result_rows.append({
                "date": date,
                "left": left,
                "right": right,
                "left_wins": left_wins,
                "right_wins": right_wins
            })

        result_df = pd.DataFrame(result_rows)

        result_df = result_df.sort_values(["date"], ascending=False)

        day_blocks = []

        for date, g in result_df.groupby("date"):

            matches_html = ""

            for _, r in g.iterrows():

                matches_html += f"""
                <div class="match-card">

                    <div class="team team-left">
                        {html_escape(r["left"])}
                    </div>

                    <div class="match-score">
                        <span class="score-left">{r["left_wins"]}</span>
                        <span class="score-divider">:</span>
                        <span class="score-right">{r["right_wins"]}</span>
                    </div>

                    <div class="team team-right">
                        {html_escape(r["right"])}
                    </div>

                </div>
                """

            day_blocks.append(f"""
            <div class="match-day">

                <div class="match-day-title">
                    {date}
                </div>

                <div class="match-grid">
                    {matches_html}
                </div>

            </div>
            """)

        content = f"""

    <style>

    .match-day {{
        margin-bottom:50px;
    }}

    .match-day-title {{
        font-size:28px;
        font-weight:800;
        margin-bottom:18px;
        color:#60a5fa;
    }}

    .match-grid {{
        display:grid;
        grid-template-columns:1fr 1fr;
        gap:18px;
    }}

    .match-card {{
        background:#020617;
        border-radius:14px;
        padding:18px 24px;
        display:flex;
        align-items:center;
        justify-content:space-between;
        box-shadow:0 10px 22px rgba(0,0,0,0.35);
        transition:all 0.2s;
    }}

    .match-card:hover {{
        transform:translateY(-2px);
    }}

    .team {{
        font-size:18px;
        font-weight:700;
        width:35%;
        color:#e2e8f0;
    }}

    .team-left {{
        text-align:left;
    }}

    .team-right {{
        text-align:right;
    }}

    .match-score {{
        font-size:28px;
        font-weight:900;
        color:#facc15;
        letter-spacing:3px;
    }}

    .score-divider {{
        margin:0 4px;
    }}

    @media (max-width:900px) {{

    .match-grid {{
        grid-template-columns:1fr;
    }}

    }}

    </style>

    {''.join(day_blocks)}

    """

        html = self.render_page(
            current_dir="",
            title="赛事情况",
            active="index",
            page_desc="每日系列赛比分统计",
            summary_cards=[
                {"label": "比赛日", "value": result_df["date"].nunique()},
                {"label": "系列赛", "value": len(result_df)},
                {"label": "总对局", "value": self.total_matches},
            ],
            tabs=False,
            content=content
        )

        self.write_file("index.html", html)
        self.write_file("daily.html", html)
    def write_file(self, rel_path: str, content: str):
        fp = os.path.join(self.out_dir, *rel_path.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)

    def render_page(
        self,
        *,
        current_dir: str,
        title: str,
        active: str,
        page_desc: str = "",
        summary_cards: Optional[List[Dict[str, Any]]] = None,
        tabs: bool = False,
        tables: Optional[Dict[str, str]] = None,
        counts: Optional[Dict[str, int]] = None,
        content: str = "",
        tabs_title: str = "",
        tabs_subtitle: str = "",
    ) -> str:
        return self.template.render(
            title=title,
            active=active,
            page_desc=page_desc,
            summary_cards_html=build_summary_cards_html(summary_cards or []),
            lanes=LANES,
            tables=tables or {},
            counts=counts or {},
            data_path=self.data_path,
            champion_json_path=self.champion_json_path,
            total_matches=self.total_matches,
            tabs=tabs,
            content=content,
            tabs_title=tabs_title,
            tabs_subtitle=tabs_subtitle,
            link_index=rel_href(current_dir, "index.html"),
            link_players=rel_href(current_dir, "players.html"),
            link_champions=rel_href(current_dir, "champions.html"),
            link_teams=rel_href(current_dir, "teams.html"),
            link_bans=rel_href(current_dir, "bans.html"),
        )

    def build_linkers(self, current_dir: str):
        def team_link(team_name: str) -> str:
            if team_name is None or (isinstance(team_name, float) and pd.isna(team_name)):
                return "-"
            href = rel_href(current_dir, team_path(str(team_name)))
            return f'<a href="{href}">{html_escape(str(team_name))}</a>'

        def player_link(player_name: str) -> str:
            href = rel_href(current_dir, player_path(str(player_name)))
            return f'<a href="{href}">{html_escape(str(player_name))}</a>'

        def champion_link(champion_id: str, champion_name: str) -> str:
            href = rel_href(current_dir, champion_path(str(champion_id)))
            return f'<a href="{href}">{html_escape(str(champion_name))}</a>'

        def match_link(match_id: str) -> str:
            href = rel_href(current_dir, match_path(str(match_id)))
            return f'<a href="{href}">{html_escape(str(match_id))}</a>'

        return team_link, player_link, champion_link, match_link

    def build_players_page(self):
        current_dir = ""
        team_link, player_link, _, _ = self.build_linkers(current_dir)

        tables = {}
        counts = {}
        pdf = self.players_df.copy()

        if not pdf.empty:
            pdf["team"] = pdf["player"].map(self.player_team_map)
            pdf["rank"] = pdf["player"].map(self.player_rank_map)
        if not pdf.empty:
            pdf["player_link"] = pdf["player"].map(player_link)
        if not pdf.empty:
            pdf["team_link"] = pdf["team"].map(team_link)
        for lane in LANES:
            dff = pdf[pdf["lane"] == lane] if not pdf.empty else pdf
            counts[lane] = len(dff)
            tables[lane] = df_to_table(
                dff,
                columns=[
                    "player_link",
                    "team_link",
                    "rank",
                    "games",
                    "wins",
                    "winrate",
                    "avg_score",
                    "avg_kda",
                    "avg_kp",
                    "avg_gold_diff",
                    "avg_level_diff"
                ],
                col_rename={
                    "player_link": "选手",
                    "team_link": "队伍",
                    "rank": "历史最高段位",
                    "games": "总场次",
                    "wins": "胜场",
                    "winrate": "胜率(%)",
                    "avg_score": "平均评分",
                    "avg_kda": "平均KDA",
                    "avg_kp": "平均参团率(%)",
                    "avg_gold_diff": "场均对位经济差",
                    "avg_level_diff": "场均对位等级差",
                },
                table_id=f"tbl_players_{lane}",
                escape=False,
            )

        summary_cards = [
            {"label": "总对局数", "value": self.total_matches},
            {"label": "参赛记录", "value": len(self.df_detail) if not self.df_detail.empty else 0},
            {"label": "选手数", "value": self.df_detail["player"].nunique() if not self.df_detail.empty else 0},
            {"label": "队伍数", "value": self.df_detail["team_name"].nunique() if not self.df_detail.empty else 0},
        ]

        html = self.render_page(
            current_dir=current_dir,
            title="各分路选手数据表现",
            active="players",
            page_desc="点击选手可查看该选手的所有对局明细。",
            summary_cards=summary_cards,
            tabs=True,
            tables=tables,
            counts=counts,
            tabs_title="分路选手榜",
            tabs_subtitle="按分路查看选手的场次、胜率、评分、KDA、参团率及对位差值表现。",
        )
        self.write_file("players.html", html)
        self.write_file("index.html", html)

    def build_player_detail_pages(self):
        current_dir = "player"
        team_link, _, champion_link, match_link = self.build_linkers(current_dir)

        for name in sorted(self.df_detail["player"].dropna().astype(str).unique().tolist()):
            dff = self.df_detail[self.df_detail["player"] == name].copy()
            if dff.empty:
                continue

            dff = sort_matches_df(dff, ascending=False)
            dff["result"] = dff["win"].map(result_badge)
            dff["kda_str"] = (
                dff["kills"].fillna(0).astype(int).astype(str) + "/" +
                dff["deaths"].fillna(0).astype(int).astype(str) + "/" +
                dff["assists"].fillna(0).astype(int).astype(str)
            )
            dff["match_link"] = dff["match_id"].map(match_link)
            dff["champ_link"] = dff.apply(lambda r: champion_link(r["champion_id"], r["champion_name"]), axis=1)
            dff["team_link"] = dff["team_name"].map(team_link)

            table_html = df_to_table(
                dff,
                columns=["match_link", "match_date", "match_no", "lane", "team_link", "result", "champ_link", "opp_player", "opp_champion", "score", "kda_str", "kpr", "gold", "level", "gold_diff", "level_diff"],
                col_rename={
                    "match_link": "对局ID",
                    "match_date": "日期",
                    "match_no": "场次",
                    "lane": "分路",
                    "team_link": "队伍",
                    "result": "胜负",
                    "champ_link": "使用英雄",
                    "opp_player": "对位选手",
                    "opp_champion": "对位英雄",
                    "score": "评分",
                    "kda_str": "K/D/A",
                    "kpr": "参团率(%)",
                    "gold": "经济",
                    "level": "等级",
                    "gold_diff": "对位经济差",
                    "level_diff": "对位等级差",
                },
                table_id="tbl_player_detail",
                escape=False,
            )

            wins = int(dff["win"].sum())
            games = len(dff)
            winrate = round(wins / games * 100, 2) if games else 0
            summary_cards = [
                {"label": "总场次", "value": games},
                {"label": "胜场", "value": wins, "sub": f"胜率 {fmt_num(winrate)}%"},
                {"label": "平均评分", "value": fmt_num(dff['score'].mean())},
                {"label": "平均KDA", "value": fmt_num(dff['kda'].mean())},
            ]

            content = build_section_card(
                title=f"选手对局明细：{name}",
                subtitle=f"共 {games} 条参赛记录",
                actions_html=f'<a class="btn btn-soft" href="{rel_href(current_dir, "players.html")}">返回选手榜</a>',
                body_html=f'<div class="table-wrap">{table_html}</div>'
            )

            html = self.render_page(
                current_dir=current_dir,
                title=f"选手对局明细：{name}",
                active="players",
                page_desc="展示该选手所有已记录对局的详细表现与对位信息。",
                summary_cards=summary_cards,
                tabs=False,
                content=content,
            )
            self.write_file(player_path(name), html)

    def build_champions_page(self):
        current_dir = ""
        _, _, champion_link, _ = self.build_linkers(current_dir)

        tables = {}
        counts = {}
        cdf = self.champs_df.copy()
        if not cdf.empty:
            cdf["champ_link"] = cdf.apply(lambda r: champion_link(r["champion_id"], r["champion_name"]), axis=1)

        for lane in LANES:
            dff = cdf[cdf["lane"] == lane] if not cdf.empty else cdf
            counts[lane] = len(dff)
            tables[lane] = df_to_table(
                dff,
                columns=["champ_link", "games", "wins", "winrate", "avg_score", "avg_kda", "avg_kp", "avg_gold_diff", "avg_level_diff"],
                col_rename={
                    "champ_link": "英雄",
                    "games": "总场次",
                    "wins": "胜场",
                    "winrate": "胜率(%)",
                    "avg_score": "平均评分",
                    "avg_kda": "平均KDA",
                    "avg_kp": "平均参团率(%)",
                    "avg_gold_diff": "场均对位经济差",
                    "avg_level_diff": "场均对位等级差",
                },
                table_id=f"tbl_champions_{lane}",
                escape=False,
            )

        summary_cards = [
            {"label": "总对局数", "value": self.total_matches},
            {"label": "已上场英雄", "value": self.df_detail["champion_name"].nunique() if not self.df_detail.empty else 0},
            {"label": "有Ban记录英雄", "value": self.ban_stats["champion_id"].nunique() if not self.ban_stats.empty else 0},
            {"label": "总Ban次数", "value": len(self.ban_df) if not self.ban_df.empty else 0},
        ]

        html = self.render_page(
            current_dir=current_dir,
            title="各分路英雄数据表现",
            active="champions",
            page_desc="点击英雄可进入该英雄的详细对局与被 Ban 记录。",
            summary_cards=summary_cards,
            tabs=True,
            tables=tables,
            counts=counts,
            tabs_title="分路英雄榜",
            tabs_subtitle="同一英雄按分路分别统计，便于观察位置差异。",
        )
        self.write_file("champions.html", html)

    def build_champion_detail_pages(self):
        current_dir = "champion"
        team_link, player_link, _, match_link = self.build_linkers(current_dir)

        champ_ids = set()
        if not self.df_detail.empty:
            champ_ids |= set(self.df_detail["champion_id"].dropna().astype(str).tolist())
        if not self.ban_df.empty:
            champ_ids |= set(self.ban_df["champion_id"].dropna().astype(str).tolist())

        def _champ_sort_key(x):
            return (0, int(x)) if str(x).isdigit() else (1, str(x))

        for cid in sorted(champ_ids, key=_champ_sort_key):
            champ_name = self.champ_map.get(cid, cid)

            played = self.df_detail[self.df_detail["champion_id"].astype(str) == cid].copy()
            if not played.empty:
                played = sort_matches_df(played, ascending=False)
                played["result"] = played["win"].map(result_badge)
                played["kda_str"] = (
                    played["kills"].fillna(0).astype(int).astype(str) + "/" +
                    played["deaths"].fillna(0).astype(int).astype(str) + "/" +
                    played["assists"].fillna(0).astype(int).astype(str)
                )
                played["match_link"] = played["match_id"].map(match_link)
                played["player_link"] = played["player"].map(player_link)
                played["team_link"] = played["team_name"].map(team_link)

                played_table = df_to_table(
                    played,
                    columns=["match_link", "match_date", "match_no", "player_link", "lane", "team_link", "result", "opp_player", "opp_champion", "score", "kda_str", "kpr", "gold", "level", "gold_diff", "level_diff"],
                    col_rename={
                        "match_link": "对局ID",
                        "match_date": "日期",
                        "match_no": "场次",
                        "player_link": "选手",
                        "lane": "分路",
                        "team_link": "队伍",
                        "result": "胜负",
                        "opp_player": "对位选手",
                        "opp_champion": "对位英雄",
                        "score": "评分",
                        "kda_str": "K/D/A",
                        "kpr": "参团率(%)",
                        "gold": "经济",
                        "level": "等级",
                        "gold_diff": "对位经济差",
                        "level_diff": "对位等级差",
                    },
                    table_id="tbl_champion_played",
                    escape=False,
                )
                played_html = f'<div class="table-wrap">{played_table}</div>'
            else:
                played_html = '<div class="empty-state">该英雄暂无上场记录。</div>'

            banned = pd.DataFrame()
            if not self.ban_df.empty:
                banned = self.ban_df[self.ban_df["champion_id"].astype(str) == cid].copy()

            if not banned.empty:
                banned = banned.drop_duplicates(subset=["match_id", "teamId"], keep="first")
                banned["match_link"] = banned["match_id"].map(match_link)
                banned["ban_team"] = banned["team_name"]
                banned["ban_team_link"] = banned["ban_team"].map(team_link)
                banned["match_ord"] = banned["match_id"].map(lambda mid: self.match_ord_map.get(mid, 0))

                banned_table = df_to_table(
                    banned.sort_values(["match_ord"], ascending=[False]),
                    columns=["match_link", "ban_team_link"],
                    col_rename={"match_link": "对局ID", "ban_team_link": "Ban方队伍"},
                    table_id="tbl_champion_banned",
                    escape=False,
                )
                banned_html = f'<div class="table-wrap">{banned_table}</div>'
            else:
                banned_html = '<div class="empty-state">该英雄暂无被 Ban 记录。</div>'

            banrate = "-"
            ban_matches = 0
            bans = 0
            if not self.ban_stats.empty:
                row = self.ban_stats[self.ban_stats["champion_id"].astype(str) == cid]
                if not row.empty:
                    r0 = row.iloc[0]
                    banrate = fmt_num(r0["banrate"])
                    ban_matches = int(r0["ban_matches"])
                    bans = int(r0["bans"])

            played_games = len(played)
            played_wins = int(played["win"].sum()) if not played.empty else 0
            played_wr = round(played_wins / played_games * 100, 2) if played_games else 0

            summary_cards = [
                {"label": "上场场次", "value": played_games},
                {"label": "胜率", "value": f"{fmt_num(played_wr)}%" if played_games else "-"},
                {"label": "被Ban对局数", "value": ban_matches},
                {"label": "Ban率", "value": f"{banrate}%" if banrate != "-" else "-"},
            ]

            content = (
                build_section_card(
                    title=f"英雄上场明细：{champ_name}（ID: {cid}）",
                    subtitle="所有使用该英雄的对局详情",
                    actions_html=f'<a class="btn btn-soft" href="{rel_href(current_dir, "champions.html")}">返回英雄榜</a>',
                    body_html=played_html
                )
                +
                build_section_card(
                    title=f"被 Ban 明细：{champ_name}",
                    subtitle=f"总 Ban 次数：{bans}",
                    body_html=banned_html
                )
            )

            html = self.render_page(
                current_dir=current_dir,
                title=f"英雄对局明细：{champ_name}",
                active="champions",
                page_desc="同时展示该英雄的上场表现和被 Ban 记录。",
                summary_cards=summary_cards,
                tabs=False,
                content=content,
            )
            self.write_file(champion_path(cid), html)

    def build_teams_page(self):
        current_dir = ""
        team_link, _, _, _ = self.build_linkers(current_dir)
        dff = self.teams_df.copy()

        summary_cards = [
            {"label": "队伍数", "value": dff["team_name"].nunique() if not dff.empty else 0},
            {"label": "队伍参赛记录",
             "value": len(self.team_match_stats) if not self.team_match_stats.empty else 0},
            {"label": "最高胜率", "value": f"{fmt_num(dff['winrate'].max())}%" if not dff.empty else "-"},
            {"label": "总对局数", "value": self.total_matches},
        ]

        if dff.empty:
            content = build_section_card(
                title="队伍排行榜",
                subtitle="展示队伍整体胜率与场均表现。",
                body_html='<div class="empty-state">暂无队伍数据。</div>'
            )
        else:
            dff["team_link"] = dff["team_name"].map(team_link)
            table_html = df_to_table(
                dff,
                columns=[
                    "team_link",
                    "games", "wins", "winrate",
                    "avg_score", "avg_kda", "avg_kp",
                    "avg_gold", "avg_level",
                    "avg_gold_diff", "avg_level_diff",
                ],
                col_rename={
                    "team_link": "队伍",
                    "games": "总场次",
                    "wins": "胜场",
                    "winrate": "胜率(%)",
                    "avg_score": "平均评分",
                    "avg_kda": "平均KDA",
                    "avg_kp": "平均参团率(%)",
                    "avg_gold": "场均总经济",
                    "avg_level": "平均等级",
                    "avg_gold_diff": "场均对位经济差",
                    "avg_level_diff": "场均对位等级差",
                },
                table_id="tbl_teams",
                escape=False,
            )
            content = build_section_card(
                title="队伍排行榜",
                subtitle="点击队伍可查看该队伍参与的全部比赛与英雄使用、Ban明细。",
                body_html=f'<div class="table-wrap">{table_html}</div>'
            )

        html = self.render_page(
            current_dir=current_dir,
            title="队伍排行榜",
            active="teams",
            page_desc="按队伍维度汇总整体表现，适合快速查看队伍强度和稳定性。",
            summary_cards=summary_cards,
            tabs=False,
            content=content,
        )
        self.write_file("teams.html", html)

    def build_team_detail_pages(self):
        if self.team_match_stats.empty:
            return

        current_dir = "team"
        team_link, _, champion_link, match_link = self.build_linkers(current_dir)

        for name in sorted(self.team_match_stats["team_name"].dropna().astype(str).unique().tolist()):
            dff = self.team_match_stats[self.team_match_stats["team_name"] == name].copy()
            if dff.empty:
                continue

            dff = sort_matches_df(dff, ascending=False)
            team_row = self.teams_df[self.teams_df["team_name"] == name].copy()

            dff["match_link"] = dff["match_id"].map(match_link)
            dff["result_badge"] = dff["win"].map(result_badge)
            dff["opp_team_link"] = dff["opponent"].map(lambda x: team_link(x) if pd.notna(x) else "-")

            table_html = df_to_table(
                dff,
                columns=[
                    "match_link", "match_date", "match_no",
                    "result_badge", "opp_team_link",
                    "avg_score", "team_kda", "avg_kp",
                    "team_gold", "team_gold_diff", "team_level_diff",
                ],
                col_rename={
                    "match_link": "对局ID",
                    "match_date": "日期",
                    "match_no": "场次",
                    "result_badge": "胜负",
                    "opp_team_link": "对手队伍",
                    "avg_score": "本场平均评分",
                    "team_kda": "本场队伍KDA",
                    "avg_kp": "本场平均参团率(%)",
                    "team_gold": "本场总经济",
                    "team_gold_diff": "本场对位经济差",
                    "team_level_diff": "本场对位等级差",
                },
                table_id="tbl_team_detail",
                escape=False,
            )

            pick_df = self.team_pick_champs_df[self.team_pick_champs_df["team_name"] == name].copy()
            if not pick_df.empty:
                pick_df["champ_link"] = pick_df.apply(
                    lambda r: champion_link(r["champion_id"], r["champion_name"]),
                    axis=1
                )
                pick_table_html = df_to_table(
                    pick_df,
                    columns=["champ_link", "games", "wins", "winrate", "avg_score"],
                    col_rename={
                        "champ_link": "英雄",
                        "games": "选用场次",
                        "wins": "胜场",
                        "winrate": "胜率(%)",
                        "avg_score": "平均评分",
                    },
                    table_id="tbl_team_pick_champs",
                    escape=False,
                )
                pick_section_html = f'<div class="table-wrap">{pick_table_html}</div>'
            else:
                pick_section_html = '<div class="empty-state">暂无该队伍的选用英雄记录。</div>'

            team_ban_df = self.team_ban_champs_df[self.team_ban_champs_df["team_name"] == name].copy()
            if not team_ban_df.empty:
                team_ban_df["champ_link"] = team_ban_df.apply(
                    lambda r: champion_link(r["champion_id"], r["champion_name"]),
                    axis=1
                )
                ban_table_html = df_to_table(
                    team_ban_df,
                    columns=["champ_link", "ban_games", "bans"],
                    col_rename={
                        "champ_link": "英雄",
                        "ban_games": "Ban场次",
                        "bans": "总Ban次数",
                    },
                    table_id="tbl_team_ban_champs",
                    escape=False,
                )
                ban_section_html = f'<div class="table-wrap">{ban_table_html}</div>'
            else:
                ban_section_html = '<div class="empty-state">暂无该队伍的 Ban 记录。</div>'

            summary_cards = []
            if not team_row.empty:
                r = team_row.iloc[0]
                summary_cards = [
                    {"label": "总场次", "value": int(r["games"])},
                    {"label": "胜场", "value": int(r["wins"]), "sub": f"胜率 {fmt_num(r['winrate'])}%"},
                    {"label": "平均评分", "value": fmt_num(r["avg_score"])},
                    {"label": "平均KDA", "value": fmt_num(r["avg_kda"])},
                ]

            content = (
                    build_section_card(
                        title=f"队伍对局列表：{name}",
                        subtitle=f"共 {len(dff)} 场",
                        actions_html=f'<a class="btn btn-soft" href="{rel_href(current_dir, "teams.html")}">返回队伍榜</a>',
                        body_html=f'<div class="table-wrap">{table_html}</div>'
                    )
                    +
                    build_section_card(
                        title=f"队伍选用英雄场次排行：{name}",
                        subtitle="按该队伍使用英雄的场次数降序统计",
                        body_html=pick_section_html
                    )
                    +
                    build_section_card(
                        title=f"队伍 Ban 英雄场次排行：{name}",
                        subtitle="按该队伍 Ban 英雄的场次数降序统计",
                        body_html=ban_section_html
                    )
            )

            html = self.render_page(
                current_dir=current_dir,
                title=f"队伍对局列表：{name}",
                active="teams",
                page_desc="展示该队伍参与的全部比赛，以及该队伍的选用英雄排行与 Ban 英雄排行。",
                summary_cards=summary_cards,
                tabs=False,
                content=content,
            )
            self.write_file(team_path(name), html)

    def build_bans_page(self):
        current_dir = ""
        _, _, champion_link, _ = self.build_linkers(current_dir)
        dff = self.ban_stats.copy()

        summary_cards = [
            {"label": "总对局数", "value": self.total_matches},
            {"label": "有Ban记录英雄", "value": dff["champion_id"].nunique() if not dff.empty else 0},
            {"label": "总Ban次数", "value": len(self.ban_df) if not self.ban_df.empty else 0},
            {"label": "最高Ban率", "value": f"{fmt_num(dff['banrate'].max())}%" if not dff.empty else "-"},
        ]

        if dff.empty:
            content = build_section_card(
                title="Ban 榜（全局）",
                subtitle="Ban率 = 被Ban的对局数 / 总对局数",
                body_html='<div class="empty-state">暂无 Ban 数据。</div>'
            )
        else:
            dff["champ_link"] = dff.apply(
                lambda r: champion_link(r["champion_id"], r["champion_name"]),
                axis=1
            )
            table_html = df_to_table(
                dff,
                columns=["champ_link", "ban_matches", "banrate", "bans"],
                col_rename={
                    "champ_link": "英雄",
                    "ban_matches": "被Ban的对局数",
                    "banrate": "Ban率(%)",
                    "bans": "总Ban次数",
                },
                table_id="tbl_bans",
                escape=False,
            )

            content = build_section_card(
                title="Ban 榜（全局）",
                subtitle="Ban率 = 被Ban的对局数 / 总对局数",
                body_html=f'<div class="table-wrap">{table_html}</div>'
            )

        html = self.render_page(
            current_dir=current_dir,
            title="Ban 榜（全局）",
            active="bans",
            page_desc="汇总所有对局中的 Ban 记录，便于查看版本热门封锁英雄。",
            summary_cards=summary_cards,
            tabs=False,
            content=content,
        )
        self.write_file("bans.html", html)

    def build_match_detail_pages(self):
        if self.df_detail.empty:
            return

        current_dir = "match"
        _, player_link, champion_link, _ = self.build_linkers(current_dir)

        lane_order = {v: i for i, v in enumerate(LANES)}
        match_ids = self.df_detail["match_id"].dropna().astype(str).unique().tolist()

        def _match_sort_key(mid: str):
            meta = self.match_meta.get(str(mid)) or {}
            date_num = int(meta.get("date") or 0) if str(meta.get("date") or "").isdigit() else 0
            no_num = int(meta.get("no") or 0) if str(meta.get("no") or "").isdigit() else 0
            ord_num = int(self.match_ord_map.get(str(mid), 0))
            return (date_num, no_num, ord_num)

        for match_id in sorted(match_ids, key=_match_sort_key, reverse=True):
            dff = self.df_detail[self.df_detail["match_id"].astype(str) == str(match_id)].copy()
            if dff.empty:
                continue

            dff["result"] = dff["win"].map(result_badge)
            dff["kda_str"] = (
                    dff["kills"].fillna(0).astype(int).astype(str) + "/" +
                    dff["deaths"].fillna(0).astype(int).astype(str) + "/" +
                    dff["assists"].fillna(0).astype(int).astype(str)
            )
            dff["player_link"] = dff["player"].map(player_link)
            dff["champ_link"] = dff.apply(lambda r: champion_link(r["champion_id"], r["champion_name"]), axis=1)
            dff["lane_ord"] = dff["lane"].map(lane_order).fillna(999).astype(int)
            dff = dff.sort_values(["teamId", "lane_ord", "player"], ascending=[True, True, True])

            meta = self.match_meta.get(str(match_id)) or {}
            team100_name = meta.get("team100") or "队伍100"
            team200_name = meta.get("team200") or "队伍200"
            subtitle = f'日期：{meta.get("date") or "-"} ｜ 场次：{meta.get("no") or "-"} ｜ 对阵：{team100_name} vs {team200_name}'

            team_panels = []
            for team_id in ["100", "200"]:
                tdf = dff[dff["teamId"].astype(str) == team_id].copy()
                if tdf.empty:
                    continue

                team_name = tdf["team_name"].iloc[0]
                team_win = bool(tdf["win"].max())
                result_html = result_badge(team_win)

                table_html = df_to_table(
                    tdf,
                    columns=[
                        "lane", "player_link", "champ_link", "result",
                        "score", "kda_str", "kpr", "gold", "level",
                        "gold_diff", "level_diff",
                    ],
                    col_rename={
                        "lane": "分路",
                        "player_link": "选手",
                        "champ_link": "英雄",
                        "result": "胜负",
                        "score": "评分",
                        "kda_str": "K/D/A",
                        "kpr": "参团率(%)",
                        "gold": "经济",
                        "level": "等级",
                        "gold_diff": "对位经济差",
                        "level_diff": "对位等级差",
                    },
                    table_id=f"tbl_match_team_{team_id}",
                    escape=False,
                )

                team_panels.append(f"""
                <div class="col-12 col-xl-6">
                  <div class="team-panel">
                    <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
                      <div>
                        <div class="team-title">{html_escape(str(team_name))}</div>
                        <div class="team-sub">队伍编号：{html_escape(str(team_id))}</div>
                      </div>
                      <div>{result_html}</div>
                    </div>
                    <div class="table-wrap">{table_html}</div>
                  </div>
                </div>
                """)

            summary_cards = [
                {"label": "对局ID", "value": match_id},
                {"label": "日期", "value": meta.get("date") or "-"},
                {"label": "场次", "value": meta.get("no") or "-"},
                {"label": "参赛人数", "value": len(dff)},
            ]

            content = build_section_card(
                title=f"对局详情：{match_id}",
                subtitle=subtitle,
                actions_html=f'<a class="btn btn-soft" href="{rel_href(current_dir, "players.html")}">返回选手榜</a>',
                body_html=f'<div class="row g-4">{"".join(team_panels)}</div>'
            )

            html = self.render_page(
                current_dir=current_dir,
                title=f"对局详情：{match_id}",
                active="players",
                page_desc="单局全员详情按两队分开展示，阅读更直观。",
                summary_cards=summary_cards,
                tabs=False,
                content=content,
            )
            self.write_file(match_path(match_id), html)

    def build_all(self):

        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(os.path.join(self.out_dir, "player"), exist_ok=True)
        os.makedirs(os.path.join(self.out_dir, "team"), exist_ok=True)
        os.makedirs(os.path.join(self.out_dir, "champion"), exist_ok=True)
        os.makedirs(os.path.join(self.out_dir, "match"), exist_ok=True)

        # 先生成所有页面
        self.build_players_page()
        self.build_player_detail_pages()

        self.build_champions_page()
        self.build_champion_detail_pages()

        self.build_teams_page()
        self.build_team_detail_pages()

        self.build_bans_page()
        self.build_match_detail_pages()

        # 最后生成每日战绩（覆盖 index.html）
        self.build_daily_results()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="./matches", help="对局JSON文件或目录（目录下所有 .json 会被读取）")
    parser.add_argument("--champion-json", default="champion.json", help="本地 champion.json 路径")
    parser.add_argument("--out", default="./docs", help="静态站点输出目录，适合 GitHub Pages")
    args = parser.parse_args()

    builder = StaticSiteBuilder(
        data_path=args.data,
        champion_json_path=args.champion_json,
        out_dir=args.out,
    )
    builder.build_all()

    print(f"静态站点已生成：{os.path.abspath(args.out)}")
    print("可本地预览：")
    print(f"  python -m http.server 8000 -d {args.out}")