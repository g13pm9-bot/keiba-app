# -*- coding: utf-8 -*-
"""
馬柱＆予想支援アプリ v4.1

v4.1:
- GeminiのJSON Schema validation errorを修正
- response_schema -> response_json_schema
- nullable型はJSON Schemaの anyOf で明示
- AIは事実抽出、採点はPython固定ルール
"""

import json
import os
import re
import math
import streamlit as st
from google import genai
from google.genai import types

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
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


def nullable(type_name):
    return {"anyOf": [{"type": type_name}, {"type": "null"}]}


def nullable_enum(values):
    return {
        "anyOf": [
            {"type": "string", "enum": values},
            {"type": "null"}
        ]
    }


SCHEMA = {
    "type": "object",
    "properties": {
        "race": {
            "type": "object",
            "properties": {
                "date": nullable("string"),
                "course": nullable("string"),
                "race_number": nullable("integer"),
                "surface": nullable("string"),
                "distance_m": nullable("integer"),
                "track_condition": nullable("string"),
                "class_name": nullable("string"),
                "field_size": nullable("integer"),
            },
            "required": [
                "date", "course", "race_number", "surface",
                "distance_m", "track_condition", "class_name", "field_size"
            ],
            "additionalProperties": False,
        },
        "horses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "horse_number": nullable("integer"),
                    "frame_number": nullable("integer"),
                    "horse_name": nullable("string"),
                    "jockey_name": nullable("string"),
                    "running_style": nullable_enum(
                        ["逃げ", "先行", "差し", "追込", "不明"]
                    ),
                    "days_since_last_race": nullable("integer"),
                    "body_weight": nullable("integer"),
                    "body_weight_change": nullable("integer"),

                    "training": {
                        "type": "object",
                        "properties": {
                            "course": nullable("string"),
                            "time_6f": nullable("number"),
                            "time_5f": nullable("number"),
                            "time_4f": nullable("number"),
                            "time_3f": nullable("number"),
                            "time_1f": nullable("number"),
                            "final_3f": nullable("number"),
                            "final_1f": nullable("number"),
                            "training_comment": nullable("string"),
                        },
                        "required": [
                            "course", "time_6f", "time_5f", "time_4f",
                            "time_3f", "time_1f", "final_3f", "final_1f",
                            "training_comment"
                        ],
                        "additionalProperties": False,
                    },

                    "previous_races": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "race_date": nullable("string"),
                                "course": nullable("string"),
                                "surface": nullable("string"),
                                "distance_m": nullable("integer"),
                                "track_condition": nullable("string"),
                                "class_name": nullable("string"),
                                "finish_position": nullable("integer"),
                                "field_size": nullable("integer"),
                                "time_diff_sec": nullable("number"),
                                "body_weight": nullable("integer"),
                                "note": nullable("string"),
                            },
                            "required": [
                                "race_date", "course", "surface", "distance_m",
                                "track_condition", "class_name", "finish_position",
                                "field_size", "time_diff_sec", "body_weight", "note"
                            ],
                            "additionalProperties": False,
                        },
                    },

                    "pedigree": {
                        "type": "object",
                        "properties": {
                            "sire": nullable("string"),
                            "dam_sire": nullable("string"),
                        },
                        "required": ["sire", "dam_sire"],
                        "additionalProperties": False,
                    },

                    "jockey_course_record_text": nullable("string"),
                    "jockey_change_text": nullable("string"),
                    "odds": nullable("number"),
                },
                "required": [
                    "horse_number", "frame_number", "horse_name", "jockey_name",
                    "running_style", "days_since_last_race",
                    "body_weight", "body_weight_change",
                    "training", "previous_races", "pedigree",
                    "jockey_course_record_text", "jockey_change_text", "odds"
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["race", "horses"],
    "additionalProperties": False,
}


PROMPT = """
あなたは競馬新聞のOCR・データ入力担当です。
採点や予想は行わず、画像に書かれている事実だけを抽出してください。

ルール:
- 読めない値は null。
- 推測しない。
- 一般知識や外部検索で補完しない。
- 複数画像は相互参照する。
- 同じ馬の情報は統合する。
- previous_races は集計せず、読める過去レースを1行ずつ保存する。
- 長期休養馬は休養前のレースも残す。
- 調教時計は見えている数字をそのまま保存する。
- 馬体重と増減があれば保存する。
- 脚質は紙面に明記されている場合だけ。
- 騎手や血統を名前だけで評価しない。
- オッズは画像に表示されている場合だけ。
- 100点評価、順位、印は作らない。
"""


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
        vals = [-x for x in vals]
        value = -value

    below = sum(x < value for x in vals)
    equal = sum(x == value for x in vals)
    return 100.0 * (below + 0.5 * equal) / len(vals)


def race_relevance(r, race):
    score = 0

    if race.get("course") and r.get("course") == race.get("course"):
        score += 3

    target_d = integer(race.get("distance_m"))
    d = integer(r.get("distance_m"))

    if target_d is not None and d is not None:
        diff = abs(d - target_d)
        if diff == 0:
            score += 3
        elif diff <= 200:
            score += 2
        elif diff <= 400:
            score += 1

    if race.get("surface") and r.get("surface") == race.get("surface"):
        score += 2

    if (
        race.get("track_condition")
        and r.get("track_condition") == race.get("track_condition")
    ):
        score += 1

    return score


def result_quality(r):
    pos = integer(r.get("finish_position"))
    field_size = integer(r.get("field_size"))
    time_diff = num(r.get("time_diff_sec"))

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

    if time_diff is not None:
        if time_diff <= 0.2:
            s += 0.08
        elif time_diff <= 0.5:
            s += 0.04
        elif time_diff >= 1.5:
            s -= 0.08

    if field_size and field_size >= 14 and pos <= 5:
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

    course_scores = []
    distance_scores = []
    surface_scores = []
    condition_scores = []

    target_d = integer(race.get("distance_m"))

    for r in races:
        q = result_quality(r)
        if q is None:
            continue

        if race.get("course") and r.get("course") == race.get("course"):
            course_scores.append(q)

        d = integer(r.get("distance_m"))
        if target_d is not None and d is not None and abs(d - target_d) <= 200:
            distance_scores.append(q)

        if race.get("surface") and r.get("surface") == race.get("surface"):
            surface_scores.append(q)

        if (
            race.get("track_condition")
            and r.get("track_condition") == race.get("track_condition")
        ):
            condition_scores.append(q)

    def avg_or_mid(values):
        return sum(values) / len(values) if values else 0.5

    score = (
        avg_or_mid(course_scores) * 5
        + avg_or_mid(distance_scores) * 5
        + avg_or_mid(surface_scores) * 4
        + avg_or_mid(condition_scores) * 4
    )

    return round(clamp(score, 0, 18), 1)


def score_training_all(horses):
    per_horse = {id(h): [] for h in horses}

    # 同じ種類の時計同士だけを比較する。
    for key in ["time_4f", "time_3f", "time_1f", "final_3f", "final_1f"]:
        values = []
        for h in horses:
            v = num((h.get("training") or {}).get(key))
            if v is not None:
                values.append((h, v))

        if len(values) < 2:
            continue

        raw = [v for _, v in values]

        for h, v in values:
            # 時計は小さい方が良い
            p = percentile(raw, v, higher_is_better=False)
            per_horse[id(h)].append(p)

    scores = {}

    for h in horses:
        ps = per_horse[id(h)]
        if not ps:
            scores[id(h)] = 8.5
        else:
            p = sum(ps) / len(ps)
            scores[id(h)] = round(clamp(17.0 * p / 100.0, 0, 17), 1)

    return scores


def score_condition(horse):
    # 馬体重増減は大きすぎる変化だけ軽く注意。
    score = 8.5
    change = integer(horse.get("body_weight_change"))

    if change is not None:
        if -4 <= change <= 6:
            score += 1.0
        elif -8 <= change <= 10:
            pass
        else:
            score -= 1.0

    return clamp(score, 0, 17)


def score_pace(horse, horses):
    styles = [h.get("running_style") or "不明" for h in horses]
    style = horse.get("running_style") or "不明"

    escape = styles.count("逃げ")
    front = styles.count("先行")
    forward_total = escape + front

    if style == "逃げ":
        return 7.0 if escape >= 2 else 12.0

    if style == "先行":
        return 10.0 if escape >= 2 else 13.0

    if style == "差し":
        return 14.0 if escape >= 2 or forward_total >= 5 else 12.0

    if style == "追込":
        return 14.0 if escape >= 2 or forward_total >= 6 else 10.0

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
        try:
            nums.append(float(x))
        except Exception:
            pass

    if nums:
        p = max(nums)
        if p >= 40:
            return 11.0
        if p >= 30:
            return 10.0
        if p >= 20:
            return 8.5
        if p >= 10:
            return 7.0
        return 5.5

    return 7.0


def score_pedigree(horse):
    pedigree = horse.get("pedigree") or {}

    if pedigree.get("sire") or pedigree.get("dam_sire"):
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


def rank_label(score):
    if score >= 85:
        return "S"
    if score >= 80:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 70:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 60:
        return "C+"
    if score >= 55:
        return "C"
    return "D"


def estimate_win_probability(results):
    if not results:
        return

    max_score = max(r["total"] for r in results)
    exp_values = [
        math.exp((r["total"] - max_score) / 5.0)
        for r in results
    ]
    total_exp = sum(exp_values)

    for r, e in zip(results, exp_values):
        r["model_win_prob"] = round(100.0 * e / total_exp, 1)


def calculate_expected_value(results):
    for r in results:
        odds = num(r.get("odds"))
        p = num(r.get("model_win_prob"))

        if odds is not None and p is not None:
            r["expected_value"] = round((p / 100.0) * odds, 2)
        else:
            r["expected_value"] = None


def add_marks(results):
    marks = ["◎", "○", "▲", "△"]

    for i, r in enumerate(results):
        r["mark"] = marks[i] if i < len(marks) else ""


def extract_json(text):
    t = (text or "").strip()

    if t.startswith("```"):
        t = t.replace("```json", "").replace("```", "").strip()

    return json.loads(t)


st.set_page_config(
    page_title="馬柱＆予想支援 v4.1",
    layout="wide"
)

st.title("🏇 馬柱 ＆ 予想支援アプリ v4.1")
st.caption(
    "AIは馬柱から事実だけを抽出し、100点採点・順位・印・期待値はPythonで固定計算します。"
)

try:
    api_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    api_key = st.sidebar.text_input(
        "Gemini APIキー",
        type="password"
    )

st.sidebar.subheader("100点配分")

for name, value in WEIGHTS.items():
    st.sidebar.write(f"{name}: {value:g}点")

uploaded_files = st.file_uploader(
    "競馬新聞・馬柱の画像（全体＋拡大を複数推奨）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
)

if uploaded_files:
    cols = st.columns(min(4, len(uploaded_files)))

    for i, f in enumerate(uploaded_files):
        with cols[i % len(cols)]:
            st.image(
                f,
                caption=f.name,
                use_container_width=True
            )

run = st.button(
    "🔍 解析 → 固定ルール採点",
    type="primary",
    disabled=not (api_key and uploaded_files),
)

if run:
    try:
        client = genai.Client(api_key=api_key)

        contents = []

        for f in uploaded_files:
            contents.append(
                types.Part.from_bytes(
                    data=f.getvalue(),
                    mime_type=f.type
                )
            )

        contents.append(PROMPT)

        with st.spinner(
            "Geminiが画像から事実データを抽出しています…"
        ):
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=TEMPERATURE,
                    response_mime_type="application/json",
                    response_json_schema=SCHEMA,
                ),
            )

        data = extract_json(response.text)

        race = data.get("race") or {}
        horses = data.get("horses") or []

        if not horses:
            st.error(
                "馬データを取得できませんでした。"
                "画像が小さい場合は拡大画像も追加してください。"
            )
            st.stop()

        training_scores = score_training_all(horses)

        results = []

        for horse in horses:
            ability = score_ability(horse, race)
            suitability = score_suitability(horse, race)

            raw_training = training_scores.get(id(horse), 8.5)
            condition = score_condition(horse)

            # 調教時計14点 + 状態3点
            training = clamp(
                raw_training * (14.0 / 17.0)
                + condition * (3.0 / 17.0),
                0,
                17
            )

            pace = score_pace(horse, horses)
            jockey = score_jockey(horse)
            pedigree = score_pedigree(horse)
            gate = score_gate(horse)

            total = round(
                ability
                + suitability
                + training
                + pace
                + jockey
                + pedigree
                + gate,
                1
            )

            results.append({
                "horse_number": integer(horse.get("horse_number")),
                "frame_number": integer(horse.get("frame_number")),
                "horse_name": horse.get("horse_name") or "不明",
                "jockey_name": horse.get("jockey_name") or "不明",
                "odds": num(horse.get("odds")),
                "ability": round(ability, 1),
                "suitability": round(suitability, 1),
                "training": round(training, 1),
                "pace": round(pace, 1),
                "jockey": round(jockey, 1),
                "pedigree": round(pedigree, 1),
                "gate": round(gate, 1),
                "total": total,
            })

        results.sort(
            key=lambda x: (
                -x["total"],
                x["odds"] if x["odds"] is not None else 999999,
                x["horse_number"] if x["horse_number"] is not None else 999
            )
        )

        for i, r in enumerate(results, 1):
            r["rank"] = i

        estimate_win_probability(results)
        calculate_expected_value(results)
        add_marks(results)

        st.success(
            f"{len(results)}頭を固定ルールで採点しました。"
        )

        st.subheader("📊 総合ランキング")

        display_rows = []

        for r in results:
            display_rows.append({
                "順位": r["rank"],
                "印": r["mark"],
                "馬番": r["horse_number"],
                "馬名": r["horse_name"],
                "騎手": r["jockey_name"],
                "総合": r["total"],
                "評価": rank_label(r["total"]),
                "能力": r["ability"],
                "適性": r["suitability"],
                "調教・状態": r["training"],
                "展開": r["pace"],
                "騎手点": r["jockey"],
                "血統": r["pedigree"],
                "枠順": r["gate"],
                "オッズ": r["odds"],
                "参考勝率%": r["model_win_prob"],
                "参考期待値": r["expected_value"],
            })

        st.dataframe(
            display_rows,
            use_container_width=True,
            hide_index=True
        )

        st.subheader("🎯 印のルール")
        st.write(
            "総合点の1位=◎、2位=○、3位=▲、4位=△です。"
            "AIには印を決めさせていません。"
        )

        st.subheader("🔎 馬ごとの詳細")

        for r in results:
            with st.expander(
                f'{r["rank"]}位 {r["mark"]} '
                f'{r["horse_name"]} — {r["total"]}点'
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

        st.subheader("🧾 Geminiが読み取った生データ")
        st.info(
            "順位を見る前に、馬名・着順・距離・調教時計などが"
            "正しく読み取れているか確認してください。"
        )

        st.json(data)

        st.download_button(
            "生データJSONを保存",
            data=json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            ),
            file_name="race_extracted_data.json",
            mime="application/json",
        )

        st.download_button(
            "採点結果JSONを保存",
            data=json.dumps(
                results,
                ensure_ascii=False,
                indent=2
            ),
            file_name="race_scored_results.json",
            mime="application/json",
        )

    except json.JSONDecodeError as e:
        st.error(f"GeminiのJSON解析に失敗しました: {e}")

        if "response" in locals():
            st.code(response.text or "")

    except Exception as e:
        st.error(f"エラーが発生しました: {e}")
        st.exception(e)

elif not api_key:
    st.info(
        "Gemini APIキーをStreamlit Secretsに設定してください。"
    )
else:
    st.info(
        "競馬新聞・馬柱画像をアップロードしてください。"
    )
