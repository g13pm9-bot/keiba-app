# -*- coding: utf-8 -*-
"""馬柱＆予想支援アプリ v5.0
OCRは画像から事実抽出のみ。Pythonが固定ルール採点。
v5.0は予想結果のCSV保存と履歴表示機能を追加。
"""

import hashlib
import io
import json
import os
import re
from copy import deepcopy
from datetime import datetime

import streamlit as st
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import ocr_gemini

# v5追加: 履歴管理モジュールのインポート
import history 
import recovery
from scoring import (
    WEIGHTS,
    KNOWN_STYLES,
    num,
    integer,
    clamp,
    normalize_text,
    clean_style,
    percentile,
    race_relevance,
    result_quality,
    score_ability,
    score_suitability,
    normalize_training_course,
    training_metric_value,
    score_training_clock_all,
    training_comment_adjustment,
    score_condition_3pt,
    score_pace,
    extract_rate,
    jockey_evidence,
    score_jockey,
    score_pedigree,
    score_gate,
    training_numeric_count,
    previous_usable_count,
    data_coverage,
    rank_label,
    add_marks,
    estimate_win_probability,
    calculate_expected_value,
    score_all,
)

APP_VERSION = "5.0"
EXTRACTION_REVISION = "v5.0-r2"


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
    h.update(json.dumps([provider, MODEL_NAME], ensure_ascii=False).encode())
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


def ocr_json(client, task_name, prompt, schema, files, identity_context="", force_reread=False):
    h = hashlib.sha256()
    h.update(json.dumps([provider, MODEL_NAME], ensure_ascii=False).encode())
    for s in (EXTRACTION_REVISION, MODEL_NAME, task_name, prompt, identity_context):
        h.update(s.encode())
    for f in files:
        h.update(file_hash(f).encode())
    cache_key = f"ocr_{h.hexdigest()}"
    if not force_reread and cache_key in st.session_state:
        return deepcopy(st.session_state[cache_key])

    response_text = request_json(
        client, prompt, schema,
        ((f.name, *prepare_image_bytes(f)) for f in files),
        identity_context,
    )
    data = extract_json(response_text)
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
    merged = []
    for src in list(existing or []) + list(new_rows or []):
        if not isinstance(src, dict):
            continue
        meaningful = [src.get("race_date"), src.get("course"), src.get("distance_m"), src.get("finish_position"), src.get("class_name"), src.get("note")]
        if not any(v not in (None, "") for v in meaningful):
            continue
        matches = [row for row in merged if same_previous_race(row, src)]
        if len(matches) == 1:
            row = matches[0]
            for k, v in src.items():
                if row.get(k) in (None, "") and v not in (None, ""):
                    row[k] = v
        else:
            merged.append(deepcopy(src))
    return merged


def same_previous_race(left, right):
    # 同じ馬の同じ開催日を軸に照合する。欠損項目は相違と扱わない。
    dates = [parse_date_loose(row.get("race_date")) for row in (left, right)]
    raw_dates = [normalize_text(row.get("race_date")) for row in (left, right)]
    if not all(raw_dates):
        return False
    if all(dates):
        if dates[0].date() != dates[1].date():
            return False
    elif raw_dates[0] != raw_dates[1]:
        return False
    # 開催場所・距離などが明確に矛盾する行は勝手に統合しない。
    for key in ("course", "distance_m", "surface", "finish_position", "class_name"):
        a, b = left.get(key), right.get(key)
        if a in (None, "") or b in (None, ""):
            continue
        convert = integer if key in ("distance_m", "finish_position") else normalize_text
        if convert(a) != convert(b):
            return False
    return True

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
    if not training_completeness(old):
        return deepcopy(new)
    combined = deepcopy(old)
    old_course, new_course = old.get("course"), new.get("course")
    # 別のコースや矛盾する時計を混ぜない。既存データは保持する。
    if not old_course or not new_course:
        return combined
    if normalize_training_course(old_course) != normalize_training_course(new_course):
        return combined
    keys = ["time_6f", "time_5f", "time_4f", "final_3f", "final_1f"]
    for key in keys:
        a, b = training_metric_value(old, key), training_metric_value(new, key)
        if a is not None and b is not None and a != b:
            return combined
    for key, value in new.items():
        if combined.get(key) in (None, "") and value not in (None, ""):
            combined[key] = deepcopy(value)
    return combined

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
    progress = st.progress(0.0, text=f"v{APP_VERSION}: 基本情報を読み取っています…")
    completed = 0

    base = ocr_json(client, "base", BASE_PROMPT, BASE_SCHEMA, base_sources, force_reread=force_reread)
    data = {"race": base.get("race") or {}, "horses": [ensure_horse_shape(h) for h in (base.get("horses") or [])]}
    if not data["horses"]:
        raise ValueError("出走馬の基本情報を取得できませんでした。全体画像を確認してください。")
    completed += 1
    identity = horse_identity_context(data["horses"])

    for i, f in enumerate(previous_sources, 1):
        progress.progress(completed / total_steps, text=f"v{APP_VERSION}: 過去走専用OCR {i}/{len(previous_sources)}")
        partial = ocr_json(client, f"previous_{i}", PREVIOUS_RACES_PROMPT, PREVIOUS_RACES_SCHEMA, [f], identity, force_reread)
        merge_previous_pass(data, partial)
        completed += 1

    for i, f in enumerate(training_sources, 1):
        progress.progress(completed / total_steps, text=f"v{APP_VERSION}: 調教専用OCR {i}/{len(training_sources)}")
        partial = ocr_json(client, f"training_{i}", TRAINING_PROMPT, TRAINING_SCHEMA, [f], identity, force_reread)
        merge_training_pass(data, partial)
        completed += 1

    for i, f in enumerate(extra_sources, 1):
        progress.progress(completed / total_steps, text=f"v{APP_VERSION}: 血統・騎手・オッズ専用OCR {i}/{len(extra_sources)}")
        partial = ocr_json(client, f"extra_{i}", EXTRA_PROMPT, EXTRA_SCHEMA, [f], identity, force_reread)
        merge_extra_pass(data, partial)
        completed += 1

    progress.progress(1.0, text=f"v{APP_VERSION}: 抽出結果を整理しています…")
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

def jvlink_race_target(data):
    """OCRに明確な年月日・中央競馬場・R番号がある場合だけ取得対象を返す。"""
    race = data.get("race") or {}
    date_text = race.get("race_date") or race.get("date")
    if not isinstance(date_text, str):
        return None
    date = None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            if len(date_text.strip()) in (8, 10):
                date = datetime.strptime(date_text.strip(), fmt).strftime("%Y%m%d")
                break
        except ValueError:
            pass
    courses = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
               "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}
    course_value = race.get("course")
    if type(course_value) not in (str, int):
        return None
    course_text = str(course_value).strip()
    course = courses.get(course_text.zfill(2), course_text)
    number = race.get("race_number")
    if type(number) is int:
        race_number = number
    elif isinstance(number, str) and number.strip().isascii() and number.strip().isdecimal():
        race_number = int(number.strip())
    else:
        return None
    if date is None or course not in courses.values() or not 1 <= race_number <= 12:
        return None
    return date, course, race_number


def show_jvlink_comparison(data, signature):
    """照合結果を保存・表示する。OCR原本は変更しない。"""
    st.subheader("🏇 JRA-VAN出馬表との照合")
    st.caption("安全条件をすべて満たす照合結果だけ採点に反映します。OCR原本は保持します。")
    cache_key = f"jvlink_comparison_{signature}"
    fingerprint = hashlib.sha256(json.dumps(
        data, ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")).hexdigest()
    cached = st.session_state.get(cache_key)
    if cached is not None and cached.get("ocr_fingerprint") != fingerprint:
        # 再OCRや追加写真でOCR内容が変わったら、古い照合結果を表示しない。
        st.session_state.pop(cache_key, None)
        cached = None
    target = jvlink_race_target(data)
    if target is None:
        st.info("照合には、OCR結果の年を含むレース日・競馬場・レース番号が必要です。")
        return
    label = "JRA-VAN再取得" if cached is not None else "JRA-VANで照合"
    if st.button(label, key=f"jvlink_comparison_button_{signature}"):
        cached = {"ocr_fingerprint": fingerprint, "target": target,
                  "external_data": None, "reconciliation": None, "error": None}
        st.session_state[cache_key] = cached
        try:
            # ボタン押下時だけimport・COM取得。通常の画面再描画では通信しない。
            from racecard_jvlink import fetch_racecard
            from reconciliation import reconcile_racecard

            with st.spinner("JRA-VAN出馬表を取得して照合しています…"):
                external_data = fetch_racecard(*target)
                cached["external_data"] = deepcopy(external_data)
                cached["reconciliation"] = reconcile_racecard(
                    deepcopy(data), deepcopy(external_data), auto_correct_by_horse_number=True,
                )
        except Exception as exc:
            cached["error"] = f"{type(exc).__name__}: {exc}"
        st.session_state[cache_key] = cached
    if cached is None:
        return
    if cached["error"]:
        st.error(f"JRA-VAN照合に失敗しました: {cached['error']}")
        return
    result = cached["reconciliation"]
    if result is None:
        return
    auto = result["auto_correction"]
    st.write({"auto_correction.requested": auto["requested"],
              "auto_correction.enabled": auto["enabled"],
              "auto_correction.reason": auto["reason"],
              "ready_for_scoring": result["ready_for_scoring"]})
    if auto["enabled"]:
        st.success("馬番が全頭確認できたため、JRA-VAN値で補正可能です")
    else:
        st.warning("自動補正は無効です。確認が必要です")
    comparison_rows = []
    for row in result["reconciliations"]:
        fields = row["fields"]
        number = fields["horse_number"]
        display_number = next((number[key] for key in ("final_value", "ocr_value", "external_value")
                               if number[key] is not None), None)
        comparison_rows.append({
            "horse_number": display_number,
            "OCR馬名": fields["horse_name"]["ocr_value"],
            "JRA-VAN馬名": fields["horse_name"]["external_value"],
            "確定馬名": fields["horse_name"]["final_value"],
            "OCR騎手名": fields["jockey_name"]["ocr_value"],
            "JRA-VAN騎手名": fields["jockey_name"]["external_value"],
            "確定騎手名": fields["jockey_name"]["final_value"],
            "OCR枠番": fields["frame_number"]["ocr_value"],
            "JRA-VAN枠番": fields["frame_number"]["external_value"],
            "確定枠番": fields["frame_number"]["final_value"],
            "status": row["status"],
        })
    st.dataframe(comparison_rows, use_container_width=True, hide_index=True)
    with st.expander("照合理由・確認待ち項目", expanded=False):
        st.json({"安全条件の不成立理由": auto.get("failures", []),
                 "確認待ち": result["pending_review"],
                 "行ごとの理由": [{"行ID": row["row_id"], "理由": row["reasons"]}
                                  for row in result["reconciliations"] if row["reasons"]]})


def jvlink_scoring_data(data, signature):
    """キャッシュを検証し、4識別項目だけを差し替えた採点用コピーを作る。

    戻り値: (採点データ, 補正使用フラグ, 照合キャッシュ有無)。
    通信・再照合は行わず、欠損・不整合・例外では必ずOCR原本へ戻る。
    """
    cached = st.session_state.get(f"jvlink_comparison_{signature}")
    if cached is None:
        return data, False, False
    try:
        fingerprint = hashlib.sha256(json.dumps(
            data, ensure_ascii=False, sort_keys=True, default=str,
        ).encode("utf-8")).hexdigest()
        if cached.get("ocr_fingerprint") != fingerprint or cached.get("error"):
            return data, False, True
        if cached.get("target") != jvlink_race_target(data):
            return data, False, True
        result = cached.get("reconciliation")
        if not isinstance(result, dict):
            return data, False, True
        if (result.get("auto_correction", {}).get("enabled") is not True
                or result.get("ready_for_scoring") is not True
                or result.get("pending_review") != []):
            return data, False, True
        ocr_horses = data.get("horses")
        final_horses = result.get("horses")
        if (not isinstance(ocr_horses, list) or not ocr_horses
                or not isinstance(final_horses, list) or len(final_horses) != len(ocr_horses)):
            return data, False, True
        ocr_numbers = [horse.get("horse_number") for horse in ocr_horses]
        final_numbers = [horse.get("horse_number") for horse in final_horses]
        if any(type(number) is not int or not 1 <= number <= 28
               for number in ocr_numbers + final_numbers):
            return data, False, True
        if (len(set(ocr_numbers)) != len(ocr_numbers)
                or len(set(final_numbers)) != len(final_numbers)
                or set(ocr_numbers) != set(final_numbers)):
            return data, False, True
        for horse in final_horses:
            if (not isinstance(horse.get("horse_name"), str) or not horse["horse_name"].strip()
                    or not isinstance(horse.get("jockey_name"), str) or not horse["jockey_name"].strip()
                    or type(horse.get("frame_number")) is not int or not 1 <= horse["frame_number"] <= 8):
                return data, False, True
        final_by_number = {horse["horse_number"]: horse for horse in final_horses}
        scoring_data = deepcopy(data)
        for horse in scoring_data["horses"]:
            confirmed = final_by_number[horse["horse_number"]]
            for field in ("horse_number", "horse_name", "frame_number", "jockey_name"):
                horse[field] = deepcopy(confirmed[field])
        return scoring_data, True, True
    except Exception:
        return data, False, True


# =========================================================
# UI
# =========================================================
st.set_page_config(page_title=f"馬柱＆予想支援 v{APP_VERSION}", layout="wide")
st.title(f"🏇 馬柱 ＆ 予想支援アプリ v{APP_VERSION}")

# v5追加: 画面切り替えメニュー
st.sidebar.subheader("メニュー")
app_mode = st.sidebar.radio("画面選択", ["🎯 新規予想", "📖 予想履歴"])
st.sidebar.divider()

if app_mode == "📖 予想履歴":
    history.show_history()
    st.stop()  # 履歴画面のときは、以降の新規予想UIを表示させない

st.caption(f"v{APP_VERSION}: 過去走・調教・その他を用途別に複数回OCRし、馬番で統合してから固定ルール採点します。さらに予想結果の保存に対応しました。")

provider = st.sidebar.selectbox("OCR provider", ["gemini", "openai"], index=0,
                                key="ocr_provider")
if provider == "gemini":
    ocr_backend = ocr_gemini
    provider_label = "Gemini"
    try:
        api_key = st.secrets["GEMINI_API_KEY"]
    except Exception:
        api_key = st.sidebar.text_input("Gemini APIキー", type="password")
else:
    import ocr_openai

    ocr_backend = ocr_openai
    provider_label = "OpenAI"
    try:
        configured_key = st.secrets["OPENAI_API_KEY"]
    except Exception:
        configured_key = os.getenv("OPENAI_API_KEY", "")
    entered_key = st.sidebar.text_input("OpenAI APIキー", type="password",
                                        key="openai_api_key_input",
                                        help="未入力の場合はsecretsまたは環境変数OPENAI_API_KEYを使用します。")
    api_key = entered_key.strip() or configured_key

MODEL_NAME = ocr_backend.MODEL_NAME
create_client = ocr_backend.create_client
request_json = ocr_backend.request_json

st.sidebar.subheader(f"v{APP_VERSION} 撮影のコツ")
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
st.sidebar.caption(f"{provider_label} model: {MODEL_NAME}")

st.subheader("📷 画像登録")
st.info("『② 馬柱・近走成績の拡大画像』が特に重要です。過去走が取れない馬には原則として印を付けません。")

whole_files = st.file_uploader(
    "① 全体画像（レース全体・馬番確認用）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="whole_files",
)
card_files = st.file_uploader(
    "② 馬柱・近走成績の拡大画像（最重要・複数可）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="card_files",
)
training_files = st.file_uploader(
    "③ 調教・追い切り欄の拡大画像（複数可）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="training_files",
)
other_files = st.file_uploader(
    "④ オッズ・血統・騎手情報・その他の拡大画像（複数可）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="other_files",
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
    f"同じ画像でも{provider_label}に再読取させる",
    value=False,
    help="通常はOFF推奨。ONにすると用途別OCRをすべて再実行します。",
)

# v5変更: 保存ボタン押下時にも結果表示をキープするための処理
run = st.button(
    f"🔍 v{APP_VERSION} 高精度解析 → 固定ルール採点",
    type="primary",
    disabled=not (api_key and all_files),
)

current_sig = group_signature(grouped_files) if all_files else None

if run:
    st.session_state.last_run_sig = current_sig

# runが押された後、ファイルが変更されない限り結果を表示し続ける
if current_sig and st.session_state.get("last_run_sig") == current_sig:
    try:
        final_cache_key = f"v50_final_{current_sig}"

        if final_cache_key in st.session_state and not (run and force_reread):
            stored = deepcopy(st.session_state[final_cache_key])
            data, meta = stored["data"], stored["meta"]
            if run: # 新規実行のときだけメッセージを出す
                st.info(f"同じ画像セットの統合データを再利用しました。{provider_label}の再読取はしていません。")
        else:
            client = create_client(api_key=api_key)
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

        # レース情報のヘッダー表示
        race = data.get("race") or {}
        course_name = race.get("course") or "競馬場不明"
        race_num = f"{race.get('race_number')}R" if race.get("race_number") else "レース番号不明"
        surface_type = race.get("surface") or ""
        distance = f"{race.get('distance_m')}m" if race.get("distance_m") else ""
        class_name = race.get("class_name") or ""
        race_detail = " / ".join([x for x in [f"{surface_type} {distance}".strip(), class_name] if x])

        st.markdown(f"## 📍 {course_name} {race_num}")
        if race_detail:
            st.caption(f"条件: {race_detail}")
        st.divider()

        quality = extraction_quality(data)
        st.subheader("✅ 抽出品質チェック")
        qcols = st.columns(4)
        qcols[0].metric("出走馬", f'{quality["horses"]}頭')
        qcols[1].metric("過去走取得", f'{quality["previous_horses"]}/{quality["horses"]}頭')
        qcols[2].metric("調教取得", f'{quality["training_horses"]}/{quality["horses"]}頭')
        qcols[3].metric("過去走行数", f'{quality["previous_rows"]}行')
        st.caption(
            f'{provider_label}呼び出し: 基本 {meta["base_calls"]}回 / 過去走 {meta["previous_calls"]}回 / '
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

        recovery.show_recovery(
            data, meta, current_sig, final_cache_key, api_key,
            data_coverage, num, horse_identity_context, ocr_json,
            finalize_extracted_data,
            {
                "previous": (PREVIOUS_RACES_PROMPT, PREVIOUS_RACES_SCHEMA, merge_previous_pass),
                "training": (TRAINING_PROMPT, TRAINING_SCHEMA, merge_training_pass),
                "extra": (EXTRA_PROMPT, EXTRA_SCHEMA, merge_extra_pass),
            },
            create_client,
        )
        comparison_failed = False
        try:
            show_jvlink_comparison(data, current_sig)
        except Exception as exc:
            # 照合表示の失敗も採点・保存の既存処理を止めない。
            st.error(f"JRA-VAN照合表示に失敗しました: {type(exc).__name__}: {exc}")
            comparison_failed = True
        scoring_data, jvlink_used, comparison_exists = jvlink_scoring_data(data, current_sig)
        if comparison_failed:
            scoring_data, jvlink_used = data, False
        if jvlink_used:
            st.success("✅ JRA-VAN補正済みデータで採点しています")
        elif comparison_exists or comparison_failed:
            st.warning("⚠️ JRA-VAN照合結果に未確認項目があるため、OCRデータで採点しています")
        else:
            st.info("ℹ️ OCRデータで採点しています")
        results = score_all(scoring_data)
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

        # ▼▼ v5追加: 保存ボタンの設置 ▼▼
        st.divider()
        if st.button("💾 この予想結果を履歴に保存する", type="primary"):
            try:
                history.save_prediction(scoring_data.get("race") or {}, results)
                st.success("🎉 CSVファイルに予想結果を保存しました！サイドバーの「メニュー」から履歴を確認できます。")
            except Exception as e:
                st.error(f"保存中にエラーが発生しました: {e}")
        st.divider()
        # ▲▲ v5追加: 保存ボタンの設置 ▲▲

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
        if jvlink_used:
            with st.expander("採点に使用した補正済みデータ", expanded=False):
                st.json(scoring_data)

    except Exception as e:
        st.error(f"エラーが発生しました: {e}")
        st.exception(e)
else:
    st.info("全体画像に加えて、②馬柱・近走成績の拡大画像をできるだけ登録してください。")
