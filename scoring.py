# -*- coding: utf-8 -*-
"""Fixed scoring rules extracted unchanged from app.py v5.0."""

import math
import re

WEIGHTS = {
    "能力・近走": 20.0,
    "コース・距離・馬場適性": 18.0,
    "調教・状態": 17.0,
    "脚質・展開": 15.0,
    "騎手": 12.0,
    "血統": 10.0,
    "枠順": 8.0,
}

KNOWN_STYLES = {"逃げ", "先行", "差し", "追込"}

def num(v):
    try:
        return None if v is None or v == "" else float(v)
    except Exception:
        return None

def integer(v):
    try:
        return None if v is None or v == "" else int(float(v))
    except Exception:
        return None

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def normalize_text(v):
    return re.sub(r"[\s ・･\-_]", "", str(v or "")).lower()

def clean_style(v):
    s = str(v or "").strip()
    return s if s in KNOWN_STYLES else "不明"

def percentile(values, value, higher_is_better=True):
    vals = [x for x in values if x is not None]
    if value is None or not vals or len(vals) == 1:
        return 50.0
    if not higher_is_better:
        value, vals = -value, [-x for x in vals]
    below = sum(x < value for x in vals)
    equal = sum(x == value for x in vals)
    return 100.0 * (below + 0.5 * equal) / len(vals)

def race_relevance(r, race):
    score = 0
    if race.get("course") and r.get("course") and normalize_text(r.get("course")) == normalize_text(race.get("course")):
        score += 3
    d, target = integer(r.get("distance_m")), integer(race.get("distance_m"))
    if d is not None and target is not None:
        diff = abs(d - target)
        score += 3 if diff == 0 else 2 if diff <= 200 else 1 if diff <= 400 else 0
    if race.get("surface") and r.get("surface") and normalize_text(r.get("surface")) == normalize_text(race.get("surface")):
        score += 2
    if race.get("track_condition") and r.get("track_condition") and normalize_text(r.get("track_condition")) == normalize_text(race.get("track_condition")):
        score += 1
    return score

def result_quality(r):
    pos, n, diff = integer(r.get("finish_position")), integer(r.get("field_size")), num(r.get("time_diff_sec"))
    if pos is None or pos <= 0:
        return None
    if pos == 1:
        s = 1.00
    elif pos == 2:
        s = 0.92
    elif pos == 3:
        s = 0.86
    elif pos <= 5:
        s = 0.76
    elif pos <= 8:
        s = 0.63
    elif pos <= 12:
        s = 0.48
    else:
        s = 0.30
    if diff is not None:
        if diff <= 0.2:
            s += 0.08
        elif diff <= 0.5:
            s += 0.04
        elif diff >= 1.5:
            s -= 0.08
    if n and n >= 14 and pos <= 5:
        s += 0.04
    return clamp(s, 0.0, 1.0)

def score_ability(horse, race):
    usable = []
    for r in (horse.get("previous_races") or [])[:8]:
        q = result_quality(r)
        if q is not None:
            usable.append((r, q))
    if not usable:
        return 10.0
    weighted = []
    for i, (r, q) in enumerate(usable):
        recency_w = max(1.0, 5.0 - i * 0.55)
        relevance_w = 1.0 + 0.08 * race_relevance(r, race)
        weighted.append((q, recency_w * relevance_w))
    avg = sum(q * w for q, w in weighted) / sum(w for _, w in weighted)
    score = avg * 20.0
    days = integer(horse.get("days_since_last_race"))
    if days is not None:
        if days >= 300:
            score *= 0.95
        elif days >= 180:
            score *= 0.98
    return round(clamp(score, 0, 20), 1)

def score_suitability(horse, race):
    races = horse.get("previous_races") or []
    if not races:
        return 9.0
    course_q, dist_q, surf_q, cond_q = [], [], [], []
    target_d = integer(race.get("distance_m"))
    for r in races:
        q = result_quality(r)
        if q is None:
            continue
        if race.get("course") and r.get("course") and normalize_text(r.get("course")) == normalize_text(race.get("course")):
            course_q.append(q)
        rd = integer(r.get("distance_m"))
        if target_d is not None and rd is not None and abs(rd - target_d) <= 200:
            dist_q.append(q)
        if race.get("surface") and r.get("surface") and normalize_text(r.get("surface")) == normalize_text(race.get("surface")):
            surf_q.append(q)
        if race.get("track_condition") and r.get("track_condition") and normalize_text(r.get("track_condition")) == normalize_text(race.get("track_condition")):
            cond_q.append(q)
    part = lambda a: sum(a) / len(a) if a else 0.5
    score = part(course_q) * 5 + part(dist_q) * 5 + part(surf_q) * 4 + part(cond_q) * 4
    return round(clamp(score, 0, 18), 1)

def normalize_training_course(v):
    s = str(v or "").strip()
    if not s:
        return "不明"
    u = s.upper()
    if "栗" in s and "坂" in s:
        return "栗坂"
    if "美" in s and "坂" in s:
        return "美坂"
    if "南" in s and "W" in u:
        return "南W"
    if "CW" in u or ("栗" in s and "W" in u):
        return "栗CW"
    if "坂" in s:
        return "坂路"
    if "W" in u:
        return "W"
    return normalize_text(s) or "不明"

def training_metric_value(t, key):
    if key == "final_3f":
        return num(t.get("final_3f")) if num(t.get("final_3f")) is not None else num(t.get("time_3f"))
    if key == "final_1f":
        return num(t.get("final_1f")) if num(t.get("final_1f")) is not None else num(t.get("time_1f"))
    return num(t.get(key))

def score_training_clock_all(horses):
    metric_weights = {"time_5f": 0.9, "time_4f": 1.1, "final_3f": 1.0, "final_1f": 1.4}
    per_horse = {id(h): [] for h in horses}
    for metric, weight in metric_weights.items():
        by_course = {}
        for h in horses:
            t = h.get("training") or {}
            v = training_metric_value(t, metric)
            if v is None:
                continue
            course = normalize_training_course(t.get("course"))
            by_course.setdefault(course, []).append((h, v))
        for _, items in by_course.items():
            raw = [v for _, v in items]
            if len(items) == 1:
                h, _v = items[0]
                per_horse[id(h)].append((50.0, weight))
            else:
                for h, v in items:
                    per_horse[id(h)].append((percentile(raw, v, higher_is_better=False), weight))
    out = {}
    for h in horses:
        vals = per_horse[id(h)]
        if not vals:
            out[id(h)] = 7.0
        else:
            p = sum(p * w for p, w in vals) / sum(w for _, w in vals)
            out[id(h)] = round(clamp(14.0 * p / 100.0, 0, 14), 1)
    return out

def training_comment_adjustment(text):
    s = str(text or "")
    positive = ["好調", "動き良", "動き軽", "伸び鋭", "余力十分", "好仕上", "仕上る", "仕上が", "順調", "力強", "活気", "高いレベル", "安定"]
    negative = ["重い", "反応鈍", "平凡", "物足", "一息", "遅れ", "不安", "まだ", "低調"]
    pos, neg = sum(w in s for w in positive), sum(w in s for w in negative)
    return 0.6 if pos > neg else -0.6 if neg > pos else 0.0

def score_condition_3pt(horse):
    score = 1.5
    change = integer(horse.get("body_weight_change"))
    if change is not None:
        if -4 <= change <= 6:
            score += 0.3
        elif change < -10 or change > 14:
            score -= 0.4
    score += training_comment_adjustment((horse.get("training") or {}).get("training_comment"))
    return round(clamp(score, 0, 3), 1)

def score_pace(horse, horses):
    styles = [clean_style(h.get("running_style")) for h in horses]
    style = clean_style(horse.get("running_style"))
    if style not in KNOWN_STYLES:
        return 7.5
    escape, front = styles.count("逃げ"), styles.count("先行")
    fast_front = escape + front
    if style == "逃げ":
        return 5.5 if escape >= 2 else 11.5
    if style == "先行":
        return 8.0 if escape >= 2 else 10.5
    if style == "差し":
        return 10.5 if escape >= 2 or fast_front >= 5 else 8.5
    if style == "追込":
        return 10.0 if escape >= 2 or fast_front >= 6 else 7.0
    return 7.5

def extract_rate(text, label):
    m = re.search(rf"{label}\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None

def jockey_evidence(horse):
    text = " ".join([horse.get("jockey_course_record_text") or "", horse.get("jockey_change_text") or ""])
    return bool(re.search(r"\d+(?:\.\d+)?\s*%", text))

def score_jockey(horse):
    text = " ".join([horse.get("jockey_course_record_text") or "", horse.get("jockey_change_text") or ""])
    if not text:
        return 6.0
    win, place, quinella = extract_rate(text, "勝率"), extract_rate(text, "複勝率"), extract_rate(text, "連対率")
    if win is not None:
        return 10.5 if win >= 25 else 9.5 if win >= 18 else 8.0 if win >= 12 else 6.5 if win >= 8 else 5.5 if win >= 4 else 4.5
    if place is not None:
        return 10.0 if place >= 50 else 9.0 if place >= 40 else 7.5 if place >= 30 else 6.0 if place >= 20 else 5.0
    if quinella is not None:
        return 9.5 if quinella >= 35 else 8.0 if quinella >= 25 else 6.5 if quinella >= 15 else 5.0
    return 6.0

def score_pedigree(horse):
    note = str((horse.get("pedigree") or {}).get("pedigree_note") or "")
    score = 5.0
    if any(w in note for w in ["適性", "得意", "向く", "好相性", "道悪○", "距離○"]):
        score += 1.0
    if any(w in note for w in ["不向き", "苦手", "割引", "距離不安", "道悪×"]):
        score -= 1.0
    return round(clamp(score, 0, 10), 1)

def score_gate(_horse):
    return 4.0

def training_numeric_count(horse):
    t = horse.get("training") or {}
    vals = [num(t.get("time_5f")), num(t.get("time_4f")), training_metric_value(t, "final_3f"), training_metric_value(t, "final_1f")]
    return sum(v is not None for v in vals)

def previous_usable_count(horse):
    return sum(result_quality(r) is not None for r in (horse.get("previous_races") or []))

def data_coverage(horse):
    prev_n, train_n = previous_usable_count(horse), training_numeric_count(horse)
    evidence, missing = 0.0, []
    if prev_n >= 3:
        evidence += 38
    elif prev_n == 2:
        evidence += 30
    elif prev_n == 1:
        evidence += 19
    else:
        missing.append("過去走")
    if train_n >= 3:
        evidence += 17
    elif train_n == 2:
        evidence += 13
    elif train_n == 1:
        evidence += 8
    else:
        missing.append("調教")
    if clean_style(horse.get("running_style")) in KNOWN_STYLES:
        evidence += 15
    else:
        missing.append("脚質")
    if jockey_evidence(horse):
        evidence += 12
    else:
        missing.append("騎手成績")
    p = horse.get("pedigree") or {}
    sire, dam_sire = bool(p.get("sire")), bool(p.get("dam_sire"))
    if sire and dam_sire:
        evidence += 10
    elif sire or dam_sire:
        evidence += 5
    else:
        missing.append("血統")
    if integer(horse.get("frame_number")) is not None:
        evidence += 8
    else:
        missing.append("枠順")
    pct = round(clamp(evidence, 0, 100), 1)
    reliability = "高" if pct >= 80 and prev_n >= 3 else "中" if pct >= 55 and prev_n >= 1 else "低"
    return pct, reliability, missing

def rank_label(x):
    return "S" if x >= 85 else "A+" if x >= 80 else "A" if x >= 75 else "B+" if x >= 70 else "B" if x >= 65 else "C+" if x >= 60 else "C" if x >= 55 else "D"

def add_marks(results):
    marks = ["◎", "○", "▲", "☆", "△", "◇"]
    eligible = [r for r in results if r.get("previous_count", 0) >= 1 and r.get("coverage_pct", 0) >= 55]
    for r in results:
        r["mark"] = ""
    for mark, r in zip(marks, eligible):
        r["mark"] = mark

def estimate_win_probability(results):
    if not results:
        return False
    eligible_count = sum(r.get("previous_count", 0) >= 1 and r.get("coverage_pct", 0) >= 55 for r in results)
    if eligible_count / len(results) < 0.8:
        for r in results:
            r["model_win_prob"] = None
        return False
    max_score = max(r["total"] for r in results)
    exps = [math.exp((r["total"] - max_score) / 5.0) for r in results]
    s = sum(exps)
    for r, e in zip(results, exps):
        r["model_win_prob"] = round(100.0 * e / s, 1)
    return True

def calculate_expected_value(results):
    for r in results:
        odds, p = num(r.get("odds")), num(r.get("model_win_prob"))
        r["expected_value"] = round((p / 100.0) * odds, 2) if odds is not None and odds > 0 and p is not None else None

def score_all(data):
    race, horses = data.get("race") or {}, data.get("horses") or []
    training_clock = score_training_clock_all(horses)
    results = []
    for h in horses:
        ability = score_ability(h, race)
        suitability = score_suitability(h, race)
        training = round(clamp(training_clock.get(id(h), 7.0) + score_condition_3pt(h), 0, 17), 1)
        pace, jockey, pedigree, gate = score_pace(h, horses), score_jockey(h), score_pedigree(h), score_gate(h)
        total = round(ability + suitability + training + pace + jockey + pedigree + gate, 1)
        coverage_pct, reliability, missing = data_coverage(h)
        results.append({
            "horse_number": integer(h.get("horse_number")), "frame_number": integer(h.get("frame_number")),
            "horse_name": h.get("horse_name") or "不明", "jockey_name": h.get("jockey_name") or "不明",
            "odds": num(h.get("odds")), "ability": ability, "suitability": suitability, "training": training,
            "pace": round(pace, 1), "jockey": round(jockey, 1), "pedigree": round(pedigree, 1), "gate": round(gate, 1),
            "total": total, "previous_count": previous_usable_count(h), "coverage_pct": coverage_pct,
            "reliability": reliability, "missing": missing,
        })
    results.sort(key=lambda x: (-x["total"], -x["coverage_pct"], x["odds"] if x["odds"] is not None else 999999, x["horse_number"] if x["horse_number"] is not None else 999))
    for i, r in enumerate(results, 1):
        r["rank"] = i
    add_marks(results)
    estimate_win_probability(results)
    calculate_expected_value(results)
    return results
