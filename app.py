# -*- coding: utf-8 -*-
"""馬柱＆予想支援アプリ v4.3
Geminiは画像から事実抽出のみ。Pythonが固定ルール採点。
v4.3は用途別OCR、馬番統合、データ根拠率、欠損時の印保留を追加。
"""

import hashlib
import io
import json
import math
import os
import re
from copy import deepcopy
from datetime import datetime

import streamlit as st
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from google import genai
from google.genai import types

APP_VERSION = "4.3"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
TEMPERATURE = 0.0
SEED = 7
EXTRACTION_REVISION = "v4.3-r1"

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

BASE_SCHEMA = {
    "type": "object",
    "properties": {
        "race": {
            "type": "object",
            "properties": {
                "date": {"type": ["string", "null"]},
                "course": {"type": ["string", "null"]},
                "race_number": {"type": ["integer", "null"]},
                "surface": {"type": ["string", "null"]},
                "distance_m": {"type": ["integer", "null"]},
                "track_condition": {"type": ["string", "null"]},
                "class_name": {"type": ["string", "null"]},
                "field_size": {"type": ["integer", "null"]},
            },
            "required": ["date", "course", "race_number", "surface", "distance_m", "track_condition", "class_name", "field_size"],
        },
        "horses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "horse_number": {"type": ["integer", "null"]},
                    "frame_number": {"type": ["integer", "null"]},
                    "horse_name": {"type": ["string", "null"]},
                    "jockey_name": {"type": ["string", "null"]},
                    "running_style": {"type": ["string", "null"], "enum": ["逃げ", "先行", "差し", "追込", "不明", None]},
                    "days_since_last_race": {"type": ["integer", "null"]},
                    "body_weight": {"type": ["integer", "null"]},
                    "body_weight_change": {"type": ["integer", "null"]},
                },
                "required": ["horse_number", "frame_number", "horse_name", "jockey_name", "running_style", "days_since_last_race", "body_weight", "body_weight_change"],
            },
        },
    },
    "required": ["race", "horses"],
}

RACE_ROW_PROPERTIES = {
    "race_date": {"type": ["string", "null"]},
    "course": {"type": ["string", "null"]},
    "surface": {"type": ["string", "null"]},
    "distance_m": {"type": ["integer", "null"]},
    "track_condition": {"type": ["string", "null"]},
    "class_name": {"type": ["string", "null"]},
    "finish_position": {"type": ["integer", "null"]},
    "field_size": {"type": ["integer", "null"]},
    "time_diff_sec": {"type": ["number", "null"]},
    "body_weight": {"type": ["integer", "null"]},
    "note": {"type": ["string", "null"]},
}
PREVIOUS_RACES_SCHEMA = {
    "type": "object",
    "properties": {
        "horses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "horse_number": {"type": ["integer", "null"]},
                    "horse_name": {"type": ["string", "null"]},
                    "previous_races": {
                        "type": "array",
                        "items": {"type": "object", "properties": RACE_ROW_PROPERTIES, "required": list(RACE_ROW_PROPERTIES.keys())},
                    },
                },
                "required": ["horse_number", "horse_name", "previous_races"],
            },
        }
    },
    "required": ["horses"],
}

TRAINING_PROPERTIES = {
    "course": {"type": ["string", "null"]},
    "time_6f": {"type": ["number", "null"]},
    "time_5f": {"type": ["number", "null"]},
    "time_4f": {"type": ["number", "null"]},
    "time_3f": {"type": ["number", "null"]},
    "time_1f": {"type": ["number", "null"]},
    "final_3f": {"type": ["number", "null"]},
    "final_1f": {"type": ["number", "null"]},
    "training_comment": {"type": ["string", "null"]},
}
TRAINING_SCHEMA = {
    "type": "object",
    "properties": {
        "horses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "horse_number": {"type": ["integer", "null"]},
                    "horse_name": {"type": ["string", "null"]},
                    "training": {"type": "object", "properties": TRAINING_PROPERTIES, "required": list(TRAINING_PROPERTIES.keys())},
                },
                "required": ["horse_number", "horse_name", "training"],
            },
        }
    },
    "required": ["horses"],
}

EXTRA_SCHEMA = {
    "type": "object",
    "properties": {
        "horses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "horse_number": {"type": ["integer", "null"]},
                    "horse_name": {"type": ["string", "null"]},
                    "body_weight": {"type": ["integer", "null"]},
                    "body_weight_change": {"type": ["integer", "null"]},
                    "pedigree": {
                        "type": "object",
                        "properties": {
                            "sire": {"type": ["string", "null"]},
                            "dam_sire": {"type": ["string", "null"]},
                            "pedigree_note": {"type": ["string", "null"]},
                        },
                        "required": ["sire", "dam_sire", "pedigree_note"],
                    },
                    "jockey_course_record_text": {"type": ["string", "null"]},
                    "jockey_change_text": {"type": ["string", "null"]},
                    "odds": {"type": ["number", "null"]},
                },
                "required": ["horse_number", "horse_name", "body_weight", "body_weight_change", "pedigree", "jockey_course_record_text", "jockey_change_text", "odds"],
            },
        }
    },
    "required": ["horses"],
}

BASE_PROMPT = r"""
あなたは競馬新聞のOCR・データ入力担当です。採点・予想は禁止です。
レース基本情報と出走馬識別情報だけを抽出してください。
画像に書かれた内容だけを使い、推測・外部検索・一般知識による補完は禁止。
馬番を最優先し、枠番、馬名、騎手名、紙面に明記された脚質、現在馬体重・増減、休養日数を読む。
読めない項目は null。同じ馬を重複登録しない。予想印・人気・能力点は作らない。
"""

PREVIOUS_RACES_PROMPT = r"""
あなたは競馬新聞の「過去走欄だけ」を読むOCR担当です。採点・予想は禁止です。
各馬の前走・2走前・3走前…を最大8走、新しい順に抽出してください。
最重要: previous_races を安易に空配列にしないでください。
画像内に過去走が1行でも判読できれば、その馬との対応が確実な範囲で保存してください。
横方向・縦方向のレイアウトを注意深く追い、馬番を最優先、馬名を補助に対応付ける。
1行の一部しか読めなくても、対応が確実なら読めた項目だけ保存し、他は null。
調教時計を過去走と誤認しない。対応不明な行は無理に登録しない。画像にない情報を推測しない。
"""

TRAINING_PROMPT = r"""
あなたは競馬新聞の「調教・追い切り欄だけ」を読むOCR担当です。採点・予想は禁止です。
馬番・馬名で馬を対応付け、調教コース、6F/5F/4F/3F/1F、終い3F、終い1F、紙面コメントを保存。
同じ数値を意味の違う欄へ推測転記しない。読めない数字は null。評価・印・点数を作らない。
"""

EXTRA_PROMPT = r"""
あなたは競馬新聞の「血統・騎手情報・オッズ・馬体重等」を読むOCR担当です。採点・予想は禁止です。
馬番・馬名で対応付ける。父、母父、血統短評、騎手コース成績・勝率等の文字列、乗り替わり表示、オッズ、現在馬体重・増減を読む。
名前や知名度から能力を推測しない。読めない項目は null。
"""


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
    return re.sub(r"[\s　・･\-_]", "", str(v or "")).lower()


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


def extract_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    return json.loads(t)


def file_hash(f):
    h = hashlib.sha256()
    h.update(f.name.encode("utf-8", errors="ignore"))
    h.update(f.getvalue())
    return h.hexdigest()


def group_signature(groups):
    h = hashlib.sha256(EXTRACTION_REVISION.encode())
    for name, files in groups:
        h.update(name.encode())
        for f in files:
            h.update(file_hash(f).encode())
    return h.hexdigest()


def blank_training():
    return {k: None for k in TRAINING_PROPERTIES.keys()}


def blank_pedigree():
    return {"sire": None, "dam_sire": None, "pedigree_note": None}


def blank_horse(number=None, name=None):
    return {
        "horse_number": integer(number), "frame_number": None, "horse_name": name,
        "jockey_name": None, "running_style": "不明", "days_since_last_race": None,
        "body_weight": None, "body_weight_change": None, "training": blank_training(),
        "previous_races": [], "pedigree": blank_pedigree(),
        "jockey_course_record_text": None, "jockey_change_text": None, "odds": None,
    }


def parse_date_loose(v, reference_year=None):
    if not v:
        return None
    s = str(v).strip().replace("年", "/").replace("月", "/").replace("日", "")
    s = s.replace(".", "/").replace("-", "/")
    s = re.sub(r"\s+", "", s)
    for fmt in ("%Y/%m/%d", "%y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", s)
    if m and reference_year:
        try:
            return datetime(reference_year, int(m.group(1)), int(m.group(2)))
        except Exception:
            return None
    return None


def prepare_image_bytes(f, target_max=3600):
    img = Image.open(io.BytesIO(f.getvalue()))
    img = ImageOps.exif_transpose(img).convert("RGB")
    max_side = max(img.size)
    if max_side < 2200:
        scale = min(2.0, target_max / max_side)
    elif max_side > target_max:
        scale = target_max / max_side
    else:
        scale = 1.0
    if abs(scale - 1.0) > 0.01:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Sharpness(img).enhance(1.12)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=90, threshold=3))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, subsampling=0)
    return buf.getvalue(), "image/jpeg"


def image_part(f):
    b, mime = prepare_image_bytes(f)
    return types.Part.from_bytes(data=b, mime_type=mime)


def gemini_json(client, task_name, prompt, schema, files, identity_context="", force_reread=False):
    h = hashlib.sha256()
    for s in (EXTRACTION_REVISION, MODEL_NAME, task_name, prompt, identity_context):
        h.update(s.encode())
    for f in files:
        h.update(file_hash(f).encode())
    cache_key = f"ocr_{h.hexdigest()}"
    if not force_reread and cache_key in st.session_state:
        return deepcopy(st.session_state[cache_key])

    contents = [prompt]
    if identity_context:
        contents.append(identity_context)
    for i, f in enumerate(files, 1):
        contents.append(f"【対象画像 {i}: {f.name}】")
        contents.append(image_part(f))

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config={
            "temperature": TEMPERATURE,
            "seed": SEED,
            "response_mime_type": "application/json",
            "response_json_schema": schema,
        },
    )
    data = extract_json(response.text)
    st.session_state[cache_key] = deepcopy(data)
    return data


def horse_identity_context(horses):
    lines = [
        "以下は基本情報OCRで得た出走馬一覧です。",
        "画像内の馬は馬番を最優先、馬名を補助に対応付けてください。",
        "この一覧を能力評価には使わず、識別だけに使ってください。",
    ]
    for h in sorted(horses, key=lambda x: integer(x.get("horse_number")) or 999):
        lines.append(f'- 馬番 {integer(h.get("horse_number"))}: {h.get("horse_name") or "不明"}')
    return "\n".join(lines)


def ensure_horse_shape(h):
    base = blank_horse(h.get("horse_number"), h.get("horse_name"))
    for k in [
        "frame_number", "jockey_name", "running_style", "days_since_last_race",
        "body_weight", "body_weight_change", "jockey_course_record_text",
        "jockey_change_text", "odds",
    ]:
        if k in h:
            base[k] = h.get(k)
    base["horse_number"] = integer(h.get("horse_number"))
    base["frame_number"] = integer(h.get("frame_number"))
    base["running_style"] = clean_style(h.get("running_style"))
    if isinstance(h.get("training"), dict):
        base["training"].update(h["training"])
    if isinstance(h.get("pedigree"), dict):
        base["pedigree"].update(h["pedigree"])
    if isinstance(h.get("previous_races"), list):
        base["previous_races"] = h["previous_races"]
    return base


def find_horse(horses, partial):
    n = integer(partial.get("horse_number"))
    if n is not None:
        for h in horses:
            if integer(h.get("horse_number")) == n:
                return h
    name = normalize_text(partial.get("horse_name"))
    if name:
        for h in horses:
            if normalize_text(h.get("horse_name")) == name:
                return h
    return None


def merge_scalar_if_empty(target, source, key):
    new = source.get(key)
    if new in (None, ""):
        return
    if target.get(key) in (None, "", "不明"):
        target[key] = new


def race_row_key(r):
    return (
        normalize_text(r.get("race_date")), normalize_text(r.get("course")),
        integer(r.get("distance_m")), integer(r.get("finish_position")),
        normalize_text(r.get("class_name")),
    )


def merge_race_rows(existing, new_rows):
    merged, by_key = [], {}
    for src in list(existing or []) + list(new_rows or []):
        if not isinstance(src, dict):
            continue
        meaningful = [src.get("race_date"), src.get("course"), src.get("distance_m"), src.get("finish_position"), src.get("class_name"), src.get("note")]
        if not any(v not in (None, "") for v in meaningful):
            continue
        key = race_row_key(src)
        nonempty = sum(v not in (None, "", 0) for v in key)
        if nonempty < 2:
            merged.append(deepcopy(src))
            continue
        if key not in by_key:
            row = deepcopy(src)
            by_key[key] = row
            merged.append(row)
        else:
            row = by_key[key]
            for k, v in src.items():
                if row.get(k) in (None, "") and v not in (None, ""):
                    row[k] = v
    return merged


def sort_previous_races(rows, race):
    current = parse_date_loose(race.get("date"))
    ref_year = current.year if current else datetime.now().year
    decorated = []
    for idx, r in enumerate(rows):
        d = parse_date_loose(r.get("race_date"), ref_year)
        if current and d and d > current and len(str(r.get("race_date") or "")) <= 5:
            try:
                d = d.replace(year=d.year - 1)
            except Exception:
                pass
        decorated.append((d.timestamp() if d else float("-inf"), -idx, r))
    decorated.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, __, r in decorated][:8]


def training_completeness(t):
    if not isinstance(t, dict):
        return 0
    return sum(t.get(k) not in (None, "") for k in TRAINING_PROPERTIES.keys())


def choose_better_training(old, new):
    old = old if isinstance(old, dict) else blank_training()
    new = new if isinstance(new, dict) else blank_training()
    if training_completeness(new) > training_completeness(old):
        return deepcopy(new)
    if training_completeness(new) == training_completeness(old) and training_completeness(new) > 0:
        keys = ["time_6f", "time_5f", "time_4f", "time_3f", "time_1f", "final_3f", "final_1f"]
        if sum(num(new.get(k)) is not None for k in keys) > sum(num(old.get(k)) is not None for k in keys):
            return deepcopy(new)
    return deepcopy(old)


def merge_previous_pass(data, partial):
    for ph in partial.get("horses", []) or []:
        target = find_horse(data["horses"], ph)
        if target is not None:
            target["previous_races"] = merge_race_rows(target.get("previous_races") or [], ph.get("previous_races") or [])


def merge_training_pass(data, partial):
    for ph in partial.get("horses", []) or []:
        target = find_horse(data["horses"], ph)
        if target is not None:
            target["training"] = choose_better_training(target.get("training"), ph.get("training"))


def merge_extra_pass(data, partial):
    for ph in partial.get("horses", []) or []:
        target = find_horse(data["horses"], ph)
        if target is None:
            continue
        for k in ["body_weight", "body_weight_change", "jockey_course_record_text", "jockey_change_text", "odds"]:
            merge_scalar_if_empty(target, ph, k)
        src_p = ph.get("pedigree") or {}
        dst_p = target.setdefault("pedigree", blank_pedigree())
        for k in ["sire", "dam_sire", "pedigree_note"]:
            if dst_p.get(k) in (None, "") and src_p.get(k) not in (None, ""):
                dst_p[k] = src_p.get(k)


def infer_days_since_last_race(horse, race):
    if integer(horse.get("days_since_last_race")) is not None:
        return
    rows = horse.get("previous_races") or []
    if not rows:
        return
    current = parse_date_loose(race.get("date"))
    if not current:
        return
    prev = parse_date_loose(rows[0].get("race_date"), current.year)
    if not prev:
        return
    if prev > current and len(str(rows[0].get("race_date") or "")) <= 5:
        prev = prev.replace(year=prev.year - 1)
    days = (current - prev).days
    if 0 <= days <= 1500:
        horse["days_since_last_race"] = days


def finalize_extracted_data(data):
    race = data.get("race") or {}
    horses = [ensure_horse_shape(h) for h in (data.get("horses") or []) if isinstance(h, dict)]
    deduped, by_num, by_name = [], {}, {}
    for h in horses:
        n = integer(h.get("horse_number"))
        name = normalize_text(h.get("horse_name"))
        existing = by_num.get(n) if n is not None else None
        if existing is None and name:
            existing = by_name.get(name)
        if existing is None:
            deduped.append(h)
            if n is not None:
                by_num[n] = h
            if name:
                by_name[name] = h
            continue
        for k in ["frame_number", "horse_name", "jockey_name", "running_style", "days_since_last_race", "body_weight", "body_weight_change", "jockey_course_record_text", "jockey_change_text", "odds"]:
            merge_scalar_if_empty(existing, h, k)
        existing["training"] = choose_better_training(existing.get("training"), h.get("training"))
        existing["previous_races"] = merge_race_rows(existing.get("previous_races") or [], h.get("previous_races") or [])
        for k, v in (h.get("pedigree") or {}).items():
            if existing["pedigree"].get(k) in (None, "") and v not in (None, ""):
                existing["pedigree"][k] = v

    deduped.sort(key=lambda h: integer(h.get("horse_number")) or 999)
    for h in deduped:
        h["previous_races"] = sort_previous_races(merge_race_rows([], h.get("previous_races") or []), race)
        infer_days_since_last_race(h, race)
    if integer(race.get("field_size")) is None and deduped:
        race["field_size"] = len(deduped)
    return {"race": race, "horses": deduped}


def extract_all_data(client, whole_files, card_files, training_files, other_files, force_reread):
    whole_files, card_files = list(whole_files or []), list(card_files or [])
    training_files, other_files = list(training_files or []), list(other_files or [])
    base_sources = whole_files + card_files or training_files + other_files
    if not base_sources:
        raise ValueError("解析できる画像がありません。")

    previous_sources = card_files or whole_files
    training_sources = training_files or whole_files or card_files
    extra_sources = other_files or whole_files or card_files
    total_steps = 1 + len(previous_sources) + len(training_sources) + len(extra_sources)
    total_steps = max(total_steps, 1)
    progress = st.progress(0.0, text="v4.3: 基本情報を読み取っています…")
    completed = 0

    base = gemini_json(client, "base", BASE_PROMPT, BASE_SCHEMA, base_sources, force_reread=force_reread)
    data = {"race": base.get("race") or {}, "horses": [ensure_horse_shape(h) for h in (base.get("horses") or [])]}
    if not data["horses"]:
        raise ValueError("出走馬の基本情報を取得できませんでした。全体画像を確認してください。")
    completed += 1
    identity = horse_identity_context(data["horses"])

    for i, f in enumerate(previous_sources, 1):
        progress.progress(completed / total_steps, text=f"v4.3: 過去走専用OCR {i}/{len(previous_sources)}")
        partial = gemini_json(client, f"previous_{i}", PREVIOUS_RACES_PROMPT, PREVIOUS_RACES_SCHEMA, [f], identity, force_reread)
        merge_previous_pass(data, partial)
        completed += 1

    for i, f in enumerate(training_sources, 1):
        progress.progress(completed / total_steps, text=f"v4.3: 調教専用OCR {i}/{len(training_sources)}")
        partial = gemini_json(client, f"training_{i}", TRAINING_PROMPT, TRAINING_SCHEMA, [f], identity, force_reread)
        merge_training_pass(data, partial)
        completed += 1

    for i, f in enumerate(extra_sources, 1):
        progress.progress(completed / total_steps, text=f"v4.3: 血統・騎手・オッズ専用OCR {i}/{len(extra_sources)}")
        partial = gemini_json(client, f"extra_{i}", EXTRA_PROMPT, EXTRA_SCHEMA, [f], identity, force_reread)
        merge_extra_pass(data, partial)
        completed += 1

    progress.progress(1.0, text="v4.3: 抽出結果を整理しています…")
    data = finalize_extracted_data(data)
    progress.empty()
    meta = {
        "base_calls": 1,
        "previous_calls": len(previous_sources),
        "training_calls": len(training_sources),
        "extra_calls": len(extra_sources),
        "total_calls": 1 + len(previous_sources) + len(training_sources) + len(extra_sources),
    }
    return data, meta

# =========================================================
# 固定採点
# =========================================================

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
    # コース・距離別統計未導入のため中立。内枠=有利を固定しない。
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


def extraction_quality(data):
    horses = data.get("horses") or []
    return {
        "horses": len(horses),
        "previous_horses": sum(bool(h.get("previous_races")) for h in horses),
        "previous_rows": sum(len(h.get("previous_races") or []) for h in horses),
        "training_horses": sum(training_numeric_count(h) > 0 for h in horses),
        "pedigree_horses": sum(bool((h.get("pedigree") or {}).get("sire")) or bool((h.get("pedigree") or {}).get("dam_sire")) for h in horses),
        "style_horses": sum(clean_style(h.get("running_style")) in KNOWN_STYLES for h in horses),
        "odds_horses": sum(num(h.get("odds")) is not None for h in horses),
    }

# =========================================================
# UI
# =========================================================
st.set_page_config(page_title=f"馬柱＆予想支援 v{APP_VERSION}", layout="wide")
st.title(f"🏇 馬柱 ＆ 予想支援アプリ v{APP_VERSION}")
st.caption("v4.3: 過去走・調教・その他を用途別に複数回OCRし、馬番で統合してから固定ルール採点します。")

try:
    api_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    api_key = st.sidebar.text_input("Gemini APIキー", type="password")

st.sidebar.subheader("v4.3 撮影のコツ")
st.sidebar.write("・全体画像: 馬番・馬名・枠番が分かる写真")
st.sidebar.write("・近走欄: 前走～数走前が読める大きさで拡大")
st.sidebar.write("・調教欄: 時計と馬名/馬番が同時に見える拡大")
st.sidebar.write("・その他: 血統、騎手成績、オッズ等を拡大")
st.sidebar.write("・画像同士を少し重複させると照合が安定")
st.sidebar.divider()
st.sidebar.subheader("100点配分")
for k, v in WEIGHTS.items():
    st.sidebar.write(f"{k}: {v:g}点")
st.sidebar.divider()
st.sidebar.caption(f"Gemini model: {MODEL_NAME}")

st.subheader("📷 画像登録")
st.info("v4.3では『② 馬柱・近走成績の拡大画像』が特に重要です。過去走が取れない馬には原則として印を付けません。")

whole_files = st.file_uploader(
    "① 全体画像（レース全体・馬番確認用）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="whole_files_v43",
)
card_files = st.file_uploader(
    "② 馬柱・近走成績の拡大画像（最重要・複数可）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="card_files_v43",
)
training_files = st.file_uploader(
    "③ 調教・追い切り欄の拡大画像（複数可）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="training_files_v43",
)
other_files = st.file_uploader(
    "④ オッズ・血統・騎手情報・その他の拡大画像（複数可）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="other_files_v43",
)

grouped_files = [
    ("全体画像", whole_files or []),
    ("馬柱・近走成績", card_files or []),
    ("調教・追い切り", training_files or []),
    ("その他", other_files or []),
]
all_files = [f for _, fs in grouped_files for f in fs]

if all_files:
    st.write(f"登録画像: **{len(all_files)}枚**")
    with st.expander("画像プレビュー", expanded=False):
        cols = st.columns(3)
        n = 0
        for group_name, fs in grouped_files:
            for f in fs:
                with cols[n % 3]:
                    st.image(f, caption=f"{group_name} / {f.name}", use_container_width=True)
                n += 1

force_reread = st.checkbox(
    "同じ画像でもGeminiに再読取させる",
    value=False,
    help="通常はOFF推奨。ONにすると用途別OCRをすべて再実行します。",
)

run = st.button(
    "🔍 v4.3 高精度解析 → 固定ルール採点",
    type="primary",
    disabled=not (api_key and all_files),
)

if run:
    try:
        client = genai.Client(api_key=api_key)
        sig = group_signature(grouped_files)
        final_cache_key = f"v43_final_{sig}"

        if not force_reread and final_cache_key in st.session_state:
            stored = deepcopy(st.session_state[final_cache_key])
            data, meta = stored["data"], stored["meta"]
            st.info("同じ画像セットのv4.3統合データを再利用しました。Geminiの再読取はしていません。")
        else:
            data, meta = extract_all_data(
                client,
                whole_files or [],
                card_files or [],
                training_files or [],
                other_files or [],
                force_reread,
            )
            st.session_state[final_cache_key] = {"data": deepcopy(data), "meta": deepcopy(meta)}

        horses = data.get("horses") or []
        if not horses:
            st.error("馬データを取得できませんでした。")
            st.stop()

        quality = extraction_quality(data)
        st.subheader("✅ 抽出品質チェック")
        qcols = st.columns(4)
        qcols[0].metric("出走馬", f'{quality["horses"]}頭')
        qcols[1].metric("過去走取得", f'{quality["previous_horses"]}/{quality["horses"]}頭')
        qcols[2].metric("調教取得", f'{quality["training_horses"]}/{quality["horses"]}頭')
        qcols[3].metric("過去走行数", f'{quality["previous_rows"]}行')
        st.caption(
            f'Gemini呼び出し: 基本 {meta["base_calls"]}回 / 過去走 {meta["previous_calls"]}回 / '
            f'調教 {meta["training_calls"]}回 / その他 {meta["extra_calls"]}回'
        )

        previous_ratio = quality["previous_horses"] / quality["horses"] if quality["horses"] else 0
        if previous_ratio < 0.5:
            st.error(
                "⚠ 過去走を取得できた馬が半数未満です。この状態では能力・適性の根拠が不足するため、"
                "印と参考勝率を原則保留します。②馬柱・近走成績の拡大画像を追加してください。"
            )
        elif previous_ratio < 0.8:
            st.warning("⚠ 過去走が不足している馬があります。ランキングは参考値として確認してください。")
        else:
            st.success("過去走の取得率は良好です。固定ルール採点へ進みます。")

        results = score_all(data)
        st.subheader("📊 総合ランキング")
        table = []
        for r in results:
            table.append({
                "順位": r["rank"], "印": r["mark"] or "—", "馬番": r["horse_number"], "馬名": r["horse_name"],
                "総合": r["total"], "評価": rank_label(r["total"]), "根拠率": f'{r["coverage_pct"]:.0f}%',
                "信頼度": r["reliability"], "近走数": r["previous_count"], "能力": r["ability"],
                "適性": r["suitability"], "調教": r["training"], "展開": r["pace"], "騎手": r["jockey"],
                "血統": r["pedigree"], "枠順": r["gate"], "オッズ": r["odds"],
                "参考勝率": r["model_win_prob"], "参考期待値": r["expected_value"],
            })
        st.dataframe(table, use_container_width=True, hide_index=True)

        st.subheader("🎯 予想の見方")
        st.write("◎○▲☆△◇は総合点から機械的に決定します。ただし、過去走0件または根拠率55%未満の馬には印を付けません。")
        st.write("血統は父名が読めただけでは加点せず、枠順もコース別統計が無い現段階では中立点です。")
        st.write("参考勝率は、十分なデータがある馬が全体の80%以上いる場合だけ表示します。")

        st.subheader("🔎 馬ごとの詳細")
        for r in results:
            mark_text = r["mark"] if r["mark"] else "印保留"
            with st.expander(f'{r["rank"]}位 {mark_text} {r["horse_name"]} — {r["total"]}点 / 根拠率 {r["coverage_pct"]:.0f}%'):
                st.write({
                    "馬番": r["horse_number"], "騎手名": r["jockey_name"], "能力・近走": r["ability"],
                    "コース・距離・馬場適性": r["suitability"], "調教・状態": r["training"],
                    "脚質・展開": r["pace"], "騎手": r["jockey"], "血統": r["pedigree"], "枠順": r["gate"],
                    "過去走取得数": r["previous_count"], "根拠率": r["coverage_pct"], "信頼度": r["reliability"],
                    "不足データ": r["missing"], "参考勝率": r["model_win_prob"], "参考期待値": r["expected_value"],
                })

        st.subheader("🧾 抽出された生データ")
        st.info("採点結果より先に生データを確認してください。特に previous_races が入っているかが最重要です。")
        st.json(data)

        st.download_button(
            "生データJSONを保存",
            json.dumps(data, ensure_ascii=False, indent=2),
            "race_extracted_data_v4_3.json",
            "application/json",
        )
        st.download_button(
            "採点結果JSONを保存",
            json.dumps(results, ensure_ascii=False, indent=2),
            "race_scored_results_v4_3.json",
            "application/json",
        )

    except Exception as e:
        st.error(f"エラーが発生しました: {e}")
        st.exception(e)
else:
    st.info("全体画像に加えて、②馬柱・近走成績の拡大画像をできるだけ登録してください。")
