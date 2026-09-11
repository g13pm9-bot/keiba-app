# -*- coding: utf-8 -*-
"""
馬柱 ＆ 予想支援アプリ - Deterministic Scoring Edition

設計方針:
1. Geminiには「画像から事実を抽出」させる。採点はさせない。
2. 採点はPython側の固定ルールで行うため、同じ入力なら同じ結果になる。
3. 画像にない情報は null / unknown とし、推測しない。
4. 休み明け・叩き2走目・叩き3走目を専用ロジックで処理する。
5. Geminiの出力はJSON Schemaで固定する。
6. オッズは「能力点」と分離し、馬券妙味の評価にだけ使う。
"""

import json
import math
import os
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import streamlit as st
from google import genai
from google.genai import types


# ============================================================
# 基本設定
# ============================================================

APP_TITLE = "馬柱 ＆ 予想支援アプリ"
MODEL_NAME = "gemini-3.6-flash"

# AIによる採点ブレをなくすため、採点そのものはPythonで実施する。
# Geminiは「画像→構造化データ抽出」だけ担当。
AI_TEMPERATURE = 0.0


# ============================================================
# データモデル
# ============================================================

@dataclass
class Score:
    ability: float = 0.0       # 20
    suitability: float = 0.0   # 18
    training: float = 0.0      # 17
    pace: float = 0.0          # 15
    jockey: float = 0.0        # 12
    pedigree: float = 0.0      # 10
    gate: float = 0.0          # 8

    @property
    def total(self) -> float:
        return round(
            self.ability
            + self.suitability
            + self.training
            + self.pace
            + self.jockey
            + self.pedigree
            + self.gate,
            1,
        )


@dataclass
class HorseResult:
    horse_number: Optional[int]
    horse_name: str
    score: Score
    rank: str
    mark: str
    notes: List[str]


# ============================================================
# Gemini JSON Schema
# ============================================================

RACE_SCHEMA = {
    "type": "object",
    "properties": {
        "race": {
            "type": "object",
            "properties": {
                "racecourse": {"type": "string"},
                "race_number": {"type": "integer"},
                "race_name": {"type": "string"},
                "surface": {"type": "string"},
                "distance_m": {"type": "integer"},
                "track_condition": {"type": "string"},
                "race_date": {"type": "string"},
            },
            "required": [
                "racecourse",
                "race_number",
                "race_name",
                "surface",
                "distance_m",
                "track_condition",
                "race_date",
            ],
        },
        "horses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "horse_number": {"type": "integer"},
                    "frame_number": {"type": "integer"},
                    "horse_name": {"type": "string"},
                    "jockey": {"type": "string"},
                    "trainer": {"type": "string"},

                    "running_style": {
                        "type": "string",
                        "enum": ["逃げ", "先行", "好位", "差し", "追込", "不明"],
                    },

                    "days_since_last_race": {
                        "type": ["integer", "null"]
                    },

                    "starts_since_long_break": {
                        "type": ["integer", "null"]
                    },

                    "previous_races": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string"},
                                "racecourse": {"type": "string"},
                                "surface": {"type": "string"},
                                "distance_m": {"type": ["integer", "null"]},
                                "track_condition": {"type": "string"},
                                "class_name": {"type": "string"},
                                "finish_position": {"type": ["integer", "null"]},
                                "field_size": {"type": ["integer", "null"]},
                                "time_diff_sec": {"type": ["number", "null"]},
                                "body_weight": {"type": ["number", "null"]},
                                "note": {"type": "string"},
                            },
                            "required": [
                                "date",
                                "racecourse",
                                "surface",
                                "distance_m",
                                "track_condition",
                                "class_name",
                                "finish_position",
                                "field_size",
                                "time_diff_sec",
                                "body_weight",
                                "note",
                            ],
                        },
                    },

                    "same_course_wins": {"type": ["integer", "null"]},
                    "same_course_places": {"type": ["integer", "null"]},
                    "same_distance_best_finish": {"type": ["integer", "null"]},
                    "same_distance_starts": {"type": ["integer", "null"]},
                    "same_distance_places": {"type": ["integer", "null"]},
                    "same_surface_places": {"type": ["integer", "null"]},
                    "same_track_condition_places": {"type": ["integer", "null"]},

                    "training_rating": {
                        "type": "string",
                        "enum": ["S", "A", "B", "C", "D", "不明"],
                    },
                    "training_comment": {"type": "string"},

                    "jockey_course_record_rating": {
                        "type": "string",
                        "enum": ["S", "A", "B", "C", "D", "不明"],
                    },
                    "jockey_change_rating": {
                        "type": "string",
                        "enum": ["大幅プラス", "プラス", "中立", "マイナス", "大幅マイナス", "不明"],
                    },

                    "pedigree_rating": {
                        "type": "string",
                        "enum": ["S", "A", "B", "C", "D", "不明"],
                    },

                    "odds": {"type": ["number", "null"]},
                },
                "required": [
                    "horse_number",
                    "frame_number",
                    "horse_name",
                    "jockey",
                    "trainer",
                    "running_style",
                    "days_since_last_race",
                    "starts_since_long_break",
                    "previous_races",
                    "same_course_wins",
                    "same_course_places",
                    "same_distance_best_finish",
                    "same_distance_starts",
                    "same_distance_places",
                    "same_surface_places",
                    "same_track_condition_places",
                    "training_rating",
                    "training_comment",
                    "jockey_course_record_rating",
                    "jockey_change_rating",
                    "pedigree_rating",
                    "odds",
                ],
            },
        },
    },
    "required": ["race", "horses"],
}


EXTRACTION_PROMPT = r"""
あなたは競馬新聞の画像から「事実を正確に構造化するOCR/データ抽出担当」です。
あなたは予想家ではありません。採点・順位付け・勝敗予測は絶対にしないでください。

【最重要ルール】
1. 画像に書かれている情報だけを使う。
2. 読めない情報・画像に存在しない情報は null または「不明」にする。
3. 推測・補完・Web検索・一般知識による穴埋めは禁止。
4. 複数画像が同じ馬を示している場合は相互参照し、明らかな重複を統合する。
5. 数字を読み間違えない。特に着順、着差、馬番、枠番、距離、日付、馬体重、調教時計を慎重に確認。
6. 「休み明け」は前走からの日数を読み取れる場合だけ計算する。
7. 長期休養馬について、休養前の最後のレースを previous_races に残す。
8. 叩き2走目/3走目は、画像から判断できる場合だけ starts_since_long_break に入れる。
9. 鉄砲実績は、画像に明確に記載がある場合のみ判断材料にする。
10. 騎手のコース成績は、画像に掲載されている場合だけ評価ランクを付ける。それ以外は「不明」。
11. 血統評価も画像から読み取れる血統情報に基づく範囲だけ。見えない父母系を想像しない。
12. odds は画像にオッズが掲載されている場合のみ数値を入れる。

【previous_races】
可能な限り馬柱に表示されている過去走を時系列で抽出してください。
finish_position は着順。
time_diff_sec は1着馬またはそのレースの基準からの着差が「0.3」のように秒表示されている場合の数値。画像が「クビ」「ハナ」「アタマ」「1/2」など秒でない場合は null。
note には、不利・出遅れ・前崩れ・Hペース先行・Sペース前残り等が明記されている場合のみ記載。

【running_style】
画像内の通過順位等から明確に判断できる場合に分類。
不明なら「不明」。

必ず指定されたJSON Schemaに従って出力してください。
"""


# ============================================================
# 共通ユーティリティ
# ============================================================

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def rating_to_score(rating: str, mapping: Dict[str, float], unknown: float) -> float:
    return mapping.get(rating, unknown)


def race_interval_category(days: Optional[int]) -> str:
    if days is None:
        return "不明"
    if days < 0:
        return "不明"
    if days <= 60:
        return "通常"
    if days <= 150:
        return "軽い休養"
    if days <= 270:
        return "長期休養"
    return "超長期休養"


def rank_from_total(total: float) -> str:
    if total >= 90.0:
        return "S"
    if total >= 85.0:
        return "A+"
    if total >= 80.0:
        return "A"
    if total >= 75.0:
        return "B+"
    if total >= 70.0:
        return "B"
    if total >= 65.0:
        return "C+"
    if total >= 60.0:
        return "C"
    return "D"


# ============================================================
# 採点エンジン
# ============================================================

def score_ability(horse: Dict[str, Any], race: Dict[str, Any]) -> tuple[float, List[str]]:
    """
    20点。
    休み明けの場合は「前走だけ」に依存しない。
    叩き補正はここで最終20点以内に収める。
    """
    notes = []
    races = horse.get("previous_races") or []

    days = safe_int(horse.get("days_since_last_race"))
    category = race_interval_category(days)

    if not races:
        notes.append("過去走情報不足")
        return 8.0, notes

    # 着順＋着差を基本にした直近実績評価
    recent = races[:3]

    def race_quality(r: Dict[str, Any]) -> float:
        pos = safe_int(r.get("finish_position"))
        field = safe_int(r.get("field_size"))
        diff = safe_float(r.get("time_diff_sec"))

        if pos is None:
            return 3.0

        # 着順ベース
        if pos == 1:
            score = 8.0
        elif pos == 2:
            score = 7.0
        elif pos == 3:
            score = 6.5
        elif pos <= 5:
            score = 5.5
        elif pos <= 8:
            score = 4.0
        elif pos <= 12:
            score = 2.5
        else:
            score = 1.5

        # 着差が読み取れる場合だけ補正
        if diff is not None:
            if diff <= 0.2:
                score += 1.5
            elif diff <= 0.5:
                score += 1.0
            elif diff <= 1.0:
                score += 0.0
            elif diff <= 1.5:
                score -= 0.5
            else:
                score -= 1.0

        # 大頭数での掲示板をわずかに評価
        if field and field >= 14 and pos <= 5:
            score += 0.3

        # 明記されたレース内容
        note = str(r.get("note") or "")
        if any(x in note for x in ["不利", "出遅れ", "前崩れ", "Hペース先行", "ハイペース先行"]):
            score += 0.4

        if any(x in note for x in ["展開有利", "前残り", "楽逃げ"]):
            score -= 0.2

        return clamp(score, 0.0, 10.0)

    qualities = [race_quality(r) for r in recent]

    # 通常馬：直近走を強く見る
    if category in ["通常", "軽い休養"]:
        base = qualities[0] * 0.45
        if len(qualities) >= 2:
            base += qualities[1] * 0.30
        if len(qualities) >= 3:
            base += qualities[2] * 0.25
    else:
        # 長期休養：直近＝休養前の最後のレースだけにしない
        # 休養前の複数実績＋同条件実績を混ぜる
        base = qualities[0] * 0.25
        if len(qualities) >= 2:
            base += qualities[1] * 0.35
        if len(qualities) >= 3:
            base += qualities[2] * 0.40
        notes.append(f"{category}:休養前実績を重視")

    # 10点スケール→20点
    result = base * 2.0

    # 同条件実績
    same_course = safe_int(horse.get("same_course_wins"))
    same_course_places = safe_int(horse.get("same_course_places"))
    same_distance_places = safe_int(horse.get("same_distance_places"))

    if same_course is not None and same_course > 0:
        result += 0.8
        notes.append("同コース勝利実績あり")
    elif same_course_places is not None and same_course_places > 0:
        result += 0.4

    if same_distance_places is not None and same_distance_places > 0:
        result += 0.4

    # 叩き2/3走目
    starts = safe_int(horse.get("starts_since_long_break"))
    if starts == 2:
        # 「上積みあり」と断定せず、調教等の別項目で仕上がりを評価。
        # ここでは小幅な上積みだけ。
        result += 0.5
        notes.append("叩き2走目")
    elif starts == 3:
        notes.append("叩き3走目")

    return round(clamp(result, 0.0, 20.0), 1), notes


def score_suitability(horse: Dict[str, Any], race: Dict[str, Any]) -> tuple[float, List[str]]:
    notes = []

    course_places = safe_int(horse.get("same_course_places"))
    distance_starts = safe_int(horse.get("same_distance_starts"))
    distance_places = safe_int(horse.get("same_distance_places"))
    surface_places = safe_int(horse.get("same_surface_places"))
    track_places = safe_int(horse.get("same_track_condition_places"))

    # コース 6点
    if course_places is None:
        course = 3.0
    elif course_places >= 3:
        course = 6.0
        notes.append("同コース好走実績")
    elif course_places >= 1:
        course = 4.5
        notes.append("同コース好走あり")
    else:
        course = 2.0

    # 距離 6点
    if distance_starts is None:
        distance = 3.0
    elif distance_places is None:
        distance = 3.0
    elif distance_places >= 3:
        distance = 6.0
        notes.append("距離実績良好")
    elif distance_places >= 1:
        distance = 4.5
    else:
        distance = 2.0

    # 馬場 6点
    if track_places is None:
        track = 3.0
    elif track_places >= 2:
        track = 6.0
        notes.append("今回馬場で好走実績")
    elif track_places == 1:
        track = 4.5
    else:
        track = 2.0

    # 芝/ダートなどの情報が画像にある場合の補助
    if surface_places is not None and surface_places > 0:
        track = min(6.0, track + 0.3)

    return round(clamp(course + distance + track, 0.0, 18.0), 1), notes


def score_training(horse: Dict[str, Any]) -> tuple[float, List[str]]:
    mapping = {
        "S": 16.5,
        "A": 14.5,
        "B": 12.0,
        "C": 8.0,
        "D": 3.0,
        "不明": 8.5,
    }
    rating = horse.get("training_rating", "不明")
    score = mapping.get(rating, 8.5)
    notes = []

    if rating in ["S", "A"]:
        notes.append("調教・仕上がり良好")
    elif rating in ["C", "D"]:
        notes.append("調教面に不安")

    days = safe_int(horse.get("days_since_last_race"))
    if days is not None and days >= 151:
        notes.append("休み明けの仕上がりを重視")
        comment = str(horse.get("training_comment") or "")
        if any(x in comment for x in ["仕上がり十分", "万全", "絶好", "抜群", "順調"]):
            score = min(17.0, score + 0.5)
        elif any(x in comment for x in ["余裕", "叩き", "試走", "途上"]):
            score = max(0.0, score - 1.0)

    return round(clamp(score, 0.0, 17.0), 1), notes


def score_pace(horses: List[Dict[str, Any]]) -> tuple[Dict[int, float], str]:
    """
    全馬の脚質構成からS/M/Hを固定ルールで推定。
    """
    styles = [h.get("running_style") for h in horses]

    escape = styles.count("逃げ")
    front = styles.count("先行")
    pace_count = escape + front

    if escape >= 3 or pace_count >= 6:
        pace = "H"
    elif escape == 0 and pace_count <= 2:
        pace = "S"
    else:
        pace = "M"

    scores = {}
    for h in horses:
        num = safe_int(h.get("horse_number"))
        style = h.get("running_style", "不明")

        if pace == "H":
            value = {
                "逃げ": 8.0,
                "先行": 10.0,
                "好位": 12.0,
                "差し": 14.0,
                "追込": 13.0,
                "不明": 10.0,
            }.get(style, 10.0)
        elif pace == "S":
            value = {
                "逃げ": 15.0,
                "先行": 14.0,
                "好位": 13.0,
                "差し": 10.0,
                "追込": 8.0,
                "不明": 10.0,
            }.get(style, 10.0)
        else:
            value = {
                "逃げ": 11.5,
                "先行": 12.5,
                "好位": 13.0,
                "差し": 12.5,
                "追込": 10.5,
                "不明": 10.0,
            }.get(style, 10.0)

        if num is not None:
            scores[num] = round(clamp(value, 0.0, 15.0), 1)

    return scores, pace


def score_jockey(horse: Dict[str, Any]) -> tuple[float, List[str]]:
    course_mapping = {
        "S": 8.0,
        "A": 7.0,
        "B": 5.5,
        "C": 4.0,
        "D": 2.5,
        "不明": 5.5,
    }
    change_mapping = {
        "大幅プラス": 3.0,
        "プラス": 1.5,
        "中立": 0.0,
        "マイナス": -1.0,
        "大幅マイナス": -2.0,
        "不明": 0.0,
    }

    base = course_mapping.get(horse.get("jockey_course_record_rating"), 5.5)
    change = change_mapping.get(horse.get("jockey_change_rating"), 0.0)

    return round(clamp(base + change, 0.0, 12.0), 1), []


def score_pedigree(horse: Dict[str, Any]) -> tuple[float, List[str]]:
    mapping = {
        "S": 9.5,
        "A": 8.0,
        "B": 6.0,
        "C": 4.0,
        "D": 2.0,
        "不明": 5.0,
    }
    return round(clamp(mapping.get(horse.get("pedigree_rating"), 5.0), 0.0, 10.0), 1), []


def score_gate(horse: Dict[str, Any], race: Dict[str, Any]) -> tuple[float, List[str]]:
    """
    枠順はコースごとの厳密な統計をまだ外部データ化していないため、
    画像から得られる馬番・枠番をベースに中立寄りにする。
    コース別バイアス統計を接続した段階でここを差し替える。
    """
    frame = safe_int(horse.get("frame_number"))
    horse_num = safe_int(horse.get("horse_number"))

    if frame is None:
        return 4.0, ["枠順情報不明"]

    # 現段階では極端な決め打ちを避ける。
    # 内外有利はコース別DBを導入したときに補正する。
    score = 4.0

    # 内枠をわずかに中立以上、外枠をわずかに中立以下。
    # これは暫定値。
    if frame <= 2:
        score = 4.5
    elif frame >= 7:
        score = 3.5

    return round(clamp(score, 0.0, 8.0), 1), []


# ============================================================
# 期待値・印
# ============================================================

def fair_probability_from_score(total: float) -> float:
    """
    100点をそのまま勝率と解釈しない。
    現段階では「能力順位」を確率化するための暫定softmax用スコア。
    """
    return total / 100.0


def calculate_marks(results: List[HorseResult]) -> None:
    """
    能力点＋オッズで最終印を決定。
    オッズがない場合は能力順位を使用。
    """
    # 能力順位
    sorted_by_score = sorted(
        results,
        key=lambda x: (-x.score.total, x.horse_number or 999)
    )

    # オッズがコード側に保持されていないため、notesからではなく
    # 今後HorseResultへ odds を追加して扱うのが理想。
    # 現在は能力点のみで印を付ける。
    marks = ["◎", "○", "▲", "☆", "△", "◇"]

    for i, result in enumerate(sorted_by_score):
        result.mark = marks[i] if i < len(marks) else ""


def analyze_race(data: Dict[str, Any]) -> tuple[Dict[str, Any], List[HorseResult]]:
    race = data.get("race", {})
    horses = data.get("horses", [])

    pace_scores, pace_type = score_pace(horses)
    results: List[HorseResult] = []

    for horse in horses:
        ability, n1 = score_ability(horse, race)
        suitability, n2 = score_suitability(horse, race)
        training, n3 = score_training(horse)
        jockey, n5 = score_jockey(horse)
        pedigree, n6 = score_pedigree(horse)
        gate, n7 = score_gate(horse, race)

        num = safe_int(horse.get("horse_number"))
        pace = pace_scores.get(num, 10.0)

        score = Score(
            ability=ability,
            suitability=suitability,
            training=training,
            pace=pace,
            jockey=jockey,
            pedigree=pedigree,
            gate=gate,
        )

        results.append(
            HorseResult(
                horse_number=num,
                horse_name=str(horse.get("horse_name") or "不明"),
                score=score,
                rank=rank_from_total(score.total),
                mark="",
                notes=n1 + n2 + n3 + n5 + n6 + n7,
            )
        )

    results.sort(key=lambda x: (-x.score.total, x.horse_number or 999))
    calculate_marks(results)

    return {"race": race, "pace": pace_type}, results


# ============================================================
# Gemini呼び出し
# ============================================================

def get_api_key() -> str:
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return os.getenv("GEMINI_API_KEY", "")


def extract_race_data(client: genai.Client, uploaded_files: List[Any]) -> Dict[str, Any]:
    content_parts: List[Any] = []

    for uploaded_file in uploaded_files:
        content_parts.append(
            types.Part.from_bytes(
                data=uploaded_file.getvalue(),
                mime_type=uploaded_file.type,
            )
        )

    content_parts.append(EXTRACTION_PROMPT)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=content_parts,
        config=types.GenerateContentConfig(
            temperature=AI_TEMPERATURE,
            response_mime_type="application/json",
            response_schema=RACE_SCHEMA,
        ),
    )

    text = response.text.strip()

    # JSONとして厳格に処理
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # ```json ... ``` が返った場合のみ安全に除去
        cleaned = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        return json.loads(cleaned)


# ============================================================
# UI
# ============================================================

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🏇",
    layout="wide",
)

st.title("🏇 馬柱 ＆ 予想支援アプリ")
st.caption(
    "画像から事実を抽出 → Pythonの固定ルールで100点採点。"
    "AIに最終点数を自由に決めさせないことで、結果のブレを抑えます。"
)

with st.sidebar:
    st.header("設定")
    api_key = get_api_key()

    if not api_key:
        api_key = st.text_input(
            "Gemini APIキー",
            type="password",
            help="Streamlit Secretsまたは環境変数 GEMINI_API_KEY も利用できます。",
        )

    st.divider()
    st.write("配点")
    st.write("① 能力・実績 20")
    st.write("② コース・距離・馬場 18")
    st.write("③ 調教・仕上がり 17")
    st.write("④ 脚質・展開 15")
    st.write("⑤ 騎手 12")
    st.write("⑥ 血統 10")
    st.write("⑦ 枠順 8")

if not api_key:
    st.info("Gemini APIキーを設定してください。")
    st.stop()

client = genai.Client(api_key=api_key)

uploaded_files = st.file_uploader(
    "競馬新聞・馬柱の画像を選択（全体＋拡大画像など複数可）",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
)

if uploaded_files:
    cols = st.columns(min(3, len(uploaded_files)))
    for i, uploaded_file in enumerate(uploaded_files):
        with cols[i % len(cols)]:
            st.image(
                uploaded_file,
                caption=uploaded_file.name,
                use_container_width=True,
            )

    if st.button("🔍 解析して100点採点", type="primary"):
        try:
            with st.spinner("画像から事実データを抽出しています…"):
                data = extract_race_data(client, uploaded_files)

            with st.spinner("固定ルールで採点しています…"):
                race_info, results = analyze_race(data)

            st.session_state["race_info"] = race_info
            st.session_state["results"] = results
            st.session_state["raw_data"] = data

        except Exception as e:
            st.error(f"解析エラー: {e}")
            st.stop()

if "results" in st.session_state:
    race_info = st.session_state["race_info"]
    results: List[HorseResult] = st.session_state["results"]
    data = st.session_state["raw_data"]

    race = race_info["race"]

    st.divider()
    st.subheader("📍 対象レース")
    st.write(
        f"**{race.get('racecourse', '不明')} "
        f"{race.get('race_number', '?')}R "
        f"{race.get('race_name', '')}**"
    )
    st.write(
        f"{race.get('surface', '不明')} "
        f"{race.get('distance_m', '?')}m / "
        f"馬場：{race.get('track_condition', '不明')} / "
        f"想定ペース：**{race_info['pace']}**"
    )

    st.subheader("📊 総合評価ランキング")

    for i, result in enumerate(results, start=1):
        c1, c2, c3, c4 = st.columns([0.6, 2.4, 1.2, 3.8])

        with c1:
            st.write(f"**{i}**")

        with c2:
            st.write(
                f"**{result.mark} {result.horse_number}番 "
                f"{result.horse_name}**"
            )

        with c3:
            st.write(
                f"**{result.score.total:.1f}点**  "
                f"`{result.rank}`"
            )

        with c4:
            if result.notes:
                st.write(" / ".join(result.notes[:5]))
            else:
                st.write("—")

    st.subheader("🎯 最終推奨印")
    for result in results[:6]:
        st.write(
            f"**{result.mark} {result.horse_number}番 "
            f"{result.horse_name}**　"
            f"{result.score.total:.1f}点（{result.rank}）"
        )

    st.subheader("🔎 各馬の採点内訳")

    for result in results:
        with st.expander(
            f"{result.mark} {result.horse_number}番 "
            f"{result.horse_name} — {result.score.total:.1f}点"
        ):
            s = result.score
            cols = st.columns(7)

            items = [
                ("能力", s.ability, 20),
                ("適性", s.suitability, 18),
                ("調教", s.training, 17),
                ("展開", s.pace, 15),
                ("騎手", s.jockey, 12),
                ("血統", s.pedigree, 10),
                ("枠順", s.gate, 8),
            ]

            for col, (label, value, max_value) in zip(cols, items):
                with col:
                    st.metric(label, f"{value:.1f}", f"/ {max_value}")

            if result.notes:
                st.write("**評価メモ:**")
                for note in result.notes:
                    st.write(f"- {note}")

    with st.expander("🧾 AIが抽出した元データ（検証用）"):
        st.json(data)

    st.caption(
        "注意：現在の枠順補正・騎手成績・オッズ期待値は、"
        "画像内情報だけで計算する暫定版です。"
        "外部の競馬データを接続した段階で、コース別枠順統計・騎手統計・"
        "オッズ期待値を独立したデータとして追加できます。"
    )
