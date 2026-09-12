# -*- coding: utf-8 -*-
"""
馬柱＆予想支援アプリ v4

設計:
  Gemini: 画像から事実を抽出
  Python: 全馬を比較して固定ルールで100点を算出

v4で追加:
  - 調教時計のレース内相対評価
  - 近走の着差・着順を機械評価
  - 休養期間と休養明け走数の扱い
  - 馬体重・増減の状態補正
  - 脚質の頭数比較
  - 同条件実績の自動集計
  - オッズから参考期待値を計算
  - AIの自由な印付けを廃止
"""

import json
import os
import hashlib
import re
from datetime import datetime
from typing import Any

import io
import streamlit as st
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from google import genai
from google.genai import types

MODEL_NAME = "gemini-3.6-flash"
TEMPERATURE = 0.0

WEIGHTS = {
    "能力・近走": 20.0,
    "コース・距離・馬場適性": 18.0,
    "調教・状態": 17.0,
    "脚質・展開": 15.0,
    "騎手": 12.0,
    "血統": 10.0,
    "枠順": 8.0,
}


# ---------------------------------------------------------
# Gemini: OCR / 構造化のみ
# ---------------------------------------------------------
SCHEMA = {
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
            "required": [
                "date", "course", "race_number", "surface",
                "distance_m", "track_condition", "class_name", "field_size"
            ],
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
                    "running_style": {
                        "type": ["string", "null"],
                        "enum": ["逃げ", "先行", "差し", "追込", "不明", None],
                    },
                    "days_since_last_race": {"type": ["integer", "null"]},

                    "body_weight": {"type": ["integer", "null"]},
                    "body_weight_change": {"type": ["integer", "null"]},

                    "training": {
                        "type": "object",
                        "properties": {
                            "course": {"type": ["string", "null"]},
                            "time_6f": {"type": ["number", "null"]},
                            "time_5f": {"type": ["number", "null"]},
                            "time_4f": {"type": ["number", "null"]},
                            "time_3f": {"type": ["number", "null"]},
                            "time_1f": {"type": ["number", "null"]},
                            "final_3f": {"type": ["number", "null"]},
                            "final_1f": {"type": ["number", "null"]},
                            "training_comment": {"type": ["string", "null"]},
                        },
                        "required": [
                            "course", "time_6f", "time_5f", "time_4f",
                            "time_3f", "time_1f", "final_3f", "final_1f",
                            "training_comment"
                        ],
                    },

                    "previous_races": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
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
                            },
                            "required": [
                                "race_date", "course", "surface", "distance_m",
                                "track_condition", "class_name", "finish_position",
                                "field_size", "time_diff_sec", "body_weight", "note"
                            ],
                        },
                    },

                    "pedigree": {
                        "type": "object",
                        "properties": {
                            "sire": {"type": ["string", "null"]},
                            "dam_sire": {"type": ["string", "null"]},
                        },
                        "required": ["sire", "dam_sire"],
                    },
                    "jockey_course_record_text": {"type": ["string", "null"]},
                    "jockey_change_text": {"type": ["string", "null"]},
                    "odds": {"type": ["number", "null"]},
                },
                "required": [
                    "horse_number", "frame_number", "horse_name", "jockey_name",
                    "running_style", "days_since_last_race", "body_weight",
                    "body_weight_change", "training", "previous_races",
                    "pedigree", "jockey_course_record_text",
                    "jockey_change_text", "odds"
                ],
            },
        },
    },
    "required": ["race", "horses"],
}

PROMPT = r"""
あなたは競馬新聞のOCR・データ入力担当です。採点担当ではありません。

画像に書かれている情報だけをJSONにしてください。
絶対に推測・補完・外部検索・一般知識による評価をしないでください。

重要:
- 読めない数字は null。
- 複数画像はすべて同じレースの資料として相互参照し、同じ馬を統合。
- 各画像の直前に「画像種別」の説明が付く。その用途を優先して読む。
- 全体画像より拡大画像で文字・数字が鮮明な場合は、拡大画像を優先する。
- 同じ項目が複数画像にある場合、最も鮮明で判読可能な値を採用する。
- 同一馬の照合は馬番を最優先し、馬名・騎手名を補助にする。
- 異なる画像で値が食い違い、どちらが正しいか判断できない場合は推測せず null。
- 馬の評価、順位、印、能力点は作らない。
- previous_races は集計せず、読めるレースを1行ずつ保存。
- 長期休養馬は休養前のレースをできるだけ残す。
- 調教時計は見える数値をそのまま保存。
- 馬体重と増減があればそのまま保存。
- 脚質は紙面に明記された場合だけ。
- 騎手成績・血統適性を名前から推測しない。
- オッズは紙面に表示されている場合だけ。
"""


# ---------------------------------------------------------
# 基本計算
# ---------------------------------------------------------
def num(v):
    try:
        return float(v)
    except Exception:
        return None


def integer(v):
    try:
        return int(v)
    except Exception:
        return None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def percentile(values, value, higher_is_better=True):
    vals = [x for x in values if x is not None]
    if value is None or not vals:
        return 50.0
    if len(vals) == 1:
        return 50.0
    if not higher_is_better:
        value = -value
        vals = [-x for x in vals]
    below = sum(x < value for x in vals)
    equal = sum(x == value for x in vals)
    return 100.0 * (below + 0.5 * equal) / len(vals)


def race_relevance(r, race):
    score = 0
    if race.get("course") and r.get("course") == race["course"]:
        score += 3
    d = integer(r.get("distance_m"))
    target = integer(race.get("distance_m"))
    if d is not None and target is not None:
        diff = abs(d - target)
        if diff == 0:
            score += 3
        elif diff <= 200:
            score += 2
        elif diff <= 400:
            score += 1
    if race.get("surface") and r.get("surface") == race["surface"]:
        score += 2
    if race.get("track_condition") and r.get("track_condition") == race["track_condition"]:
        score += 1
    return score


def result_quality(r):
    pos = integer(r.get("finish_position"))
    n = integer(r.get("field_size"))
    diff = num(r.get("time_diff_sec"))

    if pos is None:
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

    # 着差が小さい凡走を少し救済
    if diff is not None:
        if diff <= 0.2:
            s += 0.08
        elif diff <= 0.5:
            s += 0.04
        elif diff >= 1.5:
            s -= 0.08

    # 大頭数で上位に来た場合を少し評価
    if n and n >= 14 and pos <= 5:
        s += 0.04

    return clamp(s, 0.0, 1.0)


def score_ability(horse, race):
    races = horse.get("previous_races") or []
    usable = []
    for r in races[:8]:
        q = result_quality(r)
        if q is not None:
            usable.append((r, q))

    if not usable:
        return 10.0

    # 現条件への関連度と新しさを組み合わせる
    weighted = []
    for i, (r, q) in enumerate(usable):
        recency_w = max(1.0, 5.0 - i * 0.55)
        relevance_w = 1.0 + 0.08 * race_relevance(r, race)
        weighted.append((q, recency_w * relevance_w))

    avg = sum(q * w for q, w in weighted) / sum(w for _, w in weighted)
    score = avg * 20.0

    days = integer(horse.get("days_since_last_race"))
    if days is not None:
        # 長期休養を能力の大幅減点にはしない
        if days >= 300:
            score *= 0.95
        elif days >= 180:
            score *= 0.98

    return round(clamp(score, 0, 20), 1)


def score_suitability(horse, race):
    races = horse.get("previous_races") or []
    if not races:
        return 9.0

    course, dist, surf, cond = [], [], [], []
    target_d = integer(race.get("distance_m"))

    for r in races:
        q = result_quality(r)
        if q is None:
            continue
        if race.get("course") and r.get("course") == race["course"]:
            course.append(q)
        if target_d and integer(r.get("distance_m")) is not None:
            if abs(integer(r.get("distance_m")) - target_d) <= 200:
                dist.append(q)
        if race.get("surface") and r.get("surface") == race["surface"]:
            surf.append(q)
        if race.get("track_condition") and r.get("track_condition") == race["track_condition"]:
            cond.append(q)

    def part(a, default=0.5):
        return sum(a) / len(a) if a else default

    # 18点: 5 + 5 + 4 + 4
    score = (
        part(course) * 5
        + part(dist) * 5
        + part(surf) * 4
        + part(cond) * 4
    )
    return round(clamp(score, 0, 18), 1)


def training_metric(horse):
    t = horse.get("training") or {}
    # 短い時計ほど速い。ただし「何の時計か」を混ぜない。
    # 主に4F/3F/1Fの存在を品質指標にする。
    metrics = []
    for key in ["time_4f", "time_3f", "time_1f", "final_3f", "final_1f"]:
        v = num(t.get(key))
        if v is not None:
            metrics.append((key, v))

    if not metrics:
        return None

    # 時計の種類ごとにレース内順位を後段で正規化するため、
    # ここでは値の平均を返さない。
    return metrics


def score_training_all(horses):
    # 全馬比較。数字のある馬だけを順位化し、欠損は中立。
    per_horse = {id(h): [] for h in horses}

    for key in ["time_4f", "time_3f", "time_1f", "final_3f", "final_1f"]:
        vals = [(h, num((h.get("training") or {}).get(key))) for h in horses]
        vals = [(h, v) for h, v in vals if v is not None]
        if not vals:
            continue
        raw = [v for _, v in vals]
        for h, v in vals:
            p = percentile(raw, v, higher_is_better=False)
            per_horse[id(h)].append(p)

    out = {}
    for h in horses:
        ps = per_horse[id(h)]
        if not ps:
            out[id(h)] = 8.5
            continue
        p = sum(ps) / len(ps)
        # 0～17点
        out[id(h)] = round(clamp(17.0 * p / 100.0, 0, 17), 1)
    return out


def score_condition(horse):
    """
    調教点に状態要素を混ぜる。
    馬体重増減は単独で悪とせず、極端な増減だけ小さく注意。
    """
    base = 8.5
    change = integer(horse.get("body_weight_change"))
    if change is not None:
        if -4 <= change <= 6:
            base += 1.0
        elif -8 <= change <= 10:
            base += 0.0
        else:
            base -= 1.0
    return clamp(base, 0, 17)


def score_pace(horse, horses):
    styles = [(h.get("running_style") or "不明") for h in horses]
    style = horse.get("running_style") or "不明"

    escape = styles.count("逃げ")
    front = styles.count("先行")
    fast_front = escape + front

    # 逃げが複数なら差し・追込を相対的に評価
    if style == "逃げ":
        return 7.0 if escape >= 2 else 12.0
    if style == "先行":
        return 10.0 if escape >= 2 else 13.0
    if style == "差し":
        return 14.0 if escape >= 2 or fast_front >= 5 else 12.0
    if style == "追込":
        return 14.0 if escape >= 2 or fast_front >= 6 else 10.0
    return 10.0


def score_jockey(horse):
    text = " ".join([
        horse.get("jockey_course_record_text") or "",
        horse.get("jockey_change_text") or "",
    ])
    if not text:
        return 6.0

    nums = []
    for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", text):
        nums.append(float(x))
    if nums:
        p = max(nums)
        if p >= 40: return 11.0
        if p >= 30: return 10.0
        if p >= 20: return 8.5
        if p >= 10: return 7.0
        return 5.5
    return 7.0


def score_pedigree(horse):
    p = horse.get("pedigree") or {}
    if p.get("sire") or p.get("dam_sire"):
        return 6.0
    return 5.0


def score_gate(horse):
    frame = integer(horse.get("frame_number"))
    if frame is None:
        return 4.0
    if frame <= 2:
        return 4.5
    if frame >= 7:
        return 3.5
    return 4.0


def rank_label(x):
    if x >= 85: return "S"
    if x >= 80: return "A+"
    if x >= 75: return "A"
    if x >= 70: return "B+"
    if x >= 65: return "B"
    if x >= 60: return "C+"
    if x >= 55: return "C"
    return "D"


def estimate_win_probability(results):
    """
    総合点を指数変換して相対的な参考勝率を作る。
    公的な勝率ではない。オッズ期待値の試算用。
    """
    import math
    exps = [math.exp((r["total"] - max(x["total"] for x in results)) / 5.0) for r in results]
    s = sum(exps)
    for r, e in zip(results, exps):
        r["model_win_prob"] = round(100.0 * e / s, 1)


def add_marks(results):
    marks = ["◎", "○", "▲", "☆", "△", "◇"]
    for i, r in enumerate(results):
        r["mark"] = marks[i] if i < len(marks) else ""


def calculate_expected_value(results):
    for r in results:
        odds = num(r.get("odds"))
        p = r.get("model_win_prob")
        if odds and p:
            # 単純化した参考値: 推定勝率 × オッズ
            r["expected_value"] = round((p / 100.0) * odds, 2)
        else:
            r["expected_value"] = None


def extract_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.replace("```json", "").replace("```", "").strip()
    return json.loads(t)



# ---------------------------------------------------------
# v4.2 画像前処理・複数画像統合
# ---------------------------------------------------------
def prepare_image_bytes(uploaded_file):
    """
    スマホ写真をGeminiへ渡す前に、
    - EXIF回転補正
    - 長辺3200pxまで拡大/縮小
    - 軽いコントラスト・シャープ化
    を行う。
    元画像自体は変更しない。
    """
    raw = uploaded_file.getvalue()
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")

    max_side = max(img.size)
    target_max = 3200

    if max_side < 2200:
        scale = min(2.0, target_max / max_side)
    elif max_side > target_max:
        scale = target_max / max_side
    else:
        scale = 1.0

    if abs(scale - 1.0) > 0.01:
        new_size = (
            max(1, int(img.width * scale)),
            max(1, int(img.height * scale)),
        )
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Sharpness(img).enhance(1.15)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=110, threshold=3))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, subsampling=0)
    return buf.getvalue(), "image/jpeg"


def files_signature(grouped_files):
    h = hashlib.sha256()
    for group_name, files in grouped_files:
        h.update(group_name.encode("utf-8"))
        for f in files:
            h.update(f.name.encode("utf-8"))
            h.update(f.getvalue())
    return h.hexdigest()


def add_group_to_contents(contents, label, files):
    for i, f in enumerate(files, 1):
        enhanced_bytes, mime = prepare_image_bytes(f)
        contents.append(
            f"【画像種別: {label} / {i}枚目】"
            " このラベルの用途を意識して読み取ってください。"
        )
        contents.append(types.Part.from_bytes(data=enhanced_bytes, mime_type=mime))


# ---------------------------------------------------------
# UI
# ---------------------------------------------------------
st.set_page_config(page_title="馬柱＆予想支援 v4.2", layout="wide")
st.title("🏇 馬柱 ＆ 予想支援アプリ v4.2")
st.caption("v4.2: 全体＋拡大画像を用途別に複数登録。画像を高解像度化してからGeminiで事実抽出します。")

try:
    api_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    api_key = st.sidebar.text_input("Gemini APIキー", type="password")

st.sidebar.subheader("v4.2 撮影のコツ")
st.sidebar.write("・全体画像: 馬番と並びが分かる程度")
st.sidebar.write("・近走欄: 文字が潰れない大きさまで拡大")
st.sidebar.write("・調教欄: 時計が読める大きさで別撮影")
st.sidebar.write("・少し重複させて撮ると同一馬を照合しやすい")
st.sidebar.divider()
st.sidebar.subheader("100点配分")
for k, v in WEIGHTS.items():
    st.sidebar.write(f"{k}: {v:g}点")

st.subheader("📷 画像登録")
st.caption(
    "同じレースの画像を用途別に登録してください。"
    "全体写真に加えて、文字が読めるように拡大した写真を複数枚入れるのがおすすめです。"
)

whole_files = st.file_uploader(
    "① 全体画像（レース全体・馬番確認用）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key="whole_files",
)
card_files = st.file_uploader(
    "② 馬柱・近走成績の拡大画像（複数可）",
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
    ("馬柱・近走成績の拡大", card_files or []),
    ("調教・追い切り欄の拡大", training_files or []),
    ("オッズ・血統・騎手・その他の拡大", other_files or []),
]
files = [f for _, fs in grouped_files for f in fs]

if files:
    st.write(f"登録画像: **{len(files)}枚**")
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
    help="通常はOFF推奨。OFFなら同一画像セットはセッション内の前回抽出データを再利用します。"
)

run = st.button(
    "🔍 複数画像を統合解析 → 固定ルール採点",
    type="primary",
    disabled=not (api_key and files),
)

if run:
    try:
        signature = files_signature(grouped_files)
        cache_key = f"extract_{signature}"

        if (not force_reread) and cache_key in st.session_state:
            data = st.session_state[cache_key]
            st.info("同じ画像セットの前回抽出データを再利用しました。Geminiの再読取はしていません。")
        else:
            client = genai.Client(api_key=api_key)
            contents = [PROMPT]
            add_group_to_contents(contents, "全体画像", whole_files or [])
            add_group_to_contents(contents, "馬柱・近走成績の拡大画像", card_files or [])
            add_group_to_contents(contents, "調教・追い切り欄の拡大画像", training_files or [])
            add_group_to_contents(contents, "オッズ・血統・騎手・その他の拡大画像", other_files or [])

            with st.spinner("複数の拡大画像を高解像度化し、Geminiが同一レースとして統合しています…"):
                response = client.models.generate_content(
                    model=MODEL_NAME,
                    contents=contents,
                    config={
                        "temperature": TEMPERATURE,
                        "seed": 7,
                        "response_mime_type": "application/json",
                        "response_json_schema": SCHEMA,
                    },
                )

            data = extract_json(response.text)
            st.session_state[cache_key] = data
        race = data.get("race", {})
        horses = data.get("horses", [])

        if not horses:
            st.error("馬データを取得できませんでした。")
            st.stop()

        training_scores = score_training_all(horses)

        results = []
        for h in horses:
            ability = score_ability(h, race)
            suitability = score_suitability(h, race)
            training = training_scores.get(id(h), 8.5)
            condition = score_condition(h)

            # 調教17点のうち、時計14点＋状態3点
            training_final = clamp(training * (14.0 / 17.0) + condition * (3.0 / 17.0), 0, 17)

            pace = score_pace(h, horses)
            jockey = score_jockey(h)
            pedigree = score_pedigree(h)
            gate = score_gate(h)

            total = round(
                ability + suitability + training_final
                + pace + jockey + pedigree + gate, 1
            )

            results.append({
                "horse_number": integer(h.get("horse_number")),
                "frame_number": integer(h.get("frame_number")),
                "horse_name": h.get("horse_name") or "不明",
                "jockey_name": h.get("jockey_name") or "不明",
                "odds": num(h.get("odds")),
                "ability": round(ability, 1),
                "suitability": round(suitability, 1),
                "training": round(training_final, 1),
                "pace": round(pace, 1),
                "jockey": round(jockey, 1),
                "pedigree": round(pedigree, 1),
                "gate": round(gate, 1),
                "total": total,
            })

        results.sort(key=lambda x: (
            -x["total"],
            x["odds"] if x["odds"] is not None else 999999,
            x["horse_number"] if x["horse_number"] is not None else 999
        ))

        for i, r in enumerate(results, 1):
            r["rank"] = i

        estimate_win_probability(results)
        calculate_expected_value(results)
        add_marks(results)

        st.success(f"{len(results)}頭を固定ルールで採点しました。")

        st.subheader("📊 総合ランキング")
        table = []
        for r in results:
            table.append({
                "順位": r["rank"],
                "印": r["mark"],
                "馬番": r["horse_number"],
                "馬名": r["horse_name"],
                "総合": r["total"],
                "評価": rank_label(r["total"]),
                "能力": r["ability"],
                "適性": r["suitability"],
                "調教・状態": r["training"],
                "展開": r["pace"],
                "騎手": r["jockey"],
                "血統": r["pedigree"],
                "枠順": r["gate"],
                "オッズ": r["odds"],
                "参考勝率": r["model_win_prob"],
                "参考期待値": r["expected_value"],
            })
        st.dataframe(table, use_container_width=True, hide_index=True)

        st.subheader("🎯 予想の見方")
        st.write("◎○▲☆△◇は総合点順位から機械的に決定。AIには印を決めさせていません。")
        st.write("参考勝率・期待値は現時点のモデル値で、実際の市場確率や払戻を保証するものではありません。")

        st.subheader("🔎 馬ごとの詳細")
        for r in results:
            with st.expander(
                f'{r["rank"]}位 {r["mark"]} {r["horse_name"]} — {r["total"]}点'
            ):
                st.write({
                    "能力・近走": r["ability"],
                    "コース・距離・馬場適性": r["suitability"],
                    "調教・状態": r["training"],
                    "脚質・展開": r["pace"],
                    "騎手": r["jockey"],
                    "血統": r["pedigree"],
                    "枠順": r["gate"],
                    "参考勝率": r["model_win_prob"],
                    "参考期待値": r["expected_value"],
                })

        st.subheader("🧾 抽出された生データ")
        st.info("採点結果より先に、ここを確認してください。読み取りが間違っていれば採点も間違います。")
        st.json(data)

        st.download_button(
            "生データJSONを保存",
            json.dumps(data, ensure_ascii=False, indent=2),
            "race_extracted_data.json",
            "application/json",
        )

        st.download_button(
            "採点結果JSONを保存",
            json.dumps(results, ensure_ascii=False, indent=2),
            "race_scored_results.json",
            "application/json",
        )

    except Exception as e:
        st.error(f"エラーが発生しました: {e}")
        st.exception(e)
else:
    st.info("全体画像＋必要な拡大画像をアップロードしてください。")
